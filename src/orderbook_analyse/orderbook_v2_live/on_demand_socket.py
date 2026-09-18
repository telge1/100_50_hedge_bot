"""Unix-domain JSON-lines control socket for on-demand OB1000."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 16_384
READ_TIMEOUT_SEC = 5.0
WRITE_TIMEOUT_SEC = 30.0
MAX_CONCURRENT_CLIENTS = 8


def default_socket_path() -> Path:
    return Path(f"/run/user/{os.getuid()}/orderbook_ob1000.sock")


def resolve_socket_path(raw: str | None = None) -> Path:
    path_raw = raw or os.environ.get("OB_V3_ON_DEMAND_SOCKET_PATH") or ""
    if path_raw:
        return Path(path_raw)
    return default_socket_path()


def prepare_socket_path(path: Path) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"runtime_dir_missing:{parent}")
    if not path.exists():
        return
    if not path.is_socket():
        raise OSError(f"not_a_socket:{path}")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(path))
        raise RuntimeError(f"socket_already_active:{path}")
    except FileNotFoundError:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if getattr(exc, "errno", None) in {111, 2}:
            path.unlink(missing_ok=True)
        elif str(exc).startswith("socket_already_active:"):
            raise
        else:
            raise


def _error_response(request_id: Any, error: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "request_id": request_id,
        "ok": False,
        "error": error,
        "symbol": extra.get("symbol"),
        "depth": extra.get("depth", 1000),
        "subscription_state": extra.get("subscription_state", "error"),
        "expires_at": extra.get("expires_at"),
    }
    out.update({k: v for k, v in extra.items() if k not in out})
    return out


async def _write_response(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    writer.write(data)
    await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SEC)


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class OnDemandSocketServer:
    """Control socket on a dedicated asyncio loop/thread.

    Keeps OB1000/FULL Levels replies responsive while the collector loop is
    busy with Full-OB REST seeds or other GIL-heavy work.
    """

    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task] = set()
        self._client_sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    async def start(self) -> None:
        """Start socket on a side thread; safe to await from the collector loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="ob1000-control-socket",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise TimeoutError("on_demand_socket_start_timeout")
        if self._start_error is not None:
            raise RuntimeError(
                f"on_demand_socket_start_failed:{self._start_error}"
            ) from self._start_error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._start_on_loop())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(self._shutdown_on_loop())
            except Exception:
                logger.exception("on_demand_socket_shutdown_error")
            loop.close()
            if self._loop is loop:
                self._loop = None

    async def _start_on_loop(self) -> None:
        prepare_socket_path(self.path)
        self._client_sem = asyncio.Semaphore(MAX_CONCURRENT_CLIENTS)
        self._server = await asyncio.start_unix_server(self._accept_client, path=str(self.path))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    async def _shutdown_on_loop(self) -> None:
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.path.is_socket():
            self.path.unlink(missing_ok=True)

    async def stop(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        acquired = False
        try:
            try:
                sem = self._client_sem
                if sem is None:
                    raise RuntimeError("socket_not_started")
                await asyncio.wait_for(sem.acquire(), timeout=1.0)
                acquired = True
            except asyncio.TimeoutError:
                try:
                    await _write_response(writer, _error_response(None, "busy"))
                except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError, OSError):
                    pass
                return
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                if len(line) > MAX_REQUEST_BYTES:
                    await self._safe_write(writer, _error_response(None, "request_too_large"))
                    break
                try:
                    req = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._safe_write(writer, _error_response(None, "invalid_json"))
                    continue
                if not isinstance(req, dict):
                    await self._safe_write(writer, _error_response(None, "invalid_json"))
                    continue
                try:
                    result = self._handler(req)
                    resp = await result if asyncio.iscoroutine(result) else result
                except Exception as exc:
                    logger.exception("on_demand_socket_handler_error")
                    resp = _error_response(req.get("request_id"), str(exc))
                if not await self._safe_write(writer, resp):
                    break
        finally:
            if acquired and self._client_sem is not None:
                self._client_sem.release()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _safe_write(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> bool:
        try:
            await _write_response(writer, payload)
            return True
        except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("on_demand_socket_write_failed error=%s", exc)
            return False

"""Start the existing Orderbook V3 collector in ram-only mode.

The process argv is fixed. Request fields (symbol, lease id, depth) never
become command-line arguments. A compatible collector that already answers
on the Unix socket is reused. This module does not stop a collector and does
not call the live-orderbook screen manager.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .ob1000_on_demand import (
    FULL_DEPTH,
    Ob1000CollectorUnavailableError,
    Ob1000DisabledError,
    Ob1000RequestError,
    _call_collector,
    _on_demand_enabled,
    _orderbook_analyse_root,
    socket_path,
)

logger = logging.getLogger(__name__)

_MODULE = "orderbook_analyse.orderbook_v2_live"
_MODE = "ram-only"
_READY_TIMEOUT_SEC = 20.0
_POLL_SEC = 0.25

_START_LOCK = threading.Lock()
_child: subprocess.Popen[bytes] | None = None
_log_handle: Any = None


class FullCollectorStartError(RuntimeError):
    pass


class IncompatibleFullCollectorError(RuntimeError):
    pass


def fixed_argv(health_file: Path) -> list[str]:
    """Constant command. health_file is chosen by this module, not by the request."""
    return [
        sys.executable,
        "-m",
        _MODULE,
        "--mode",
        _MODE,
        "--health-file",
        str(health_file),
    ]


def _child_env(sock: Path) -> dict[str, str]:
    root = _orderbook_analyse_root()
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "XDG_RUNTIME_DIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(root / "src")
    env["BYBIT_WS_URL"] = os.environ.get("BYBIT_WS_URL") or "wss://stream.bybit.com/v5/public/linear"
    env["OB_V3_ON_DEMAND_SOCKET_PATH"] = str(sock)
    # The collector applies the same flags again after dotenv. Set them here
    # so a hand-edited .env cannot turn archive or keeper back on.
    env["OB_V3_ON_DEMAND_ENABLE"] = "true"
    env["OB_V3_ON_DEMAND_KEEPER"] = "false"
    env["OB_V3_FULL_BOOK_ENABLE"] = "true"
    # Collector default is 3 FULL topics; the multi view needs one per pane.
    env["OB_V3_FULL_BOOK_MAX_ACTIVE"] = os.environ.get("OB_V3_FULL_BOOK_MAX_ACTIVE") or "5"
    env["OB_V3_RAW_ARCHIVE_ENABLE"] = "false"
    env["OB_V3_OB1000_RAW_ARCHIVE_ENABLE"] = "false"
    env["OB_V3_FULL_OB_RAW_ARCHIVE_ENABLED"] = "false"
    env["OB_V3_FULL_OB_RAW_ARCHIVE_ENABLE"] = "false"
    env["FULL_OB_CONTINUOUS_ARCHIVE_ENABLED"] = "false"
    env["OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE"] = "false"
    env["FULL_OB_CACHE_BRIDGE_ENABLED"] = "false"
    env["OB_V3_ON_DEMAND_HEARTBEAT_SEC"] = "15"
    env["OB_V3_ON_DEMAND_LEASE_TTL_SEC"] = "45"
    return env


def _health_file() -> Path:
    return _orderbook_analyse_root() / "logs" / "orderbook_v3_ram_only.health"


def _log_file() -> Path:
    return _orderbook_analyse_root() / "logs" / "orderbook_v3_ram_only.log"


def probe_full_collector() -> str:
    """Return ready, down, or incompatible. Does not start a process."""
    if not _on_demand_enabled():
        raise Ob1000DisabledError("disabled")
    try:
        resp = _call_collector(
            {
                "request_id": "ob-live-full-probe",
                "operation": "status",
                "depth": FULL_DEPTH,
            }
        )
    except Ob1000CollectorUnavailableError:
        return "down"
    except Ob1000RequestError:
        return "incompatible"
    if not isinstance(resp, dict):
        return "incompatible"
    if resp.get("ok") and (resp.get("book_mode") == "full" or resp.get("depth") == FULL_DEPTH):
        return "ready"
    err = str(resp.get("error") or "")
    if err in {"disabled", "only_depth_1000_supported"}:
        return "incompatible"
    if resp.get("ok"):
        return "ready"
    return "incompatible"


def _spawn(sock: Path) -> subprocess.Popen[bytes]:
    global _log_handle
    root = _orderbook_analyse_root()
    log_path = _log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_handle = open(log_path, "ab", buffering=0)
    argv = fixed_argv(_health_file())
    if argv[1:4] != ["-m", _MODULE, "--mode"] or argv[4] != _MODE:
        raise FullCollectorStartError("refusing_unfixed_argv")
    logger.info("starting ram-only collector module=%s socket=%s", _MODULE, sock)
    return subprocess.Popen(
        argv,
        cwd=str(root),
        env=_child_env(sock),
        stdin=subprocess.DEVNULL,
        stdout=_log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _wait_until_ready(proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise FullCollectorStartError(f"ram_only_exited:{code}")
        if probe_full_collector() == "ready":
            return
        time.sleep(_POLL_SEC)
    raise FullCollectorStartError("ram_only_socket_timeout")


def ensure_full_collector() -> dict[str, Any]:
    """Reuse a FULL-capable collector, or start the fixed ram-only process."""
    global _child
    state = probe_full_collector()
    if state == "ready":
        return {"action": "reused", "started": False}
    if state == "incompatible":
        raise IncompatibleFullCollectorError("collector_not_full_capable")
    with _START_LOCK:
        state = probe_full_collector()
        if state == "ready":
            return {"action": "reused", "started": False}
        if state == "incompatible":
            raise IncompatibleFullCollectorError("collector_not_full_capable")
        proc = _child
        if proc is None or proc.poll() is not None:
            proc = _spawn(socket_path())
            _child = proc
        _wait_until_ready(proc)
        return {"action": "started", "started": True, "pid": proc.pid}


def ensure_full_collector_for_depth(depth: Any) -> dict[str, Any] | None:
    """Start only for the FULL channel. OB1000/Walls leases do not spawn a process."""
    from .ob1000_on_demand import _normalize_depth

    if _normalize_depth(depth) != FULL_DEPTH:
        return None
    return ensure_full_collector()

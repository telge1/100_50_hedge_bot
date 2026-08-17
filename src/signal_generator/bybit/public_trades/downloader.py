"""Stream Bybit public-trade day files to a local gzip cache (no full RAM load)."""

from __future__ import annotations

import gzip
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Protocol

import httpx

from signal_generator.bybit.public_trades.urls import daily_filename, daily_url

GZIP_MAGIC = b"\x1f\x8b"
DEFAULT_CHUNK = 1024 * 256
DISK_FREE_MIN_BYTES = 80 * 1024 * 1024 * 1024


class PublicTradeDownloadError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


class HttpTransport(Protocol):
    def head(self, url: str) -> httpx.Response: ...

    def stream_get(self, url: str) -> Iterator[bytes]: ...


class HttpxTransport:
    def __init__(
        self,
        *,
        timeout: float = 120.0,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.client = client

    def _client(self) -> tuple[httpx.Client, bool]:
        if self.client is not None:
            return self.client, False
        return httpx.Client(timeout=self.timeout, follow_redirects=True), True

    def head(self, url: str) -> httpx.Response:
        client, owns = self._client()
        try:
            last_exc: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    response = client.head(url)
                    if response.status_code in (403, 429) or response.status_code >= 500:
                        if attempt + 1 >= self.max_retries:
                            return response
                        time.sleep(self.backoff_base * (2**attempt))
                        continue
                    return response
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt + 1 >= self.max_retries:
                        raise PublicTradeDownloadError(
                            "FAILED", f"HEAD failed after retries: {exc}"
                        ) from exc
                    time.sleep(self.backoff_base * (2**attempt))
            raise PublicTradeDownloadError("FAILED", str(last_exc))
        finally:
            if owns:
                client.close()

    def stream_get(self, url: str) -> Iterator[bytes]:
        client, owns = self._client()
        last_exc: Exception | None = None
        try:
            for attempt in range(self.max_retries):
                try:
                    with client.stream("GET", url) as response:
                        if response.status_code in (403, 429) or response.status_code >= 500:
                            if attempt + 1 >= self.max_retries:
                                raise PublicTradeDownloadError(
                                    "HTTP_RETRY_EXHAUSTED",
                                    f"HTTP {response.status_code} for {url}",
                                )
                            time.sleep(self.backoff_base * (2**attempt))
                            continue
                        if response.status_code == 404:
                            raise PublicTradeDownloadError(
                                "SOURCE_FILE_MISSING", f"HTTP 404 for {url}"
                            )
                        if response.status_code != 200:
                            raise PublicTradeDownloadError(
                                "FAILED", f"HTTP {response.status_code} for {url}"
                            )
                        yield from response.iter_bytes(chunk_size=DEFAULT_CHUNK)
                        return
                except PublicTradeDownloadError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt + 1 >= self.max_retries:
                        raise PublicTradeDownloadError(
                            "FAILED", f"GET failed after retries: {exc}"
                        ) from exc
                    time.sleep(self.backoff_base * (2**attempt))
            raise PublicTradeDownloadError("FAILED", str(last_exc))
        finally:
            if owns:
                client.close()


def disk_free_bytes(path: Path) -> int:
    import shutil

    return int(shutil.disk_usage(path).free)


def assert_disk_free(path: Path, *, minimum: int = DISK_FREE_MIN_BYTES) -> int:
    free = disk_free_bytes(path)
    if free < minimum:
        raise PublicTradeDownloadError(
            "DISK_HARD_STOP",
            f"free disk {free} bytes below hard-stop {minimum} bytes",
        )
    return free


def verify_gzip_file(path: Path) -> None:
    with path.open("rb") as fh:
        magic = fh.read(2)
    if magic != GZIP_MAGIC:
        raise PublicTradeDownloadError("GZIP_INVALID", f"missing gzip magic in {path.name}")
    try:
        with gzip.open(path, "rb") as gz:
            while gz.read(1024 * 1024):
                pass
    except OSError as exc:
        raise PublicTradeDownloadError("GZIP_INVALID", f"gzip integrity failed: {exc}") from exc


def download_day_file(
    symbol: str,
    day: date,
    dest_dir: Path,
    *,
    transport: HttpTransport | None = None,
    disk_free_fn=assert_disk_free,
) -> Path:
    """Stream one day file to dest_dir/{filename}. Atomic replace from .partial."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    disk_free_fn(dest_dir)
    name = daily_filename(symbol, day)
    final_path = dest_dir / name
    partial = dest_dir / f"{name}.partial"
    url = daily_url(symbol, day)
    http = transport or HttpxTransport()
    if partial.exists():
        partial.unlink()
    wrote = 0
    with partial.open("wb") as fh:
        for chunk in http.stream_get(url):
            if not chunk:
                continue
            fh.write(chunk)
            wrote += len(chunk)
        fh.flush()
    if wrote < 2:
        partial.unlink(missing_ok=True)
        raise PublicTradeDownloadError("GZIP_INVALID", f"empty download for {url}")
    verify_gzip_file(partial)
    partial.replace(final_path)
    return final_path

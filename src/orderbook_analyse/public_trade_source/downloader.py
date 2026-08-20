"""Deterministic Bybit public-trade day-file downloader.

Source (confirmed):
  https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{YYYY-MM-DD}.csv.gz

No cookies, no secure-token, no browser download-list API.
Content-Type is untrusted (Bybit serves gzip as text/csv).
"""

from __future__ import annotations

import gzip
import io
import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

import httpx

PUBLIC_BYBIT_TRADING_BASE = "https://public.bybit.com/trading"
GZIP_MAGIC = b"\x1f\x8b"

STATUS_COMPLETE = "COMPLETE"
STATUS_SKIPPED = "SKIPPED_UNCHANGED"
STATUS_SOURCE_MISSING = "SOURCE_FILE_MISSING"
STATUS_FORBIDDEN = "HTTP_FORBIDDEN"
STATUS_RATE_LIMITED = "HTTP_RATE_LIMITED"
STATUS_SERVER_ERROR = "HTTP_SERVER_ERROR"
STATUS_CLIENT_ERROR = "HTTP_CLIENT_ERROR"
STATUS_GZIP_INVALID = "GZIP_INVALID"
STATUS_SIZE_MISMATCH = "SIZE_MISMATCH"
STATUS_FAILED = "FAILED"

RETRIABLE_STATUSES = frozenset({STATUS_RATE_LIMITED, STATUS_SERVER_ERROR, STATUS_FORBIDDEN})


class PublicTradeDownloadError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class HttpTransport(Protocol):
    def head(self, url: str) -> httpx.Response: ...

    def stream_get(self, url: str) -> Iterator[bytes]: ...


@dataclass
class HttpxTransport:
    timeout: float = 60.0
    max_retries: int = 5
    backoff_base: float = 0.5
    client: httpx.Client | None = None

    def _request(self, method: str, url: str) -> httpx.Response:
        owns = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout, follow_redirects=True)
        try:
            last_exc: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    response = client.request(method, url)
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
                            STATUS_FAILED, f"transport failed after retries: {exc}"
                        ) from exc
                    time.sleep(self.backoff_base * (2**attempt))
            assert last_exc is not None
            raise PublicTradeDownloadError(STATUS_FAILED, str(last_exc)) from last_exc
        finally:
            if owns:
                client.close()

    def head(self, url: str) -> httpx.Response:
        return self._request("HEAD", url)

    def stream_get(self, url: str) -> Iterator[bytes]:
        owns = self.client is None
        client = self.client or httpx.Client(timeout=self.timeout, follow_redirects=True)
        last_exc: Exception | None = None
        try:
            for attempt in range(self.max_retries):
                try:
                    with client.stream("GET", url) as response:
                        if response.status_code in (403, 429) or response.status_code >= 500:
                            if attempt + 1 >= self.max_retries:
                                body = response.read()
                                yield from ()
                                raise _http_status_error(response.status_code, body[:200])
                            time.sleep(self.backoff_base * (2**attempt))
                            continue
                        if response.status_code != 200:
                            body = response.read()
                            raise _http_status_error(response.status_code, body[:200])
                        yield from response.iter_bytes()
                        return
                except PublicTradeDownloadError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt + 1 >= self.max_retries:
                        raise PublicTradeDownloadError(
                            STATUS_FAILED, f"GET failed after retries: {exc}"
                        ) from exc
                    time.sleep(self.backoff_base * (2**attempt))
            if last_exc is not None:
                raise PublicTradeDownloadError(STATUS_FAILED, str(last_exc)) from last_exc
        finally:
            if owns:
                client.close()


def daily_filename(symbol: str, day: date) -> str:
    return f"{symbol.upper()}{day.isoformat()}.csv.gz"


def daily_url(symbol: str, day: date) -> str:
    sym = symbol.upper()
    name = daily_filename(sym, day)
    return f"{PUBLIC_BYBIT_TRADING_BASE}/{sym}/{name}"


def iter_utc_days(start: datetime, end: datetime) -> list[date]:
    """Calendar days overlapping half-open UTC window [start, end)."""
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end < start:
        raise ValueError("end before start")
    if end == start:
        return []
    days: list[date] = []
    d = start.date()
    while True:
        day_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        if day_start >= end:
            break
        days.append(d)
        d += timedelta(days=1)
    return days


def _http_status_error(code: int, snippet: bytes | str) -> PublicTradeDownloadError:
    text = snippet.decode("utf-8", "replace") if isinstance(snippet, bytes) else str(snippet)
    if code == 404:
        return PublicTradeDownloadError(STATUS_SOURCE_MISSING, f"HTTP 404: {text[:200]}")
    if code == 403:
        return PublicTradeDownloadError(STATUS_FORBIDDEN, f"HTTP 403: {text[:200]}")
    if code == 429:
        return PublicTradeDownloadError(STATUS_RATE_LIMITED, f"HTTP 429: {text[:200]}")
    if code >= 500:
        return PublicTradeDownloadError(STATUS_SERVER_ERROR, f"HTTP {code}: {text[:200]}")
    if code >= 400:
        return PublicTradeDownloadError(STATUS_CLIENT_ERROR, f"HTTP {code}: {text[:200]}")
    return PublicTradeDownloadError(STATUS_FAILED, f"HTTP {code}: {text[:200]}")


def classify_http_status(code: int) -> str:
    if code == 200:
        return STATUS_COMPLETE
    if code == 404:
        return STATUS_SOURCE_MISSING
    if code == 403:
        return STATUS_FORBIDDEN
    if code == 429:
        return STATUS_RATE_LIMITED
    if code >= 500:
        return STATUS_SERVER_ERROR
    if code >= 400:
        return STATUS_CLIENT_ERROR
    return STATUS_FAILED


def verify_gzip_bytes(payload: bytes) -> None:
    if len(payload) < 2 or payload[:2] != GZIP_MAGIC:
        raise PublicTradeDownloadError(STATUS_GZIP_INVALID, "missing gzip magic 1f 8b")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as gz:
            while gz.read(1024 * 1024):
                pass
    except OSError as exc:
        raise PublicTradeDownloadError(STATUS_GZIP_INVALID, f"gzip integrity failed: {exc}") from exc


@dataclass
class FileMeta:
    url: str
    etag: str | None = None
    content_length: int | None = None
    last_modified: str | None = None
    status_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DayDownloadResult:
    symbol: str
    day: str
    url: str
    dest: str
    status: str
    bytes_written: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def meta_from_headers(url: str, headers: httpx.Headers, status_code: int) -> FileMeta:
    cl = headers.get("content-length")
    return FileMeta(
        url=url,
        etag=headers.get("etag"),
        content_length=int(cl) if cl and str(cl).isdigit() else None,
        last_modified=headers.get("last-modified"),
        status_code=status_code,
    )


class PublicTradeDayDownloader:
    def __init__(
        self,
        dest_root: Path | str,
        *,
        transport: HttpTransport | None = None,
        checkpoint_path: Path | str | None = None,
        use_head: bool = True,
    ) -> None:
        self.dest_root = Path(dest_root)
        self.transport = transport or HttpxTransport()
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else self.dest_root / "download_checkpoint.json"
        self.use_head = use_head
        self.dest_root.mkdir(parents=True, exist_ok=True)

    def _load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.is_file():
            return {"files": {}}
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def _save_checkpoint(self, store: dict[str, Any]) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(store, indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.checkpoint_path.parent),
            delete=False,
            prefix=".dlckpt_",
            suffix=".tmp",
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.checkpoint_path)

    def download_day(self, symbol: str, day: date) -> DayDownloadResult:
        symbol = symbol.upper()
        name = daily_filename(symbol, day)
        url = daily_url(symbol, day)
        dest = self.dest_root / name
        result = DayDownloadResult(symbol=symbol, day=day.isoformat(), url=url, dest=str(dest), status=STATUS_FAILED)

        store = self._load_checkpoint()
        files = store.setdefault("files", {})
        prev = files.get(name) or {}

        head_meta: FileMeta | None = None
        if self.use_head:
            head = self.transport.head(url)
            st = classify_http_status(head.status_code)
            if st == STATUS_SOURCE_MISSING:
                result.status = STATUS_SOURCE_MISSING
                result.error = f"HEAD HTTP 404 for {url}"
                files[name] = {**result.to_dict(), "updated_at": _utcnow_iso()}
                self._save_checkpoint(store)
                return result
            if st in RETRIABLE_STATUSES or st == STATUS_CLIENT_ERROR:
                result.status = st
                result.error = f"HEAD HTTP {head.status_code} for {url}"
                files[name] = {**result.to_dict(), "updated_at": _utcnow_iso()}
                self._save_checkpoint(store)
                return result
            if head.status_code == 200:
                head_meta = meta_from_headers(url, head.headers, head.status_code)

        if dest.is_file() and prev.get("status") == STATUS_COMPLETE:
            same_len = (
                head_meta is not None
                and head_meta.content_length is not None
                and dest.stat().st_size == head_meta.content_length
            )
            same_etag = (
                head_meta is not None
                and head_meta.etag
                and prev.get("meta", {}).get("etag") == head_meta.etag
            )
            if same_len or same_etag or (head_meta is None and dest.stat().st_size > 0):
                result.status = STATUS_SKIPPED
                result.skipped = True
                result.bytes_written = dest.stat().st_size
                result.meta = (head_meta.to_dict() if head_meta else prev.get("meta") or {})
                return result

        part = dest.with_suffix(dest.suffix + ".part")
        if part.exists():
            part.unlink()

        chunks: list[bytes] = []
        try:
            for chunk in self.transport.stream_get(url):
                chunks.append(chunk)
        except PublicTradeDownloadError as exc:
            result.status = exc.status
            result.error = str(exc)
            files[name] = {**result.to_dict(), "updated_at": _utcnow_iso()}
            self._save_checkpoint(store)
            return result

        payload = b"".join(chunks)
        result.bytes_written = len(payload)

        expected_len = head_meta.content_length if head_meta is not None else None
        if expected_len is not None and len(payload) != expected_len:
            result.status = STATUS_SIZE_MISMATCH
            result.error = f"got {len(payload)} bytes, Content-Length {expected_len}"
            files[name] = {**result.to_dict(), "updated_at": _utcnow_iso()}
            self._save_checkpoint(store)
            return result

        try:
            verify_gzip_bytes(payload)
        except PublicTradeDownloadError as exc:
            result.status = exc.status
            result.error = str(exc)
            files[name] = {**result.to_dict(), "updated_at": _utcnow_iso()}
            self._save_checkpoint(store)
            return result

        part.write_bytes(payload)
        part.replace(dest)

        meta = head_meta.to_dict() if head_meta is not None else {"url": url, "content_length": len(payload)}
        meta["content_length"] = meta.get("content_length") or len(payload)
        result.meta = meta
        result.status = STATUS_COMPLETE
        files[name] = {**result.to_dict(), "updated_at": _utcnow_iso()}
        self._save_checkpoint(store)
        return result

    def download_range(self, symbol: str, start: datetime, end: datetime) -> list[DayDownloadResult]:
        out: list[DayDownloadResult] = []
        for day in iter_utc_days(start, end):
            out.append(self.download_day(symbol, day))
        return out


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

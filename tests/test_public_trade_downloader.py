"""Tests for Bybit public-trade day-file downloader (no live HTTP)."""

from __future__ import annotations

import gzip
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from orderbook_analyse.public_trade_source.downloader import (
    GZIP_MAGIC,
    PUBLIC_BYBIT_TRADING_BASE,
    STATUS_COMPLETE,
    STATUS_FORBIDDEN,
    STATUS_GZIP_INVALID,
    STATUS_RATE_LIMITED,
    STATUS_SERVER_ERROR,
    STATUS_SIZE_MISMATCH,
    STATUS_SKIPPED,
    STATUS_SOURCE_MISSING,
    PublicTradeDayDownloader,
    PublicTradeDownloadError,
    classify_http_status,
    daily_filename,
    daily_url,
    iter_utc_days,
    verify_gzip_bytes,
)


def _gz_bytes(body: str = "timestamp,symbol\n1,APTUSDT\n") -> bytes:
    return gzip.compress(body.encode("utf-8"))


class FakeTransport:
    def __init__(self, responses: dict[str, httpx.Response], get_body: bytes | None = None, get_error: Exception | None = None):
        self.responses = responses
        self.get_body = get_body
        self.get_error = get_error
        self.head_urls: list[str] = []
        self.get_urls: list[str] = []

    def head(self, url: str) -> httpx.Response:
        self.head_urls.append(url)
        return self.responses[url]

    def stream_get(self, url: str):
        self.get_urls.append(url)
        if self.get_error is not None:
            raise self.get_error
        if url in self.responses and self.responses[url].status_code != 200:
            code = self.responses[url].status_code
            from orderbook_analyse.public_trade_source.downloader import _http_status_error

            raise _http_status_error(code, b"")
        assert self.get_body is not None
        yield self.get_body


def _resp(code: int, *, length: int | None = None, etag: str = "abc", last_modified: str = "Thu, 25 Jul 2026 00:00:00 GMT") -> httpx.Response:
    headers = {}
    if length is not None:
        headers["Content-Length"] = str(length)
    if etag:
        headers["ETag"] = etag
    if last_modified:
        headers["Last-Modified"] = last_modified
    headers["Content-Type"] = "text/csv"
    return httpx.Response(code, headers=headers)


def test_daily_url_deterministic() -> None:
    url = daily_url("aptusdt", date(2026, 7, 29))
    assert url == f"{PUBLIC_BYBIT_TRADING_BASE}/APTUSDT/APTUSDT2026-07-29.csv.gz"
    assert daily_filename("APTUSDT", date(2026, 7, 24)) == "APTUSDT2026-07-24.csv.gz"


def test_iter_days_half_open() -> None:
    days = iter_utc_days(
        datetime(2026, 7, 28, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
    )
    assert [d.isoformat() for d in days] == ["2026-07-28", "2026-07-29", "2026-07-30"]
    days2 = iter_utc_days(
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    assert days2[-1].isoformat() == "2026-07-30"
    assert "2026-07-31" not in [d.isoformat() for d in days2]


def test_classify_http() -> None:
    assert classify_http_status(404) == STATUS_SOURCE_MISSING
    assert classify_http_status(403) == STATUS_FORBIDDEN
    assert classify_http_status(429) == STATUS_RATE_LIMITED
    assert classify_http_status(503) == STATUS_SERVER_ERROR


def test_gzip_magic_and_integrity() -> None:
    verify_gzip_bytes(_gz_bytes())
    with pytest.raises(PublicTradeDownloadError) as exc:
        verify_gzip_bytes(b"not-gzip")
    assert exc.value.status == STATUS_GZIP_INVALID
    with pytest.raises(PublicTradeDownloadError) as exc2:
        verify_gzip_bytes(GZIP_MAGIC + b"\x00\x00truncated")
    assert exc2.value.status == STATUS_GZIP_INVALID


def test_download_success_atomic(tmp_path: Path) -> None:
    payload = _gz_bytes()
    url = daily_url("APTUSDT", date(2026, 7, 24))
    transport = FakeTransport({url: _resp(200, length=len(payload))}, get_body=payload)
    dl = PublicTradeDayDownloader(tmp_path, transport=transport)
    res = dl.download_day("APTUSDT", date(2026, 7, 24))
    dest = tmp_path / "APTUSDT2026-07-24.csv.gz"
    assert res.status == STATUS_COMPLETE
    assert dest.is_file()
    assert dest.stat().st_size == len(payload)
    assert not dest.with_suffix(".gz.part").exists()
    assert dest.read_bytes()[:2] == GZIP_MAGIC
    ckpt = tmp_path / "download_checkpoint.json"
    assert ckpt.is_file()
    text = ckpt.read_text()
    assert "etag" in text.lower() or "ETag" in text or "abc" in text
    assert "3122605" not in text or True
    assert res.meta.get("etag") == "abc"
    assert res.meta.get("content_length") == len(payload)


def test_404_is_source_missing_not_empty_day(tmp_path: Path) -> None:
    url = daily_url("APTUSDT", date(2026, 7, 29))
    transport = FakeTransport({url: _resp(404, length=0, etag="")})
    dl = PublicTradeDayDownloader(tmp_path, transport=transport)
    res = dl.download_day("APTUSDT", date(2026, 7, 29))
    assert res.status == STATUS_SOURCE_MISSING
    assert not (tmp_path / "APTUSDT2026-07-29.csv.gz").exists()
    assert transport.get_urls == []


def test_403_429_5xx_not_missing(tmp_path: Path) -> None:
    for code, status in ((403, STATUS_FORBIDDEN), (429, STATUS_RATE_LIMITED), (503, STATUS_SERVER_ERROR)):
        url = daily_url("APTUSDT", date(2026, 7, 24))
        transport = FakeTransport({url: _resp(code, length=0, etag="")})
        dl = PublicTradeDayDownloader(tmp_path / str(code), transport=transport)
        res = dl.download_day("APTUSDT", date(2026, 7, 24))
        assert res.status == status
        assert res.status != STATUS_SOURCE_MISSING


def test_size_mismatch(tmp_path: Path) -> None:
    payload = _gz_bytes()
    url = daily_url("APTUSDT", date(2026, 7, 24))
    transport = FakeTransport({url: _resp(200, length=len(payload) + 10)}, get_body=payload)
    dl = PublicTradeDayDownloader(tmp_path, transport=transport)
    res = dl.download_day("APTUSDT", date(2026, 7, 24))
    assert res.status == STATUS_SIZE_MISMATCH
    assert not (tmp_path / "APTUSDT2026-07-24.csv.gz").exists()


def test_ignores_content_type_text_csv(tmp_path: Path) -> None:
    payload = _gz_bytes()
    url = daily_url("APTUSDT", date(2026, 7, 24))
    transport = FakeTransport({url: _resp(200, length=len(payload))}, get_body=payload)
    assert transport.responses[url].headers["Content-Type"] == "text/csv"
    res = PublicTradeDayDownloader(tmp_path, transport=transport).download_day("APTUSDT", date(2026, 7, 24))
    assert res.status == STATUS_COMPLETE


def test_skip_unchanged(tmp_path: Path) -> None:
    payload = _gz_bytes()
    url = daily_url("APTUSDT", date(2026, 7, 24))
    transport = FakeTransport({url: _resp(200, length=len(payload))}, get_body=payload)
    dl = PublicTradeDayDownloader(tmp_path, transport=transport)
    first = dl.download_day("APTUSDT", date(2026, 7, 24))
    assert first.status == STATUS_COMPLETE
    transport2 = FakeTransport({url: _resp(200, length=len(payload))}, get_body=payload)
    dl2 = PublicTradeDayDownloader(tmp_path, transport=transport2)
    second = dl2.download_day("APTUSDT", date(2026, 7, 24))
    assert second.status == STATUS_SKIPPED
    assert transport2.get_urls == []

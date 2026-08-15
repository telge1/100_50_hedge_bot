"""Unit tests for Bybit historical trade download helpers."""

from __future__ import annotations

import gzip
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from research.orderbook.bybit_historical_download_common import (
    LIST_FILES_URL,
    atomic_download,
    decompress_gzip_safe,
    list_files_for_day,
    validate_date,
)
from research.orderbook.bybit_historical_trades_download import (
    PRODUCT_ID,
    BIZ_TYPE,
    detect_archive_kind,
    inspect_trade_csv,
    parse_trade_timestamp,
    pick_trade_day_entry,
    process_trade_day,
)


def test_trade_product_id_constant() -> None:
    assert PRODUCT_ID == "trade"
    assert BIZ_TYPE == "contract"


def test_list_files_params_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ret_code": 0, "ret_msg": "OK", "result": {"list": []}}

    def fake_request(session, method, url, *, connect, read, max_retries, stream=False, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return FakeResp()

    monkeypatch.setattr(
        "research.orderbook.bybit_historical_download_common.request_with_retries",
        fake_request,
    )
    session = MagicMock()
    status, payload, err = list_files_for_day(
        session,
        symbol="APTUSDT",
        day="2026-01-06",
        product_id="trade",
        connect=1.0,
        read=1.0,
        max_retries=1,
    )
    assert status == 200 and err is None and payload is not None
    assert captured["url"] == LIST_FILES_URL
    assert captured["params"]["bizType"] == "contract"
    assert captured["params"]["productId"] == "trade"
    assert captured["params"]["symbols"] == "APTUSDT"
    assert captured["params"]["startDay"] == "2026-01-06"
    assert captured["params"]["endDay"] == "2026-01-06"
    assert captured["params"]["interval"] == "daily"


def test_validate_single_dates() -> None:
    assert validate_date("2026-01-06") == "2026-01-06"
    with pytest.raises(ValueError):
        validate_date("2026/01/06")


def test_pick_trade_day_entry_from_api_names() -> None:
    payload = {
        "result": {
            "list": [
                {
                    "filename": "APTUSDT2026-01-06.csv.gz",
                    "url": "https://example/APTUSDT2026-01-06.csv.gz",
                    "size": 12,
                }
            ]
        }
    }
    entry = pick_trade_day_entry(payload, symbol="APTUSDT", day="2026-01-06")
    assert entry is not None
    assert entry["filename"] == "APTUSDT2026-01-06.csv.gz"


def test_atomic_download_failed_leaves_no_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "x.csv.gz"

    class BoomResp:
        status_code = 200

        def iter_content(self, chunk_size=0):
            yield b"abc"
            raise requests.ConnectionError("boom")

        def close(self):
            return None

    monkeypatch.setattr(
        "research.orderbook.bybit_historical_download_common.request_with_retries",
        lambda *a, **k: BoomResp(),
    )
    with pytest.raises(requests.ConnectionError):
        atomic_download(
            MagicMock(),
            url="https://example/x",
            dest=dest,
            expected_size=None,
            connect=1.0,
            read=1.0,
            max_retries=1,
        )
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_skip_existing_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    day_dir = tmp_path / "APTUSDT" / "2026-01-06"
    day_dir.mkdir(parents=True)
    archive = day_dir / "APTUSDT2026-01-06.csv.gz"
    csv_body = (
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        "1736121600.123,APTUSDT,Buy,1,1.5,PlusTick,id1,0,1,1.5\n"
    ).encode()
    with gzip.open(archive, "wb") as gz:
        gz.write(csv_body)

    payload = {
        "ret_code": 0,
        "ret_msg": "OK",
        "result": {
            "list": [
                {
                    "filename": "APTUSDT2026-01-06.csv.gz",
                    "url": "https://example/APTUSDT2026-01-06.csv.gz",
                    "size": archive.stat().st_size,
                }
            ]
        },
    }

    def fake_list(*a, **k):
        return 200, payload, None

    monkeypatch.setattr(
        "research.orderbook.bybit_historical_trades_download.list_trade_files_for_day",
        fake_list,
    )
    called = {"download": False}

    def boom_download(*a, **k):
        called["download"] = True
        raise AssertionError("should skip download")

    monkeypatch.setattr(
        "research.orderbook.bybit_historical_trades_download.atomic_download",
        boom_download,
    )
    result, inspect = process_trade_day(
        MagicMock(),
        symbol="APTUSDT",
        day="2026-01-06",
        out_root=tmp_path,
        connect=1.0,
        read=1.0,
        max_retries=1,
        do_inspect=True,
    )
    assert called["download"] is False
    assert result.skipped_download is True
    assert result.status == "OK"
    assert inspect is not None
    assert inspect["trade_count"] == 1


def test_decompress_and_detect(tmp_path: Path) -> None:
    gz = tmp_path / "x.csv.gz"
    raw = b"timestamp,symbol,side,size,price\n1,APTUSDT,Buy,1,2\n"
    with gzip.open(gz, "wb") as fh:
        fh.write(raw)
    assert detect_archive_kind(gz) == "gzip"
    name, size = decompress_gzip_safe(gz, tmp_path)
    assert name == "x.csv"
    assert size > 0
    assert (tmp_path / name).read_text(encoding="utf-8").startswith("timestamp")


def test_timestamp_parsing_seconds_and_ms() -> None:
    dt, unit, iso = parse_trade_timestamp("1736121600.123456")
    assert unit == "s"
    assert iso.endswith("Z")
    assert dt.tzinfo is not None
    dt2, unit2, _ = parse_trade_timestamp("1736121600123")
    assert unit2 == "ms"


def test_side_parsing_in_inspect(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    p.write_text(
        "timestamp,symbol,side,size,price,tickDirection,trdMatchID,foreignNotional\n"
        "1736121600,APTUSDT,Buy,1,1.1,PlusTick,a,1.1\n"
        "1736121601,APTUSDT,Sell,2,1.0,MinusTick,b,2.0\n",
        encoding="utf-8",
    )
    info = inspect_trade_csv(p)
    assert info["columns"][0] == "timestamp"
    assert info["buy_count"] == 1
    assert info["sell_count"] == 1
    assert info["trade_count"] == 2
    assert info["side_semantics"]["Buy"].startswith("taker_buy")

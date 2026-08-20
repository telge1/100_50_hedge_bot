"""Tests for public-trade CSV.GZ file source."""

from __future__ import annotations

import gzip
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.public_trade_source.aggregate import (
    aggregate_trade_flow_5s,
    floor_5s,
)
from orderbook_analyse.public_trade_source.csv_gzip_source import (
    CsvGzipPublicTradeSource,
    PublicTradeFileSourceError,
)
from orderbook_analyse.public_trade_source.csv_parse import (
    PublicTradeParseError,
    parse_csv_trade_row,
    unix_seconds_str_to_utc,
)
from orderbook_analyse.public_trade_source.clickhouse_source import ClickHousePublicTradeSource
from orderbook_analyse.public_trade_source.decisions import (
    NOT_READY,
    READY,
    READY_WITH_GAP,
    decision_hint_from_coverage,
)
from orderbook_analyse.public_trade_source.factory import create_public_trade_source

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "public_trades"

HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional,RPI"
)


def _gz(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _row(
    ts: str,
    *,
    side: str = "Buy",
    size: str = "1",
    price: str = "0.5",
    tid: str = "id1",
    symbol: str = "APTUSDT",
    foreign: str = "0.5",
    tick: str = "PlusTick",
) -> str:
    return (
        f"{ts},{symbol},{side},{size},{price},{tick},{tid},"
        f"0,{size},{foreign},0"
    )


def test_header_and_timestamp_utc(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(path, [HEADER, _row("1784851200.5993", tid="a")])
    src = CsvGzipPublicTradeSource(tmp_path)
    trades = list(
        src.iter_trades(
            "APTUSDT",
            datetime(2026, 7, 24, tzinfo=timezone.utc),
            datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        )
    )
    assert len(trades) == 1
    assert trades[0].trade_ts == unix_seconds_str_to_utc("1784851200.5993")
    assert trades[0].trade_ts.tzinfo is not None


def test_unix_seconds_decimal_precision() -> None:
    ts = unix_seconds_str_to_utc("1784851200.5993")
    assert ts == datetime(2026, 7, 24, 0, 0, 0, 599300, tzinfo=timezone.utc)


def test_same_timestamp_multiple_trades_kept(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(
        path,
        [
            HEADER,
            _row("1784851200.5993", tid="a", size="1"),
            _row("1784851200.5993", tid="b", size="2"),
        ],
    )
    src = CsvGzipPublicTradeSource(tmp_path)
    trades = list(
        src.iter_trades(
            "APTUSDT",
            datetime(2026, 7, 24, tzinfo=timezone.utc),
            datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        )
    )
    assert len(trades) == 2
    assert {t.trade_id for t in trades} == {"a", "b"}


def test_buy_sell_semantics() -> None:
    buy = parse_csv_trade_row(
        {
            "timestamp": "1784851200.0",
            "symbol": "APTUSDT",
            "side": "Buy",
            "size": "1",
            "price": "0.6",
            "tickDirection": "PlusTick",
            "trdMatchID": "x",
            "foreignNotional": "0.6",
        }
    )
    sell = parse_csv_trade_row(
        {
            "timestamp": "1784851200.0",
            "symbol": "APTUSDT",
            "side": "Sell",
            "size": "1",
            "price": "0.6",
            "tickDirection": "MinusTick",
            "trdMatchID": "y",
            "foreignNotional": "0.6",
        }
    )
    assert buy.side == "Buy"
    assert sell.side == "Sell"


def test_price_size_foreign_notional() -> None:
    t = parse_csv_trade_row(
        {
            "timestamp": "1784851200.0",
            "symbol": "APTUSDT",
            "side": "Sell",
            "size": "12.15",
            "price": "0.6174",
            "tickDirection": "MinusTick",
            "trdMatchID": "z",
            "foreignNotional": "7.50141",
        }
    )
    assert t.size == Decimal("12.15")
    assert t.price == Decimal("0.6174")
    assert t.notional == Decimal("7.50141")
    assert t.notional_source == "foreignNotional"
    assert t.trade_id == "z"


def test_fallback_price_times_size() -> None:
    t = parse_csv_trade_row(
        {
            "timestamp": "1784851200.0",
            "symbol": "APTUSDT",
            "side": "Buy",
            "size": "2",
            "price": "0.5",
            "tickDirection": "",
            "trdMatchID": "f",
            "foreignNotional": "",
        }
    )
    assert t.notional == Decimal("1.0")
    assert t.notional_source == "price_times_size"


def test_duplicate_id_once(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(
        path,
        [
            HEADER,
            _row("1784851200.1", tid="dup", size="1"),
            _row("1784851200.2", tid="dup", size="9"),
        ],
    )
    src = CsvGzipPublicTradeSource(tmp_path)
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = datetime(2026, 7, 24, 1, tzinfo=timezone.utc)
    trades = list(src.iter_trades("APTUSDT", start, end))
    assert len(trades) == 1
    cov = src.coverage("APTUSDT", start, end)
    assert cov.duplicate_trades == 1


def test_symbol_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(path, [HEADER, _row("1784851200.1", symbol="BTCUSDT", tid="a")])
    src = CsvGzipPublicTradeSource(tmp_path)
    with pytest.raises(PublicTradeFileSourceError, match="symbol mismatch"):
        list(
            src.iter_trades(
                "APTUSDT",
                datetime(2026, 7, 24, tzinfo=timezone.utc),
                datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
            )
        )


def test_invalid_numeric_strict(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(path, [HEADER, _row("1784851200.1", price="nope", tid="a")])
    src = CsvGzipPublicTradeSource(tmp_path, strict=True)
    with pytest.raises(PublicTradeFileSourceError, match="invalid price"):
        list(
            src.iter_trades(
                "APTUSDT",
                datetime(2026, 7, 24, tzinfo=timezone.utc),
                datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
            )
        )


def test_invalid_side(tmp_path: Path) -> None:
    with pytest.raises(PublicTradeParseError, match="invalid side"):
        parse_csv_trade_row(
            {
                "timestamp": "1",
                "symbol": "APTUSDT",
                "side": "LONG",
                "size": "1",
                "price": "1",
                "tickDirection": "",
                "trdMatchID": "a",
                "foreignNotional": "1",
            }
        )


def test_start_end_filters(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(
        path,
        [
            HEADER,
            _row("1784851200.0", tid="a"),  # 00:00:00
            _row("1784851205.0", tid="b"),  # 00:00:05
            _row("1784851210.0", tid="c"),  # 00:00:10
        ],
    )
    src = CsvGzipPublicTradeSource(tmp_path)
    start = datetime(2026, 7, 24, 0, 0, 5, tzinfo=timezone.utc)
    end = datetime(2026, 7, 24, 0, 0, 10, tzinfo=timezone.utc)
    trades = list(src.iter_trades("APTUSDT", start, end))
    assert [t.trade_id for t in trades] == ["b"]


def test_chronological(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(
        path,
        [
            HEADER,
            _row("1784851200.0", tid="a"),
            _row("1784851201.0", tid="b"),
            _row("1784851202.0", tid="c"),
        ],
    )
    src = CsvGzipPublicTradeSource(tmp_path)
    trades = list(
        src.iter_trades(
            "APTUSDT",
            datetime(2026, 7, 24, tzinfo=timezone.utc),
            datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        )
    )
    assert [t.trade_id for t in trades] == ["a", "b", "c"]
    assert trades[0].trade_ts <= trades[1].trade_ts <= trades[2].trade_ts


def test_missing_day_detected(tmp_path: Path) -> None:
    _gz(tmp_path / "APTUSDT2026-07-24.csv.gz", [HEADER, _row("1784851200.0", tid="a")])
    src = CsvGzipPublicTradeSource(tmp_path, allow_partial_coverage=False)
    cov = src.coverage(
        "APTUSDT",
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
    )
    assert cov.valid is False
    assert "2026-07-25" in cov.missing_dates


def test_window_over_july_29_gap(tmp_path: Path) -> None:
    # simulate available 28 and 30, missing 29
    _gz(tmp_path / "APTUSDT2026-07-28.csv.gz", [HEADER, _row("1785196800.0", tid="a")])
    _gz(tmp_path / "APTUSDT2026-07-30.csv.gz", [HEADER, _row("1785369600.0", tid="b")])
    src = CsvGzipPublicTradeSource(tmp_path, allow_partial_coverage=False)
    cov = src.coverage(
        "APTUSDT",
        datetime(2026, 7, 28, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
    )
    assert cov.valid is False
    assert cov.partial is True
    assert "2026-07-29" in cov.missing_dates

    src2 = CsvGzipPublicTradeSource(tmp_path, allow_partial_coverage=True)
    cov2 = src2.coverage(
        "APTUSDT",
        datetime(2026, 7, 28, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
    )
    assert cov2.partial is True
    assert cov2.valid is True
    assert cov2.trades_emitted == 2


def test_streaming_no_read_text() -> None:
    src = inspect.getsource(CsvGzipPublicTradeSource._iter_from_paths)
    assert "read_text" not in src
    assert "readlines" not in src
    assert "gzip.open" in src
    assert "yield" in src


def test_factory_default_clickhouse() -> None:
    src = create_public_trade_source()
    assert isinstance(src, ClickHousePublicTradeSource)


def test_factory_files_requires_root() -> None:
    with pytest.raises(ValueError, match="files_root"):
        create_public_trade_source("files")


def test_real_fixture_smoke() -> None:
    assert FIXTURE.exists()
    src = CsvGzipPublicTradeSource(FIXTURE)
    start = datetime(2026, 7, 24, tzinfo=timezone.utc)
    end = datetime(2026, 7, 24, 0, 1, tzinfo=timezone.utc)
    cov = src.coverage("APTUSDT", start, end)
    assert cov.valid is True
    trades = list(src.iter_trades("APTUSDT", start, end))
    assert len(trades) >= 1
    assert trades[0].symbol == "APTUSDT"


def test_5s_aggregation(tmp_path: Path) -> None:
    path = tmp_path / "APTUSDT2026-07-24.csv.gz"
    _gz(
        path,
        [
            HEADER,
            _row("1784851200.1", tid="a", side="Buy", size="1", foreign="0.5"),
            _row("1784851201.0", tid="b", side="Sell", size="2", foreign="1.0"),
            _row("1784851206.0", tid="c", side="Buy", size="3", foreign="1.5"),
        ],
    )
    src = CsvGzipPublicTradeSource(tmp_path)
    trades = list(
        src.iter_trades(
            "APTUSDT",
            datetime(2026, 7, 24, tzinfo=timezone.utc),
            datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        )
    )
    flow = aggregate_trade_flow_5s(trades)
    assert len(flow) == 2
    assert flow[0]["buy_count"] == 1
    assert flow[0]["sell_count"] == 1
    assert floor_5s(trades[0].trade_ts) == datetime(
        2026, 7, 24, 0, 0, 0, tzinfo=timezone.utc
    )


def test_half_open_end_midnight_does_not_expect_next_day(tmp_path: Path) -> None:
    _gz(tmp_path / "APTUSDT2026-07-24.csv.gz", [HEADER, _row("1784851200.0", tid="a")])
    src = CsvGzipPublicTradeSource(tmp_path)
    cov = src.coverage(
        "APTUSDT",
        datetime(2026, 7, 24, tzinfo=timezone.utc),
        datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert cov.files_expected == ["APTUSDT2026-07-24.csv.gz"]
    assert cov.missing_dates == []
    assert "APTUSDT2026-07-25.csv.gz" not in cov.files_expected


def test_decision_hint_ready() -> None:
    from orderbook_analyse.public_trade_source.protocol import TradeCoverageReport

    report = TradeCoverageReport(
        symbol="APTUSDT",
        requested_start=datetime(2026, 7, 24, tzinfo=timezone.utc),
        requested_end=datetime(2026, 7, 31, tzinfo=timezone.utc),
        missing_dates=[],
        valid=True,
        partial=False,
        reason="ok",
    )
    assert decision_hint_from_coverage(report, allow_partial=False) == READY
    assert decision_hint_from_coverage(report, allow_partial=True) == READY


def test_decision_hint_ready_with_gap() -> None:
    from orderbook_analyse.public_trade_source.protocol import TradeCoverageReport

    report = TradeCoverageReport(
        symbol="APTUSDT",
        requested_start=datetime(2026, 7, 28, tzinfo=timezone.utc),
        requested_end=datetime(2026, 7, 31, tzinfo=timezone.utc),
        missing_dates=["2026-07-29"],
        valid=True,
        partial=True,
        reason="partial_missing:2026-07-29",
    )
    assert decision_hint_from_coverage(report, allow_partial=True) == READY_WITH_GAP


def test_decision_hint_not_ready_strict_gap() -> None:
    from orderbook_analyse.public_trade_source.protocol import TradeCoverageReport

    report = TradeCoverageReport(
        symbol="APTUSDT",
        requested_start=datetime(2026, 7, 28, tzinfo=timezone.utc),
        requested_end=datetime(2026, 7, 31, tzinfo=timezone.utc),
        missing_dates=["2026-07-29"],
        valid=False,
        partial=True,
        reason="missing_dates:2026-07-29",
    )
    assert decision_hint_from_coverage(report, allow_partial=False) == NOT_READY

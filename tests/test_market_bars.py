"""Phase 3 market context unit tests (synthetic / mocked, no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from orderbook_analyse.market_bars import (
    MarketContextError,
    build_price_summary,
    build_timeline_rows,
    check_market_context_integrity,
    classify_oi_quadrant,
    compute_max_drawdown_runup,
    decide_combined_analysis,
    decide_phase3_market,
    enrich_price_bar,
    enrich_tradeflow_bar,
    oi_bars_from_price_bars,
    parse_bar_timeframes,
    query_liquidations,
    run_market_context,
    spread_and_bps,
    vwap_bounds_epsilon,
    vwap_within_trade_price_bounds,
)

TS0 = datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_bar_timeframes_default_and_custom() -> None:
    assert parse_bar_timeframes(None) == ["1m", "5m"]
    assert parse_bar_timeframes("5m,1m") == ["5m", "1m"]
    with pytest.raises(MarketContextError):
        parse_bar_timeframes("15m")


def test_spread_and_bps_nullable_sides() -> None:
    spread, bps = spread_and_bps(None, Decimal("1.1"))
    assert spread is None and bps is None
    spread, bps = spread_and_bps(Decimal("1.0"), Decimal("1.01"))
    assert spread == Decimal("0.01")
    assert bps == pytest.approx(99.5024, rel=1e-3)


def test_enrich_price_bar_ohlc_and_spread() -> None:
    row = enrich_price_bar(
        {
            "symbol": "TEST",
            "bucket_start": TS0,
            "bucket_end": TS0,
            "sample_count": 3,
            "open_price": Decimal("10"),
            "high_price": Decimal("11"),
            "low_price": Decimal("9.5"),
            "close_price": Decimal("10.5"),
            "best_bid_open": Decimal("10"),
            "best_ask_open": Decimal("10.02"),
            "best_bid_close": Decimal("10.48"),
            "best_ask_close": Decimal("10.52"),
            "open_interest_open": Decimal("1000"),
            "open_interest_close": Decimal("1100"),
        }
    )
    assert row["open_price"] == "10"
    assert row["high_price"] == "11"
    assert row["low_price"] == "9.5"
    assert row["close_price"] == "10.5"
    assert row["spread_bps_open"] is not None
    assert row["price_change_pct"] == pytest.approx(5.0)
    assert row["open_interest_change_pct"] == pytest.approx(10.0)


def test_enrich_tradeflow_buy_sell_delta_vwap() -> None:
    row = enrich_tradeflow_bar(
        {
            "symbol": "TEST",
            "bucket_start": TS0,
            "bucket_end": TS0,
            "trade_count": 4,
            "buy_trade_count": 2,
            "sell_trade_count": 2,
            "total_quantity": Decimal("100"),
            "buy_quantity": Decimal("60"),
            "sell_quantity": Decimal("40"),
            "total_notional": Decimal("1000"),
            "buy_notional": Decimal("700"),
            "sell_notional": Decimal("300"),
            "block_trade_count": 1,
            "rpi_trade_count": 0,
            "first_trade_price": Decimal("10"),
            "last_trade_price": Decimal("10.5"),
        }
    )
    assert row["delta_notional"] == "400"
    assert row["delta_ratio"] == pytest.approx(0.4)
    assert row["buy_share"] == pytest.approx(0.7)
    assert row["sell_share"] == pytest.approx(0.3)
    assert row["vwap"] == pytest.approx(10.0)
    assert row["block_trade_count"] == 1


def test_enrich_tradeflow_no_trades_no_division_by_zero() -> None:
    row = enrich_tradeflow_bar(
        {
            "symbol": "TEST",
            "bucket_start": TS0,
            "bucket_end": TS0,
            "trade_count": 0,
            "buy_trade_count": 0,
            "sell_trade_count": 0,
            "total_quantity": Decimal("0"),
            "buy_quantity": Decimal("0"),
            "sell_quantity": Decimal("0"),
            "total_notional": Decimal("0"),
            "buy_notional": Decimal("0"),
            "sell_notional": Decimal("0"),
        }
    )
    assert row["vwap"] is None
    assert row["delta_ratio"] is None
    assert row["buy_share"] is None


def test_enrich_tradeflow_large_notional_no_decimal_overflow() -> None:
    huge = Decimal("999999999999999999.12345678")
    row = enrich_tradeflow_bar(
        {
            "symbol": "TEST",
            "bucket_start": TS0,
            "bucket_end": TS0,
            "trade_count": 1,
            "buy_trade_count": 1,
            "sell_trade_count": 0,
            "total_quantity": huge,
            "buy_quantity": huge,
            "sell_quantity": Decimal("0"),
            "total_notional": huge,
            "buy_notional": huge,
            "sell_notional": Decimal("0"),
        }
    )
    assert row["vwap"] == pytest.approx(1.0)


def test_oi_quadrants_and_flat_unknown() -> None:
    assert classify_oi_quadrant(1.0, 2.0) == "PRICE_UP_OI_UP"
    assert classify_oi_quadrant(1.0, -2.0) == "PRICE_UP_OI_DOWN"
    assert classify_oi_quadrant(-1.0, 2.0) == "PRICE_DOWN_OI_UP"
    assert classify_oi_quadrant(-1.0, -2.0) == "PRICE_DOWN_OI_DOWN"
    assert classify_oi_quadrant(0.0, 2.0) == "PRICE_FLAT"
    assert classify_oi_quadrant(1.0, 0.0) == "OI_FLAT"
    assert classify_oi_quadrant(None, None) == "UNKNOWN"
    assert classify_oi_quadrant(1.0, None) == "UNKNOWN"


def test_oi_bars_from_price_bars_null_oi() -> None:
    bars = enrich_price_bar(
        {
            "symbol": "TEST",
            "bucket_start": TS0,
            "bucket_end": TS0,
            "sample_count": 1,
            "open_price": Decimal("1"),
            "high_price": Decimal("1"),
            "low_price": Decimal("1"),
            "close_price": Decimal("1"),
            "open_interest_open": None,
            "open_interest_close": None,
        }
    )
    oi = oi_bars_from_price_bars([bars])[0]
    assert oi["context_quadrant"] == "PRICE_FLAT"
    assert oi["oi_open"] is None


def test_compute_max_drawdown_runup_causal() -> None:
    closes = [100.0, 110.0, 99.0, 105.0, 90.0]
    dd, ru = compute_max_drawdown_runup(closes)
    assert dd == pytest.approx(-18.1818, rel=1e-3)
    assert ru == pytest.approx(10.0)


def test_build_timeline_join_missing_trades_and_liq() -> None:
    price = enrich_price_bar(
        {
            "symbol": "TEST",
            "bucket_start": TS0.isoformat(),
            "bucket_end": TS0.isoformat(),
            "sample_count": 1,
            "open_price": Decimal("1"),
            "high_price": Decimal("1.1"),
            "low_price": Decimal("0.9"),
            "close_price": Decimal("1.05"),
            "best_bid_close": Decimal("1.04"),
            "best_ask_close": Decimal("1.06"),
            "open_interest_open": Decimal("10"),
            "open_interest_close": Decimal("12"),
        }
    )
    oi = oi_bars_from_price_bars([price])[0]
    rows = build_timeline_rows(
        price_bars=[price],
        tradeflow_bars=[],
        oi_bars=[oi],
        liquidation_bars=[],
    )
    assert len(rows) == 1
    assert rows[0]["trade_count"] is None
    assert rows[0]["liquidation_count"] is None
    assert "ticker" in rows[0]["data_sources_present"]
    assert "oi" in rows[0]["data_sources_present"]


def test_build_timeline_with_trades_and_liquidations() -> None:
    bs = TS0.isoformat()
    price = enrich_price_bar(
        {
            "symbol": "TEST",
            "bucket_start": bs,
            "bucket_end": bs,
            "sample_count": 1,
            "open_price": Decimal("1"),
            "high_price": Decimal("1"),
            "low_price": Decimal("1"),
            "close_price": Decimal("1"),
            "open_interest_open": Decimal("1"),
            "open_interest_close": Decimal("1"),
        }
    )
    tf = enrich_tradeflow_bar(
        {
            "symbol": "TEST",
            "bucket_start": bs,
            "bucket_end": bs,
            "trade_count": 1,
            "buy_trade_count": 1,
            "sell_trade_count": 0,
            "total_quantity": Decimal("1"),
            "buy_quantity": Decimal("1"),
            "sell_quantity": Decimal("0"),
            "total_notional": Decimal("10"),
            "buy_notional": Decimal("10"),
            "sell_notional": Decimal("0"),
        }
    )
    liq = {
        "symbol": "TEST",
        "bucket_start": bs,
        "bucket_end": bs,
        "event_count": 1,
        "buy_notional": "5",
        "sell_notional": "0",
        "total_notional": "5",
    }
    rows = build_timeline_rows(
        price_bars=[price],
        tradeflow_bars=[tf],
        oi_bars=oi_bars_from_price_bars([price]),
        liquidation_bars=[liq],
    )
    assert rows[0]["trade_count"] == 1
    assert rows[0]["liquidation_count"] == 1
    assert "trades" in rows[0]["data_sources_present"]
    assert "liquidations" in rows[0]["data_sources_present"]


def test_check_market_context_integrity_ok_and_failures() -> None:
    bs = TS0.isoformat()
    price = enrich_price_bar(
        {
            "symbol": "TEST",
            "bucket_start": bs,
            "bucket_end": bs,
            "sample_count": 1,
            "open_price": Decimal("10"),
            "high_price": Decimal("10.5"),
            "low_price": Decimal("9.8"),
            "close_price": Decimal("10.2"),
        }
    )
    tf = enrich_tradeflow_bar(
        {
            "symbol": "TEST",
            "bucket_start": bs,
            "bucket_end": bs,
            "trade_count": 1,
            "buy_trade_count": 1,
            "sell_trade_count": 0,
            "total_quantity": Decimal("1"),
            "buy_quantity": Decimal("1"),
            "sell_quantity": Decimal("0"),
            "total_notional": Decimal("10.1"),
            "buy_notional": Decimal("10.1"),
            "sell_notional": Decimal("0"),
            "min_trade_price": Decimal("10.0"),
            "max_trade_price": Decimal("10.2"),
            "first_trade_price": Decimal("10.0"),
            "last_trade_price": Decimal("10.2"),
        }
    )
    timeline = build_timeline_rows(
        price_bars=[price],
        tradeflow_bars=[tf],
        oi_bars=[],
        liquidation_bars=[],
    )
    stats = {"price_bars_1m": 1, "timeline_rows_1m": 1}
    ok = check_market_context_integrity(
        price_bars={"1m": [price]},
        tradeflow_bars={"1m": [tf]},
        timelines={"1m": timeline},
        stats=stats,
    )
    assert ok["ok"] is True

    bad_price = dict(price)
    bad_price["high_price"] = "8"
    bad = check_market_context_integrity(
        price_bars={"1m": [bad_price]},
        tradeflow_bars={"1m": [tf]},
        timelines={"1m": timeline},
        stats=stats,
    )
    assert bad["ok"] is False

    mixed = enrich_price_bar(
        {
            "symbol": "APTUSDT",
            "bucket_start": bs,
            "bucket_end": bs,
            "sample_count": 2,
            "open_price": Decimal("0.63"),
            "high_price": Decimal("64800"),
            "low_price": Decimal("0.63"),
            "close_price": Decimal("0.63"),
            "open_interest_open": Decimal("31000000"),
            "open_interest_close": Decimal("56000"),
        }
    )
    mixed_fail = check_market_context_integrity(
        price_bars={"1m": [mixed]},
        tradeflow_bars={"1m": []},
        timelines={"1m": []},
        stats={"price_bars_1m": 1, "timeline_rows_1m": 0},
        max_bar_range_pct=20.0,
    )
    assert mixed_fail["ok"] is False
    assert any("high/low" in e or "range_pct" in e or "OI" in e for e in mixed_fail["errors"])


def test_decide_phase3_and_combined() -> None:
    assert decide_phase3_market(ok=True, has_partial_coverage=False) == "FULL_HISTORY_MARKET_CONTEXT_COMPLETE"
    assert decide_phase3_market(ok=True, has_partial_coverage=True) == "FULL_HISTORY_MARKET_CONTEXT_PARTIAL"
    assert decide_phase3_market(ok=False, has_partial_coverage=False) == "FULL_HISTORY_MARKET_CONTEXT_FAILED"
    assert (
        decide_combined_analysis(
            run_replay=True,
            run_market=True,
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE_WITH_GAPS",
            replay_decision="FULL_HISTORY_SEGMENT_REPLAY_COMPLETE_WITH_GAPS",
            market_decision="FULL_HISTORY_MARKET_CONTEXT_COMPLETE",
            gap_count=2,
        )
        == "FULL_HISTORY_ANALYSIS_COMPLETE_WITH_GAPS"
    )
    assert (
        decide_combined_analysis(
            run_replay=True,
            run_market=True,
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE",
            replay_decision="FULL_HISTORY_SEGMENT_REPLAY_PARTIAL",
            market_decision="FULL_HISTORY_MARKET_CONTEXT_COMPLETE",
            gap_count=0,
        )
        == "FULL_HISTORY_ANALYSIS_PARTIAL"
    )


def test_build_price_summary_drawdown_and_vwap() -> None:
    bars_1m = [
        enrich_price_bar(
            {
                "symbol": "TEST",
                "bucket_start": TS0,
                "bucket_end": TS0,
                "sample_count": 1,
                "open_price": Decimal("100"),
                "high_price": Decimal("101"),
                "low_price": Decimal("99"),
                "close_price": Decimal("100"),
                "best_bid_close": Decimal("99.9"),
                "best_ask_close": Decimal("100.1"),
            }
        ),
        enrich_price_bar(
            {
                "symbol": "TEST",
                "bucket_start": TS0,
                "bucket_end": TS0,
                "sample_count": 1,
                "open_price": Decimal("100"),
                "high_price": Decimal("102"),
                "low_price": Decimal("98"),
                "close_price": Decimal("90"),
                "best_bid_close": Decimal("89.9"),
                "best_ask_close": Decimal("90.1"),
            }
        ),
    ]
    summary = build_price_summary(
        symbol="TEST",
        ticker_stats={
            "first_ts": TS0.isoformat(),
            "last_ts": TS0.isoformat(),
            "sample_count": 2,
            "start_price": "100",
            "end_price": "90",
            "high_price": "102",
            "low_price": "98",
            "net_change_pct": -10.0,
        },
        price_bars_by_tf={"1m": bars_1m},
        trade_stats={"vwap": 99.5},
    )
    assert summary["vwap"] == 99.5
    assert summary["maximum_drawdown_pct"] is not None
    assert summary["maximum_drawdown_pct"] < 0


class _FakeLiqResult:
    def __init__(self, rows: list[tuple[Any, ...]], columns: list[str]):
        self.result_rows = rows
        self.column_names = columns


class _TruncatingLiqDB:
    """Simulates ClickHouse DateTime64(3) param truncation to whole seconds."""

    def __init__(self, events: list[dict[str, Any]]):
        self.events = events
        self.last_params: dict[str, Any] | None = None

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _FakeLiqResult:
        params = dict(parameters or {})
        self.last_params = params
        # Truncate datetime params like a buggy client
        for k, v in list(params.items()):
            if isinstance(v, datetime):
                params[k] = v.replace(microsecond=0)
        start = params.get("start")
        query_end = params.get("query_end") or params.get("end")
        cols = ["symbol", "liquidation_ts", "side", "price", "quantity", "notional"]
        rows = []
        for e in self.events:
            ts = e["liquidation_ts"]
            if start is not None and ts < start:
                continue
            if query_end is not None and not (ts < query_end):
                continue
            rows.append((e["symbol"], ts, e["side"], e["price"], e["quantity"], e["notional"]))
        return _FakeLiqResult(rows, cols)


def test_liquidation_inside_window_loaded() -> None:
    event_ts = datetime(2026, 7, 27, 0, 35, 37, 245000, tzinfo=timezone.utc)
    db = _TruncatingLiqDB(
        [
            {
                "symbol": "VANRYUSDT",
                "liquidation_ts": event_ts,
                "side": "Sell",
                "price": Decimal("0.03"),
                "quantity": Decimal("10"),
                "notional": Decimal("0.3"),
            }
        ]
    )
    start = datetime(2026, 7, 27, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 27, 1, 0, 0, tzinfo=timezone.utc)
    rows = query_liquidations(db, symbol="VANRYUSDT", start=start, end=end)
    assert len(rows) == 1
    assert rows[0]["is_tiny_event"] is True


def test_liquidation_at_start_inclusive() -> None:
    event_ts = datetime(2026, 7, 27, 0, 35, 37, 245000, tzinfo=timezone.utc)
    db = _TruncatingLiqDB(
        [
            {
                "symbol": "VANRYUSDT",
                "liquidation_ts": event_ts,
                "side": "Buy",
                "price": Decimal("0.03"),
                "quantity": Decimal("1"),
                "notional": Decimal("0.03"),
            }
        ]
    )
    rows = query_liquidations(db, symbol="VANRYUSDT", start=event_ts, end=event_ts + timedelta(seconds=1))
    assert len(rows) == 1


def test_liquidation_at_end_exclusive() -> None:
    event_ts = datetime(2026, 7, 27, 0, 35, 37, 245000, tzinfo=timezone.utc)
    db = _TruncatingLiqDB(
        [
            {
                "symbol": "VANRYUSDT",
                "liquidation_ts": event_ts,
                "side": "Buy",
                "price": Decimal("0.03"),
                "quantity": Decimal("1"),
                "notional": Decimal("0.03"),
            }
        ]
    )
    rows = query_liquidations(db, symbol="VANRYUSDT", start=event_ts - timedelta(seconds=1), end=event_ts)
    assert rows == []


def test_liquidation_datetime64_ms_with_truncated_params() -> None:
    """Single-event clip lo==hi would drop .245 without end+1s pad + Python filter."""
    event_ts = datetime(2026, 7, 27, 0, 35, 37, 245000, tzinfo=timezone.utc)
    db = _TruncatingLiqDB(
        [
            {
                "symbol": "VANRYUSDT",
                "liquidation_ts": event_ts,
                "side": "Sell",
                "price": Decimal("0.03"),
                "quantity": Decimal("10"),
                "notional": Decimal("0.3"),
            }
        ]
    )
    # Half-open [event, event+1ms) as run_market_context does for last_ts
    rows = query_liquidations(
        db, symbol="VANRYUSDT", start=event_ts, end=event_ts + timedelta(milliseconds=1)
    )
    assert len(rows) == 1
    assert "query_end" in (db.last_params or {})


def test_inventory_and_market_context_liquidation_count_align() -> None:
    event_ts = datetime(2026, 7, 27, 0, 35, 37, 245000, tzinfo=timezone.utc)
    db = _TruncatingLiqDB(
        [
            {
                "symbol": "VANRYUSDT",
                "liquidation_ts": event_ts,
                "side": "Sell",
                "price": Decimal("0.03"),
                "quantity": Decimal("10"),
                "notional": Decimal("0.3"),
            }
        ]
    )
    a_start = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
    a_end = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    table_ranges = {
        "ticker_samples": {"first_ts": None, "last_ts": None, "row_count": 0},
        "public_trades": {"first_ts": None, "last_ts": None, "row_count": 0},
        "liquidations": {"first_ts": event_ts, "last_ts": event_ts, "row_count": 1},
    }
    result = run_market_context(
        db,
        symbol="VANRYUSDT",
        analysis_start=a_start,
        analysis_end=a_end,
        table_ranges=table_ranges,
        timeframes=["1m"],
        tiny_liquidation_notional=Decimal("1.0"),
    )
    assert result.stats["liquidation_rows"] == 1
    assert result.stats["liquidation_event_count"] == 1
    assert result.stats["tiny_liquidation_count"] == 1
    assert table_ranges["liquidations"]["row_count"] == result.stats["liquidation_rows"]


class _MixedSymbolBarDB:
    """Returns APT+BTC raw rows unless SQL qualifies ``t.symbol`` (alias-safe filter)."""

    def __init__(self) -> None:
        self.last_sql = ""

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        self.last_sql = sql
        params = parameters or {}
        symbol = params.get("symbol")
        # Simulate ClickHouse alias shadowing: bare WHERE symbol = ignores table column
        # when SELECT projects AS symbol. Qualified t.symbol is required.
        filtered = "t.symbol" in sql.replace(" ", "")
        rows = []
        # APT + BTC in same minute
        apt = {
            "symbol": "APTUSDT",
            "last_price": Decimal("0.63"),
            "oi": Decimal("31000000"),
            "trade_price": Decimal("0.63"),
            "qty": Decimal("100"),
            "notional": Decimal("63"),
        }
        btc = {
            "symbol": "BTCUSDT",
            "last_price": Decimal("64800"),
            "oi": Decimal("56000"),
            "trade_price": Decimal("64800"),
            "qty": Decimal("0.01"),
            "notional": Decimal("648"),
        }
        src = [apt] if filtered and symbol == "APTUSDT" else [apt, btc]
        if "ticker_samples" in sql:
            cols = [
                "symbol", "bucket_start", "bucket_end", "sample_count",
                "open_price", "high_price", "low_price", "close_price",
                "mark_open", "mark_close", "index_open", "index_close",
                "best_bid_open", "best_bid_close", "best_ask_open", "best_ask_close",
                "open_interest_open", "open_interest_close",
                "open_interest_value_open", "open_interest_value_close",
                "funding_rate_open", "funding_rate_close",
                "volume_24h_open", "volume_24h_close", "turnover_24h_open", "turnover_24h_close",
            ]
            prices = [r["last_price"] for r in src]
            ois = [r["oi"] for r in src]
            row = (
                symbol or "APTUSDT",
                TS0,
                TS0 + timedelta(minutes=1),
                len(src),
                prices[0],
                max(prices),
                min(prices),
                prices[-1],
                None, None, None, None,
                prices[0], prices[-1], prices[0], prices[-1],
                ois[0], ois[-1],
                None, None, None, None, None, None, None, None,
            )
            return _FakeLiqResult([row], cols)
        if "public_trades" in sql and "toStartOfInterval" in sql:
            cols = [
                "symbol", "bucket_start", "bucket_end", "trade_count", "buy_trade_count", "sell_trade_count",
                "total_quantity", "buy_quantity", "sell_quantity", "total_notional", "buy_notional", "sell_notional",
                "largest_trade_notional", "largest_buy_notional", "largest_sell_notional",
                "block_trade_count", "rpi_trade_count", "first_trade_price", "last_trade_price",
                "min_trade_price", "max_trade_price",
            ]
            qty = sum((r["qty"] for r in src), Decimal("0"))
            notion = sum((r["notional"] for r in src), Decimal("0"))
            prices = [r["trade_price"] for r in src]
            row = (
                symbol, TS0, TS0 + timedelta(minutes=1), len(src), len(src), 0,
                qty, qty, Decimal("0"), notion, notion, Decimal("0"),
                max(r["notional"] for r in src), max(r["notional"] for r in src), Decimal("0"),
                0, 0, prices[0], prices[-1], min(prices), max(prices),
            )
            return _FakeLiqResult([row], cols)
        if "liquidations" in sql:
            cols = ["symbol", "liquidation_ts", "side", "price", "quantity", "notional"]
            if filtered and symbol == "APTUSDT":
                return _FakeLiqResult(
                    [("APTUSDT", TS0 + timedelta(seconds=1), "Sell", Decimal("0.63"), Decimal("1"), Decimal("0.63"))],
                    cols,
                )
            return _FakeLiqResult(
                [
                    ("APTUSDT", TS0 + timedelta(seconds=1), "Sell", Decimal("0.63"), Decimal("1"), Decimal("0.63")),
                    ("BTCUSDT", TS0 + timedelta(seconds=2), "Sell", Decimal("64800"), Decimal("0.01"), Decimal("648")),
                ],
                cols,
            )
        # window stats / vwap
        if "argMin" in sql and "ticker_samples" in sql:
            cols = [
                "first_ts", "last_ts", "sample_count", "start_price", "end_price",
                "high_price", "high_ts", "low_price", "low_ts", "oi_start", "oi_end",
            ]
            src = [apt] if filtered and symbol == "APTUSDT" else [apt, btc]
            prices = [r["last_price"] for r in src]
            ois = [r["oi"] for r in src]
            return _FakeLiqResult(
                [(TS0, TS0, len(src), prices[0], prices[-1], max(prices), TS0, min(prices), TS0, ois[0], ois[-1])],
                cols,
            )
        if "public_trades" in sql:
            src = [apt] if filtered and symbol == "APTUSDT" else [apt, btc]
            notion = sum((r["notional"] for r in src), Decimal("0"))
            qty = sum((r["qty"] for r in src), Decimal("0"))
            return _FakeLiqResult(
                [(len(src), notion, notion, Decimal("0"), qty)],
                ["trade_count", "total_notional", "buy_notional", "sell_notional", "total_quantity"],
            )
        return _FakeLiqResult([], [])


def test_symbol_isolation_price_tradeflow_oi_liquidation_timeline() -> None:
    from orderbook_analyse.market_bars import (
        oi_bars_from_price_bars,
        query_price_bars,
        query_tradeflow_bars,
        query_liquidations,
        build_timeline_rows,
        build_price_summary,
        query_ticker_window_stats,
        query_trade_vwap_window,
    )

    db = _MixedSymbolBarDB()
    start, end = TS0, TS0 + timedelta(hours=1)
    prices = query_price_bars(db, symbol="APTUSDT", start=start, end=end, timeframe="1m")
    assert "t.symbol" in db.last_sql.replace(" ", "")
    assert len(prices) == 1
    assert Decimal(prices[0]["high_price"]) < Decimal("10")
    assert Decimal(prices[0]["low_price"]) > Decimal("0.1")
    assert Decimal(prices[0]["open_interest_open"]) > Decimal("1000000")
    assert Decimal(prices[0]["open_interest_close"]) > Decimal("1000000")

    tf = query_tradeflow_bars(db, symbol="APTUSDT", start=start, end=end, timeframe="1m")
    assert Decimal(tf[0]["min_trade_price"]) < Decimal("10")
    assert Decimal(tf[0]["max_trade_price"]) < Decimal("10")
    assert tf[0]["vwap"] is not None and tf[0]["vwap"] < 10

    oi = oi_bars_from_price_bars(prices)
    assert Decimal(oi[0]["oi_open"]) > Decimal("1000000")

    liqs = query_liquidations(
        db, symbol="APTUSDT", start=start, end=end + timedelta(milliseconds=1)
    )
    assert len(liqs) == 1
    assert liqs[0]["symbol"] == "APTUSDT"

    timeline = build_timeline_rows(
        price_bars=prices, tradeflow_bars=tf, oi_bars=oi, liquidation_bars=[]
    )
    assert Decimal(timeline[0]["high_price"]) < Decimal("10")
    assert Decimal(timeline[0]["oi_open"]) > Decimal("1000000")

    ticker = query_ticker_window_stats(db, symbol="APTUSDT", start=start, end=end)
    trades = query_trade_vwap_window(db, symbol="APTUSDT", start=start, end=end)
    summary = build_price_summary(
        symbol="APTUSDT", ticker_stats=ticker, price_bars_by_tf={"1m": prices}, trade_stats=trades
    )
    assert Decimal(summary["high_price"]) < Decimal("10")
    assert summary["vwap"] is not None and summary["vwap"] < 10

    integ = check_market_context_integrity(
        price_bars={"1m": prices},
        tradeflow_bars={"1m": tf},
        timelines={"1m": timeline},
        stats={"price_bars_1m": 1, "timeline_rows_1m": 1, "max_bar_range_pct": 20},
        price_summary=summary,
    )
    assert integ["ok"] is True


def test_vwap_float_tolerance_at_min_max_boundary() -> None:
    ok_hi, eps = vwap_within_trade_price_bounds(
        vwap=0.6213000000000001, min_price=0.6213, max_price=0.6213
    )
    assert ok_hi is True
    assert eps == pytest.approx(1e-12)
    ok_lo, _ = vwap_within_trade_price_bounds(
        vwap=0.6212999999999999, min_price=0.6213, max_price=0.6213
    )
    assert ok_lo is True


def test_vwap_within_range_ok() -> None:
    ok, _ = vwap_within_trade_price_bounds(vwap=0.62135, min_price=0.6213, max_price=0.6214)
    assert ok is True


def test_vwap_1e13_outside_still_within_epsilon() -> None:
    # 1e-13 beyond bound with abs eps 1e-12 → still OK
    ok, eps = vwap_within_trade_price_bounds(
        vwap=0.6213 + 1e-13, min_price=0.6213, max_price=0.6213
    )
    assert ok is True
    assert 1e-13 < eps


def test_vwap_1e6_outside_fails() -> None:
    ok, _ = vwap_within_trade_price_bounds(
        vwap=0.6213 + 1e-6, min_price=0.6213, max_price=0.6213
    )
    assert ok is False


def test_vwap_relative_epsilon_large_price() -> None:
    # scale ~ 65000 → relative eps = 1e-12 * 65000 = 6.5e-8
    lo = 65000.0
    eps = vwap_bounds_epsilon(vwap=lo, min_price=lo, max_price=lo)
    assert eps == pytest.approx(6.5e-8)
    ok, _ = vwap_within_trade_price_bounds(vwap=lo + 1e-8, min_price=lo, max_price=lo)
    assert ok is True
    ok_bad, _ = vwap_within_trade_price_bounds(vwap=lo + 1e-6, min_price=lo, max_price=lo)
    assert ok_bad is False


def test_vwap_absolute_epsilon_small_price() -> None:
    # scale floored at 1.0 → abs eps 1e-12 dominates
    lo = 1e-9
    eps = vwap_bounds_epsilon(vwap=lo, min_price=lo, max_price=lo)
    assert eps == pytest.approx(1e-12)
    ok, _ = vwap_within_trade_price_bounds(vwap=lo + 5e-13, min_price=lo, max_price=lo)
    assert ok is True


def test_vwap_nan_inf_fail() -> None:
    assert vwap_within_trade_price_bounds(vwap=float("nan"), min_price=1.0, max_price=2.0)[0] is False
    assert vwap_within_trade_price_bounds(vwap=float("inf"), min_price=1.0, max_price=2.0)[0] is False
    assert vwap_within_trade_price_bounds(vwap=1.5, min_price=float("nan"), max_price=2.0)[0] is False


def test_vwap_integrity_error_message_includes_context() -> None:
    bs = TS0.isoformat()
    bad_tf = {
        "symbol": "APTUSDT",
        "bucket_start": bs,
        "bucket_end": bs,
        "trade_count": 1,
        "buy_trade_count": 1,
        "sell_trade_count": 0,
        "total_quantity": "1",
        "buy_quantity": "1",
        "sell_quantity": "0",
        "total_notional": "1",
        "buy_notional": "1",
        "sell_notional": "0",
        "delta_notional": "1",
        "buy_share": 1.0,
        "sell_share": 0.0,
        "vwap": 0.6213 + 1e-6,
        "min_trade_price": "0.6213",
        "max_trade_price": "0.6213",
    }
    # minimal valid price bar so other checks don't dominate
    price = enrich_price_bar(
        {
            "symbol": "APTUSDT",
            "bucket_start": bs,
            "bucket_end": bs,
            "sample_count": 1,
            "open_price": Decimal("0.6213"),
            "high_price": Decimal("0.6214"),
            "low_price": Decimal("0.6212"),
            "close_price": Decimal("0.6213"),
        }
    )
    result = check_market_context_integrity(
        price_bars={"1m": [price]},
        tradeflow_bars={"1m": [bad_tf]},
        timelines={"1m": []},
        stats={"price_bars_1m": 1, "timeline_rows_1m": 0},
    )
    assert result["ok"] is False
    msg = " ".join(result["errors"])
    assert "vwap" in msg
    assert "1m" in msg
    assert bs in msg
    assert "min_trade_price" in msg
    assert "max_trade_price" in msg
    assert "epsilon=" in msg

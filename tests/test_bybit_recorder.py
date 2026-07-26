"""Unit tests for Bybit recorder parsers and sampling (no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orderbook_analyse.bybit_recorder import (
    OrderbookSequenceState,
    SequenceError,
    TickerState,
    parse_liquidation_rows,
    parse_orderbook_rows,
    parse_public_trade_rows,
)
from orderbook_analyse.config import Settings, redact_settings


RECEIVED = datetime(2026, 7, 26, 12, 0, 0, 123456, tzinfo=timezone.utc)


def test_orderbook_snapshot_rows() -> None:
    state = OrderbookSequenceState()
    msg = {
        "topic": "orderbook.200.APTUSDT",
        "type": "snapshot",
        "ts": 1_672_304_484_978,
        "data": {
            "s": "APTUSDT",
            "b": [["5.10", "100"], ["5.09", "50"]],
            "a": [["5.11", "80"]],
            "u": 100,
            "seq": 1000,
        },
    }
    rows = parse_orderbook_rows(msg, received_ts=RECEIVED, seq_state=state)
    assert len(rows) == 3
    assert rows[0][3] == "bid"
    assert rows[0][4] == Decimal("5.10")
    assert rows[0][5] == Decimal("100")
    assert rows[0][6] == "snapshot"
    assert rows[0][7] == 100
    assert rows[0][8] == 1000
    assert rows[0][9] == 0
    assert rows[2][3] == "ask"
    assert rows[2][9] == 0
    assert state.has_snapshot
    assert state.last_update_id == 100


def test_orderbook_delta_including_qty_zero() -> None:
    state = OrderbookSequenceState()
    parse_orderbook_rows(
        {
            "type": "snapshot",
            "ts": 1,
            "data": {"s": "APTUSDT", "b": [["5.10", "1"]], "a": [], "u": 10, "seq": 50},
        },
        received_ts=RECEIVED,
        seq_state=state,
    )
    rows = parse_orderbook_rows(
        {
            "type": "delta",
            "ts": 2,
            "data": {
                "s": "APTUSDT",
                "b": [["5.10", "0"]],
                "a": [["5.12", "2"]],
                "u": 11,
                "seq": 55,
            },
        },
        received_ts=RECEIVED,
        seq_state=state,
    )
    assert len(rows) == 2
    assert rows[0][5] == Decimal("0")
    assert rows[0][6] == "delta"
    assert state.last_update_id == 11
    assert state.last_seq == 55


def test_sequence_gap_and_reset_on_snapshot() -> None:
    state = OrderbookSequenceState()
    parse_orderbook_rows(
        {
            "type": "snapshot",
            "ts": 1,
            "data": {"s": "APTUSDT", "b": [], "a": [], "u": 10, "seq": 100},
        },
        received_ts=RECEIVED,
        seq_state=state,
    )
    with pytest.raises(SequenceError, match="update_id gap"):
        parse_orderbook_rows(
            {
                "type": "delta",
                "ts": 2,
                "data": {"s": "APTUSDT", "b": [], "a": [], "u": 12, "seq": 101},
            },
            received_ts=RECEIVED,
            seq_state=state,
        )

    # New snapshot resets continuity
    parse_orderbook_rows(
        {
            "type": "snapshot",
            "ts": 3,
            "data": {"s": "APTUSDT", "b": [], "a": [], "u": 20, "seq": 200},
        },
        received_ts=RECEIVED,
        seq_state=state,
    )
    parse_orderbook_rows(
        {
            "type": "delta",
            "ts": 4,
            "data": {"s": "APTUSDT", "b": [], "a": [], "u": 21, "seq": 201},
        },
        received_ts=RECEIVED,
        seq_state=state,
    )
    assert state.last_update_id == 21


def test_delta_before_snapshot_is_error() -> None:
    state = OrderbookSequenceState()
    with pytest.raises(SequenceError, match="before snapshot"):
        parse_orderbook_rows(
            {
                "type": "delta",
                "ts": 1,
                "data": {"s": "APTUSDT", "b": [], "a": [], "u": 1, "seq": 1},
            },
            received_ts=RECEIVED,
            seq_state=state,
        )


def test_public_trades_multiple() -> None:
    msg = {
        "topic": "publicTrade.APTUSDT",
        "type": "snapshot",
        "ts": 100,
        "data": [
            {
                "T": 1000,
                "s": "APTUSDT",
                "S": "Buy",
                "v": "1.5",
                "p": "5.10",
                "L": "PlusTick",
                "i": "a",
                "BT": False,
            },
            {
                "T": 1001,
                "s": "APTUSDT",
                "S": "Sell",
                "v": "0.2",
                "p": "5.09",
                "i": "b",
                "RPI": True,
            },
        ],
    }
    rows = parse_public_trade_rows(msg, received_ts=RECEIVED)
    assert len(rows) == 2
    assert rows[0][4] == "Buy"
    assert rows[0][7] == Decimal("5.10") * Decimal("1.5")
    assert rows[0][8] == "PlusTick"
    assert rows[0][9] == 0
    assert rows[1][4] == "Sell"
    assert rows[1][10] == 1
    assert rows[1][8] == ""


def test_ticker_partial_merge_and_sampling() -> None:
    state = TickerState()
    ts1 = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
    state.merge({"symbol": "APTUSDT", "lastPrice": "5.1", "openInterest": "100"}, ts1)
    assert state.ready
    assert state.last_price == Decimal("5.1")
    assert state.open_interest == Decimal("100")
    assert state.mark_price is None

    sample = state.maybe_sample(received_ts=RECEIVED, interval_sec=1.0)
    assert sample is not None
    assert sample[2] == "APTUSDT"
    assert sample[3] == Decimal("5.1")
    assert sample[4] is None  # mark_price never seen → NULL

    # Within interval → no second sample
    state.merge({"markPrice": "5.2"}, ts1)
    assert state.maybe_sample(received_ts=RECEIVED, interval_sec=60.0) is None
    assert state.mark_price == Decimal("5.2")

    forced = state.maybe_sample(received_ts=RECEIVED, interval_sec=60.0, force=True)
    assert forced is not None
    assert forced[4] == Decimal("5.2")


def test_ticker_not_ready_without_ts_or_symbol() -> None:
    state = TickerState()
    state.merge({"lastPrice": "1"}, None)
    assert not state.ready
    assert state.maybe_sample(received_ts=RECEIVED, interval_sec=0, force=True) is None


def test_liquidation_rows() -> None:
    msg = {
        "topic": "allLiquidation.APTUSDT",
        "type": "snapshot",
        "ts": 1,
        "data": [
            {"T": 10, "s": "APTUSDT", "S": "Sell", "v": "200", "p": "5.00"},
            {"T": 11, "s": "APTUSDT", "S": "Buy", "v": "50", "p": "5.20"},
        ],
    }
    rows = parse_liquidation_rows(msg, received_ts=RECEIVED)
    assert len(rows) == 2
    assert rows[0][3] == "Sell"
    assert rows[0][6] == Decimal("5.00") * Decimal("200")
    assert rows[1][3] == "Buy"


def test_liquidation_missing_side_not_coerced_to_buy(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    msg = {
        "topic": "allLiquidation.APTUSDT",
        "type": "snapshot",
        "ts": 1,
        "data": [
            {"T": 10, "s": "APTUSDT", "v": "200", "p": "5.00"},  # missing S
            {"T": 11, "s": "APTUSDT", "S": "Sell", "v": "50", "p": "5.20"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        rows = parse_liquidation_rows(msg, received_ts=RECEIVED)
    assert len(rows) == 1
    assert rows[0][3] == "Sell"
    assert any("Invalid liquidation side" in r.message for r in caplog.records)


def test_liquidation_invalid_side_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    msg = {
        "topic": "allLiquidation.APTUSDT",
        "type": "snapshot",
        "ts": 1,
        "data": [
            {"T": 10, "s": "APTUSDT", "S": "Long", "v": "200", "p": "5.00"},
            {"T": 11, "s": "APTUSDT", "S": "Buy", "v": "50", "p": "5.20"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        rows = parse_liquidation_rows(msg, received_ts=RECEIVED)
    assert len(rows) == 1
    assert rows[0][3] == "Buy"
    assert any("Invalid liquidation side" in r.message for r in caplog.records)


def test_redact_settings_hides_password() -> None:
    settings = Settings(
        bybit_ws_url="wss://example",
        symbol="APTUSDT",
        orderbook_depth=200,
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="orderbook_analysis",
        clickhouse_user="user",
        clickhouse_password="SUPER_SECRET",
    )
    public = redact_settings(settings)
    assert public["clickhouse_password"] == "***"
    assert "SUPER_SECRET" not in str(public)

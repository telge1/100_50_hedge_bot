from datetime import datetime, timezone
from decimal import Decimal

import pytest

from research.btc_doge_research.contracts import sanitize_json
from research.btc_doge_research.phase2_contracts import (
    ORDERING_AMBIGUOUS_BUCKETS,
    PILOT_DAY,
    producer_lineage_rows,
)
from research.btc_doge_research.phase2_ddl import statements
from research.btc_doge_research.phase2_transform import (
    aggregate_trade_buckets,
    assert_no_carry_after_terminal,
    compact_ob_state,
)


UTC = timezone.utc


def test_producer_lineage_and_queue_full_incomplete() -> None:
    rows = producer_lineage_rows()
    live = [row for row in rows if row["producer_type"] == "LIVE_WEBSOCKET_COLLECTOR"]
    assert len(live) == 2
    assert all(row["source_semantics"] == "RECEIVE_TIME_ASOF" for row in live)
    assert all(row["terminal_reason"] == "queue_full" for row in live)
    assert all(row["coverage_complete"] is False for row in live)
    assert PILOT_DAY == "2026-08-26"


def test_ordering_ambiguous_is_not_source_gap() -> None:
    assert len(ORDERING_AMBIGUOUS_BUCKETS) == 4
    assert all("SOURCE_GAP" not in row for row in ORDERING_AMBIGUOUS_BUCKETS)


def test_no_carried_forward_over_queue_full() -> None:
    terminal = datetime(2026, 8, 28, 16, 26, 23, tzinfo=UTC)
    with pytest.raises(ValueError):
        assert_no_carry_after_terminal(
            datetime(2026, 8, 28, 16, 26, 24, tzinfo=UTC), terminal, True
        )


def test_public_trade_bucket_conservation_and_rollups() -> None:
    trades = [
        {"symbol": "BTCUSDT", "event_time": datetime(2026, 8, 26, 0, 0, 0, 10_000, tzinfo=UTC), "trade_id": "1", "side": "Buy", "size": "2", "notional": "200"},
        {"symbol": "BTCUSDT", "event_time": datetime(2026, 8, 26, 0, 0, 0, 210_000, tzinfo=UTC), "trade_id": "2", "side": "Sell", "size": "3", "notional": "303"},
    ]
    rows100 = aggregate_trade_buckets(trades, 100)
    rows500 = aggregate_trade_buckets(trades, 500)
    rows1s = aggregate_trade_buckets(trades, 1000)
    assert sum(row["deduplicated_trade_count"] for row in rows100) == 2
    assert sum(row["buy_base_volume"] for row in rows500) == Decimal("2")
    assert sum(row["sell_base_volume"] for row in rows1s) == Decimal("3")
    assert rows1s[0]["taker_delta_quote_notional"] == Decimal("-103")


def test_ob_integer_ticks_and_array_order() -> None:
    book = compact_ob_state(
        "BTCUSDT",
        ((Decimal("100.2"), Decimal("1")), (Decimal("100.1"), Decimal("2"))),
        ((Decimal("100.3"), Decimal("3")), (Decimal("100.4"), Decimal("4"))),
    )
    assert book["bid_price_ticks"] == [1002, 1001]
    assert book["ask_price_ticks"] == [1003, 1004]
    assert book["bid_quantities"] == [Decimal("1"), Decimal("2")]


def test_phase2_ddl_is_idempotent_and_target_isolated() -> None:
    ddl = statements()
    assert ddl == statements()
    assert all("CREATE TABLE IF NOT EXISTS btc_doge_research." in sql for sql in ddl)
    assert all("DROP " not in sql and "TRUNCATE " not in sql for sql in ddl)


def test_phase2_json_has_no_nonfinite_values() -> None:
    assert sanitize_json({"nan": float("nan"), "inf": float("inf")}) == {
        "nan": None,
        "inf": None,
    }

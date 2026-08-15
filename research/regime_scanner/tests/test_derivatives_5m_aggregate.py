"""Unit tests for 5m derivatives aggregation and causality."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.regime_scanner.derivatives.aggregate_5m import (
    SourceMinuteRow,
    aggregate_rows,
    aggregate_symbol_minutes,
    floor_5m,
    parse_source_row,
    parse_utc,
)
from research.regime_scanner.derivatives.config import SEQUENCE_GAP_SECONDS
from research.regime_scanner.derivatives.importer import validate_symbols
from research.regime_scanner.derivatives.store_memory import InMemoryDerivativeStore


def _ts(s: str) -> datetime:
    return parse_utc(s)


def _row(
    ts: str,
    symbol: str = "BTCUSDT",
    *,
    oi: float = 100.0,
    oi_usd: float = 1000.0,
    long_liq: float = 0.0,
    short_liq: float = 0.0,
    buy: float = 10.0,
    sell: float = 5.0,
    spread: float = 0.1,
) -> SourceMinuteRow:
    return SourceMinuteRow(
        timestamp=_ts(ts),
        symbol=symbol,
        open_interest=oi,
        open_interest_value=oi_usd,
        long_liq_usd=long_liq,
        short_liq_usd=short_liq,
        total_liq_usd=long_liq + short_liq,
        buy_volume=buy,
        sell_volume=sell,
        spread=spread,
    )


def test_floor_5m_boundaries():
    assert floor_5m(_ts("2026-03-15T00:00:00Z")) == _ts("2026-03-15T00:00:00Z")
    assert floor_5m(_ts("2026-03-15T00:04:00Z")) == _ts("2026-03-15T00:00:00Z")
    assert floor_5m(_ts("2026-03-15T00:05:00Z")) == _ts("2026-03-15T00:05:00Z")
    assert floor_5m(_ts("2026-03-15T00:09:00Z")) == _ts("2026-03-15T00:05:00Z")


def test_row_at_bucket_end_goes_to_next_bucket():
    # Minutes 00-04 → bucket 00; minute 05 → bucket 05
    rows = [
        _row("2026-03-15T00:00:00Z", oi=1),
        _row("2026-03-15T00:01:00Z", oi=2),
        _row("2026-03-15T00:02:00Z", oi=3),
        _row("2026-03-15T00:03:00Z", oi=4),
        _row("2026-03-15T00:04:00Z", oi=5),
        _row("2026-03-15T00:05:00Z", oi=99),
    ]
    res = aggregate_symbol_minutes(rows, import_version="derivatives_5m_v1")
    b0 = [b for b in res.buckets if b.bucket_start == _ts("2026-03-15T00:00:00Z")][0]
    assert b0.open_interest == 5
    assert b0.source_row_count == 5
    assert b0.data_available is True
    # Incomplete next bucket with single minute
    b5 = [b for b in res.buckets if b.bucket_start == _ts("2026-03-15T00:05:00Z")][0]
    assert b5.open_interest == 99
    assert b5.data_available is False


def test_oi_last_snapshot_not_mean():
    rows = [
        _row("2026-03-15T00:00:00Z", oi=10),
        _row("2026-03-15T00:01:00Z", oi=20),
        _row("2026-03-15T00:02:00Z", oi=30),
        _row("2026-03-15T00:03:00Z", oi=40),
        _row("2026-03-15T00:04:00Z", oi=50),
    ]
    res = aggregate_symbol_minutes(rows, import_version="v1")
    assert res.buckets[0].open_interest == 50
    assert res.buckets[0].open_interest != 30  # not mean


def test_liquidations_summed_and_true_zero():
    rows = [
        _row("2026-03-15T00:00:00Z", long_liq=0, short_liq=0),
        _row("2026-03-15T00:01:00Z", long_liq=0, short_liq=0),
        _row("2026-03-15T00:02:00Z", long_liq=100, short_liq=0),
        _row("2026-03-15T00:03:00Z", long_liq=0, short_liq=50),
        _row("2026-03-15T00:04:00Z", long_liq=0, short_liq=0),
    ]
    res = aggregate_symbol_minutes(rows, import_version="v1")
    b = res.buckets[0]
    assert b.long_liquidation_usd == 100
    assert b.short_liquidation_usd == 50
    assert b.total_liquidation_usd == 150
    assert b.data_available is True


def test_missing_minutes_not_invented_as_zero_liq_bucket():
    # Only 3 minutes → incomplete bucket emitted with data_available=false
    rows = [
        _row("2026-03-15T00:00:00Z", long_liq=10),
        _row("2026-03-15T00:01:00Z", long_liq=0),
        _row("2026-03-15T00:02:00Z", long_liq=0),
    ]
    res = aggregate_symbol_minutes(rows, import_version="v1")
    assert len(res.buckets) == 1
    assert res.buckets[0].data_available is False
    assert res.buckets[0].source_row_count == 3
    # No second bucket invented for 00:05
    assert all(b.bucket_start == _ts("2026-03-15T00:00:00Z") for b in res.buckets)


def test_buy_sell_delta_ratio():
    rows = [
        _row("2026-03-15T00:00:00Z", buy=10, sell=5, spread=0.1),
        _row("2026-03-15T00:01:00Z", buy=20, sell=5, spread=0.3),
        _row("2026-03-15T00:02:00Z", buy=0, sell=0, spread=0.2),
        _row("2026-03-15T00:03:00Z", buy=0, sell=10, spread=0.4),
        _row("2026-03-15T00:04:00Z", buy=5, sell=5, spread=0.5),
    ]
    b = aggregate_symbol_minutes(rows, import_version="v1").buckets[0]
    assert b.buy_volume == 35
    assert b.sell_volume == 25
    assert b.delta == 10
    assert b.total_volume == 60
    assert abs(b.delta_ratio - (10 / 60)) < 1e-12
    assert abs(b.spread_mean - 0.3) < 1e-12
    assert b.spread_max == 0.5


def test_large_gap_new_sequence():
    base = [
        _row("2026-03-15T00:00:00Z"),
        _row("2026-03-15T00:01:00Z"),
        _row("2026-03-15T00:02:00Z"),
        _row("2026-03-15T00:03:00Z"),
        _row("2026-03-15T00:04:00Z"),
    ]
    # Resume on a clean 5m boundary after >= SEQUENCE_GAP_SECONDS
    later = [
        _row("2026-03-15T02:00:00Z", oi=200),
        _row("2026-03-15T02:01:00Z", oi=201),
        _row("2026-03-15T02:02:00Z", oi=202),
        _row("2026-03-15T02:03:00Z", oi=203),
        _row("2026-03-15T02:04:00Z", oi=204),
    ]
    res = aggregate_symbol_minutes(base + later, import_version="v1")
    assert len(res.buckets) == 2
    assert res.buckets[0].sequence_id == 1
    assert res.buckets[1].sequence_id == 2
    assert res.buckets[1].gap_before_seconds is not None
    assert res.buckets[1].gap_before_seconds >= SEQUENCE_GAP_SECONDS
    assert res.buckets[0].open_interest == 100.0
    assert res.buckets[1].open_interest == 204.0


def test_future_row_cannot_change_earlier_bucket():
    rows = [
        _row("2026-03-15T00:00:00Z", oi=1),
        _row("2026-03-15T00:01:00Z", oi=2),
        _row("2026-03-15T00:02:00Z", oi=3),
        _row("2026-03-15T00:03:00Z", oi=4),
        _row("2026-03-15T00:04:00Z", oi=5),
        _row("2026-03-15T00:10:00Z", oi=999),  # later bucket
    ]
    res = aggregate_symbol_minutes(rows, import_version="v1")
    b0 = [b for b in res.buckets if b.bucket_start == _ts("2026-03-15T00:00:00Z")][0]
    assert b0.open_interest == 5


def test_naive_timestamp_rejected_by_parse_utc():
    with pytest.raises(ValueError):
        parse_utc("2026-03-15T00:00:00")


def test_source_mysql_naive_treated_as_utc():
    out = parse_source_row(
        {
            "timestamp": "2026-03-15 00:00:00",
            "symbol": "btcusdt",
            "open_interest": 1,
            "open_interest_value": 2,
            "long_liq_usd": 0,
            "short_liq_usd": 0,
            "total_liq_usd": 0,
            "buy_volume": 1,
            "sell_volume": 1,
            "spread": 0.1,
        }
    )
    assert isinstance(out, SourceMinuteRow)
    assert out.timestamp.tzinfo == timezone.utc


def test_idempotent_memory_upsert():
    rows = [_row(f"2026-03-15T00:0{i}:00Z", oi=float(i)) for i in range(5)]
    # fix formatting for 00-04
    rows = [
        _row("2026-03-15T00:00:00Z", oi=1),
        _row("2026-03-15T00:01:00Z", oi=2),
        _row("2026-03-15T00:02:00Z", oi=3),
        _row("2026-03-15T00:03:00Z", oi=4),
        _row("2026-03-15T00:04:00Z", oi=5),
    ]
    buckets = aggregate_symbol_minutes(rows, import_version="derivatives_5m_v1").buckets
    store = InMemoryDerivativeStore()
    s1 = store.upsert_buckets(buckets)
    s2 = store.upsert_buckets(buckets)
    assert s1.inserted == 1
    assert s2.inserted == 0
    assert s2.unchanged == 1
    assert s2.updated == 0


def test_validate_symbols_flags_ena_arb_op():
    accepted, unavailable = validate_symbols(["BTCUSDT", "ENAUSDT", "ARBUSDT"])
    assert accepted == ["BTCUSDT"]
    assert set(unavailable) == {"ENAUSDT", "ARBUSDT"}


def test_aggregate_rows_raw_dict_path():
    raw = [
        {
            "timestamp": "2026-03-15T00:00:00Z",
            "symbol": "ETHUSDT",
            "open_interest": 10,
            "open_interest_value": 20,
            "long_liq_usd": 1,
            "short_liq_usd": 2,
            "total_liq_usd": 3,
            "buy_volume": 4,
            "sell_volume": 1,
            "spread": 0.01,
        }
        for _ in range(5)
    ]
    # make distinct minutes
    for i, r in enumerate(raw):
        r["timestamp"] = f"2026-03-15T00:0{i}:00Z"
        r["open_interest"] = 10 + i
    res = aggregate_rows(raw, import_version="v1")
    assert res.buckets[0].open_interest == 14
    assert res.buckets[0].delta == 15  # (4-1)*5

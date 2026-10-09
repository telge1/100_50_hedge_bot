"""Unit tests for incremental 5m materialization (no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data.models import Candle
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CandleRevisionConflictError,
    CanonicalCandleSession,
    candles_equal,
)


def _make_1m(unix: int, price: float = 1.0) -> Candle:
    return Candle(
        timestamp=datetime.fromtimestamp(unix, tz=timezone.utc),
        open=price,
        high=price + 0.01,
        low=price - 0.01,
        close=price,
        volume=100.0,
        symbol="DOGEUSDT",
        timeframe="1m",
    )


def _contiguous_1m(start_unix: int, count: int) -> list[Candle]:
    return [_make_1m(start_unix + i * 60) for i in range(count)]


def test_incremental_matches_full_trp_on_synthetic_buffer():
    trp_start = int(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    rows = _contiguous_1m(trp_start, 60)
    inc = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=True)
    full = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=False)
    inc.ingest_1m_closed(rows[:25])
    inc.ingest_1m_closed(rows[25:])
    full.ingest_1m_closed(rows)
    agg_lo = trp_start + 5 * 300
    agg_hi = trp_start + 11 * 300
    one_m = inc._slice_1m(trp_start, trp_start + 59 * 60)
    ref = full._aggregate_5m_full_path(one_m, agg_lo, agg_hi)
    got = inc._slice_5m_materialized(agg_lo, agg_hi)
    assert candles_equal(ref, got)


def test_bucket_open_aligns_to_utc_5m_floor():
    session = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=True)
    t = int(datetime(2026, 3, 15, 12, 7, tzinfo=timezone.utc).timestamp())
    bo = session._bucket_open_unix(t)
    assert bo == int(datetime(2026, 3, 15, 12, 5, tzinfo=timezone.utc).timestamp())


def test_backward_decision_close_triggers_full_rebuild():
    session = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=True)
    t0 = int(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    session.ingest_1m_closed(_contiguous_1m(t0, 30))
    session._last_to_unix = t0 + 29 * 60
    session._last_src_start_unix = t0
    session._maybe_rebuild_incremental_5m(t0, t0 + 10 * 60)
    assert session.stats.full_5m_rebuilds >= 1


def test_revision_conflict_fail_closed():
    session = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=True)
    u = int(datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    session.ingest_1m_closed([_make_1m(u, 1.0)])
    with pytest.raises(CandleRevisionConflictError):
        session.ingest_1m_closed([_make_1m(u, 2.0)])

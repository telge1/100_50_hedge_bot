"""Phase 11K — revision snapshot and bucket invalidation (in-memory)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data.models import Candle
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CandleRevisionConflictError,
    CanonicalCandleSession,
)
from research_charts.live_micro_lld_v2_ch_test.phase11k_revision_safety import (
    ImmutableInputSnapshot,
    detect_ohlcv_change,
    invalidate_buckets,
    ohlcv_fingerprint,
)


def _c(unix: int, price: float = 1.0) -> Candle:
    return Candle(
        timestamp=datetime.fromtimestamp(unix, tz=timezone.utc),
        open=price,
        high=price + 0.01,
        low=price - 0.01,
        close=price,
        volume=1.0,
        symbol="DOGEUSDT",
        timeframe="1m",
    )


def test_immutable_snapshot_detects_revision():
    candles = [_c(1000), _c(1060)]
    snap = ImmutableInputSnapshot.from_candles("run-a", candles)
    assert snap.assert_compatible(candles) == []
    revised = [_c(1000, 2.0), _c(1060)]
    errs = snap.assert_compatible(revised)
    assert any("revision:1000" in e for e in errs)


def test_snapshot_hash_stable():
    a = ImmutableInputSnapshot.from_candles("x", [_c(60), _c(120)])
    b = ImmutableInputSnapshot.from_candles("y", [_c(60), _c(120)])
    assert a.content_hash() == b.content_hash()


def test_invalidate_buckets_removes_affected():
    mat = {300: object(), 600: object(), 900: object()}
    invalidate_buckets(mat, 300 + 120)
    assert 300 not in mat
    assert 600 in mat


def test_session_merge_conflict_fail_closed():
    s = CanonicalCandleSession("DOGEUSDT", "5m", use_incremental_5m=True)
    s.ingest_1m_closed([_c(1000)])
    with pytest.raises(CandleRevisionConflictError):
        s.ingest_1m_closed([_c(1000, 9.0)])


def test_detect_ohlcv_change():
    assert not detect_ohlcv_change(_c(1), _c(1))
    assert detect_ohlcv_change(_c(1, 1.0), _c(1, 2.0))

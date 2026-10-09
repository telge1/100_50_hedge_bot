"""Phase 11D — canonical candle session parity (requires ClickHouse DOGE 1m)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CanonicalCandleSession,
    candles_equal,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    enumerate_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window

SYMBOL = "DOGEUSDT"
TF = "5m"
START = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)


@pytest.mark.integration
def test_canonical_session_matches_reference_loader():
    closes = enumerate_decision_closes(START, 30, TF)
    session = CanonicalCandleSession(SYMBOL, TF)
    for dc in closes:
        hf = assert_rolling_coverage(dc, months=4)
        ref, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, 10)
        cand, _ = session.load_for_window(hf, dc, 10)
        assert candles_equal(ref, cand)


@pytest.mark.integration
def test_cached_build_active_state_matches_reference():
    closes = enumerate_decision_closes(START, 15, TF)
    session = CanonicalCandleSession(SYMBOL, TF)
    for dc in closes:
        hf = assert_rolling_coverage(dc, months=4)
        ref_snap, _, _ = build_active_state_at_decision_close(
            SYMBOL, TF, hf, dc, pool_detail=10, history_policy="ROLLING_4M"
        )
        cand_snap, _, _ = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hf,
            dc,
            pool_detail=10,
            history_policy="ROLLING_4M",
            candle_session=session,
        )
        assert ref_snap.engine_input_hash == cand_snap.engine_input_hash
        assert ref_snap.active_state_fingerprint == cand_snap.active_state_fingerprint

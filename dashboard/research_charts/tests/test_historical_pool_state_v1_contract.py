"""Contract: future backtester and chart must consume identical HistoricalPoolStateAtClose."""

from __future__ import annotations

import sys
from pathlib import Path

_DASH = Path(__file__).resolve().parents[1]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_pools
from research_charts.historical_pool_state_v1 import (
    PIPELINE_LINE_ID,
    load_historical_pool_state_at_close,
    load_window_pool_state,
    remaining_fingerprint,
)

REF_CLOSES = (
    "2026-08-01T00:00:00Z",
    "2026-08-01T02:30:00Z",
    "2026-08-01T04:45:00Z",
)
PHASE5C_FP = {
    "2026-08-01T00:00:00Z": "d845b011195d1ee11930db87ca1e7790fe75873647cbbf4e2866444229a168fc",
    "2026-08-01T04:45:00Z": "6b16ebc667047d0f7607f6797fb87ec402affc48bf179e7be1a950fa5e3053c4",
}


def test_single_close_matches_window_slice():
    win = load_window_pool_state(
        "2026-08-01T00:00:00Z",
        "2026-08-01T04:45:00Z",
        "2026-08-01T02:30:00Z",
    )
    for iso in REF_CLOSES:
        single = load_historical_pool_state_at_close(iso)
        slice_row = next(r for r in win["per_close"] if r["decision_close"] == iso)
        assert slice_row["snapshot_id"] == single.snapshot_id
        assert slice_row["remaining_fingerprint"] == single.remaining_fingerprint
        assert slice_row["active_state_fingerprint"] == single.active_state_fingerprint


def test_remaining_full_count_at_pilot_end():
    st = load_historical_pool_state_at_close("2026-08-01T04:45:00Z")
    assert len(st.remaining_segments) == 6423
    assert st.remaining_fingerprint == PHASE5C_FP["2026-08-01T04:45:00Z"]


def test_api_uses_reader_remaining_full_metadata():
    body = chart_historical_lmv2_pools(
        "DOGEUSDT",
        "2026-08-01T00:00:00Z",
        "2026-08-01T04:45:00Z",
        include_active=False,
        include_remaining=True,
        as_of_close="2026-08-01T04:45:00Z",
        remaining_limit=6500,
    )
    assert body["remaining_segment_count_full"] == 6423
    assert body["remaining_segment_count"] == 6423
    assert body["remaining_truncated"] is False
    assert body["pipeline_line_id"] == PIPELINE_LINE_ID


def test_phase5c_fingerprint_spot():
    for iso, expected in PHASE5C_FP.items():
        st = load_historical_pool_state_at_close(iso)
        assert st.remaining_fingerprint == expected

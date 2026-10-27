"""Unit tests for BT-D71 chronology comparison helpers."""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from compare_chronology_v1 import build_close_comparison_rows, pending_difference_rows  # noqa: E402


def test_pending_difference_detects_authorize_mismatch():
    bars = ["2026-01-01T00:00:00Z"]
    replay_v1 = {
        "gate_decisions": [
            {
                "decision_close_utc": bars[0],
                "allow_new_short_intent": True,
                "reasons": [],
                "pause_state": "READY",
                "ema_decision": "SHORT_OK",
            }
        ],
        "baseline_pool_candidates": [{"decision_close_utc": bars[0], "pool_key": "pk1"}],
        "pending_lifecycle": [
            {"event": "CREATE", "decision_close_utc": bars[0], "order_id": "a", "pool_key": "pk1"}
        ],
        "modeled_positions": [],
    }
    replay_v3 = {
        "gate_decisions": [
            {
                "decision_close_utc": bars[0],
                "allow_new_short_intent": False,
                "reasons": ["EMA_UNCERTAIN"],
                "pause_state": "READY",
                "ema_decision": "UNCERTAIN",
            }
        ],
        "baseline_pool_candidates": [{"decision_close_utc": bars[0], "pool_key": "pk1"}],
        "pending_lifecycle": [],
        "modeled_positions": [],
    }
    rows = build_close_comparison_rows(
        pilot_id="t",
        pause_mode="RESEARCH_READY",
        bars=bars,
        replay_v1=replay_v1,
        replay_v3=replay_v3,
        ema_rows_v1={bars[0]: {"ema_structure_decision": "SHORT_OK"}},
        ema_rows_v3={bars[0]: {"ema_structure_decision": "UNCERTAIN"}},
    )
    assert rows[0]["pool_conditional_differs"] is True
    diffs = pending_difference_rows(rows)
    assert len(diffs) == 1

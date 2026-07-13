"""Tests for trend_state_audit helpers (no live/pipeline mutation)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pandas as pd

from research.regime_scanner import trend_state_audit as audit
from research.regime_scanner.trend_state_machine import TrendStateSnapshot


def test_audit_defaults_window_not_trading_rule() -> None:
    src = inspect.getsource(audit.run_trend_state_audit)
    assert "timestamp >=" not in src
    # Window constants exist for the research audit runner only
    assert "2026-03-05" in audit.DEFAULT_AUDIT_START
    assert "2026-03-10" in audit.DEFAULT_AUDIT_END


def test_extract_transitions() -> None:
    snaps = [
        TrendStateSnapshot(
            current_state="neutral",
            previous_state=None,
            entered_at="t0",
            age_5m_bars=1,
            min_hold_remaining=0,
            state_confidence=0.1,
            active_reasons=["hold"],
            active_structure_events=[],
            bearish_score=0,
            bullish_score=0,
            weakening_score=0,
            bottoming_score=0,
            structure_5m={},
            structure_15m={},
            context_30m={},
            allow_long=True,
            allow_short=True,
            require_stricter_long_confirmation=False,
            require_stricter_short_confirmation=False,
            decision_time="2026-03-06T01:00:00+00:00",
        ),
        TrendStateSnapshot(
            current_state="bearish_warning",
            previous_state="neutral",
            entered_at="t1",
            age_5m_bars=0,
            min_hold_remaining=2,
            state_confidence=0.4,
            active_reasons=["enter:bearish_warning"],
            active_structure_events=[],
            bearish_score=1,
            bullish_score=0,
            weakening_score=0,
            bottoming_score=0,
            structure_5m={},
            structure_15m={},
            context_30m={},
            allow_long=True,
            allow_short=True,
            require_stricter_long_confirmation=True,
            require_stricter_short_confirmation=False,
            decision_time="2026-03-06T01:05:00+00:00",
        ),
    ]
    tl = audit.snapshots_to_frame(snaps)
    tr = audit.extract_transitions(tl)
    assert len(tr) == 2
    assert tr.iloc[-1]["to_state"] == "bearish_warning"


def test_summarize_answers_keys() -> None:
    tl = pd.DataFrame(
        [
            {
                "decision_time": pd.Timestamp("2026-03-06T10:00:00+00:00"),
                "current_state": "early_bearish",
                "allow_long": False,
                "allow_short": True,
                "min_hold_remaining": 0,
            }
        ]
    )
    summary = audit.summarize_audit(tl, audit.extract_transitions(tl), pd.DataFrame(), pd.DataFrame())
    assert "answers" in summary
    assert summary["answers"]["2_early_bearish"] is not None
    assert summary["answers"]["9_longs_blocked_from"] is not None


def test_default_out_dir_distinct_from_pipeline() -> None:
    assert "trend_state" in audit.DEFAULT_OUT
    assert Path(audit.DEFAULT_PIPELINE).name != Path(audit.DEFAULT_OUT).name

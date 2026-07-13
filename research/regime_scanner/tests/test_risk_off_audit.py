"""Tests for risk_off_audit helpers / confirmation window semantics."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner import risk_off_audit as audit
from research.regime_scanner.risk_off import RiskOffConfig


def test_classify_long_quality() -> None:
    assert audit.classify_long_quality({"reached_plus_025": False, "max_adverse_drop_pct": 2.0}) in {
        "weak",
        "mixed",
    }


def test_focus_setup_ids() -> None:
    assert "setup_00055" in audit.FOCUS_SETUPS
    assert "setup_00059" in audit.FOCUS_SETUPS


def test_join_risk_at_time_asof() -> None:
    tl = pd.DataFrame(
        {
            "decision_time": pd.to_datetime(
                [
                    "2026-03-06T01:30:00+00:00",
                    "2026-03-06T01:35:00+00:00",
                    "2026-03-06T01:40:00+00:00",
                ],
                utc=True,
            ),
            "risk_state": ["normal", "long_risk_elevated", "long_risk_off"],
            "risk_score_long": [1.0, 3.5, 6.0],
            "b3_state": ["neutral", "neutral", "neutral"],
            "would_block_long": [False, False, True],
            "would_block_short": [False, False, False],
            "blocking_layer": ["none", "risk_elevated_only", "risk_off"],
            "risk_entry_reason": [None, "elev", "off"],
        }
    )
    r = audit.join_risk_at_time(tl, pd.Timestamp("2026-03-06T01:35:00+00:00"))
    assert r["risk_state"] == "long_risk_elevated"
    r2 = audit.join_risk_at_time(tl, pd.Timestamp("2026-03-06T01:40:00+00:00"))
    assert r2["risk_state"] == "long_risk_off"


def test_elevated_uses_three_candles_policy() -> None:
    cfg = RiskOffConfig()
    assert cfg.confirm_candles_normal == 2
    assert cfg.confirm_candles_elevated == 3


def test_b3_and_risk_helpers() -> None:
    assert audit._is_b3_strong_bearish("strong_bearish") is True
    assert audit._is_risk_off_long("long_risk_off") is True
    assert audit._is_elevated_long("long_risk_elevated") is True


def test_confirm_decision_times_strictly_after() -> None:
    times = pd.to_datetime(
        [
            "2026-03-06T07:00:00+00:00",
            "2026-03-06T07:05:00+00:00",
            "2026-03-06T07:10:00+00:00",
            "2026-03-06T07:15:00+00:00",
        ],
        utc=True,
    )
    out = audit.confirm_decision_times(times, pd.Timestamp("2026-03-06T07:00:00+00:00"), n=3)
    assert list(out) == list(times[1:4])

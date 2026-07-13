"""Tests for LuxAlgo structure policy-gate audit (read-only)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from research.regime_scanner.luxalgo_structure_policy_gate_audit import (
    PROTECTED,
    ROOT,
    choch_then_bos,
    failed_reversal,
    gate_decisions,
)

import pandas as pd


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_protected_hashes_unchanged():
    for name, expected in PROTECTED.items():
        assert _md5(ROOT / name) == expected


def test_gate_modules_not_imported_by_policy():
    for name in ("trend_state_policy.py", "trend_state_machine.py", "trend_structure.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "luxalgo_structure_policy_gate" not in text


def test_jan13_style_recovery_not_released_under_g2_g4_g5():
    """Bullish 30m CHoCH alone under bearish S2 must not ALLOW long as macro uptrend."""
    ctx = {
        "direction": "long",
        "existing_decision": "ALLOW",
        "s2_macro_side": -1,
        "k2_local_side": 0,
        "lux_30m_swing_bias": 1,
        "lux_30m_latest_choch": "bullish",
        "lux_30m_latest_bos": None,
        "choch_followed_by_bos": False,
        "bull_choch_followed_by_bos": False,
        "bear_choch_followed_by_bos": False,
        "bull_4h_choch_followed_by_bos": False,
        "bear_4h_choch_followed_by_bos": False,
        "lux_4h_latest_choch": None,
        "lux_4h_latest_bos": None,
        "failed_reversal_flag": True,
        "g3_macro_side": -1,
    }
    g = gate_decisions(ctx)
    assert g["G2_decision"] == "BLOCK"
    assert g["G4_decision"] == "BLOCK"
    assert g["G5_decision"] == "BLOCK"
    assert g["G0_decision"] == "ALLOW"


def test_g5_allows_short_without_counter_structure():
    ctx = {
        "direction": "short",
        "existing_decision": "ALLOW",
        "s2_macro_side": -1,
        "k2_local_side": -1,
        "lux_30m_swing_bias": -1,
        "lux_30m_latest_choch": None,
        "lux_30m_latest_bos": None,
        "choch_followed_by_bos": False,
        "bull_choch_followed_by_bos": False,
        "bear_choch_followed_by_bos": False,
        "bull_4h_choch_followed_by_bos": False,
        "bear_4h_choch_followed_by_bos": False,
        "lux_4h_latest_choch": None,
        "lux_4h_latest_bos": None,
        "failed_reversal_flag": False,
        "g3_macro_side": -1,
    }
    g = gate_decisions(ctx)
    assert g["G5_decision"] == "ALLOW"


def test_g5_waits_on_30m_choch_bos_against_macro():
    ctx = {
        "direction": "short",
        "existing_decision": "ALLOW",
        "s2_macro_side": -1,
        "k2_local_side": -1,
        "lux_30m_swing_bias": 1,
        "lux_30m_latest_choch": "bullish",
        "lux_30m_latest_bos": "bullish",
        "choch_followed_by_bos": True,
        "bull_choch_followed_by_bos": True,
        "bear_choch_followed_by_bos": False,
        "bull_4h_choch_followed_by_bos": False,
        "bear_4h_choch_followed_by_bos": False,
        "lux_4h_latest_choch": "bullish",
        "lux_4h_latest_bos": None,
        "failed_reversal_flag": False,
        "g3_macro_side": -1,
    }
    g = gate_decisions(ctx)
    assert g["G5_decision"] == "WAIT"


def test_choch_then_bos_and_failed_reversal_helpers():
    t0 = pd.Timestamp("2026-01-13T14:00:00+00:00")
    t1 = pd.Timestamp("2026-01-14T00:00:00+00:00")
    t2 = pd.Timestamp("2026-01-15T03:30:00+00:00")
    events = [
        {"decision_time": t0, "direction": "bullish", "kind": "choch", "level": 1.0, "bias_after": 1},
        {"decision_time": t2, "direction": "bearish", "kind": "choch", "level": 0.9, "bias_after": -1},
    ]
    assert choch_then_bos(events, t1, "bullish")["followed"] is False
    assert failed_reversal(events, t2, "bullish") is True

    events2 = events[:1] + [
        {"decision_time": t1, "direction": "bullish", "kind": "bos", "level": 1.1, "bias_after": 1}
    ]
    assert choch_then_bos(events2, t1, "bullish")["followed"] is True

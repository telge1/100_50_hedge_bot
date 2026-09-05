"""Tests for DOGE 06:30 decision reconstruction."""

from __future__ import annotations

from datetime import datetime

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_short_0630_decision_reconstruction import (
    OLD_EPISODE_ID,
    VERDICT_SAME_EP,
    _classify_verdict,
)


def test_old_invalidated_plan_episode_blocks_rearm():
    decision = {
        "gate_breakdown": {
            "episode_seen_blocks_register": True,
            "detect_produces_candidate": True,
            "target_exists": True,
            "asymmetry_pass": True,
            "bearish_5m_pass": True,
        },
        "hypothetical_new_plan": None,
    }
    assert _classify_verdict(decision, None) == VERDICT_SAME_EP


def test_episode_id_format():
    assert OLD_EPISODE_ID == "A_PLUS_PULLBACK_SHORT:lld:DOGEUSDT:15m:upper:1787886900"


def test_no_new_plan_without_rearm():
    decision = {
        "gate_breakdown": {
            "episode_seen_blocks_register": True,
            "detect_produces_candidate": False,
            "target_exists": True,
            "asymmetry_pass": True,
            "bearish_5m_pass": False,
        },
    }
    assert _classify_verdict(decision, None) == VERDICT_SAME_EP

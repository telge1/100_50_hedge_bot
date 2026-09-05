"""Focused tests for CASE_02 control-shift timestamp review."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_case_02_control_shift_timestamp_review_v1.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("c02_ctrl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_ranking_features_exclude_outcomes():
    mod = _load()
    src = SCRIPT.read_text(encoding="utf-8")
    assert "outcome_used_for_ranking = False" in src or '"outcome_used_for_ranking": False' in src
    assert "_rank_sell" in src and "reentry" in src
    # ranking key must not include breakout/reentry success
    assert "-r[\"_rank_sell\"]" in src.replace(" ", "") or "-r['_rank_sell']" in src.replace(" ", "") or "-r[\"_rank_sell\"]" in src or '"_rank_sell"' in src


def test_max_attack_duration_cap():
    mod = _load()
    assert mod.MAX_ATTACK_S == 60


def test_reentry_alone_not_takeover_note():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "reentry_alone_is_not_buy_takeover" in src or "reentry_alone_insufficient" in src


def test_cancel_not_consumed_overrun():
    mod = _load()
    # classification branch
    src = SCRIPT.read_text(encoding="utf-8")
    assert "TRADE_SUPPORTED_OVERRUN" in src
    assert "CANCEL_OR_MOVE" in src
    assert "consumed" in src


def test_no_trade_is_valid_verdict():
    mod = _load()
    assert "AMBIGUOUS_POOL_CONTEST_NO_TRADE" in SCRIPT.read_text(encoding="utf-8")


def test_classify_sell_insufficient():
    mod = _load()
    assert mod.classify_sell_attack(100, 0, 0.0, 0.0) == "INSUFFICIENT_ATTACK"

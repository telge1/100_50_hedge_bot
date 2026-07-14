"""Tests for Phase C2A topping/bottoming root-cause audit (read-only)."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    default_trend_state_config,
    trend_state_config_c1,
)
from research.regime_scanner.trend_topping_bottoming_root_cause_audit import (
    CODE_AUDIT,
    NEUTRAL_TIMEOUT_BARS,
    TbEvidence,
    assert_safe_output_dir,
    c1_strict_config,
    diagnose_bottoming_exit,
    diagnose_topping_exit,
    state_machine_source_unchanged_for_topping_paths,
    _run_stats,
)


def test_c1_c_config_strict_and_default_still_off() -> None:
    cfg = c1_strict_config()
    assert cfg.weakening_multi_bar_mode == "strict"
    assert default_trend_state_config().weakening_multi_bar_mode == "off"
    assert trend_state_config_c1("strict").weakening_multi_bar_mode == "strict"


def test_audit_does_not_modify_state_machine_source() -> None:
    assert state_machine_source_unchanged_for_topping_paths()
    import research.regime_scanner.trend_state_machine as m

    src = inspect.getsource(m._propose_transition)
    assert "topping" in src and "bottoming" in src
    # still no topping→neutral
    topping_chunk = src[src.find('if state == "topping"') : src.find('if state == "early_bearish"')]
    assert 'return "neutral"' not in topping_chunk


def test_code_audit_documents_no_neutral_path() -> None:
    ids = {c["id"] for c in CODE_AUDIT["conditions"]}
    assert "topping_to_neutral" in ids
    assert "bottoming_to_neutral" in ids
    top_n = next(c for c in CODE_AUDIT["conditions"] if c["id"] == "topping_to_neutral")
    assert "NONE" in top_n["if"]


def test_refuse_overwrite_prior_result_dirs() -> None:
    with pytest.raises(ValueError):
        assert_safe_output_dir(Path("research/regime_scanner/results_trend_weakening_multi_bar_phase_c1"))
    with pytest.raises(ValueError):
        assert_safe_output_dir(Path("research/regime_scanner/results_trend_robustness_phase_b"))


def test_run_stats_long_thresholds() -> None:
    stats = _run_stats([10, 24, 48, 96, 288, 300])
    assert stats["n_runs"] == 6
    assert stats["ge24"] == 5
    assert stats["ge48"] == 4
    assert stats["ge96"] == 3
    assert stats["ge288"] == 2
    assert stats["maximum"] == 300
    assert stats["median"] == 72.0


def test_diagnose_topping_blocks_without_same_bar_bos() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 10
    rt.unavailable_reason = None
    rt.consecutive_bearish_closes = 3
    rt.structure_5m.last_high_label = "lower_high"
    rt.structure_5m.last_bos = None
    rt.structure_5m.last_choch = None
    cfg = c1_strict_config()
    ev = TbEvidence()
    row = {
        "close": 1.0,
        "ema_9": 0.99,
        "ema_20": 1.01,
        "di_spread": -8.0,
        "adx": 25.0,
        "ema_9_slope_3_pct": -0.1,
        "ema_20_slope_3_pct": -0.05,
    }
    d = diagnose_topping_exit(rt, types=set(), row=row, cfg=cfg, evidence=ev)
    assert d["would_exit_existing"] is False
    assert "bearish_bos_or_choch_same_bar" in d["block_reasons"]

    # CF1: persisted bearish choch unlocks
    from research.regime_scanner.trend_structure import StructureEvent

    rt.structure_5m.last_choch = StructureEvent(
        event_type="bearish_choch",
        timeframe="5m",
        event_time=pd.Timestamp("2026-01-01T00:00:00+00:00"),
        level=1.0,
        reference_pivot_time=None,
        reference_pivot_price=None,
        direction="bearish",
        reason_codes=("t",),
    )
    d2 = diagnose_topping_exit(rt, types=set(), row=row, cfg=cfg, evidence=ev)
    assert d2["cf1_persist_would_exit"] is True
    assert d2["would_exit_existing"] is False


def test_diagnose_topping_cf2_multibar_and_cf3_timeout() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = NEUTRAL_TIMEOUT_BARS
    rt.unavailable_reason = None
    rt.consecutive_bearish_closes = 2
    cfg = c1_strict_config()
    ev = TbEvidence()
    ev.cats = {"lower_high": "x", "bearish_choch": "y"}
    ev.seen_age = {"lower_high": 1, "bearish_choch": 2}
    row = {
        "close": 1.0,
        "ema_9": 1.0,
        "ema_20": 1.0,
        "di_spread": 0.0,
        "adx": 10.0,
        "ema_9_slope_3_pct": 0.0,
        "ema_20_slope_3_pct": 0.0,
    }
    d = diagnose_topping_exit(rt, types=set(), row=row, cfg=cfg, evidence=ev)
    assert d["cf2_multibar_would_exit"] is True
    assert d["cf3_neutral_timeout_would_exit"] is True


def test_diagnose_bottoming_htf_gate_and_same_bar() -> None:
    rt = TrendRuntime()
    rt.state = "bottoming"
    rt.age_5m_bars = 10
    rt.unavailable_reason = None
    rt.consecutive_bullish_closes = 3
    rt.structure_5m.last_low_label = "higher_low"
    cfg = c1_strict_config()
    ev = TbEvidence()
    row = {
        "close": 1.1,
        "ema_9": 1.1,
        "ema_20": 1.0,
        "di_spread": 8.0,
        "adx": 25.0,
        "ema_9_slope_3_pct": 0.1,
        "ema_20_slope_3_pct": 0.05,
    }
    d = diagnose_bottoming_exit(rt, types={"bullish_choch"}, row=row, cfg=cfg, evidence=ev)
    # may exit existing if no HTF veto
    assert d["hl_ok"] is True
    assert d["bos_same_bar"] is True


def test_hold_reasons_reproducible_hash() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 5
    rt.unavailable_reason = None
    cfg = c1_strict_config()
    ev = TbEvidence()
    row = {"close": 1.0}
    a = diagnose_topping_exit(rt, types=set(), row=row, cfg=cfg, evidence=ev)
    b = diagnose_topping_exit(rt, types=set(), row=row, cfg=cfg, evidence=ev)
    ha = hashlib.sha256(str(sorted(a["block_reasons"])).encode()).hexdigest()
    hb = hashlib.sha256(str(sorted(b["block_reasons"])).encode()).hexdigest()
    assert ha == hb


def test_no_march_hardcode_in_sm() -> None:
    import research.regime_scanner.trend_state_machine as m

    assert "2026-03-06" not in inspect.getsource(m)

"""Tests for Phase C1 multi-bar weakening evidence + audit guards."""

from __future__ import annotations

import inspect
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    clear_weakening_evidence,
    default_trend_state_config,
    multi_bar_weakening_exit,
    trend_state_config_c1,
    update_weakening_evidence,
)
from research.regime_scanner.trend_structure import StructureEvent as SE
from research.regime_scanner.trend_weakening_multi_bar_audit import (
    CODE_AUDIT,
    assert_safe_output_dir,
    config_for_variant,
    replay_variant,
    replay_variant_naive,
)
from research.regime_scanner.trend_robustness_audit import load_analysis_frame
from research.regime_scanner.trend_audit_shared_replay import (
    build_shared_structure_timeline,
    load_or_build_shared_context,
    reset_audit_counters,
)
import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod
from research.regime_scanner import swings as swings_mod


def _ev(etype: str, t: str, level: float = 1.0) -> SE:
    return SE(
        event_type=etype,
        timeframe="5m",
        event_time=pd.Timestamp(t, tz="UTC"),
        level=level,
        reference_pivot_time=None,
        reference_pivot_price=None,
        direction="bearish" if "bearish" in etype or etype == "lower_high" else "bullish",
        reason_codes=("test",),
    )


def test_default_config_is_baseline_off() -> None:
    cfg = default_trend_state_config()
    assert cfg.weakening_multi_bar_mode == "off"
    assert config_for_variant("off").weakening_multi_bar_mode == "off"


def test_no_march_hardcodes_in_sm_or_audit() -> None:
    import research.regime_scanner.trend_state_machine as m
    import research.regime_scanner.trend_weakening_multi_bar_audit as a

    # SM must not special-case March; audit may mention Mar6 as evaluation window only.
    src = inspect.getsource(m)
    assert "2026-03-06" not in src
    assert "APTUSDT" not in src


def test_evidence_accumulates_across_bars_and_exits_loose() -> None:
    rt = TrendRuntime()
    rt.state = "bullish_weakening"
    rt.age_5m_bars = 5
    rt.unavailable_reason = None
    cfg = trend_state_config_c1("loose")

    notes1 = update_weakening_evidence(
        rt, events=[_ev("bearish_choch", "2026-01-01T00:00:00+00:00", 1.1)], cfg=cfg
    )
    assert any("add:bearish_choch" in n for n in notes1)
    assert len(rt.weakening_evidence_keys) == 1

    # Same bar would not exit (only one category)
    st, reasons = multi_bar_weakening_exit(rt, types=set(), row={"close": 1.0}, cfg=cfg)
    assert st is None

    rt.age_5m_bars = 10
    update_weakening_evidence(
        rt, events=[_ev("lower_high", "2026-01-01T00:50:00+00:00", 1.05)], cfg=cfg
    )
    assert len(rt.weakening_evidence_keys) == 2
    st2, reasons2 = multi_bar_weakening_exit(rt, types=set(), row={"close": 1.0}, cfg=cfg)
    assert st2 == "topping"
    assert "multi_bar_topping_structure" in reasons2
    assert "mode:loose" in reasons2


def test_same_event_not_double_counted() -> None:
    rt = TrendRuntime()
    rt.state = "bullish_weakening"
    rt.age_5m_bars = 3
    cfg = trend_state_config_c1("loose")
    e = _ev("bearish_choch", "2026-01-01T00:00:00+00:00", 1.1)
    update_weakening_evidence(rt, events=[e], cfg=cfg)
    update_weakening_evidence(rt, events=[e], cfg=cfg)
    assert list(rt.weakening_evidence_keys.keys()) == ["bearish_choch"]


def test_continuation_resets_evidence() -> None:
    rt = TrendRuntime()
    rt.state = "bullish_weakening"
    rt.age_5m_bars = 4
    cfg = trend_state_config_c1("loose")
    update_weakening_evidence(
        rt, events=[_ev("bearish_choch", "2026-01-01T00:00:00+00:00")], cfg=cfg
    )
    assert rt.weakening_evidence_keys
    notes = update_weakening_evidence(
        rt, events=[_ev("higher_high", "2026-01-01T01:00:00+00:00")], cfg=cfg
    )
    assert not rt.weakening_evidence_keys
    assert "weakening_evidence_reset_continuation" in notes


def test_evidence_expires_outside_window() -> None:
    rt = TrendRuntime()
    rt.state = "bullish_weakening"
    rt.age_5m_bars = 0
    cfg = trend_state_config_c1("loose")
    # shrink window for test via object replace — config is frozen
    from dataclasses import replace

    cfg = replace(cfg, weakening_evidence_window_bars=5)
    update_weakening_evidence(
        rt, events=[_ev("bearish_choch", "2026-01-01T00:00:00+00:00")], cfg=cfg
    )
    rt.age_5m_bars = 10  # > window
    update_weakening_evidence(rt, events=[], cfg=cfg)
    assert "bearish_choch" not in rt.weakening_evidence_keys


def test_strict_requires_hard_structure_and_impulse() -> None:
    rt = TrendRuntime()
    rt.state = "bullish_weakening"
    rt.age_5m_bars = 8
    rt.consecutive_bearish_closes = 0
    rt.structure_15m.current_structure_bias = "bullish"
    cfg = trend_state_config_c1("strict")
    rt.weakening_evidence_keys = {
        "lower_high": "lh|x|1",
        "failed_breakout": "fo|x|1",
    }
    rt.weakening_evidence_seen_age = {"lower_high": 1, "failed_breakout": 2}
    flat_row = {
        "close": 1.05,
        "ema_20": 1.0,
        "ema_9": 1.02,
        "di_spread": 0.0,
        "adx": 10.0,
        "ema_9_slope_3_pct": 0.0,
        "ema_20_slope_3_pct": 0.0,
    }
    st, reasons = multi_bar_weakening_exit(
        rt,
        types=set(),
        row=flat_row,
        cfg=cfg,
    )
    assert st is None
    assert "multi_bar_strict_need_bos_or_choch" in reasons

    rt.weakening_evidence_keys["bearish_choch"] = "bc|x|1"
    rt.weakening_evidence_seen_age["bearish_choch"] = 3
    st2, reasons2 = multi_bar_weakening_exit(
        rt,
        types=set(),
        row=flat_row,
        cfg=cfg,
    )
    assert st2 is None
    assert "multi_bar_strict_need_impulse_or_htf" in reasons2

    rt.consecutive_bearish_closes = 2
    st3, reasons3 = multi_bar_weakening_exit(
        rt,
        types=set(),
        row=flat_row,
        cfg=cfg,
    )
    assert st3 == "topping"
    assert "mode:strict" in reasons3


def test_clear_on_helper() -> None:
    rt = TrendRuntime()
    rt.weakening_evidence_keys["x"] = "y"
    rt.weakening_evidence_seen_age["x"] = 1
    clear_weakening_evidence(rt)
    assert not rt.weakening_evidence_keys


def test_refuse_overwrite_forbidden_dirs() -> None:
    with pytest.raises(ValueError):
        assert_safe_output_dir(Path("research/regime_scanner/results_trend_robustness_phase_b"))
    with pytest.raises(ValueError):
        assert_safe_output_dir(Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0"))


def test_code_audit_documents_new_fields() -> None:
    assert "weakening_evidence_keys" in str(CODE_AUDIT["new_fields_required"])


def test_mode_off_ignores_accumulated_evidence() -> None:
    rt = TrendRuntime()
    rt.state = "bullish_weakening"
    rt.weakening_evidence_keys = {"bearish_choch": "a", "lower_high": "b"}
    cfg = default_trend_state_config()
    st, _ = multi_bar_weakening_exit(rt, types=set(), row={}, cfg=cfg)
    assert st is None


def _metrics_signature(result: dict) -> dict:
    skip = {"multi_bar_exits", "weakening_runs", "march_rows", "config", "mar6_first_exit"}
    sig = {k: v for k, v in result.items() if k not in skip}
    sig["mar6_first_exit_time"] = (result.get("mar6_first_exit") or {}).get("decision_time")
    sig["mar6_first_exit_to"] = (result.get("mar6_first_exit") or {}).get("state")
    return sig


@pytest.mark.parametrize("mode", ["off", "loose", "strict"])
def test_optimized_replay_matches_naive_for_march_window(mode: str) -> None:
    """Parity: shared structure + policy replay equals full per-variant replay."""
    try:
        frame = load_analysis_frame(
            "APTUSDT",
            load_start="2026-02-20",
            load_end="2026-03-15",
            max_bars=2500,
        )
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    reset_audit_counters()
    swings_mod.FILTER_PIVOTS_AS_OF_CALLS = 0
    a0 = pd.Timestamp("2026-03-01", tz="UTC")
    a1 = pd.Timestamp("2026-03-12", tz="UTC")
    shared = build_shared_structure_timeline(frame)
    assert shared_replay_mod.SHARED_STRUCTURE_PASS_COUNT == 1

    naive = replay_variant_naive(frame, mode=mode, analyze_start=a0, analyze_end=a1)  # type: ignore[arg-type]
    calls_during_naive = swings_mod.FILTER_PIVOTS_AS_OF_CALLS

    reset_audit_counters()
    swings_mod.FILTER_PIVOTS_AS_OF_CALLS = 0
    shared2 = build_shared_structure_timeline(frame)
    build_filter_calls = swings_mod.FILTER_PIVOTS_AS_OF_CALLS
    opt = replay_variant(frame, mode=mode, analyze_start=a0, analyze_end=a1, shared=shared2)  # type: ignore[arg-type]
    total_filter_calls = swings_mod.FILTER_PIVOTS_AS_OF_CALLS

    assert _metrics_signature(naive) == _metrics_signature(opt)
    assert build_filter_calls == 0
    assert total_filter_calls == 0
    assert calls_during_naive > 0


def test_shared_context_disk_cache_reuse(tmp_path: Path) -> None:
    try:
        frame = load_analysis_frame("APTUSDT", load_start="2026-02-20", load_end="2026-03-08", max_bars=800)
    except Exception as exc:
        pytest.skip(f"APTUSDT candles unavailable: {exc}")

    reset_audit_counters()
    ctx1 = load_or_build_shared_context(frame, cache_dir=tmp_path)
    assert shared_replay_mod.SHARED_STRUCTURE_PASS_COUNT == 1
    reset_audit_counters()
    ctx2 = load_or_build_shared_context(frame, cache_dir=tmp_path)
    assert shared_replay_mod.SHARED_STRUCTURE_PASS_COUNT == 0
    assert ctx1.cache_key == ctx2.cache_key
    assert len(ctx1.prepared_bars) == len(ctx2.prepared_bars)

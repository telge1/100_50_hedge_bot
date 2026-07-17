"""Tests for C3.5c × C3.4B 4h Protected Structure trend audit."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5c_c34b_4h_trend_audit import (
    DEFAULT_OUT,
    alignment_flags,
    classify_structure_strength,
    guard_decision,
    lookup_closed_c34b_bar,
    run_c34b_4h_trend_audit,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    DEFAULT_OUT as EXCURSION_DIR,
)


def test_output_and_guardrails() -> None:
    assert "c35c_c34b_4h_trend_audit" in str(DEFAULT_OUT)
    src = Path("research/regime_scanner/pullback_entry_c3_5c_c34b_4h_trend_audit.py").read_text()
    assert "no_entry_filter_activation" in src
    assert "c34b_unchanged" in src
    assert "apply_protected_structure" in src


def test_sm_and_c34b_untouched() -> None:
    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    c34b = Path("research/regime_scanner/market_structure_c3_4b.py")
    h1, c1 = hashlib.sha256(sm.read_bytes()).hexdigest(), hashlib.sha256(c34b.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_c34b_4h_trend_audit as mod

    _ = mod.DEFAULT_OUT
    assert hashlib.sha256(sm.read_bytes()).hexdigest() == h1
    assert hashlib.sha256(c34b.read_bytes()).hexdigest() == c1
    src = inspect.getsource(mod)
    assert "build_pullback_entry_pine" not in src
    assert "lookahead_on" not in src


def test_structure_strength_fixed_rules() -> None:
    assert (
        classify_structure_strength(
            {
                "major_direction": 1,
                "micro_direction": 1,
                "last_external_bos_side": "up",
                "protected_low": 1.0,
                "protected_high": np.nan,
            }
        )
        == "strong_bull_structure"
    )
    assert (
        classify_structure_strength(
            {
                "major_direction": -1,
                "micro_direction": -1,
                "last_external_bos_side": "down",
                "protected_high": 2.0,
                "protected_low": np.nan,
            }
        )
        == "strong_bear_structure"
    )
    assert classify_structure_strength({"major_direction": 1, "micro_direction": -1, "last_external_bos_side": "up", "protected_low": 1.0}) == "bull_structure"
    assert classify_structure_strength({"major_direction": 0, "micro_direction": 0}) == "mixed_structure"


def test_alignment_long_short() -> None:
    f = alignment_flags("long", -1, "strong_bear_structure")
    assert f["against_c34b_4h_major"] is True
    assert f["against_strong_c34b_4h"] is True
    assert f["alignment_category"] == "countertrend_strong"
    f2 = alignment_flags("short", -1, "strong_bear_structure")
    assert f2["with_c34b_4h_major"] is True
    assert f2["alignment_category"] == "aligned_strong"


def test_guards_g1_g2_g3() -> None:
    assert guard_decision("long", -1, "bear_structure", "G1") == "block"
    assert guard_decision("short", -1, "bear_structure", "G1") == "allow"
    assert guard_decision("long", -1, "strong_bear_structure", "G2") == "block"
    assert guard_decision("long", -1, "bear_structure", "G2") == "allow"
    assert guard_decision("long", 0, "mixed_structure", "G3") == "block"
    assert guard_decision("short", -1, "bear_structure", "G3") == "allow"
    assert guard_decision("long", 1, "bull_structure", "G0") == "allow"


def test_lookup_causal_closed_only() -> None:
    ts = pd.Timestamp("2026-02-01 00:00:00", tz="UTC")
    htf = pd.DataFrame(
        {
            "timestamp": [ts, ts + pd.Timedelta(hours=4)],
            "htf_close_decision": [ts + pd.Timedelta(hours=4), ts + pd.Timedelta(hours=8)],
            "major_direction": [-1, 1],
            "micro_direction": [-1, 1],
            "structure_strength_category": ["strong_bear_structure", "strong_bull_structure"],
            "protected_high": [2.0, np.nan],
            "protected_low": [np.nan, 1.0],
            "last_external_bos_side": ["down", "up"],
            "major_micro_alignment": [1.0, 1.0],
            "candidate_leg": ["none", "none"],
            "protected_structure_state": ["bearish_structure", "bullish_structure"],
            "bars_since_external_bos": [1, 1],
            "bars_since_internal_bos": [2, 2],
            "bars_since_choch": [3, 3],
            "bars_since_major_flip": [4, 4],
            "last_internal_bos_side": ["down", "up"],
            "last_choch_side": [None, None],
            "atr_14": [0.1, 0.1],
            "external_bos_up": [False, True],
            "external_bos_down": [True, False],
            "internal_bos_up": [False, False],
            "internal_bos_down": [False, False],
            "choch_side": [None, None],
            "external_bos_side": ["down", "up"],
            "internal_bos_side": [None, None],
        }
    )
    trig = ts + pd.Timedelta(hours=4)
    hit = lookup_closed_c34b_bar(htf, trigger_decision=trig)
    assert hit["found"] is True
    assert int(hit["row"]["major_direction"]) == -1
    assert pd.Timestamp(hit["selected_4h_bar_close_time"]) <= trig


@pytest.mark.skipif(
    not (EXCURSION_DIR / "fill_excursion_panel.csv").exists(),
    reason="excursion artifacts missing",
)
def test_live_audit_55_and_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "c35c_c34b_4h_trend_audit"
    meta1 = run_c34b_4h_trend_audit(output_dir=out, write_plots=False)
    assert meta1["n_fills"] == 55
    assert meta1["share_4h_closed_before_trigger"] == 1.0
    assert meta1["c34b_unchanged"] is True

    panel = pd.read_csv(out / "fill_c34b_4h_context.csv")
    assert len(panel) == 55
    assert panel["four_hour_bar_closed_before_trigger"].all()
    assert panel["context_is_causal"].all()

    exc = pd.read_csv(EXCURSION_DIR / "fill_excursion_panel.csv")
    m = panel.merge(exc[["fill_id", "maximum_favorable_excursion_pct", "maximum_adverse_excursion_pct"]], on="fill_id")
    assert np.allclose(m["primary_mfe_pct"], m["maximum_favorable_excursion_pct"])
    assert np.allclose(m["primary_mae_pct"], m["maximum_adverse_excursion_pct"])

    required = [
        "fill_c34b_4h_context.csv",
        "c34b_4h_bar_states.csv",
        "c34b_vs_ema_htf_comparison.csv",
        "c34b_vs_ema_aggregate.csv",
        "c34b_guard_impact.csv",
        "long_c34b_4h_risk_cases.csv",
        "c34b_4h_trend_persistence.csv",
        "protected_level_distance_summary.csv",
        "hypothesis_evaluation.csv",
        "robustness_slices.csv",
        "severe_mae_by_c34b_context.csv",
        "report.md",
        "metadata.json",
    ]
    for name in required:
        assert (out / name).exists(), name

    # G1: longs blocked only when major bearish
    long_block = panel[(panel["side"] == "long") & (panel["guard_G1"] == "block")]
    assert (long_block["major_direction_4h"] < 0).all()

    meta2 = run_c34b_4h_trend_audit(output_dir=out / "run2", write_plots=False)
    assert meta1["content_hash"] == meta2["content_hash"]

    c34b = Path("research/regime_scanner/market_structure_c3_4b.py")
    assert hashlib.sha256(c34b.read_bytes()).hexdigest() == meta1["c34b_source_hash"]

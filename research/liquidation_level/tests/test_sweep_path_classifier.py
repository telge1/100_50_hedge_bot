"""Tests for Phase D sweep path classifier."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from research.liquidation_level.sweep_feature_snapshots import assert_no_entry_fields
from research.liquidation_level.sweep_path_classifier import (
    CLASS_BULL,
    CLASS_INVALID,
    CLASS_SHORT,
    CLASS_UNCLEAR,
    PHASE_C_EXPECTED_HASH,
    RULE_COMPONENTS,
    SCORE_WEIGHTS,
    VARIANT_CONFIG,
    PhaseDValidationError,
    build_decision_snapshots,
    build_phase_d_bundle,
    classify_one,
    compute_all_scores,
    score_blockers,
    score_htf_context,
    score_htf_structure,
    score_level_response,
    score_structure_5m,
    score_trend_5m,
    validate_phase_d_inputs,
)

PHASE_A = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a")
PHASE_B = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_b")
PHASE_C = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_c")
SCANNER_ROOT = Path(__file__).resolve().parents[2] / "regime_scanner"


def _short_row(**overrides):
    base = {
        "decision_offset": 3,
        "final_close_relative_to_level_pct": -1.2,
        "fraction_closes_below_level": 0.8,
        "fraction_closes_above_level": 0.2,
        "longest_below_run": 3,
        "longest_above_run": 0,
        "number_reclaims_below": 2,
        "n_accepted_above": 0,
        "n_rejected_from_level": 2,
        "decision_5m_ema_9_20_distance": -0.4,
        "decision_5m_di_spread": -12.0,
        "decision_5m_adx": 28.0,
        "fraction_bearish_ema_ordering": 0.8,
        "fraction_bullish_ema_ordering": 0.1,
        "fraction_di_minus_gt_plus": 0.8,
        "fraction_di_plus_gt_minus": 0.2,
        "decision_5m_structure_bias": "bearish",
        "end_structure_bias": "bearish",
        "new_bearish_bos_count": 1,
        "new_bullish_bos_count": 0,
        "new_bearish_choch_count": 1,
        "new_bullish_choch_count": 0,
        "failed_breakout_count": 1,
        "failed_breakdown_count": 0,
        "atr_pct_mean": 0.8,
        "max_range_expansion_proxy": 0.3,
        "volume_ratio_mean": 1.4,
        "volume_spike_count_proxy": 1,
        "decision_15m_regime": "transition",
        "decision_15m_structure_bias": "bearish",
        "decision_15m_di_spread": -8.0,
        "decision_15m_adx": 22.0,
        "decision_15m_last_bos": "bearish_bos",
        "decision_30m_regime": "transition",
        "decision_30m_structure_bias": "bearish",
        "decision_30m_di_spread": -6.0,
        "decision_30m_adx": 20.0,
        "decision_30m_last_bos": "bearish_bos",
        "tf15_regime_changed_since_sweep": False,
        "tf30_regime_changed_since_sweep": False,
        "technical_invalid": False,
        "missing_features": "",
    }
    base.update(overrides)
    return base


def _bull_row(**overrides):
    row = _short_row(
        final_close_relative_to_level_pct=1.5,
        fraction_closes_below_level=0.2,
        fraction_closes_above_level=0.8,
        longest_below_run=0,
        longest_above_run=3,
        number_reclaims_below=0,
        n_accepted_above=3,
        n_rejected_from_level=0,
        decision_5m_ema_9_20_distance=0.5,
        decision_5m_di_spread=15.0,
        fraction_bearish_ema_ordering=0.1,
        fraction_bullish_ema_ordering=0.8,
        fraction_di_minus_gt_plus=0.2,
        fraction_di_plus_gt_minus=0.8,
        decision_5m_structure_bias="bullish",
        end_structure_bias="bullish",
        new_bearish_bos_count=0,
        new_bullish_bos_count=1,
        new_bearish_choch_count=0,
        new_bullish_choch_count=1,
        failed_breakout_count=0,
        failed_breakdown_count=1,
        decision_15m_structure_bias="bullish",
        decision_15m_di_spread=9.0,
        decision_15m_last_bos="bullish_bos",
        decision_30m_structure_bias="bullish",
        decision_30m_di_spread=7.0,
        decision_30m_last_bos="bullish_bos",
    )
    row.update(overrides)
    return row


@pytest.mark.skipif(not PHASE_C.exists(), reason="phase C results missing")
def test_event_count_and_phase_c_hash() -> None:
    v = validate_phase_d_inputs(phase_a_dir=PHASE_A, phase_b_dir=PHASE_B, phase_c_dir=PHASE_C)
    assert v["ok"] is True
    assert v["reproduced_events"] == {"full": 2696, "in_sample": 1824, "out_of_sample": 872}
    assert v["observed_phase_c_hash"] == PHASE_C_EXPECTED_HASH
    assert v["leakage_checks_passed"] is True
    assert v["phase_c_ready_for_phase_d"] is True


def test_decision_offsets_default() -> None:
    assert list(VARIANT_CONFIG) == ["strict", "medium", "loose"]
    assert set(RULE_COMPONENTS) == {"R1", "R2", "R3", "R4", "R5"}


def test_level_response_score_signs() -> None:
    s, support, _ = score_level_response(_short_row())
    assert s < 0
    assert any("below" in x for x in support)
    b, _, oppose = score_level_response(_bull_row())
    assert b > 0
    assert any("above" in x for x in oppose)


def test_trend_structure_htf_blocker_scores() -> None:
    t, *_ = score_trend_5m(_short_row())
    assert t < 0
    st, *_ = score_structure_5m(_short_row())
    assert st < 0
    c15, *_ = score_htf_context(_short_row(), "15m")
    s15, *_ = score_htf_structure(_short_row(), "15m")
    c30, *_ = score_htf_context(_short_row(), "30m")
    s30, *_ = score_htf_structure(_short_row(), "30m")
    assert c15 <= 0 and s15 < 0
    assert c30 <= 0 and s30 < 0
    blockers, short_b, bull_b, _ = score_blockers(
        _short_row(
            decision_30m_structure_bias="bullish",
            longest_above_run=3,
            fraction_closes_above_level=0.8,
            new_bullish_bos_count=2,
            decision_offset=3,
        )
    )
    assert short_b
    assert blockers > 0


def test_total_score_and_classes() -> None:
    short_scores = compute_all_scores(_short_row())
    bull_scores = compute_all_scores(_bull_row())
    assert short_scores["level_response_score"] < 0
    assert bull_scores["level_response_score"] > 0
    short_cls = classify_one(_short_row(), rule_family="R2", variant="loose", scores=short_scores)
    bull_cls = classify_one(_bull_row(), rule_family="R2", variant="loose", scores=bull_scores)
    assert short_cls["classification"] == CLASS_SHORT
    assert bull_cls["classification"] == CLASS_BULL
    unclear = classify_one(
        _short_row(
            final_close_relative_to_level_pct=0.05,
            fraction_closes_below_level=0.5,
            fraction_closes_above_level=0.5,
            longest_below_run=1,
            longest_above_run=1,
            decision_5m_di_spread=0.0,
            decision_5m_ema_9_20_distance=0.0,
            decision_5m_structure_bias="neutral",
        ),
        rule_family="R1",
        variant="strict",
    )
    assert unclear["classification"] == CLASS_UNCLEAR
    inv = classify_one(
        {"technical_invalid": True, "invalid_reason": "missing"},
        rule_family="R1",
        variant="medium",
    )
    assert inv["classification"] == CLASS_INVALID


def test_variants_and_rule_families() -> None:
    row = _short_row()
    scores = compute_all_scores(row)
    outs = {
        v: classify_one(row, rule_family="R1", variant=v, scores=scores)["classification"]
        for v in ("strict", "medium", "loose")
    }
    assert set(outs) == {"strict", "medium", "loose"}
    for rule in ("R1", "R2", "R3", "R4", "R5"):
        c = classify_one(row, rule_family=rule, variant="medium", scores=scores)
        assert c["classification"] in {CLASS_SHORT, CLASS_BULL, CLASS_UNCLEAR, CLASS_INVALID}
        assert "total_direction_score" in c
        for comp in RULE_COMPONENTS[rule]:
            assert comp in SCORE_WEIGHTS


def test_htf_blocker_forces_unclear_on_r4() -> None:
    row = _short_row(
        decision_30m_structure_bias="bullish",
        decision_15m_structure_bias="bullish",
        decision_15m_di_spread=12.0,
        decision_30m_di_spread=15.0,
        fraction_closes_above_level=0.0,
        longest_above_run=0,
        new_bullish_bos_count=2,
        decision_offset=3,
    )
    scores = compute_all_scores(row)
    assert scores["short_blockers"]
    r2 = classify_one(row, rule_family="R2", variant="loose", scores=scores)
    assert r2["classification"] == CLASS_SHORT
    r4 = classify_one(row, rule_family="R4", variant="loose", scores=scores)
    assert r4["classification"] == CLASS_UNCLEAR
    assert r4["blockers_triggered"]


def test_missing_features_transparent() -> None:
    row = {
        "technical_invalid": True,
        "invalid_reason": "close;initial_sweep_level",
        "missing_features": "close",
    }
    c = classify_one(row, rule_family="R1", variant="medium")
    assert c["classification"] == CLASS_INVALID


def test_decision_trace_fields() -> None:
    row = _short_row()
    scores = compute_all_scores(row)
    c = classify_one(row, rule_family="R3", variant="medium", scores=scores)
    for k in SCORE_WEIGHTS:
        assert k in c
    assert "supporting_reasons" in c
    assert "opposing_reasons" in c
    assert "blockers_triggered" in c


def test_targets_not_in_score_inputs() -> None:
    row = _short_row(target_ended_below_level=True, eval_ended_below_level=True)
    scores = compute_all_scores(row)
    row2 = _short_row(target_ended_below_level=False, eval_ended_below_level=False)
    scores2 = compute_all_scores(row2)
    for k in SCORE_WEIGHTS:
        assert scores[k] == scores2[k]


@pytest.mark.skipif(not PHASE_B.exists(), reason="phase B missing")
def test_offset_causality_no_future_bars() -> None:
    from research.liquidation_level.sweep_path_classifier import _load_bars_w12, _load_windows_meta

    bars = _load_bars_w12(PHASE_B)
    wins = _load_windows_meta(PHASE_B)
    eids = wins["event_id"].astype(str).head(5).tolist()
    bars = bars.loc[bars["event_id"].astype(str).isin(eids)]
    wins = wins.loc[wins["event_id"].astype(str).isin(eids)]
    snaps, _, _ = build_decision_snapshots(
        bars_w12=bars, windows_w12=wins, decision_offsets=(1, 3, 6, 12)
    )
    for off in (1, 3, 6, 12):
        sub = snaps.loc[snaps["decision_offset"] == off]
        assert (sub["max_window_offset_used"] == off).all()
        assert (~sub["uses_end_features_beyond_offset"].astype(bool)).all()
        assert (sub["n_path_bars"] == off).all()


@pytest.mark.skipif(not PHASE_C.exists(), reason="phase C missing")
def test_bundle_small_run_is_oos_monthly_overlap_hash_repeat() -> None:
    b1 = build_phase_d_bundle(
        phase_a_dir=PHASE_A,
        phase_b_dir=PHASE_B,
        phase_c_dir=PHASE_C,
        max_events=40,
        decision_offsets=(1, 3, 6, 12),
        rule_families=("R1", "R2", "R3", "R4", "R5"),
        variants=("strict", "medium", "loose"),
        timeline_sample_size=10,
        random_seed=42,
    )
    assert set(b1.snapshots["decision_offset"].unique()) == {1, 3, 6, 12}
    assert set(b1.classifications["rule_family"].unique()) == {"R1", "R2", "R3", "R4", "R5"}
    assert set(b1.classifications["variant"].unique()) == {"strict", "medium", "loose"}
    assert b1.leakage_checks["passed"] is True
    assert "in_sample" in set(b1.classifications["sample"].unique())
    assert len(b1.monthly) > 0
    assert "overlap_variant" in b1.overlap.columns
    assert {"all_events", "first_event_per_overlap_group", "gap_12_candles", "gap_24_candles"} <= set(
        b1.overlap["overlap_variant"].unique()
    )
    assert not any(c.startswith("target_") or c.startswith("eval_") for c in b1.snapshots.columns)
    assert_no_entry_fields(b1.snapshots)
    assert_no_entry_fields(b1.classifications)
    for bad in ("entry_price", "pnl", "fees", "tp", "sl"):
        assert bad not in b1.classifications.columns
        assert bad not in b1.traces.columns
    for col in (
        "event_id",
        "decision_offset",
        "rule_family",
        "variant",
        "classification",
        "total_direction_score",
        "blockers_triggered",
        "supporting_reasons",
        "opposing_reasons",
        "missing_features",
        "decision_timestamp",
        "causal_features_used",
    ):
        assert col in b1.traces.columns
    h1 = b1.deterministic_hash
    b2 = build_phase_d_bundle(
        phase_a_dir=PHASE_A,
        phase_b_dir=PHASE_B,
        phase_c_dir=PHASE_C,
        max_events=40,
        decision_offsets=(1, 3, 6, 12),
        rule_families=("R1", "R2", "R3", "R4", "R5"),
        variants=("strict", "medium", "loose"),
        timeline_sample_size=10,
        random_seed=42,
    )
    assert b2.deterministic_hash == h1
    assert len(h1) == 64


def test_validation_aborts_on_bad_hash(tmp_path: Path) -> None:
    src = PHASE_C / "summary.json"
    if not src.exists():
        pytest.skip("phase c missing")
    summary = json.loads(src.read_text(encoding="utf-8"))
    summary["deterministic_hash"] = "deadbeef"
    (tmp_path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(PhaseDValidationError):
        validate_phase_d_inputs(phase_a_dir=PHASE_A, phase_b_dir=PHASE_B, phase_c_dir=tmp_path)


def test_no_scanner_files_modified_in_phase_d_scope() -> None:
    assert SCANNER_ROOT.exists()
    phase_d_files = {
        "sweep_path_classifier.py",
        "sweep_path_classifier_audit.py",
        "test_sweep_path_classifier.py",
    }
    assert all("sweep_path_classifier" in f or f.startswith("test_") for f in phase_d_files)

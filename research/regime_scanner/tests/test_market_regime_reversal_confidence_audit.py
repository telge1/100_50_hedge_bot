"""Tests for macro reversal confidence audit."""
from __future__ import annotations

from pathlib import Path

from research.regime_scanner.market_regime_reversal_confidence_audit import (
    points_from_features,
    score_to_state,
)


def test_score_thresholds() -> None:
    assert score_to_state(0) == "countertrend_recovery"
    assert score_to_state(1) == "countertrend_recovery"
    assert score_to_state(2) == "possible_reversal"
    assert score_to_state(3) == "possible_reversal"
    assert score_to_state(4) == "probable_reversal"
    assert score_to_state(5) == "probable_reversal"
    assert score_to_state(6) == "confirmed_reversal"


def test_points_break_and_reentry() -> None:
    feat = {
        "structure_break": True,
        "n_closes_beyond": 2,
        "higher_low_after_break": True,
        "lower_high_after_break": False,
        "retest_held": False,
        "local_30m_aligned": True,
        "hh_hl_or_lh_ll_sequence": False,
        "old_structure_reentered": False,
        "counter_reaction": False,
    }
    score, comp = points_from_features(feat, proposed=1)
    assert score == 2 + 1 + 2 + 1  # break + two closes + HL + local
    assert "structure_break" in comp

    feat["old_structure_reentered"] = True
    score2, _ = points_from_features(feat, proposed=1)
    assert score2 == score - 3


def test_artifacts_if_present() -> None:
    out = Path("research/regime_scanner/results/market_regime_reversal_confidence_audit")
    if not (out / "summary.json").exists():
        return
    for name in (
        "summary.json",
        "variant_comparison.csv",
        "reversal_cases.csv",
        "reversal_score_timeline.csv",
        "failed_reversals.csv",
        "confirmed_reversals.csv",
        "possible_reversals.csv",
        "probable_reversals.csv",
        "delay_comparison.csv",
        "jan13_15_detail.csv",
        "audit_metadata.json",
        "README.md",
    ):
        assert (out / name).exists()
    for v in ("c0", "c1", "c2", "c3", "c4"):
        pine = out / f"market_regime_reversal_confidence_{v}_2026_01.pine"
        assert pine.exists()
        text = pine.read_text()
        assert text.lstrip().startswith("//@version=6")
        assert "showLabels = input.bool(false" in text
        assert "box.new" not in text
        assert "BOUNCE" not in text and "ALIGNED" not in text

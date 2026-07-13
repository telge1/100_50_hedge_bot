"""Tests for macro flip structure audit (R0–R4)."""
from __future__ import annotations

from pathlib import Path

from research.regime_scanner.market_regime_macro_flip_structure_audit import (
    BEAR_BULL_RECOVERY,
    MACRO_BEAR,
    MACRO_BULL,
    POSSIBLE_BULL_REV,
    map_s2_to_display,
    run_gated_variant,
)
from research.regime_scanner.market_regime_macro_stability_audit import (
    BEAR_CONSOL,
    BEAR_TRENDING,
    BULL_TRENDING,
    TRUE_RANGE,
)


def _snap(i: int, close: float, **kw):
    import pandas as pd

    t0 = pd.Timestamp("2026-01-10", tz="UTC") + pd.Timedelta(hours=4 * i)
    base = {
        "decision_time": t0 + pd.Timedelta(hours=4),
        "close": close,
        "high": close + 0.01,
        "low": close - 0.01,
        "structure_bias": "bearish",
        "last_lower_high": 1.05,
        "last_lower_high_ts": (t0 - pd.Timedelta(hours=20)).isoformat(),
        "last_higher_low": 0.95,
        "last_higher_low_ts": (t0 - pd.Timedelta(hours=24)).isoformat(),
        "last_swing_high": 1.05,
        "last_swing_low": 0.95,
        "protective_high": 1.05,
        "protective_low": 0.95,
        "events": [],
        "bullish_choch": False,
        "bearish_choch": False,
        "bullish_retest_holds": False,
        "bearish_retest_holds": False,
        "regime": "strong_bearish_trend",
    }
    base.update(kw)
    return base


def test_map_s2_r0() -> None:
    codes = map_s2_to_display([BEAR_TRENDING, BEAR_CONSOL, BULL_TRENDING, TRUE_RANGE])
    assert codes[0] == MACRO_BEAR
    assert codes[1] == BEAR_BULL_RECOVERY
    assert codes[2] == MACRO_BULL


def test_r1_requires_close_break_of_lh() -> None:
    # intent bull but close below LH → no confirmed macro bull
    s2 = [BEAR_TRENDING, BEAR_TRENDING, BULL_TRENDING, BULL_TRENDING]
    snaps = [
        _snap(0, 1.00),
        _snap(1, 0.99),
        _snap(2, 1.02),  # below LH 1.05
        _snap(3, 1.03),
    ]
    codes, flips = run_gated_variant(variant="R1", s2_codes=s2, snaps=snaps)
    assert MACRO_BULL not in codes
    assert any(c in (BEAR_BULL_RECOVERY, POSSIBLE_BULL_REV) for c in codes[2:])


def test_r1_flips_when_close_breaks_lh() -> None:
    s2 = [BEAR_TRENDING, BEAR_TRENDING, BULL_TRENDING]
    snaps = [
        _snap(0, 1.00),
        _snap(1, 0.99),
        _snap(2, 1.06, bullish_choch=True, events=["bullish_choch"]),
    ]
    codes, flips = run_gated_variant(variant="R1", s2_codes=s2, snaps=snaps)
    assert codes[-1] == MACRO_BULL
    assert flips and flips[-1]["proposed_new_direction"] == "bullish"


def test_r2_needs_hl_after_break() -> None:
    s2 = [BEAR_TRENDING, BULL_TRENDING, BULL_TRENDING, BULL_TRENDING]
    snaps = [
        _snap(0, 1.00),
        _snap(1, 1.06, bullish_choch=True, events=["bullish_choch"]),
        _snap(2, 1.07),
        _snap(3, 1.08, events=["higher_low"], last_higher_low=1.04,
              last_higher_low_ts="2026-01-10T16:00:00+00:00"),
    ]
    # fix HL ts after break
    import pandas as pd
    snaps[3]["last_higher_low_ts"] = (_ts_after := (pd.Timestamp(snaps[1]["decision_time"]) + pd.Timedelta(hours=8))).isoformat()
    codes, flips = run_gated_variant(variant="R2", s2_codes=s2, snaps=snaps)
    assert codes[1] != MACRO_BULL  # break bar alone insufficient for R2
    assert codes[-1] == MACRO_BULL
    assert flips[-1]["higher_low_after_break"] is True


def test_artifacts_if_present() -> None:
    out = Path("research/regime_scanner/results/market_regime_macro_flip_structure_audit")
    if not (out / "summary.json").exists():
        return
    for v in ("r0", "r1", "r2", "r3", "r4"):
        pine = out / f"market_regime_macro_flip_{v}_2026_01.pine"
        assert pine.exists()
        text = pine.read_text()
        assert text.lstrip().startswith("//@version=6")
        assert "showLabels = input.bool(false" in text
        assert "box.new" not in text
        assert "ALIGNED" not in text and "BOUNCE" not in text
    for name in (
        "all_macro_flips.csv",
        "bullish_flip_cases.csv",
        "bearish_flip_cases.csv",
        "false_flip_cases.csv",
        "valid_reversal_cases.csv",
        "countertrend_recovery_cases.csv",
        "variant_comparison.csv",
        "jan13_15_detail.csv",
        "chart_review_flip_variants.csv",
        "summary.json",
        "audit_metadata.json",
    ):
        assert (out / name).exists()

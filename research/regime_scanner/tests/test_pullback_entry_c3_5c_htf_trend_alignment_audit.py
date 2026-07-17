"""Tests for C3.5c HTF trend alignment audit (research-only)."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    DEFAULT_OUT as EXCURSION_DIR,
)
from research.regime_scanner.pullback_entry_c3_5c_htf_trend_alignment_audit import (
    DEFAULT_OUT,
    classify_alignment_category,
    classify_combined_htf,
    classify_ema_trend,
    lookup_last_closed_htf,
    major_to_label,
    recovery_and_risk_proxy,
    run_htf_trend_alignment_audit,
    with_trend_flags,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import DEFAULT_BASELINE_DIR


def test_output_path_and_guardrails() -> None:
    assert "c35c_htf_trend_alignment_audit" in str(DEFAULT_OUT)
    src = Path("research/regime_scanner/pullback_entry_c3_5c_htf_trend_alignment_audit.py").read_text()
    assert "no_entry_filter_activation" in src
    assert "no_hedge_bot_implementation" in src
    assert "no_stop_tp_optimization" in src


def test_sm_pine_untouched() -> None:
    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    h1 = hashlib.sha256(sm.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_htf_trend_alignment_audit as mod

    _ = mod.DEFAULT_OUT
    assert hashlib.sha256(sm.read_bytes()).hexdigest() == h1
    src = inspect.getsource(mod)
    assert "build_pullback_entry_pine" not in src
    assert "lookahead_on" not in src
    assert "shift(-" not in src


def test_reuses_excursion_and_shared_fills() -> None:
    src = Path("research/regime_scanner/pullback_entry_c3_5c_htf_trend_alignment_audit.py").read_text()
    assert "fill_excursion_panel" in src
    assert "_filled_sorted" in src
    assert "trades_exit_a_opposite_entry" in src
    assert "aggregate_complete_from_5m" in src
    assert "build_extended_tf_frame" in src


def test_ema_trend_bull_bear_mixed() -> None:
    bull = {"ema_9": 3, "ema_20": 2, "ema_50": 1, "ema_20_slope_1": 0.1, "ema_50_slope_1": 0.0}
    bear = {"ema_9": 1, "ema_20": 2, "ema_50": 3, "ema_20_slope_1": -0.1, "ema_50_slope_1": 0.0}
    mixed = {"ema_9": 3, "ema_20": 2, "ema_50": 1, "ema_20_slope_1": -0.1, "ema_50_slope_1": 0.1}
    assert classify_ema_trend(bull) == "bullish"
    assert classify_ema_trend(bear) == "bearish"
    assert classify_ema_trend(mixed) == "mixed"


def test_combined_and_alignment_deterministic() -> None:
    assert classify_combined_htf("bearish", "bearish", "bearish") == "strong_bear"
    assert classify_combined_htf("bullish", "bullish", "bullish") == "strong_bull"
    assert classify_combined_htf("bearish", "bearish", "mixed") == "bear"
    assert classify_combined_htf("bullish", "bearish", "bearish") == "mixed"
    assert classify_alignment_category("short", "strong_bear", "bearish", "bearish") == "aligned_strong"
    assert classify_alignment_category("long", "strong_bear", "bearish", "bearish") == "countertrend_strong"
    assert classify_alignment_category("long", "mixed", "bullish", "bearish") == "conflicting_timeframes"
    flags = with_trend_flags("short", "bearish", "bearish", "bearish", "strong_bear")
    assert flags["with_major_trend"] is True
    assert flags["all_timeframes_aligned"] is True
    assert major_to_label(-1) == "bearish"


def test_lookup_rejects_open_htf_bar() -> None:
    ts = pd.Timestamp("2026-02-01 00:00:00", tz="UTC")
    htf = pd.DataFrame(
        {
            "timestamp": [ts, ts + pd.Timedelta(hours=1)],
            "htf_close_decision": [ts + pd.Timedelta(hours=1), ts + pd.Timedelta(hours=2)],
            "ema_9": [1.0, 3.0],
            "ema_20": [2.0, 2.0],
            "ema_50": [3.0, 1.0],
            "ema_20_slope_1": [-0.1, 0.1],
            "ema_50_slope_1": [-0.1, 0.0],
            "close": [100.0, 101.0],
            "major_direction": [-1, 1],
        }
    )
    # trigger decision at 01:00 → only first bar closed
    trig = ts + pd.Timedelta(hours=1)
    hit = lookup_last_closed_htf(htf, trigger_decision=trig, tf_minutes=60)
    assert hit["found"] is True
    assert hit["context_bar_time"] == ts
    assert hit["htf_bar_closed_before_trigger"] is True
    assert pd.Timestamp(hit["context_close_decision"]) <= trig
    # second bar closes at 02:00 — must not be used
    assert hit["trend"] == "bearish"


def test_recovery_long_and_short() -> None:
    # long: underwater without touching entry, then recovers on bar 2
    highs = np.array([99.8, 99.5, 100.2])
    lows = np.array([99.0, 98.0, 99.8])
    closes = np.array([99.5, 98.5, 100.1])
    r = recovery_and_risk_proxy(
        side=1, entry=100.0, fill_i=0, highs=highs, lows=lows, closes=closes, n_bars=3, opp_bar=None
    )
    assert r["ever_underwater"] is True
    assert r["recovery_to_entry_reached"] is True
    assert r["bars_to_recovery"] == 2
    assert r["drawdown_exceeded_5pct"] is False  # mae ~ -2%

    # short: adverse up then recover
    highs = np.array([100.5, 102.0, 99.5])
    lows = np.array([100.2, 100.5, 98.0])
    closes = np.array([100.4, 101.5, 99.0])
    r2 = recovery_and_risk_proxy(
        side=-1, entry=100.0, fill_i=0, highs=highs, lows=lows, closes=closes, n_bars=3, opp_bar=None
    )
    assert r2["ever_underwater"] is True
    assert r2["recovery_to_entry_reached"] is True
    assert r2["bars_to_recovery"] == 2


def test_never_recovered_flag() -> None:
    highs = np.array([99.5, 99.0, 98.5])
    lows = np.array([98.0, 97.0, 96.0])
    closes = np.array([99.0, 98.0, 97.0])
    r = recovery_and_risk_proxy(
        side=1, entry=100.0, fill_i=0, highs=highs, lows=lows, closes=closes, n_bars=3, opp_bar=2
    )
    assert r["never_recovered_before_opposite_or_data_end"] is True
    assert r["continued_against_10pct"] is False


def test_drawdown_thresholds() -> None:
    highs = np.array([100.0, 90.0])
    lows = np.array([89.0, 79.0])
    closes = np.array([90.0, 80.0])
    r = recovery_and_risk_proxy(
        side=1, entry=100.0, fill_i=0, highs=highs, lows=lows, closes=closes, n_bars=2, opp_bar=None
    )
    assert r["drawdown_exceeded_5pct"] is True
    assert r["drawdown_exceeded_10pct"] is True
    assert r["drawdown_exceeded_15pct"] is True
    assert r["drawdown_exceeded_20pct"] is True
    assert r["continued_against_20pct"] is True


@pytest.mark.skipif(
    not EXCURSION_DIR.exists() or not (EXCURSION_DIR / "fill_excursion_panel.csv").exists(),
    reason="excursion audit artifacts missing",
)
def test_live_audit_55_fills_and_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "c35c_htf_trend_alignment_audit"
    meta1 = run_htf_trend_alignment_audit(output_dir=out, write_plots=False)
    assert meta1["n_fills"] == 55
    assert meta1["share_htf_closed_before_trigger"] == 1.0
    assert meta1["no_entry_filter_activation"] is True

    panel = pd.read_csv(out / "fill_htf_alignment_panel.csv")
    assert len(panel) == 55
    assert set(panel["combined_htf_trend"]).issubset({"strong_bull", "bull", "mixed", "bear", "strong_bear"})
    assert panel["context_is_causal"].all()

    # MFE/MAE identical to excursion
    exc = pd.read_csv(EXCURSION_DIR / "fill_excursion_panel.csv")
    m = panel.merge(exc[["fill_id", "maximum_favorable_excursion_pct", "maximum_adverse_excursion_pct"]], on="fill_id")
    assert np.allclose(m["primary_mfe_pct"], m["maximum_favorable_excursion_pct"])
    assert np.allclose(m["primary_mae_pct"], m["maximum_adverse_excursion_pct"])

    required = [
        "fill_htf_alignment_panel.csv",
        "alignment_excursion_summary.csv",
        "alignment_exit_a_summary.csv",
        "long_short_trend_matrix.csv",
        "timeframe_agreement_summary.csv",
        "hedgebot_directional_risk_proxy.csv",
        "recovery_by_alignment.csv",
        "severe_countertrend_cases.csv",
        "trend_persistence_after_fill.csv",
        "hypothesis_evaluation.csv",
        "robustness_slices.csv",
        "report.md",
        "metadata.json",
    ]
    for name in required:
        assert (out / name).exists(), name

    meta2 = run_htf_trend_alignment_audit(output_dir=out / "run2", write_plots=False)
    assert meta1["content_hash"] == meta2["content_hash"]

    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    assert len(hashlib.sha256(sm.read_bytes()).hexdigest()) == 64

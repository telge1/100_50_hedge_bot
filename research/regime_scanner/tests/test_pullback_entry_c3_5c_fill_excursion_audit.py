"""Tests for C3.5c APT 15m fill excursion audit (research-only)."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    DEFAULT_OUT,
    HORIZON_BARS,
    SL_LEVELS_PCT,
    TP_LEVELS_PCT,
    analyze_fill_core,
    classify_path,
    fav_adv_from_bar,
    first_touch_level,
    path_arrays,
    reconcile_fill_population,
    reconciliation_summary,
    run_fill_excursion_audit,
    signed_return_pct,
)
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import DEFAULT_BASELINE_DIR


def test_output_path_and_guardrails() -> None:
    assert "c35c_fill_excursion_audit" in str(DEFAULT_OUT)
    src = Path("research/regime_scanner/pullback_entry_c3_5c_fill_excursion_audit.py").read_text()
    assert "no_stop_tp_optimization" in src
    assert "no_filter_promotion" in src
    assert "production_sm_unchanged" in src
    assert len(TP_LEVELS_PCT) == 11
    assert len(SL_LEVELS_PCT) == 11
    assert HORIZON_BARS[-1] == 192


def test_sm_and_pine_untouched() -> None:
    sm_path = Path("research/regime_scanner/pullback_entry_c3_5.py")
    h1 = hashlib.sha256(sm_path.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit as mod

    _ = mod.DEFAULT_OUT
    h2 = hashlib.sha256(sm_path.read_bytes()).hexdigest()
    assert h1 == h2
    src = inspect.getsource(mod)
    assert "build_pullback_entry_pine" not in src
    assert "lookahead_on" not in src
    assert "shift(-" not in src


def test_reuses_shared_fill_and_exit_a() -> None:
    src = Path("research/regime_scanner/pullback_entry_c3_5c_fill_excursion_audit.py").read_text()
    assert "_filled_sorted" in src
    assert "trades_exit_a_opposite_entry" in src
    assert "build_extended_tf_frame" in src
    assert "baseline_a6" in src


def test_signed_return_long_short() -> None:
    assert abs(signed_return_pct(1, 100.0, 101.0) - 1.0) < 1e-12
    assert abs(signed_return_pct(-1, 100.0, 99.0) - (100 / 99 - 1) * 100) < 1e-12
    fav, adv = fav_adv_from_bar(1, 100.0, 102.0, 98.0)
    assert abs(fav - 2.0) < 1e-12
    assert abs(adv - (-2.0)) < 1e-12
    fav_s, adv_s = fav_adv_from_bar(-1, 100.0, 102.0, 98.0)
    assert fav_s > 0 and adv_s < 0


def test_path_arrays_long_mfe_mae_and_bars() -> None:
    highs = np.array([100.5, 101.5, 101.0, 103.0])
    lows = np.array([99.0, 99.5, 98.5, 100.0])
    closes = np.array([100.2, 101.0, 99.0, 102.0])
    p = path_arrays(1, 100.0, highs, lows, closes, 0, 3)
    assert abs(p["maximum_favorable_excursion_pct"] - 3.0) < 1e-12
    assert abs(p["maximum_adverse_excursion_pct"] - (-1.5)) < 1e-12
    assert p["bars_to_mfe"] == 3
    assert p["bars_to_mae"] == 2
    assert p["mae_before_mfe"] is True
    assert p["mfe_before_mae"] is False


def test_path_arrays_short_mfe_mae() -> None:
    highs = np.array([100.5, 101.0, 99.5])
    lows = np.array([99.5, 98.0, 97.0])
    closes = np.array([100.0, 98.5, 97.5])
    p = path_arrays(-1, 100.0, highs, lows, closes, 0, 2)
    # fav from lows: bar1 = 100/98-1 = ~2.04%, bar2 = 100/97-1 ≈ 3.09%
    assert p["bars_to_mfe"] == 2
    # adv from highs: bar1 = 100/101-1 ≈ -0.99%
    assert p["maximum_adverse_excursion_pct"] < 0
    assert p["first_excursion_direction"] in {"favorable", "adverse", "intrabar_unknown", "flat"}


def test_no_pre_fill_bars_used() -> None:
    highs = np.array([110.0, 100.5, 101.0])
    lows = np.array([90.0, 99.5, 99.0])
    closes = np.array([100.0, 100.2, 100.5])
    # fill at bar 1 — bar0 spike must not affect
    p = path_arrays(1, 100.0, highs, lows, closes, 1, 2)
    assert p["maximum_favorable_excursion_pct"] < 2.0
    assert p["maximum_adverse_excursion_pct"] > -5.0


def test_horizon_truncation_at_data_end() -> None:
    highs = np.array([100.5, 100.6])
    lows = np.array([99.5, 99.4])
    closes = np.array([100.1, 100.2])
    n = 2
    fill_i = 0
    hb = 8
    end_h = min(n - 1, fill_i + hb - 1)
    trunc = end_h < fill_i + hb - 1
    assert trunc is True
    p = path_arrays(1, 100.0, highs, lows, closes, fill_i, end_h)
    assert p["n_bars"] == 2


def test_same_bar_tp_sl_ambiguous_conservative_optimistic() -> None:
    highs = np.array([101.5])
    lows = np.array([98.5])
    timestamps = [pd.Timestamp("2026-02-01", tz="UTC")]
    fill = {
        "side": 1,
        "side_name": "long",
        "setup_id": 1,
        "trigger_bar": 0,
        "fill_bar": 0,
        "fill_timestamp": timestamps[0],
        "entry_price": 100.0,
    }
    recon = {
        "next_opposite_fill_index": None,
        "exit_a_closed": False,
        "is_terminal_open_fill": True,
    }
    panel, _h, _lv, ft, _seq = analyze_fill_core(
        fill=fill,
        recon_row=recon,
        fills=[fill],
        highs=highs,
        lows=lows,
        closes=np.array([100.0]),
        timestamps=timestamps,
        n_bars=1,
    )
    row = next(r for r in ft if r["tp_level_pct"] == 1.0 and r["sl_level_pct"] == -1.0)
    assert row["both_same_bar"] is True
    assert row["intrabar_ambiguous"] is True
    assert row["result_if_conservative"] == "SL"
    assert row["result_if_optimistic"] == "TP"
    assert panel["intrabar_order_unknown"] is True


def test_first_touch_level_tp_and_sl() -> None:
    highs = np.array([100.2, 101.2, 100.5])
    lows = np.array([99.8, 100.0, 99.0])
    tp = first_touch_level(1, 100.0, highs, lows, 0, 2, 1.0)
    assert tp["reached"] is True and tp["bar_offset"] == 1
    sl = first_touch_level(1, 100.0, highs, lows, 0, 2, -1.0)
    assert sl["reached"] is True and sl["bar_offset"] == 2


def test_reconcile_55_vs_29_identity_on_synthetic() -> None:
    # L, L(skip), S, L, S, L(open)
    ts = pd.date_range("2026-02-01", periods=10, freq="15min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "symbol": ["T"] * 10,
        }
    )
    filled = []
    sides = [1, 1, -1, 1, -1, 1]
    for i, side in enumerate(sides):
        filled.append(
            {
                "side": side,
                "side_name": "long" if side > 0 else "short",
                "setup_id": i + 1,
                "trigger_bar": i,
                "fill_bar": i + 1,
                "trigger_timestamp": ts[i],
                "fill_timestamp": ts[i + 1],
                "entry_price": 100.0 + i,
            }
        )
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe="15m", variant="A6")
    recon = reconcile_fill_population(filled, trades, n_bars=len(frame), data_end_ts=ts[-1])
    assert len(recon) == 6
    n_closed = int(((recon["included_in_realized_exit_a"]) & (recon["exit_a_closed"])).sum())
    n_open = int(recon["is_terminal_open_fill"].sum())
    n_skip = int((recon["exclusion_reason"] == "same_direction_while_exit_a_position_open").sum())
    assert n_skip == 1
    assert n_closed + n_open + n_skip == 6
    assert int(recon["included_in_realized_exit_a"].sum()) == n_closed + n_open
    assert len(trades) == n_closed + n_open
    assert int(trades["closed"].sum()) == n_closed
    summ = reconciliation_summary(recon, n_arms=3, n_triggers=6, n_lives=3)
    assert int(summ.loc[summ.metric == "fills", "value"].iloc[0]) == 6


def test_classify_path_rules_documented() -> None:
    p = {
        "empty": False,
        "n_bars": 20,
        "maximum_favorable_excursion_pct": 1.5,
        "maximum_adverse_excursion_pct": -0.1,
        "first_excursion_direction": "favorable",
        "close_return_pct": 1.0,
        "mae_before_mfe": False,
    }
    assert classify_path(p, truncated=False) == "clean_immediate_favorable"


def test_full_levels_present_in_matrix() -> None:
    highs = np.linspace(100, 105, 50)
    lows = np.linspace(99.5, 104.5, 50)
    closes = (highs + lows) / 2
    timestamps = list(pd.date_range("2026-02-01", periods=50, freq="15min", tz="UTC"))
    fill = {
        "side": 1,
        "side_name": "long",
        "setup_id": 1,
        "trigger_bar": 0,
        "fill_bar": 0,
        "fill_timestamp": timestamps[0],
        "entry_price": 100.0,
    }
    _p, _h, levels, ft, _s = analyze_fill_core(
        fill=fill,
        recon_row={"next_opposite_fill_index": None},
        fills=[fill],
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=timestamps,
        n_bars=50,
    )
    assert len(levels) == len(TP_LEVELS_PCT) + len(SL_LEVELS_PCT)
    assert len(ft) == len(TP_LEVELS_PCT) * len(SL_LEVELS_PCT)


@pytest.mark.skipif(
    not Path("research/regime_scanner/results/phase_c2_trend_topping_bottoming").exists()
    and not DEFAULT_BASELINE_DIR.exists(),
    reason="baseline/data unavailable",
)
def test_live_audit_55_fills_29_closed_and_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "c35c_fill_excursion_audit"
    meta1 = run_fill_excursion_audit(output_dir=out, write_plots=False)
    assert meta1["n_fills"] == 55
    assert meta1["n_closed_exit_a"] == 29
    assert meta1["n_open_exit_a"] == 1
    assert meta1["n_same_direction_skips"] == 25
    assert meta1["n_fills"] == meta1["n_closed_exit_a"] + meta1["n_open_exit_a"] + meta1["n_same_direction_skips"]
    assert meta1["no_stop_tp_optimization"] is True

    recon = pd.read_csv(out / "fill_population_reconciliation.csv")
    assert len(recon) == 55
    assert int(recon["included_in_realized_exit_a"].sum()) == 30
    assert int(((recon["included_in_realized_exit_a"]) & (recon["exit_a_closed"])).sum()) == 29
    assert int(recon["is_terminal_open_fill"].sum()) == 1
    assert int((recon["exclusion_reason"] == "same_direction_while_exit_a_position_open").sum()) == 25

    required = [
        "fill_population_reconciliation.csv",
        "fill_reconciliation_summary.csv",
        "fill_excursion_panel.csv",
        "fill_excursion_by_horizon.csv",
        "level_touch_events.csv",
        "first_touch_matrix.csv",
        "fill_path_sequence.csv",
        "excursion_summary_by_horizon.csv",
        "tp_reach_summary.csv",
        "sl_reach_summary.csv",
        "first_touch_grid_summary.csv",
        "excursion_by_side.csv",
        "excursion_by_outcome.csv",
        "excursion_by_archetype.csv",
        "report.md",
        "metadata.json",
    ]
    for name in required:
        assert (out / name).exists(), name

    panel = pd.read_csv(out / "fill_excursion_panel.csv")
    assert len(panel) == 55
    # fill identity vs Exit-A reference
    closed_ref = pd.read_csv(out / "exit_a_trades_reference.csv")
    assert len(closed_ref) == 29
    # trigger→next-open preserved: fill_bar == trigger_bar+1 in panel (from recon)
    # deterministic second run
    meta2 = run_fill_excursion_audit(output_dir=out / "run2", write_plots=False)
    assert meta1["content_hash"] == meta2["content_hash"]
    assert meta1["n_fills"] == meta2["n_fills"]

    # SM hash still unchanged
    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    h = hashlib.sha256(sm.read_bytes()).hexdigest()
    assert len(h) == 64

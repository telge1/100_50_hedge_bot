"""Tests for C3.5c APT trade case review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.regime_scanner.pullback_entry_c3_5c_trade_case_review import (
    DEFAULT_OUT,
    HYPOTHESIS_STATUSES,
    apply_diagnostic_flags,
    assign_archetypes,
    build_archetype_table,
    build_case_group_summary,
    build_manual_review_template,
    build_trade_case_index,
    compute_dev_thresholds,
    reconstruct_lifecycle_bars,
    run_trade_case_review,
    safe_trade_slug,
    write_case_package,
)


def _mini_closed(n: int = 8) -> pd.DataFrame:
    base = pd.Timestamp("2026-02-01", tz="UTC")
    rows = []
    rets = [12.0, 8.0, 5.0, 1.0, -1.0, -2.0, -4.0, -6.0]
    for i in range(n):
        side = "short" if i % 2 == 0 else "long"
        split = "development" if i < 5 else ("validation" if i < 7 else "oos")
        rows.append(
            {
                "trade_id": f"{(base + pd.Timedelta(days=i*7)).isoformat()}_{side}_{i+1}",
                "side": side,
                "split": split,
                "entry_timestamp": base + pd.Timedelta(days=i * 7),
                "exit_timestamp": base + pd.Timedelta(days=i * 7, hours=6),
                "holding_bars": 10 + i,
                "holding_minutes": (10 + i) * 15,
                "gross_return_pct": rets[i] + 0.2,
                "net_return_0_20_pct": rets[i],
                "net_return_020_pct": rets[i],
                "winner_net020": rets[i] > 0,
                "top1_trade": i == 0,
                "top3_trade": i < 3,
                "month": (base + pd.Timedelta(days=i * 7)).strftime("%Y-%m"),
                "entry_price": 1.0 + i * 0.01,
                "exit_price": 1.0,
                "setup_id": i + 1,
                "trigger_bar": 100 + i * 20,
                "fill_bar": 101 + i * 20,
                "mfe_pct": abs(rets[i]) + 1,
                "mae_pct": -1.0,
                "pullback_depth_atr": 1.0 + (i % 4) * 0.5,
                "pullback_duration_bars": 1 + (i % 3),
                "bars_arm_to_trigger": 5 + i,
                "bars_arm_to_pullback": 2,
                "bars_pullback_to_ready": 1,
                "bars_ready_to_trigger": 2 + i,
                "bars_since_external_bos": 3 + i,
                "bars_since_internal_bos": 1,
                "bars_since_choch": 4,
                "ret_3": 0.1 * i,
                "ret_5": 0.2 * i,
                "ret_10": 0.3 * i,
                "adx": 20 + i,
                "adx_change_5": 1.0 if i % 2 == 0 else -1.0,
                "adx_slope_5": 0.2 if i % 2 == 0 else -0.2,
                "di_alignment_age": 5,
                "di_spread_signed": 3.0,
                "ema9_minus_ema20_pct": -0.1,
                "ema20_minus_ema50_pct": -0.2,
                "cross_age_bars": 8,
                "rejection_wick_ratio": 0.3,
                "confirmation_body_ratio": 0.5,
                "chase_distance_atr": 0.4 + 0.2 * i,
                "vol_range_pct_5": 0.5,
                "vol_range_pct_10": 0.6,
                "atr_pct": 0.8,
                "major_direction": -1 if side == "short" else 1,
                "micro_direction": -1 if side == "short" else 1,
                "major_micro_alignment": 1.0,
                "regime": "trend",
                "closed": True,
            }
        )
    return pd.DataFrame(rows)


def test_output_path_and_no_filter_promotion() -> None:
    assert "c35c_trade_case_review" in str(DEFAULT_OUT)
    src = Path("research/regime_scanner/pullback_entry_c3_5c_trade_case_review.py").read_text()
    assert "no_filter_promotion" in src
    assert "filter recommendation" in src.lower()


def test_sm_untouched_hash() -> None:
    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    h1 = hashlib.sha256(sm.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_trade_case_review as mod

    _ = mod.DEFAULT_OUT
    assert hashlib.sha256(sm.read_bytes()).hexdigest() == h1
    assert "build_pullback_entry_pine" not in Path(mod.__file__).read_text()


def test_dev_thresholds_frozen_on_val() -> None:
    df = _mini_closed()
    thr = compute_dev_thresholds(df)
    flagged = apply_diagnostic_flags(df, thr)
    assert thr["fixed_before_flagging"] is True
    assert flagged["shallow_pullback_relative_to_dev"].dtype == bool
    q33 = thr["pullback_depth_atr_q33"]
    val = flagged[flagged["split"] == "validation"].iloc[0]
    assert bool(val["shallow_pullback_relative_to_dev"]) == (float(val["pullback_depth_atr"]) <= q33)


def test_lifecycle_and_fill_next_open() -> None:
    row = {
        "trigger_bar": 10,
        "fill_bar": 11,
        "holding_bars": 5,
        "bars_arm_to_trigger": 4,
        "bars_arm_to_pullback": 1,
        "bars_pullback_to_ready": 1,
        "bars_ready_to_trigger": 2,
    }
    life = reconstruct_lifecycle_bars(row)
    assert life["trigger_bar"] == 10
    assert life["fill_bar"] == 11
    assert life["fill_bar"] == life["trigger_bar"] + 1
    assert life["arm_bar"] == 6
    assert life["pullback_bar"] == 7
    assert life["ready_bar"] == 8
    assert life["exit_bar"] == 16


def test_top3_and_rank() -> None:
    flagged = apply_diagnostic_flags(_mini_closed(), compute_dev_thresholds(_mini_closed()))
    assert flagged["top1_trade"].sum() == 1
    assert flagged["top3_trade"].sum() == 3
    assert flagged.loc[flagged["rank_by_return"] == 1, "net_return_020_pct"].iloc[0] == 12.0


def test_direction_archetypes_long_short() -> None:
    short_row = _mini_closed().iloc[0].to_dict()
    short_row.update(
        {
            "side": "short",
            "major_direction": -1,
            "shallow_pullback_relative_to_dev": True,
            "slow_setup_relative_to_dev": True,
            "low_chase_relative_to_dev": True,
            "deep_pullback_relative_to_dev": False,
            "fast_setup_relative_to_dev": False,
            "high_chase_relative_to_dev": False,
            "adx_rising_5": True,
            "adx_falling_5": False,
            "major_micro_alignment": 1.0,
        }
    )
    tags = assign_archetypes(short_row)
    assert "short_trend_continuation" in tags
    assert "shallow_slow_low_chase" in tags
    long_ct = dict(short_row)
    long_ct["side"] = "long"
    long_ct["major_direction"] = -1
    assert "long_countertrend" in assign_archetypes(long_ct)


def test_case_package_deterministic(tmp_path: Path) -> None:
    df = apply_diagnostic_flags(_mini_closed(), compute_dev_thresholds(_mini_closed()))
    row = df.iloc[0].to_dict()
    thr = compute_dev_thresholds(_mini_closed())
    write_case_package(tmp_path / "cases", row, thr, frame=None)
    p = tmp_path / "cases" / f"trade_{row['case_slug']}" / "case_summary.json"
    s1 = json.loads(p.read_text())
    write_case_package(tmp_path / "cases", row, thr, frame=None)
    s2 = json.loads(p.read_text())
    assert s1 == s2
    assert s1["no_strategy_decision"] is True
    notes = (tmp_path / "cases" / f"trade_{row['case_slug']}" / "case_notes.md").read_text()
    assert "hätte gefiltert" not in notes.lower()


def test_index_groups_archetypes_manual() -> None:
    flagged = apply_diagnostic_flags(_mini_closed(), compute_dev_thresholds(_mini_closed()))
    idx = build_trade_case_index(flagged)
    assert "entry_time" in idx.columns and "exit_time" in idx.columns
    assert len(idx) == len(flagged)
    g = build_case_group_summary(flagged)
    assert not g.empty
    assert set(g["group_name"]) >= {"winner_vs_loser", "side", "split"}
    arch = build_archetype_table(flagged)
    assert "archetype" in arch.columns
    man = build_manual_review_template(flagged)
    assert man["reviewer_notes"].eq("").all()
    assert man["reviewer_keep_for_hypothesis"].eq("").all()


def test_hypothesis_statuses_allowed() -> None:
    flagged = apply_diagnostic_flags(_mini_closed(), compute_dev_thresholds(_mini_closed()))
    arch = build_archetype_table(flagged)
    groups = build_case_group_summary(flagged)
    from research.regime_scanner.pullback_entry_c3_5c_trade_case_review import _hypothesis_table

    hyps = _hypothesis_table(flagged, arch, groups)
    for h in hyps:
        assert h["status"] in HYPOTHESIS_STATUSES


def test_slug_stable() -> None:
    tid = "2026-01-26T07:15:00+00:00_long_1"
    assert safe_trade_slug(tid) == safe_trade_slug(tid)
    assert ":" not in safe_trade_slug(tid)


def test_run_on_real_pattern_artifacts_no_charts(tmp_path: Path) -> None:
    pattern = Path(
        "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
        "c35c_pattern_diagnostic_audit"
    )
    if not (pattern / "trade_feature_panel.csv").exists():
        return
    out = tmp_path / "review"
    meta = run_trade_case_review(
        pattern_dir=pattern,
        output_dir=out,
        write_charts=False,
        load_frame=False,
    )
    assert meta["n_closed"] == 29
    assert meta["closed_count_matches_29"] is True
    assert meta["n_case_packages"] == 29
    idx = pd.read_csv(out / "trade_case_index.csv")
    panel = pd.read_csv(pattern / "trade_feature_panel.csv")
    closed = panel[panel["closed"] == True]  # noqa: E712
    assert set(idx["trade_id"]) == set(closed["trade_id"])
    m = idx.merge(closed[["trade_id", "net_return_0_20_pct"]], on="trade_id", how="left")
    assert np.allclose(m["net_return_020_pct"], m["net_return_0_20_pct"])
    meta2 = run_trade_case_review(
        pattern_dir=pattern,
        output_dir=out,
        write_charts=False,
        load_frame=False,
    )
    assert meta["content_hash"] == meta2["content_hash"]
    assert (out / "report.md").exists()
    assert (out / "outlier_cases.md").exists()
    assert len(list((out / "cases").glob("trade_*/case_summary.json"))) == 29

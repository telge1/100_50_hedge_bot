"""Tests for offline liquidation exhaustion evaluation (no DB / no full CSVs)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.liquidation_exhaustion.evaluate_core import (
    assign_split,
    build_ids,
    candidate_variant_id,
    first_touch_summary,
    mfe_mae_table,
    physical_anchor_id,
    physical_burst_anchors,
    reconstruct_exit_series,
)
from research.regime_scanner.liquidation_exhaustion.evaluate import candidate_matrix_and_gates
from research.regime_scanner.run_liquidation_exhaustion_evaluation import main as eval_main


def test_physical_anchor_id_stable():
    a = physical_anchor_id("BTCUSDT", "long", 1, "2026-04-01 00:00:00+00:00")
    b = physical_anchor_id("BTCUSDT", "long", 1, "2026-04-01 00:00:00+00:00")
    assert a == b
    assert a != physical_anchor_id("BTCUSDT", "short", 1, "2026-04-01 00:00:00+00:00")


def test_variant_id_separates_burst_and_reclaim():
    base = "BTCUSDT|long|1|t0"
    burst = candidate_variant_id(
        base_event_id=base, burst="B1", price="P1", oi="O0", reclaim="none", reclaim_window=0, entry_mode="burst_next_open"
    )
    reclaim = candidate_variant_id(
        base_event_id=base, burst="B1", price="P1", oi="O0", reclaim="R1", reclaim_window=2, entry_mode="reclaim_next_open"
    )
    assert burst != reclaim


def test_assign_split_boundaries():
    assert assign_split(pd.Timestamp("2026-03-20", tz="UTC")) == "dev"
    assert assign_split(pd.Timestamp("2026-04-10", tz="UTC")) == "validation"
    assert assign_split(pd.Timestamp("2026-04-25", tz="UTC")) == "oos"


def test_mfe_sample_size_uses_unique_candidates():
    rows = []
    for i in range(5):
        rows.append(
            {
                "symbol": "BTCUSDT",
                "side": "long",
                "burst": "B1",
                "price": "P1",
                "oi": "O0",
                "anchor_bucket": "2026-03-20 00:00:00+00:00",
                "sequence_id": 1,
                "entry_mode": "burst_next_open",
                "candidate_variant_id": "candA",
                "base_event_id": "baseA",
                "h12_mfe_pct": 1.0,
                "h12_mae_pct": -0.5,
                "h12_close_ret": 0.2,
                "favorable_first": True,
                "adverse_first": False,
                "same_bar_ambiguous": False,
                "reclaim": np.nan,
                "reclaim_window": np.nan,
            }
        )
    # duplicate candidate rows should not inflate unique_candidates
    df = pd.DataFrame(rows)
    out = mfe_mae_table(df, horizon=12)
    all_row = out[(out["group_type"] == "all") & (out["horizon"] == 12)].iloc[0]
    assert all_row["unique_candidates"] == 1
    assert all_row["unique_physical_events"] == 1


def test_same_bar_adverse_in_first_touch():
    df = pd.DataFrame(
        [
            {
                "symbol": "ETHUSDT",
                "side": "long",
                "burst": "B1",
                "price": "P1",
                "oi": "O0",
                "entry_mode": "burst_next_open",
                "candidate_variant_id": "c1",
                "base_event_id": "b1",
                "reclaim": np.nan,
                "ft_p0_50_reached": True,
                "ft_m0_50_reached": True,
                "ft_p0_50_bars": 0,
                "ft_m0_50_bars": 0,
                "favorable_first": False,
                "adverse_first": True,
                "same_bar_ambiguous": True,
            }
        ]
    )
    # add other level cols empty
    for lvl in ["0_25", "0_75", "1_00", "1_50", "2_00"]:
        df[f"ft_p{lvl}_reached"] = False
        df[f"ft_m{lvl}_reached"] = False
        df[f"ft_p{lvl}_bars"] = np.nan
        df[f"ft_m{lvl}_bars"] = np.nan
    for lvl in ["0_5", "1_0", "1_5", "2_0"]:
        df[f"ft_atrp{lvl}_reached"] = False
        df[f"ft_atrm{lvl}_reached"] = False
        df[f"ft_atrp{lvl}_bars"] = np.nan
        df[f"ft_atrm{lvl}_bars"] = np.nan
    ft = first_touch_summary(df)
    row = ft[(ft["level"] == "0.50%") & (ft["group_type"] == "all")].iloc[0]
    assert row["same_bar_ambiguity_pct"] == 100.0
    assert row["adverse_first_pct"] == 100.0


def test_exit_reconstruction_tp_before_sl():
    df = pd.DataFrame(
        {
            "ft_p0_50_reached": [True],
            "ft_m0_50_reached": [True],
            "ft_p0_50_bars": [1],
            "ft_m0_50_bars": [3],
            "ft_p0_75_reached": [False],
            "ft_m0_75_reached": [False],
            "ft_p0_75_bars": [np.nan],
            "ft_m0_75_bars": [np.nan],
            "ft_p1_00_reached": [False],
            "ft_m1_00_reached": [False],
            "ft_p1_00_bars": [np.nan],
            "ft_m1_00_bars": [np.nan],
            "ft_p1_50_reached": [False],
            "ft_p1_50_bars": [np.nan],
            "h12_close_ret": [0.0],
        }
    )
    res = reconstruct_exit_series(df, tp=0.5, sl=0.5, hold=12, cost=0.25)
    assert res.iloc[0]["reason"] == "TP"
    assert res.iloc[0]["net_pct"] == pytest.approx(0.25)


def test_gates_require_physical_count():
    # tiny synthetic outcomes for matrix/gates
    rows = []
    for i in range(20):
        rows.append(
            {
                "symbol": f"C{i%3}USDT",
                "side": "long",
                "burst": "B1",
                "price": "P1",
                "oi": "O0",
                "anchor_bucket": f"2026-03-20 00:{i:02d}:00+00:00",
                "sequence_id": 1,
                "entry_mode": "burst_next_open",
                "candidate_variant_id": f"cand{i}",
                "base_event_id": f"base{i}",
                "split": "dev",
                "reclaim": np.nan,
                "reclaim_window": np.nan,
                "h12_mfe_pct": 1.0,
                "h12_mae_pct": -0.2,
                "h12_close_ret": 0.3,
                "favorable_first": True,
                "adverse_first": False,
                "ft_p0_50_reached": True,
                "ft_m0_50_reached": False,
                "ft_p0_50_bars": 1,
                "ft_m0_50_bars": np.nan,
                "ft_p0_75_reached": False,
                "ft_m0_75_reached": False,
                "ft_p0_75_bars": np.nan,
                "ft_m0_75_bars": np.nan,
                "ft_p1_00_reached": False,
                "ft_m1_00_reached": False,
                "ft_p1_00_bars": np.nan,
                "ft_m1_00_bars": np.nan,
                "ft_p1_50_reached": False,
                "ft_p1_50_bars": np.nan,
            }
        )
    df = pd.DataFrame(rows)
    matrix, gates = candidate_matrix_and_gates(df)
    assert not gates.empty
    assert bool(gates.iloc[0]["min_100_physical"]) is False
    assert bool(gates.iloc[0]["hard_pass"]) is False


def test_eval_cli_missing_dir(tmp_path):
    rc = eval_main(["--input-dir", str(tmp_path / "nope"), "--output-dir", str(tmp_path), "--mode", "full"])
    assert rc == 2

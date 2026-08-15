"""Full offline evaluation pipeline + report generation."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.evaluate_core import (
    GATE_HORIZON,
    PRIMARY_HORIZON,
    _dedupe_candidates,
    _sha256_file,
    assign_split,
    build_ids,
    first_touch_summary,
    fixed_exit_summary,
    load_inputs,
    mfe_mae_table,
    multiplicity_summary,
    physical_burst_anchors,
    reconstruct_exit_series,
)


def oi_condition_comparison(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = _dedupe_candidates(outcomes.copy())
    o = o[o["entry_mode"] == "burst_next_open"].copy()  # comparable entry mode
    h = PRIMARY_HORIZON
    if "ft_p0_50_reached" in o.columns:
        o["_net"] = reconstruct_exit_series(o, tp=0.50, sl=0.50, hold=12, cost=0.25)["net_pct"].to_numpy()
    else:
        o["_net"] = np.nan
    o["_mfe"] = pd.to_numeric(o[f"h{h}_mfe_pct"], errors="coerce")
    o["_mae"] = pd.to_numeric(o[f"h{h}_mae_pct"], errors="coerce")
    o["_edge"] = o["_mfe"] - o["_mae"].abs()
    o["_ff"] = o["favorable_first"].astype(str).str.lower().isin(["true", "1"])
    o["_af"] = o["adverse_first"].astype(str).str.lower().isin(["true", "1"])
    o["_ret"] = pd.to_numeric(o[f"h{h}_close_ret"], errors="coerce")
    rows = []
    for oi in sorted(o["oi"].dropna().unique()):
        g = o[o["oi"] == oi]
        coin_edge = g.groupby("symbol")["_edge"].median()
        split_stats = {}
        for sp, sg in g.groupby("split"):
            split_stats[f"{sp}_n"] = int(sg["base_event_id"].nunique())
            split_stats[f"{sp}_edge_med"] = float(sg["_edge"].median())
        rows.append(
            {
                "oi": oi,
                "unique_physical": int(g["base_event_id"].nunique()),
                "unique_candidates": int(g["candidate_variant_id"].nunique()),
                "median_mfe": float(g["_mfe"].median()),
                "median_mae": float(g["_mae"].median()),
                "median_edge": float(g["_edge"].median()),
                "fav_first_pct": float(g["_ff"].mean() * 100),
                "adv_first_pct": float(g["_af"].mean() * 100),
                "median_close_ret": float(g["_ret"].median()),
                "exit_x1_h12_c025_expectancy": float(g["_net"].mean()) if g["_net"].notna().any() else np.nan,
                "equal_coin_edge": float(coin_edge.mean()) if len(coin_edge) else np.nan,
                "median_coin_edge": float(coin_edge.median()) if len(coin_edge) else np.nan,
                **split_stats,
            }
        )
    # paired subset: O1 vs O0 same base+burst+price
    o0 = o[o["oi"] == "O0"][["base_event_id", "burst", "price", "_edge"]].copy()
    o1 = o[o["oi"] == "O1"][["base_event_id", "burst", "price", "_edge"]].copy()
    paired = o0.merge(o1, on=["base_event_id", "burst", "price"], suffixes=("_O0", "_O1"))
    if len(paired):
        rows.append(
            {
                "oi": "paired_O1_minus_O0",
                "unique_physical": int(paired["base_event_id"].nunique()),
                "unique_candidates": int(len(paired)),
                "median_mfe": np.nan,
                "median_mae": np.nan,
                "median_edge": float((paired["_edge_O1"] - paired["_edge_O0"]).median()),
                "fav_first_pct": np.nan,
                "adv_first_pct": np.nan,
                "median_close_ret": np.nan,
                "exit_x1_h12_c025_expectancy": np.nan,
                "equal_coin_edge": np.nan,
                "median_coin_edge": np.nan,
                "note": "positive means O1 better edge than matched O0 parent",
            }
        )
    return pd.DataFrame(rows)


def reclaim_comparison(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = _dedupe_candidates(outcomes.copy())
    if "ft_p0_50_reached" in o.columns:
        o["_net"] = reconstruct_exit_series(o, tp=0.50, sl=0.50, hold=12, cost=0.25)["net_pct"].to_numpy()
    else:
        o["_net"] = np.nan
    h = PRIMARY_HORIZON
    o["_mfe"] = pd.to_numeric(o[f"h{h}_mfe_pct"], errors="coerce")
    o["_mae"] = pd.to_numeric(o[f"h{h}_mae_pct"], errors="coerce")
    o["_edge"] = o["_mfe"] - o["_mae"].abs()
    o["_ff"] = o["favorable_first"].astype(str).str.lower().isin(["true", "1"])
    o["_af"] = o["adverse_first"].astype(str).str.lower().isin(["true", "1"])
    o["_ret"] = pd.to_numeric(o[f"h{h}_close_ret"], errors="coerce")
    rows = []
    burst = o[o["entry_mode"] == "burst_next_open"]
    reclaim = o[o["entry_mode"] == "reclaim_next_open"]

    def _stats(g: pd.DataFrame, label: str) -> dict[str, Any]:
        return {
            "group": label,
            "unique_physical": int(g["base_event_id"].nunique()),
            "unique_candidates": int(g["candidate_variant_id"].nunique()),
            "median_mfe": float(g["_mfe"].median()) if len(g) else np.nan,
            "median_mae": float(g["_mae"].median()) if len(g) else np.nan,
            "median_edge": float(g["_edge"].median()) if len(g) else np.nan,
            "fav_minus_adv": float((g["_ff"].mean() - g["_af"].mean()) * 100) if len(g) else np.nan,
            "median_close_ret": float(g["_ret"].median()) if len(g) else np.nan,
            "exit_expectancy": float(g["_net"].mean()) if len(g) and g["_net"].notna().any() else np.nan,
            "mean_bars_to_reclaim": float(pd.to_numeric(g.get("bars_to_reclaim"), errors="coerce").mean())
            if "bars_to_reclaim" in g.columns
            else np.nan,
        }

    rows.append(_stats(burst, "burst_only"))
    for r in sorted(reclaim["reclaim"].dropna().unique()):
        rows.append(_stats(reclaim[reclaim["reclaim"] == r], f"reclaim_{r}"))
    for w in sorted(reclaim["reclaim_window"].dropna().unique()):
        rows.append(_stats(reclaim[reclaim["reclaim_window"] == w], f"window_{int(w)}"))

    # sample loss: share of physical anchors with any reclaim
    phys_b = set(burst["base_event_id"])
    phys_r = set(reclaim["base_event_id"])
    rows.append(
        {
            "group": "sample_loss",
            "unique_physical": len(phys_b),
            "unique_candidates": len(phys_r),
            "median_mfe": np.nan,
            "median_mae": np.nan,
            "median_edge": np.nan,
            "fav_minus_adv": np.nan,
            "median_close_ret": np.nan,
            "exit_expectancy": np.nan,
            "mean_bars_to_reclaim": np.nan,
            "reclaim_coverage_of_burst_physical": float(len(phys_b & phys_r) / len(phys_b)) if phys_b else np.nan,
        }
    )
    return pd.DataFrame(rows)


def control_group_comparison(outcomes: pd.DataFrame, controls: pd.DataFrame) -> pd.DataFrame:
    """Compare H1 reclaim/burst candidates vs control labels (diagnostic counts)."""
    o = _dedupe_candidates(outcomes.copy())
    h = PRIMARY_HORIZON
    rows = []
    for mode, g in (
        ("H1_burst_only", o[o["entry_mode"] == "burst_next_open"]),
        ("H1_reclaim", o[o["entry_mode"] == "reclaim_next_open"]),
    ):
        mfe = pd.to_numeric(g[f"h{h}_mfe_pct"], errors="coerce")
        mae = pd.to_numeric(g[f"h{h}_mae_pct"], errors="coerce")
        ff = g["favorable_first"].astype(str).str.lower().isin(["true", "1"])
        af = g["adverse_first"].astype(str).str.lower().isin(["true", "1"])
        rows.append(
            {
                "group": mode,
                "n_rows": int(len(g)),
                "unique_physical": int(g["base_event_id"].nunique()),
                "n_coins": int(g["symbol"].nunique()),
                "median_edge": float((mfe - mae.abs()).median()),
                "fav_minus_adv": float((ff.mean() - af.mean()) * 100),
            }
        )
    # controls: only counts / distribution (no outcomes attached in CSV)
    for c, g in controls.groupby("control"):
        rows.append(
            {
                "group": f"control_{c}",
                "n_rows": int(len(g)),
                "unique_physical": int(g[["symbol", "bucket_start"]].drop_duplicates().shape[0]),
                "n_coins": int(g["symbol"].nunique()),
                "median_edge": np.nan,
                "fav_minus_adv": np.nan,
                "note": "controls lack forward outcomes in full-run CSV; count/distribution only",
            }
        )
    # C3 = burst physical without reclaim
    burst_ids = set(o.loc[o["entry_mode"] == "burst_next_open", "base_event_id"])
    reclaim_ids = set(o.loc[o["entry_mode"] == "reclaim_next_open", "base_event_id"])
    c3 = burst_ids - reclaim_ids
    rows.append(
        {
            "group": "C3_burst_without_any_reclaim",
            "n_rows": int(len(c3)),
            "unique_physical": int(len(c3)),
            "n_coins": np.nan,
            "median_edge": np.nan,
            "fav_minus_adv": np.nan,
            "note": "physical anchors with burst outcome but no reclaim outcome row",
        }
    )
    return pd.DataFrame(rows)


def coin_direction_summaries(outcomes: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    o = _dedupe_candidates(outcomes.copy())
    h = PRIMARY_HORIZON
    if "ft_p0_50_reached" in o.columns:
        o["_net"] = reconstruct_exit_series(o, tp=0.5, sl=0.5, hold=12, cost=0.25)["net_pct"].to_numpy()
    else:
        o["_net"] = np.nan
    o["_mfe"] = pd.to_numeric(o[f"h{h}_mfe_pct"], errors="coerce")
    o["_mae"] = pd.to_numeric(o[f"h{h}_mae_pct"], errors="coerce")
    o["_edge"] = o["_mfe"] - o["_mae"].abs()
    o["_ff"] = o["favorable_first"].astype(str).str.lower().isin(["true", "1"])
    o["_af"] = o["adverse_first"].astype(str).str.lower().isin(["true", "1"])

    def _agg(g: pd.DataFrame) -> dict[str, Any]:
        net = g["_net"]
        pf_num = float(net[net > 0].sum()) if net.notna().any() else np.nan
        pf_den = float(abs(net[net <= 0].sum())) if net.notna().any() else np.nan
        return {
            "unique_physical": int(g["base_event_id"].nunique()),
            "unique_candidates": int(g["candidate_variant_id"].nunique()),
            "median_mfe": float(g["_mfe"].median()),
            "median_mae": float(g["_mae"].median()),
            "median_edge": float(g["_edge"].median()),
            "fav_minus_adv": float((g["_ff"].mean() - g["_af"].mean()) * 100),
            "exit_expectancy": float(net.mean()) if net.notna().any() else np.nan,
            "exit_pf": float(pf_num / pf_den) if pf_den and pf_den > 1e-12 else np.nan,
            "reclaim_rate": float((g["entry_mode"] == "reclaim_next_open").mean()),
            "oi_down_share_O1plus": float(g["oi"].isin(["O1", "O2", "O3"]).mean()),
        }

    coin_rows = []
    for (sym, side), g in o.groupby(["symbol", "side"]):
        coin_rows.append({"symbol": sym, "side": side, **_agg(g)})
    for sym, g in o.groupby("symbol"):
        coin_rows.append({"symbol": sym, "side": "both", **_agg(g)})
    coin = pd.DataFrame(coin_rows)

    dir_rows = []
    for side, g in o.groupby("side"):
        dir_rows.append({"side": side, **_agg(g)})
    direction = pd.DataFrame(dir_rows)

    # equal / median coin over side=both
    both = coin[coin["side"] == "both"]
    equal = pd.DataFrame(
        [
            {
                "metric": "equal_coin_median_edge",
                "value": float(both["median_edge"].mean()) if len(both) else np.nan,
            },
            {
                "metric": "equal_coin_exit_expectancy",
                "value": float(both["exit_expectancy"].mean()) if len(both) else np.nan,
            },
            {
                "metric": "median_coin_median_edge",
                "value": float(both["median_edge"].median()) if len(both) else np.nan,
            },
            {
                "metric": "median_coin_exit_expectancy",
                "value": float(both["exit_expectancy"].median()) if len(both) else np.nan,
            },
            {
                "metric": "pooled_median_edge",
                "value": float(o["_edge"].median()),
            },
        ]
    )
    median_coin = both.sort_values("median_edge")[
        ["symbol", "unique_physical", "median_edge", "exit_expectancy", "fav_minus_adv"]
    ].reset_index(drop=True)
    return coin, direction, equal, median_coin


def split_summary(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = _dedupe_candidates(outcomes.copy())
    h = PRIMARY_HORIZON
    if "ft_p0_50_reached" in o.columns:
        o["_net"] = reconstruct_exit_series(o, tp=0.5, sl=0.5, hold=12, cost=0.25)["net_pct"].to_numpy()
    else:
        o["_net"] = np.nan
    o["_edge"] = pd.to_numeric(o[f"h{h}_mfe_pct"], errors="coerce") - pd.to_numeric(
        o[f"h{h}_mae_pct"], errors="coerce"
    ).abs()
    o["_ff"] = o["favorable_first"].astype(str).str.lower().isin(["true", "1"])
    o["_af"] = o["adverse_first"].astype(str).str.lower().isin(["true", "1"])
    rows = []
    for (burst, oi, entry_mode, side, sp), g in o.groupby(
        ["burst", "oi", "entry_mode", "side", "split"], dropna=False
    ):
        coin_edge = g.groupby("symbol")["_edge"].median()
        rows.append(
            {
                "burst": burst,
                "oi": oi,
                "entry_mode": entry_mode,
                "side": side,
                "split": sp,
                "unique_physical": int(g["base_event_id"].nunique()),
                "unique_candidates": int(g["candidate_variant_id"].nunique()),
                "median_edge": float(g["_edge"].median()),
                "fav_minus_adv": float((g["_ff"].mean() - g["_af"].mean()) * 100),
                "exit_expectancy": float(g["_net"].mean()) if g["_net"].notna().any() else np.nan,
                "equal_coin_edge": float(coin_edge.mean()) if len(coin_edge) else np.nan,
            }
        )
    return pd.DataFrame(rows)


def top_coin_ablation(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = _dedupe_candidates(outcomes.copy())
    h = PRIMARY_HORIZON
    if "ft_p0_50_reached" in o.columns:
        o["_net"] = reconstruct_exit_series(o, tp=0.5, sl=0.5, hold=12, cost=0.25)["net_pct"].to_numpy()
    else:
        o["_net"] = np.nan
    o["_edge"] = pd.to_numeric(o[f"h{h}_mfe_pct"], errors="coerce") - pd.to_numeric(
        o[f"h{h}_mae_pct"], errors="coerce"
    ).abs()
    # best coins from DEV only by median edge on burst_only B1 O0 long+short pooled
    dev = o[(o["split"] == "dev") & (o["entry_mode"] == "burst_next_open") & (o["burst"] == "B1") & (o["oi"] == "O0")]
    ranking = dev.groupby("symbol")["_edge"].median().sort_values(ascending=False)
    best = list(ranking.index[:1])
    top3 = list(ranking.index[:3])

    rows = []
    scopes = {
        "all": o,
        "without_top1_dev": o[~o["symbol"].isin(best)],
        "without_top3_dev": o[~o["symbol"].isin(top3)],
        "without_btc": o[o["symbol"] != "BTCUSDT"],
        "without_btc_eth": o[~o["symbol"].isin(["BTCUSDT", "ETHUSDT"])],
        "only_btc_eth": o[o["symbol"].isin(["BTCUSDT", "ETHUSDT"])],
    }
    for name, g in scopes.items():
        if g.empty:
            continue
        for mode in ("burst_next_open", "reclaim_next_open"):
            gg = g[g["entry_mode"] == mode]
            if gg.empty:
                continue
            rows.append(
                {
                    "scope": name,
                    "entry_mode": mode,
                    "dev_best_coin": best[0] if best else "",
                    "dev_top3": "|".join(top3),
                    "unique_physical": int(gg["base_event_id"].nunique()),
                    "n_coins": int(gg["symbol"].nunique()),
                    "median_edge": float(gg["_edge"].median()),
                    "exit_expectancy": float(gg["_net"].mean()) if gg["_net"].notna().any() else np.nan,
                }
            )
    return pd.DataFrame(rows)


def candidate_matrix_and_gates(outcomes: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    o = _dedupe_candidates(outcomes.copy())
    h = GATE_HORIZON
    rows = []
    # collapse reclaim window to primary set; include none for burst
    o["reclaim_key"] = np.where(
        o["entry_mode"] == "burst_next_open",
        "none",
        o["reclaim"].fillna("none").astype(str)
        + "w"
        + o["reclaim_window"].fillna(0).astype(int).astype(str),
    )
    # precompute exit once for all rows (X1 / hold12 / cost 0.25)
    ex_all = reconstruct_exit_series(o, tp=0.5, sl=0.5, hold=12, cost=0.25)
    o = o.copy()
    o["_net"] = ex_all["net_pct"].to_numpy()
    o["_mfe"] = pd.to_numeric(o[f"h{h}_mfe_pct"], errors="coerce")
    o["_mae"] = pd.to_numeric(o[f"h{h}_mae_pct"], errors="coerce")
    o["_edge"] = o["_mfe"] - o["_mae"].abs()
    o["_ff"] = o["favorable_first"].astype(str).str.lower().isin(["true", "1"])
    o["_af"] = o["adverse_first"].astype(str).str.lower().isin(["true", "1"])

    for (burst, price, oi, reclaim_key, side), g in o.groupby(
        ["burst", "price", "oi", "reclaim_key", "side"], dropna=False, sort=False
    ):
        edge = g["_edge"]
        net = g["_net"]
        pf_num = float(net[net > 0].sum())
        pf_den = float(abs(net[net <= 0].sum()))
        pf = float(pf_num / pf_den) if pf_den > 1e-12 else np.nan
        coin_counts = g.groupby("symbol")["base_event_id"].nunique()
        max_share = float(coin_counts.max() / coin_counts.sum()) if coin_counts.sum() else np.nan
        coin_edge = g.groupby("symbol")["_edge"].median()
        split_edge = g.groupby("split")["_edge"].median().to_dict()
        # ablation without top coin from DEV of this group
        dev = g[g["split"] == "dev"]
        if len(dev):
            ranking = dev.groupby("symbol")["_edge"].median().sort_values(ascending=False)
            top1 = list(ranking.index[:1])
            top3 = list(ranking.index[:3])
        else:
            top1, top3 = [], []
        without_top1 = g[~g["symbol"].isin(top1)] if top1 else g
        without_top3 = g[~g["symbol"].isin(top3)] if top3 else g

        def _med_edge(gg: pd.DataFrame) -> float:
            if gg.empty:
                return np.nan
            return float(gg["_edge"].median())

        rows.append(
            {
                "burst": burst,
                "price": price,
                "oi": oi,
                "reclaim": reclaim_key,
                "side": side,
                "exit_model": "X1",
                "cost": 0.25,
                "hold": 12,
                "physical_events": int(g["base_event_id"].nunique()),
                "candidate_count": int(g["candidate_variant_id"].nunique()),
                "coins": int(g["symbol"].nunique()),
                "max_coin_share": max_share,
                "median_edge": float(edge.median()),
                "fav_minus_adv": float((g["_ff"].mean() - g["_af"].mean()) * 100),
                "expectancy": float(net.mean()),
                "profit_factor": pf,
                "equal_coin_edge": float(coin_edge.mean()) if len(coin_edge) else np.nan,
                "median_coin_edge": float(coin_edge.median()) if len(coin_edge) else np.nan,
                "dev_edge": split_edge.get("dev", np.nan),
                "validation_edge": split_edge.get("validation", np.nan),
                "oos_edge": split_edge.get("oos", np.nan),
                "without_top1_edge": _med_edge(without_top1),
                "without_top3_edge": _med_edge(without_top3),
            }
        )
    matrix = pd.DataFrame(rows)

    # gates
    gate_rows = []
    for _, r in matrix.iterrows():
        checks = {
            "min_100_physical": r["physical_events"] >= 100,
            "min_8_coins": r["coins"] >= 8,
            "max_coin_share_le_25": r["max_coin_share"] <= 0.25 if pd.notna(r["max_coin_share"]) else False,
            "mfe_gt_abs_mae": r["median_edge"] > 0 if pd.notna(r["median_edge"]) else False,
            "fav_gt_adv": r["fav_minus_adv"] > 0 if pd.notna(r["fav_minus_adv"]) else False,
            "expectancy_pos_at_0_25": r["expectancy"] > 0 if pd.notna(r["expectancy"]) else False,
            "pf_gt_1": r["profit_factor"] > 1 if pd.notna(r["profit_factor"]) else False,
            "equal_coin_pos": r["equal_coin_edge"] > 0 if pd.notna(r["equal_coin_edge"]) else False,
            "median_coin_not_clearly_neg": r["median_coin_edge"] >= -0.05
            if pd.notna(r["median_coin_edge"])
            else False,
            "val_oos_not_both_neg": not (
                (r["validation_edge"] < 0 if pd.notna(r["validation_edge"]) else False)
                and (r["oos_edge"] < 0 if pd.notna(r["oos_edge"]) else False)
            ),
            "without_top1_not_clearly_neg": r["without_top1_edge"] >= -0.05
            if pd.notna(r["without_top1_edge"])
            else False,
            "without_top3_not_clearly_neg": r["without_top3_edge"] >= -0.05
            if pd.notna(r["without_top3_edge"])
            else False,
        }
        hard = [
            "min_100_physical",
            "min_8_coins",
            "max_coin_share_le_25",
            "mfe_gt_abs_mae",
            "fav_gt_adv",
            "expectancy_pos_at_0_25",
            "pf_gt_1",
            "equal_coin_pos",
            "val_oos_not_both_neg",
            "without_top1_not_clearly_neg",
            "without_top3_not_clearly_neg",
        ]
        diagnostic = ["median_coin_not_clearly_neg"]
        hard_pass = all(checks[k] for k in hard)
        gate_rows.append(
            {
                **{k: r[k] for k in ("burst", "price", "oi", "reclaim", "side")},
                **checks,
                "hard_pass": hard_pass,
                "diagnostic_pass": all(checks[k] for k in diagnostic),
                "all_pass": hard_pass and all(checks[k] for k in diagnostic),
                "n_hard_failed": sum(1 for k in hard if not checks[k]),
            }
        )
    gates = pd.DataFrame(gate_rows)
    matrix = matrix.merge(
        gates[["burst", "price", "oi", "reclaim", "side", "hard_pass", "all_pass", "n_hard_failed"]],
        on=["burst", "price", "oi", "reclaim", "side"],
        how="left",
    )
    return matrix, gates


def write_full_report(
    out_dir: Path,
    *,
    phys: pd.DataFrame,
    mult: pd.DataFrame,
    mfe: pd.DataFrame,
    ft: pd.DataFrame,
    oi: pd.DataFrame,
    reclaim: pd.DataFrame,
    exits: pd.DataFrame,
    controls: pd.DataFrame,
    coin: pd.DataFrame,
    direction: pd.DataFrame,
    equal: pd.DataFrame,
    splits: pd.DataFrame,
    ablation: pd.DataFrame,
    matrix: pd.DataFrame,
    gates: pd.DataFrame,
) -> str:
    n_phys = int(len(phys))
    n_pass = int(gates["all_pass"].sum()) if len(gates) else 0
    n_hard = int(gates["hard_pass"].sum()) if len(gates) else 0
    long_n = int((phys["side"] == "long").sum())
    short_n = int((phys["side"] == "short").sum())
    eq_edge = float(equal.loc[equal["metric"] == "equal_coin_median_edge", "value"].iloc[0]) if len(equal) else np.nan
    med_edge = float(equal.loc[equal["metric"] == "median_coin_median_edge", "value"].iloc[0]) if len(equal) else np.nan

    # strongest burst by median edge burst_only
    burst_rows = mfe[(mfe["group_type"] == "burst") & (mfe["horizon"] == PRIMARY_HORIZON)]
    best_burst = burst_rows.sort_values("median_mfe_minus_abs_mae", ascending=False).head(1)
    best_burst_s = str(best_burst.iloc[0]["group_key"]) if len(best_burst) else "n/a"

    oi_o0 = oi[oi["oi"] == "O0"]
    oi_o1 = oi[oi["oi"] == "O1"]
    oi_improve = (
        float(oi_o1.iloc[0]["median_edge"] - oi_o0.iloc[0]["median_edge"])
        if len(oi_o0) and len(oi_o1)
        else np.nan
    )
    paired = oi[oi["oi"] == "paired_O1_minus_O0"]
    paired_edge = float(paired.iloc[0]["median_edge"]) if len(paired) else np.nan

    rc_burst = reclaim[reclaim["group"] == "burst_only"]
    rc_best = reclaim[reclaim["group"].astype(str).str.startswith("reclaim_")].sort_values(
        "median_edge", ascending=False
    )
    best_rc = str(rc_best.iloc[0]["group"]) if len(rc_best) else "n/a"

    exit_ok = exits[(exits["scope"] == "all") & (exits["cost_pct"] == 0.25) & (exits["expectancy_pct"] > 0)]

    decision = "REJECT_LIQUIDATION_EXHAUSTION_REVERSAL"
    if n_pass > 0:
        decision = "PROCEED_TO_FIXED_ENTRY_STRATEGY"
    elif n_hard > 0 or (pd.notna(eq_edge) and eq_edge > 0 and n_phys >= 100):
        decision = "EVENT_EDGE_INTERESTING_BUT_INSUFFICIENT"

    report = f"""# Liquidation Exhaustion Reversal — Full Audit Report

## 1. Executive Summary

- Physical burst anchors: **{n_phys}** (long {long_n} / short {short_n})
- Hard gate passes: **{n_hard}**; all-gate passes: **{n_pass}**
- Equal-coin median edge (h{PRIMARY_HORIZON}): **{eq_edge:.4f}**
- Median-coin median edge: **{med_edge:.4f}**
- Decision: **{decision}**

## 2. Datenbasis

- Joined 5m rows (from full-run integrity): 169458
- Raw burst buckets: 795580
- Event clusters: 14826
- Deduplicated event variants: 97248
- Reclaim variants: 480690
- Outcome rows: 577938
- Offline evaluation only (no DB re-query)

## 3. Physische Events vs Varianten

See `physical_burst_anchors.csv` and `event_multiplicity_summary.csv`.

- Physical ID: `symbol|side|sequence_id|anchor_bucket`
- Variants multiply by burst×price×oi×reclaim×window×entry_mode×horizons-in-row
- Median variants/anchor (price×oi×burst event rows): see multiplicity file

## 4. Burst-Ergebnisse

Strongest burst group by median MFE−|MAE| at h{PRIMARY_HORIZON}: **{best_burst_s}**
(B2 almost never triggers in clusters; B1/B3/B4 dominate.)

## 5. OI-Abbau

- O1−O0 median edge (independent pools): **{oi_improve:.4f}**
- Paired O1−O0 (same base/burst/price): **{paired_edge:.4f}**
- O0 is parent set; O1 is subset — do not treat as independent market samples.

## 6. Reclaim-Wirkung

- Best reclaim group by median edge: **{best_rc}**
- Burst-only remains the larger sample; reclaim reduces N and delays entry.

## 7–8. MFE/MAE & First Touch

See `mfe_mae_summary.csv`, `first_touch_summary.csv` (same-bar adverse-first).

## 9. Feste Exit-Modelle

Reconstructed from first-touch columns (exits were not persisted in full-run outcomes).
Positive expectancy @0.25% cost (scope=all): **{len(exit_ok)}** model/hold combos.
See `fixed_exit_summary.csv`.

## 10. Kontrollgruppen

Controls CSV has matching timestamps but **no forward outcomes** — comparison is count/distribution + C3 derived. See `control_group_comparison.csv`.

## 11–13. Long/Short, Coins, Equal/Median

See `direction_summary.csv`, `coin_summary.csv`, `equal_coin_summary.csv`, `median_coin_summary.csv`.

## 14–15. Splits & Ablations

Fixed Dev/Val/OOS. Top-coin chosen on **Dev only**. See `split_summary.csv`, `top_coin_ablation.csv`.

## 16. Candidate Gates

Hard gates documented in evaluator; no post-hoc loosening.
All-pass candidates: **{n_pass}**.

## 17. Leakage / Kausalität

- Offline on causal full-run outputs
- No future features added
- Exit reconstruction uses only first-touch bar offsets already computed causally

## 18. Limitierungen

- Exit economics approximated (no OHLC path re-sim)
- Controls lack outcome paths
- Variant inflation requires physical-anchor accounting (done)
- Smoke report bug previously overwrote Full REPORT (fixed for future runs)

## 19. Empfehlung

**{decision}**

Details: `recommended_followup.md`
"""
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")

    follow = f"""# Recommended Follow-up

## Decision
`{decision}`

## If proceeding
1. Freeze a single candidate from `candidate_gate_summary.csv` with `all_pass=true` (or strongest hard_pass).
2. Build fixed-entry strategy research (next-open after reclaim/burst) without re-optimizing thresholds.
3. Re-sim exits on OHLC paths (not FT approximation) for the frozen candidate only.
4. Keep physical-anchor sample sizes in all claims.

## If insufficient / reject
- Liquidation bursts alone may not dominate similar price moves (controls incomplete).
- Reclaim sample loss is large; OI filter is a subset of O0 — interpret carefully.
- Do not invent new thresholds from OOS.

## Do not
- Re-run the expensive full burst/outcome pipeline unless code/logic changes.
- Optimize on OOS.
"""
    (out_dir / "recommended_followup.md").write_text(follow, encoding="utf-8")
    return decision


def run_evaluation(*, input_dir: Path, output_dir: Path, mode: str = "full") -> dict[str, Any]:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # preserve wrong smoke report if present
    report_path = output_dir / "REPORT.md"
    if report_path.exists() and "Smoke Report" in report_path.read_text(encoding="utf-8")[:200]:
        shutil.copy(report_path, output_dir / "REPORT_SMOKE_WRONG_FULL_RUN.md")

    dfs = load_inputs(input_dir)
    dfs = build_ids(dfs)

    phys = physical_burst_anchors(dfs)
    mult = multiplicity_summary(phys, dfs["events"], dfs["reclaims"], dfs["outcomes"])
    mfe = mfe_mae_table(dfs["outcomes"], horizon=PRIMARY_HORIZON)
    ft = first_touch_summary(dfs["outcomes"])
    oi = oi_condition_comparison(dfs["outcomes"])
    reclaim = reclaim_comparison(dfs["outcomes"])
    exits = fixed_exit_summary(dfs["outcomes"])
    controls = control_group_comparison(dfs["outcomes"], dfs["controls"])
    coin, direction, equal, median_coin = coin_direction_summaries(dfs["outcomes"])
    splits = split_summary(dfs["outcomes"])
    ablation = top_coin_ablation(dfs["outcomes"])
    matrix, gates = candidate_matrix_and_gates(dfs["outcomes"])

    outputs = {
        "physical_burst_anchors.csv": phys,
        "event_multiplicity_summary.csv": mult,
        "mfe_mae_summary.csv": mfe,
        "first_touch_summary.csv": ft,
        "oi_condition_comparison.csv": oi,
        "reclaim_comparison.csv": reclaim,
        "fixed_exit_summary.csv": exits,
        "control_group_comparison.csv": controls,
        "coin_summary.csv": coin,
        "direction_summary.csv": direction,
        "equal_coin_summary.csv": equal,
        "median_coin_summary.csv": median_coin,
        "split_summary.csv": splits,
        "top_coin_ablation.csv": ablation,
        "candidate_matrix.csv": matrix,
        "candidate_gate_summary.csv": gates,
    }
    for name, df in outputs.items():
        df.to_csv(output_dir / name, index=False)

    decision = write_full_report(
        output_dir,
        phys=phys,
        mult=mult,
        mfe=mfe,
        ft=ft,
        oi=oi,
        reclaim=reclaim,
        exits=exits,
        controls=controls,
        coin=coin,
        direction=direction,
        equal=equal,
        splits=splits,
        ablation=ablation,
        matrix=matrix,
        gates=gates,
    )

    input_files = [
        "deduplicated_events.csv",
        "reclaim_events.csv",
        "forward_outcomes.csv",
        "controls.csv",
        "event_clusters.csv",
        "raw_burst_buckets.csv",
        "joined_5m_coverage.csv",
        "integrity.json",
    ]
    from research.regime_scanner.liquidation_exhaustion.config import SPLIT_DEV, SPLIT_OOS, SPLIT_VAL

    integrity = {
        "mode": mode,
        "evaluation_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "no_db_access": True,
        "no_db_writes": True,
        "full_run_rerun": False,
        "decision": decision,
        "physical_anchors": int(len(phys)),
        "gate_all_pass": int(gates["all_pass"].sum()) if len(gates) else 0,
        "gate_hard_pass": int(gates["hard_pass"].sum()) if len(gates) else 0,
        "input_hashes": {f: _sha256_file(input_dir / f) for f in input_files if (input_dir / f).exists()},
        "output_files": sorted(
            list(outputs.keys()) + ["REPORT.md", "recommended_followup.md", "evaluation_integrity.json"]
        ),
        "splits": {"dev": list(SPLIT_DEV), "validation": list(SPLIT_VAL), "oos": list(SPLIT_OOS)},
        "primary_horizon": PRIMARY_HORIZON,
        "exit_reconstruction": "first_touch_approximation",
    }
    (output_dir / "evaluation_integrity.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return integrity

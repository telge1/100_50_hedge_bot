"""Offline evaluation of liquidation exhaustion full-run CSVs (no DB)."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.config import (
    COST_PCT,
    EXIT_HOLDS,
    EXIT_MODELS,
    MFE_HORIZONS,
    SPLIT_DEV,
    SPLIT_OOS,
    SPLIT_VAL,
)

PRIMARY_HORIZON = 12
GATE_HORIZON = 12


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ts(s: Any) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def assign_split(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    dev_a, dev_b = pd.Timestamp(SPLIT_DEV[0]), pd.Timestamp(SPLIT_DEV[1])
    val_a, val_b = pd.Timestamp(SPLIT_VAL[0]), pd.Timestamp(SPLIT_VAL[1])
    oos_a, oos_b = pd.Timestamp(SPLIT_OOS[0]), pd.Timestamp(SPLIT_OOS[1])
    if dev_a <= t < dev_b:
        return "dev"
    if val_a <= t < val_b:
        return "validation"
    if oos_a <= t < oos_b:
        return "oos"
    return "other"


def physical_anchor_id(symbol: str, side: str, sequence_id: Any, anchor_bucket: str) -> str:
    return f"{symbol}|{side}|{sequence_id}|{anchor_bucket}"


def candidate_variant_id(
    *,
    base_event_id: str,
    burst: str,
    price: str,
    oi: str,
    reclaim: str,
    reclaim_window: int | str | None,
    entry_mode: str,
) -> str:
    if entry_mode == "burst_next_open" or reclaim in ("none", "", None):
        return f"{base_event_id}|{burst}|{price}|{oi}|none|0|burst"
    return f"{base_event_id}|{burst}|{price}|{oi}|{reclaim}|{reclaim_window}|reclaim"


def load_inputs(input_dir: Path) -> dict[str, pd.DataFrame]:
    input_dir = Path(input_dir)
    clusters = pd.read_csv(input_dir / "event_clusters.csv")
    events = pd.read_csv(input_dir / "deduplicated_events.csv")
    reclaims = pd.read_csv(input_dir / "reclaim_events.csv")
    controls = pd.read_csv(input_dir / "controls.csv")
    coverage = pd.read_csv(input_dir / "joined_5m_coverage.csv")

    # Outcomes: select needed columns only (661MB full file)
    usecols = [
        "symbol",
        "side",
        "burst",
        "price",
        "oi",
        "anchor_bucket",
        "sequence_id",
        "anchor_liq_usd",
        "entry_mode",
        "fill_bucket",
        "fill_price",
        "variant_id",
        "reclaim",
        "reclaim_variant",
        "reclaim_window",
        "bars_to_reclaim",
        "reclaim_level",
        "favorable_first",
        "adverse_first",
        "same_bar_ambiguous",
        "first_touch_order",
    ]
    # add horizon + first-touch cols by peeking header
    header = pd.read_csv(input_dir / "forward_outcomes.csv", nrows=0).columns.tolist()
    for h in MFE_HORIZONS:
        for suf in ("mfe_pct", "mae_pct", "close_ret", "mfe_atr", "mae_atr", "bars_to_mfe", "bars_to_mae"):
            c = f"h{h}_{suf}"
            if c in header:
                usecols.append(c)
    for c in header:
        if c.startswith("ft_") and c not in usecols:
            usecols.append(c)
    usecols = [c for c in usecols if c in header]
    outcomes = pd.read_csv(input_dir / "forward_outcomes.csv", usecols=usecols)

    for df in (clusters, events, reclaims, outcomes, controls):
        if "anchor_bucket" in df.columns:
            df["anchor_ts"] = pd.to_datetime(df["anchor_bucket"], utc=True)
        if "fill_bucket" in df.columns:
            df["fill_ts"] = pd.to_datetime(df["fill_bucket"], utc=True)
        if "bucket_start" in df.columns:
            df["bucket_ts"] = pd.to_datetime(df["bucket_start"], utc=True)

    return {
        "clusters": clusters,
        "events": events,
        "reclaims": reclaims,
        "outcomes": outcomes,
        "controls": controls,
        "coverage": coverage,
    }


def build_ids(dfs: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    events = dfs["events"].copy()
    reclaims = dfs["reclaims"].copy()
    outcomes = dfs["outcomes"].copy()
    clusters = dfs["clusters"].copy()

    for df in (events, reclaims, outcomes, clusters):
        df["base_event_id"] = [
            physical_anchor_id(s, d, q, a)
            for s, d, q, a in zip(
                df["symbol"], df["side"], df["sequence_id"], df["anchor_bucket"].astype(str)
            )
        ]

    events["candidate_variant_id"] = [
        candidate_variant_id(
            base_event_id=b,
            burst=burst,
            price=price,
            oi=oi,
            reclaim="none",
            reclaim_window=0,
            entry_mode="burst_next_open",
        )
        for b, burst, price, oi in zip(
            events["base_event_id"], events["burst"], events["price"], events["oi"]
        )
    ]
    reclaims["candidate_variant_id"] = [
        candidate_variant_id(
            base_event_id=b,
            burst=burst,
            price=price,
            oi=oi,
            reclaim=str(r),
            reclaim_window=w,
            entry_mode="reclaim_next_open",
        )
        for b, burst, price, oi, r, w in zip(
            reclaims["base_event_id"],
            reclaims["burst"],
            reclaims["price"],
            reclaims["oi"],
            reclaims["reclaim"],
            reclaims["reclaim_window"],
        )
    ]

    outcomes["candidate_variant_id"] = np.where(
        outcomes["entry_mode"].to_numpy() == "burst_next_open",
        [
            candidate_variant_id(
                base_event_id=b,
                burst=burst,
                price=price,
                oi=oi,
                reclaim="none",
                reclaim_window=0,
                entry_mode="burst_next_open",
            )
            for b, burst, price, oi in zip(
                outcomes["base_event_id"], outcomes["burst"], outcomes["price"], outcomes["oi"]
            )
        ],
        [
            candidate_variant_id(
                base_event_id=b,
                burst=burst,
                price=price,
                oi=oi,
                reclaim=str(r if pd.notna(r) else rv),
                reclaim_window=w,
                entry_mode="reclaim_next_open",
            )
            for b, burst, price, oi, r, rv, w in zip(
                outcomes["base_event_id"],
                outcomes["burst"],
                outcomes["price"],
                outcomes["oi"],
                outcomes.get("reclaim", pd.Series([None] * len(outcomes))),
                outcomes.get("reclaim_variant", pd.Series([None] * len(outcomes))),
                outcomes.get("reclaim_window", pd.Series([0] * len(outcomes))),
            )
        ],
    )
    outcomes["split"] = outcomes["anchor_ts"].map(assign_split)
    events["split"] = events["anchor_ts"].map(assign_split)
    reclaims["split"] = reclaims["anchor_ts"].map(assign_split)
    clusters["split"] = clusters["anchor_ts"].map(assign_split)

    dfs = dict(dfs)
    dfs["events"] = events
    dfs["reclaims"] = reclaims
    dfs["outcomes"] = outcomes
    dfs["clusters"] = clusters
    return dfs


def physical_burst_anchors(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    clusters = dfs["clusters"]
    events = dfs["events"]
    reclaims = dfs["reclaims"]
    outcomes = dfs["outcomes"]

    # one row per physical anchor (collapse burst defs)
    g = (
        clusters.groupby(["symbol", "side", "sequence_id", "anchor_bucket"], as_index=False)
        .agg(
            anchor_liq_usd=("anchor_liq_usd", "max"),
            n_burst_defs=("burst", "nunique"),
            bursts=("burst", lambda s: "|".join(sorted(set(s.astype(str))))),
            n_cluster_rows=("burst", "size"),
            anchor_index=("anchor_index", "min"),
        )
    )
    g["base_event_id"] = [
        physical_anchor_id(s, d, q, a)
        for s, d, q, a in zip(g["symbol"], g["side"], g["sequence_id"], g["anchor_bucket"].astype(str))
    ]
    g["anchor_ts"] = pd.to_datetime(g["anchor_bucket"], utc=True)
    g["split"] = g["anchor_ts"].map(assign_split)

    ev_n = events.groupby("base_event_id").agg(
        n_price_oi_variants=("candidate_variant_id", "nunique"),
        n_burst_in_events=("burst", "nunique"),
        n_price=("price", "nunique"),
        n_oi=("oi", "nunique"),
    )
    rc_n = reclaims.groupby("base_event_id").agg(n_reclaim_variants=("candidate_variant_id", "nunique"))
    out_n = outcomes.groupby("base_event_id").size().rename("n_outcome_rows")

    g = g.merge(ev_n, left_on="base_event_id", right_index=True, how="left")
    g = g.merge(rc_n, left_on="base_event_id", right_index=True, how="left")
    g = g.merge(out_n, left_on="base_event_id", right_index=True, how="left")
    for c in ("n_price_oi_variants", "n_burst_in_events", "n_price", "n_oi", "n_reclaim_variants", "n_outcome_rows"):
        if c in g.columns:
            g[c] = g[c].fillna(0).astype(int)
    # placeholders for missing causal features in CSV
    g["oi_chg_5m"] = np.nan
    g["oi_chg_15m"] = np.nan
    g["ret_5m_pct"] = np.nan
    g["ret_15m_pct"] = np.nan
    g["ret_30m_pct"] = np.nan
    g["event_atr"] = np.nan
    g["total_liquidation_usd"] = np.nan
    return g.sort_values(["symbol", "anchor_ts", "side"]).reset_index(drop=True)


def multiplicity_summary(phys: pd.DataFrame, events: pd.DataFrame, reclaims: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    per = events.groupby("base_event_id").size()
    rows = [
        {"metric": "physical_burst_anchors", "value": int(len(phys))},
        {"metric": "event_variant_rows", "value": int(len(events))},
        {"metric": "reclaim_variant_rows", "value": int(len(reclaims))},
        {"metric": "outcome_rows", "value": int(len(outcomes))},
        {"metric": "variants_per_anchor_median", "value": float(per.median()) if len(per) else 0},
        {"metric": "variants_per_anchor_p90", "value": float(per.quantile(0.9)) if len(per) else 0},
        {"metric": "variants_per_anchor_max", "value": int(per.max()) if len(per) else 0},
        {"metric": "mean_outcome_rows_per_anchor", "value": float(outcomes.groupby("base_event_id").size().mean()) if len(outcomes) else 0},
        {"metric": "burst_dim", "value": int(events["burst"].nunique())},
        {"metric": "price_dim", "value": int(events["price"].nunique())},
        {"metric": "oi_dim", "value": int(events["oi"].nunique())},
        {"metric": "reclaim_dim", "value": int(reclaims["reclaim"].nunique()) if len(reclaims) else 0},
        {"metric": "reclaim_window_dim", "value": int(reclaims["reclaim_window"].nunique()) if len(reclaims) else 0},
        {"metric": "long_physical_anchors", "value": int((phys["side"] == "long").sum())},
        {"metric": "short_physical_anchors", "value": int((phys["side"] == "short").sum())},
    ]
    return pd.DataFrame(rows)


def _dedupe_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """One row per candidate_variant_id (first occurrence)."""
    if df.empty:
        return df
    return df.drop_duplicates(subset=["candidate_variant_id"], keep="first")


def mfe_mae_table(outcomes: pd.DataFrame, horizon: int = PRIMARY_HORIZON) -> pd.DataFrame:
    o = outcomes.copy()
    o["reclaim_group"] = np.where(o["entry_mode"] == "burst_next_open", "burst_only", "reclaim")
    o["reclaim_label"] = o["reclaim"].fillna("none").astype(str)
    o["window_label"] = o["reclaim_window"].fillna(0).astype(int)

    mfe_c = f"h{horizon}_mfe_pct"
    mae_c = f"h{horizon}_mae_pct"
    ret_c = f"h{horizon}_close_ret"
    mfe_a = f"h{horizon}_mfe_atr"
    mae_a = f"h{horizon}_mae_atr"

    group_specs: list[tuple[str, list[str]]] = [
        ("all", []),
        ("entry_mode", ["reclaim_group"]),
        ("side", ["side"]),
        ("burst", ["burst"]),
        ("price", ["price"]),
        ("oi", ["oi"]),
        ("reclaim", ["reclaim_label"]),
        ("reclaim_window", ["window_label"]),
        ("burst_side", ["burst", "side"]),
        ("coin", ["symbol"]),
        ("entry_side", ["reclaim_group", "side"]),
        ("burst_oi", ["burst", "oi"]),
        ("burst_reclaim", ["burst", "reclaim_label"]),
    ]

    rows = []
    for gname, cols in group_specs:
        if not cols:
            groups = [("all", o)]
        else:
            groups = list(o.groupby(cols, dropna=False))
        for key, g in groups:
            g1 = _dedupe_candidates(g)
            if g1.empty or mfe_c not in g1.columns:
                continue
            mfe = pd.to_numeric(g1[mfe_c], errors="coerce")
            mae = pd.to_numeric(g1[mae_c], errors="coerce")
            ret = pd.to_numeric(g1[ret_c], errors="coerce")
            edge = mfe - mae.abs()
            label = key if not isinstance(key, tuple) else "|".join(map(str, key))
            # equal-coin: mean of per-coin medians of edge
            coin_edge = []
            for _, cg in g1.groupby("symbol"):
                e = pd.to_numeric(cg[mfe_c], errors="coerce") - pd.to_numeric(cg[mae_c], errors="coerce").abs()
                if e.notna().any():
                    coin_edge.append(float(e.median()))
            rows.append(
                {
                    "group_type": gname,
                    "group_key": label,
                    "horizon": horizon,
                    "unique_physical_events": int(g1["base_event_id"].nunique()),
                    "unique_candidates": int(g1["candidate_variant_id"].nunique()),
                    "n_coins": int(g1["symbol"].nunique()),
                    "mean_mfe_pct": float(mfe.mean()) if mfe.notna().any() else np.nan,
                    "median_mfe_pct": float(mfe.median()) if mfe.notna().any() else np.nan,
                    "mean_mae_pct": float(mae.mean()) if mae.notna().any() else np.nan,
                    "median_mae_pct": float(mae.median()) if mae.notna().any() else np.nan,
                    "mean_mfe_minus_abs_mae": float(edge.mean()) if edge.notna().any() else np.nan,
                    "median_mfe_minus_abs_mae": float(edge.median()) if edge.notna().any() else np.nan,
                    "mfe_abs_mae_ratio": float((mfe.median() / mae.abs().median()))
                    if mae.abs().median() not in (0, np.nan) and mae.notna().any() and mfe.notna().any()
                    else np.nan,
                    "mean_close_ret": float(ret.mean()) if ret.notna().any() else np.nan,
                    "median_close_ret": float(ret.median()) if ret.notna().any() else np.nan,
                    "positive_close_ret_rate": float((ret > 0).mean()) if ret.notna().any() else np.nan,
                    "mean_mfe_atr": float(pd.to_numeric(g1.get(mfe_a), errors="coerce").mean())
                    if mfe_a in g1
                    else np.nan,
                    "mean_mae_atr": float(pd.to_numeric(g1.get(mae_a), errors="coerce").mean())
                    if mae_a in g1
                    else np.nan,
                    "equal_coin_median_edge": float(np.mean(coin_edge)) if coin_edge else np.nan,
                    "median_coin_edge": float(np.median(coin_edge)) if coin_edge else np.nan,
                }
            )
    # also all horizons overall
    for h in MFE_HORIZONS:
        mfe_c = f"h{h}_mfe_pct"
        mae_c = f"h{h}_mae_pct"
        if mfe_c not in o.columns:
            continue
        g1 = _dedupe_candidates(o)
        mfe = pd.to_numeric(g1[mfe_c], errors="coerce")
        mae = pd.to_numeric(g1[mae_c], errors="coerce")
        edge = mfe - mae.abs()
        rows.append(
            {
                "group_type": "horizon_all",
                "group_key": f"h{h}",
                "horizon": h,
                "unique_physical_events": int(g1["base_event_id"].nunique()),
                "unique_candidates": int(g1["candidate_variant_id"].nunique()),
                "n_coins": int(g1["symbol"].nunique()),
                "mean_mfe_pct": float(mfe.mean()),
                "median_mfe_pct": float(mfe.median()),
                "mean_mae_pct": float(mae.mean()),
                "median_mae_pct": float(mae.median()),
                "mean_mfe_minus_abs_mae": float(edge.mean()),
                "median_mfe_minus_abs_mae": float(edge.median()),
                "mfe_abs_mae_ratio": float(mfe.median() / mae.abs().median())
                if mae.abs().median()
                else np.nan,
                "mean_close_ret": float(pd.to_numeric(g1[f"h{h}_close_ret"], errors="coerce").mean()),
                "median_close_ret": float(pd.to_numeric(g1[f"h{h}_close_ret"], errors="coerce").median()),
                "positive_close_ret_rate": float((pd.to_numeric(g1[f"h{h}_close_ret"], errors="coerce") > 0).mean()),
                "mean_mfe_atr": np.nan,
                "mean_mae_atr": np.nan,
                "equal_coin_median_edge": np.nan,
                "median_coin_edge": np.nan,
            }
        )
    return pd.DataFrame(rows)


def _ft_level_key(level: str) -> tuple[str, str]:
    """Map level label to favorable/adverse column prefixes."""
    mapping = {
        "0.25%": ("ft_p0_25", "ft_m0_25"),
        "0.50%": ("ft_p0_50", "ft_m0_50"),
        "0.75%": ("ft_p0_75", "ft_m0_75"),
        "1.00%": ("ft_p1_00", "ft_m1_00"),
        "1.50%": ("ft_p1_50", "ft_m1_50"),
        "2.00%": ("ft_p2_00", "ft_m2_00"),
        "0.5ATR": ("ft_atrp0_5", "ft_atrm0_5"),
        "1.0ATR": ("ft_atrp1_0", "ft_atrm1_0"),
        "1.5ATR": ("ft_atrp1_5", "ft_atrm1_5"),
        "2.0ATR": ("ft_atrp2_0", "ft_atrm2_0"),
    }
    return mapping[level]


def first_touch_summary(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = _dedupe_candidates(outcomes.copy())
    o["reclaim_group"] = np.where(o["entry_mode"] == "burst_next_open", "burst_only", "reclaim")
    if "reclaim" in o.columns:
        o["reclaim"] = o["reclaim"].fillna("none").astype(str)
    levels = ["0.25%", "0.50%", "0.75%", "1.00%", "1.50%", "2.00%", "0.5ATR", "1.0ATR", "1.5ATR", "2.0ATR"]
    group_specs = [
        ("all", []),
        ("entry_mode", ["reclaim_group"]),
        ("side", ["side"]),
        ("burst", ["burst"]),
        ("oi", ["oi"]),
        ("reclaim", ["reclaim"]),
        ("coin", ["symbol"]),
        ("entry_side", ["reclaim_group", "side"]),
    ]
    rows = []
    for level in levels:
        fav_p, adv_p = _ft_level_key(level)
        fr, ar = f"{fav_p}_reached", f"{adv_p}_reached"
        fb, ab = f"{fav_p}_bars", f"{adv_p}_bars"
        if fr not in o.columns or ar not in o.columns:
            continue
        for gname, cols in group_specs:
            groups = [("all", o)] if not cols else list(o.groupby(cols, dropna=False))
            for key, g in groups:
                label = key if not isinstance(key, tuple) else "|".join(map(str, key))
                fav_r = g[fr].astype(str).str.lower().isin(["true", "1"])
                adv_r = g[ar].astype(str).str.lower().isin(["true", "1"])
                fav_b = pd.to_numeric(g.get(fb), errors="coerce")
                adv_b = pd.to_numeric(g.get(ab), errors="coerce")
                # same-bar ambiguity when both reached at same bars
                same = fav_r & adv_r & fav_b.notna() & adv_b.notna() & (fav_b == adv_b)
                # conservative: same -> adverse
                fav_first = fav_r & (~adv_r | (fav_b < adv_b))
                adv_first = (adv_r & (~fav_r | (adv_b < fav_b))) | same
                neither = (~fav_r) & (~adv_r)
                n = len(g)
                if n == 0:
                    continue
                rows.append(
                    {
                        "group_type": gname,
                        "group_key": label,
                        "level": level,
                        "unique_candidates": int(g["candidate_variant_id"].nunique()),
                        "unique_physical_events": int(g["base_event_id"].nunique()),
                        "favorable_first_pct": float(fav_first.mean() * 100),
                        "adverse_first_pct": float(adv_first.mean() * 100),
                        "neither_pct": float(neither.mean() * 100),
                        "same_bar_ambiguity_pct": float(same.mean() * 100),
                        "favorable_minus_adverse": float((fav_first.mean() - adv_first.mean()) * 100),
                    }
                )
    # also use built-in 0.5% favorable_first columns overall
    if "favorable_first" in o.columns:
        ff = o["favorable_first"].astype(str).str.lower().isin(["true", "1"])
        af = o["adverse_first"].astype(str).str.lower().isin(["true", "1"])
        rows.append(
            {
                "group_type": "builtin_0_5pct",
                "group_key": "all",
                "level": "0.50%_builtin",
                "unique_candidates": int(o["candidate_variant_id"].nunique()),
                "unique_physical_events": int(o["base_event_id"].nunique()),
                "favorable_first_pct": float(ff.mean() * 100),
                "adverse_first_pct": float(af.mean() * 100),
                "neither_pct": float((~ff & ~af).mean() * 100),
                "same_bar_ambiguity_pct": float(
                    o["same_bar_ambiguous"].astype(str).str.lower().isin(["true", "1"]).mean() * 100
                ),
                "favorable_minus_adverse": float((ff.mean() - af.mean()) * 100),
            }
        )
    return pd.DataFrame(rows)


def reconstruct_exit_series(
    o: pd.DataFrame, *, tp: float, sl: float, hold: int, cost: float
) -> pd.DataFrame:
    """Vectorized approximate fixed-exit economics from first-touch columns."""
    tp_map = {0.50: "ft_p0_50", 0.75: "ft_p0_75", 1.00: "ft_p1_00", 1.50: "ft_p1_50"}
    sl_map = {0.50: "ft_m0_50", 0.75: "ft_m0_75", 1.00: "ft_m1_00"}
    tp_k = min(tp_map.keys(), key=lambda x: abs(x - tp))
    sl_k = min(sl_map.keys(), key=lambda x: abs(x - sl))
    fav, adv = tp_map[tp_k], sl_map[sl_k]
    fr = o[f"{fav}_reached"].astype(str).str.lower().isin(["true", "1"])
    ar = o[f"{adv}_reached"].astype(str).str.lower().isin(["true", "1"])
    fb = pd.to_numeric(o[f"{fav}_bars"], errors="coerce")
    ab = pd.to_numeric(o[f"{adv}_bars"], errors="coerce")
    fr = fr & fb.notna() & (fb < hold)
    ar = ar & ab.notna() & (ab < hold)

    reason = np.full(len(o), "time_exit", dtype=object)
    gross = np.zeros(len(o), dtype=float)

    both = fr & ar
    tp_first = fr & (~ar | (fb < ab))
    sl_first = ar & (~fr | (ab < fb))
    same = both & (fb == ab)

    reason = np.where(same, "same_bar_conservative_sl", reason)
    gross = np.where(same, -sl, gross)
    reason = np.where(tp_first & ~same, "TP", reason)
    gross = np.where(tp_first & ~same, tp, gross)
    reason = np.where(sl_first & ~same, "SL", reason)
    gross = np.where(sl_first & ~same, -sl, gross)

    # time exit: use close ret at hold if available
    close_col = f"h{hold}_close_ret"
    if close_col not in o.columns:
        for h in (12, 24, 48, 6):
            if f"h{h}_close_ret" in o.columns:
                close_col = f"h{h}_close_ret"
                break
    close = pd.to_numeric(o[close_col], errors="coerce").fillna(0.0).to_numpy() if close_col in o.columns else np.zeros(len(o))
    is_time = reason == "time_exit"
    gross = np.where(is_time, close, gross)
    net = gross - cost
    return pd.DataFrame({"reason": reason, "gross_pct": gross, "net_pct": net})


def fixed_exit_summary(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = _dedupe_candidates(outcomes.copy())
    o["reclaim_group"] = np.where(o["entry_mode"] == "burst_next_open", "burst_only", "reclaim")
    if "ft_p0_50_reached" not in o.columns:
        return pd.DataFrame()
    rows = []
    scope_masks = {
        "all": np.ones(len(o), dtype=bool),
        "burst_only": (o["reclaim_group"] == "burst_only").to_numpy(),
        "reclaim": (o["reclaim_group"] == "reclaim").to_numpy(),
        "long": (o["side"] == "long").to_numpy(),
        "short": (o["side"] == "short").to_numpy(),
    }
    symbols = o["symbol"].to_numpy()
    base_ids = o["base_event_id"].to_numpy()
    for xid, (tp, sl, _) in EXIT_MODELS.items():
        for hold in EXIT_HOLDS:
            for cost in COST_PCT:
                # reconstruct once per (exit, hold, cost), then slice scopes
                res = reconstruct_exit_series(o, tp=tp, sl=sl, hold=hold, cost=cost)
                arr_all = res["net_pct"].to_numpy(dtype=float)
                reasons_all = res["reason"].to_numpy()
                for scope_name, mask in scope_masks.items():
                    if not mask.any():
                        continue
                    arr = arr_all[mask]
                    reasons = reasons_all[mask]
                    wins = arr[arr > 0]
                    losses = arr[arr <= 0]
                    pf = (
                        float(wins.sum() / abs(losses.sum()))
                        if losses.size and abs(losses.sum()) > 1e-12
                        else np.nan
                    )
                    # equal/median coin expectancy
                    sym_m = symbols[mask]
                    coin_exp = []
                    for s in np.unique(sym_m):
                        coin_exp.append(float(arr[sym_m == s].mean()))
                    rows.append(
                        {
                            "scope": scope_name,
                            "exit_id": xid,
                            "tp_pct": tp,
                            "sl_pct": sl,
                            "hold": hold,
                            "cost_pct": cost,
                            "trades": int(len(arr)),
                            "unique_physical": int(len(np.unique(base_ids[mask]))),
                            "expectancy_pct": float(arr.mean()),
                            "median_net_pct": float(np.median(arr)),
                            "total_net_pct": float(arr.sum()),
                            "win_rate": float((arr > 0).mean()),
                            "profit_factor": pf,
                            "max_loss": float(arr.min()),
                            "tp_rate": float((reasons == "TP").mean()),
                            "sl_rate": float(np.isin(reasons, ["SL", "same_bar_conservative_sl"]).mean()),
                            "time_exit_rate": float((reasons == "time_exit").mean()),
                            "equal_coin_expectancy": float(np.mean(coin_exp)) if coin_exp else np.nan,
                            "median_coin_expectancy": float(np.median(coin_exp)) if coin_exp else np.nan,
                        }
                    )
    return pd.DataFrame(rows)

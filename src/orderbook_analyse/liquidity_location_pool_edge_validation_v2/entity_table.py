"""Build entity-level table from v1 lifecycle artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .stats import (
    age_at_touch_bucket,
    component_bucket,
    distance_atr_bucket,
    distance_pct_bucket,
    touch_timing,
)

DEFAULT_V1 = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_pool_lifecycle_v1"
)


def _parse_ts(s: Any) -> pd.Timestamp | pd.NaT:
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return pd.NaT
    t = pd.Timestamp(s)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def load_primary_outcomes(v1_dir: Path) -> pd.DataFrame:
    outcomes = pd.read_csv(v1_dir / "pool_outcomes.csv", low_memory=False)
    prim = outcomes[
        (outcomes["acceptance_bars"] == 2)
        & (outcomes["reclaim_horizon_bars"] == 6)
        & (
            outcomes["reaction_atr_mult"].isna()
            | (outcomes["reaction_atr_mult"] == 0.5)
            | (outcomes["swept"] == True)  # noqa: E712
        )
    ].copy()
    prim = prim.sort_values(["entity_id", "reaction_atr_mult"], na_position="first")
    prim = prim.groupby("entity_id", as_index=False).first()
    return prim


def load_ema_created(v1_dir: Path) -> pd.DataFrame:
    ema = pd.read_csv(v1_dir / "pool_ema_context.csv", low_memory=False)
    created = ema[ema["label"] == "CREATED"].copy()
    created = created.drop_duplicates("entity_id", keep="first")
    keep = [
        "entity_id",
        "ema9",
        "ema20",
        "ema59",
        "ema200",
        "ema_order",
        "ema_regime",
        "ema9_slope",
        "ema20_slope",
        "ema59_slope",
        "ema200_slope",
        "ema_compression",
        "ema_expansion",
        "pool_vs_ema200",
        "pool_between_ema20_59",
        "touch_ema_with_bar",
        "dist_pool_ema20",
        "dist_pool_ema59",
        "dist_pool_ema200",
    ]
    return created[[c for c in keep if c in created.columns]]


def load_ema_event(v1_dir: Path, label: str, suffix: str) -> pd.DataFrame:
    ema = pd.read_csv(v1_dir / "pool_ema_context.csv", low_memory=False)
    sub = ema[ema["label"] == label].drop_duplicates("entity_id", keep="first")
    cols = {
        "entity_id": "entity_id",
        "mfe_frac": f"mfe_{suffix}",  # may not exist
        "ema_regime": f"ema_regime_{suffix}",
        "ema20_slope": f"ema20_slope_{suffix}",
        "touch_ema_with_bar": f"touch_ema_{suffix}",
    }
    out = pd.DataFrame({"entity_id": sub["entity_id"]})
    for src, dst in cols.items():
        if src == "entity_id":
            continue
        if src in sub.columns:
            out[dst] = sub[src].values
    return out


def load_first_destination_60m(v1_dir: Path) -> pd.DataFrame:
    dest = pd.read_csv(v1_dir / "first_destination_outcomes.csv", low_memory=False)
    d = dest[(dest["horizon_minutes"] == 60) & (dest["trigger"] == "FIRST_TOUCH")].copy()
    d = d.drop_duplicates("entity_id", keep="first")
    return d[["entity_id", "first_destination"]].rename(
        columns={"first_destination": "first_destination_60m"}
    )


def build_entity_table(v1_dir: Path | None = None) -> pd.DataFrame:
    v1_dir = Path(v1_dir) if v1_dir else DEFAULT_V1
    pools = pd.read_csv(v1_dir / "pool_instances.csv", low_memory=False)
    clusters = pd.read_csv(v1_dir / "pool_clusters.csv", low_memory=False)
    prim = load_primary_outcomes(v1_dir)
    ema_c = load_ema_created(v1_dir)
    dest = load_first_destination_60m(v1_dir)

    pool_base = pools.copy()
    pool_base["entity_id"] = pool_base["pool_id"]
    pool_base["entity_kind"] = "pool"
    pool_base["n_components"] = 1
    pool_base["pool_width"] = pool_base["upper_price"] - pool_base["lower_price"]

    cl_base = clusters.copy()
    cl_base["entity_id"] = cl_base["cluster_id"]
    cl_base["entity_kind"] = "cluster"
    cl_base["n_components"] = cl_base["number_of_component_pools"]
    cl_base["pool_width"] = cl_base["upper_price"] - cl_base["lower_price"]
    # align column names with pools
    rename = {
        "known_at": "known_at",
        "timeframe": "timeframe",
    }
    for c in ("created_at", "source_at", "first_available_at"):
        if c not in cl_base.columns:
            cl_base[c] = cl_base.get("known_at")

    common_pool_cols = [
        "entity_id",
        "entity_kind",
        "symbol",
        "timeframe",
        "side",
        "lower_price",
        "upper_price",
        "center_price",
        "strength",
        "known_at",
        "n_components",
        "pool_width",
        "distance_from_price",
        "distance_from_price_atr",
        "bars_to_touch",
        "bars_to_sweep",
        "minutes_to_touch",
        "minutes_to_sweep",
        "touched",
        "swept",
        "analysis_start_index",
        "first_approach_index",
        "first_touch_index",
        "sweep_index",
    ]
    for c in common_pool_cols:
        if c not in pool_base.columns:
            pool_base[c] = np.nan
        if c not in cl_base.columns:
            cl_base[c] = np.nan

    base = pd.concat([pool_base[common_pool_cols], cl_base[common_pool_cols]], ignore_index=True)
    # outcomes overwrite touch/sweep flags with primary-variant defended/reclaim/accept
    out_cols = [
        "entity_id",
        "touched",
        "defended",
        "swept",
        "swept_reclaimed",
        "consumed_accepted",
        "primary_outcome",
        "mfe_frac",
        "mae_frac",
        "minutes_to_reclaim",
    ]
    prim2 = prim[[c for c in out_cols if c in prim.columns]].copy()
    # avoid duplicate touched/swept from base
    base = base.drop(columns=[c for c in ("touched", "swept") if c in base.columns], errors="ignore")
    df = base.merge(prim2, on="entity_id", how="inner")
    df = df.merge(ema_c, on="entity_id", how="left")
    df = df.merge(dest, on="entity_id", how="left")

    # reclaim MFE/MAE: use outcome mfe/mae as proxy after touch (documented)
    df["mfe_after_reclaim"] = np.where(df["swept_reclaimed"].astype(bool), df["mfe_frac"], np.nan)
    df["mae_before_reclaim"] = np.where(df["swept_reclaimed"].astype(bool), df["mae_frac"], np.nan)

    df["known_at_ts"] = df["known_at"].map(_parse_ts)
    df["utc_day"] = df["known_at_ts"].dt.strftime("%Y-%m-%d")

    def _tf_min(tf: Any) -> float:
        t = str(tf).lower()
        if t.endswith("m"):
            return float(t[:-1])
        if t.endswith("h"):
            return float(t[:-1]) * 60.0
        return 15.0

    # clusters may lack bars_to_touch; recover from minutes_to_touch
    miss = df["bars_to_touch"].isna() & df["minutes_to_touch"].notna()
    df.loc[miss, "bars_to_touch"] = [
        float(m) / _tf_min(tf)
        for m, tf in zip(df.loc[miss, "minutes_to_touch"], df.loc[miss, "timeframe"])
    ]
    miss_s = df["bars_to_sweep"].isna() & df["minutes_to_sweep"].notna()
    df.loc[miss_s, "bars_to_sweep"] = [
        float(m) / _tf_min(tf)
        for m, tf in zip(df.loc[miss_s, "minutes_to_sweep"], df.loc[miss_s, "timeframe"])
    ]

    df["distance_atr_bucket"] = df["distance_from_price_atr"].map(distance_atr_bucket)
    df["distance_pct_bucket"] = [
        distance_pct_bucket(d, c)
        for d, c in zip(df["distance_from_price"], df["center_price"])
    ]
    df["touch_timing"] = [
        touch_timing(b, bool(t)) for b, t in zip(df["bars_to_touch"], df["touched"])
    ]
    df["age_at_touch_bucket"] = [
        age_at_touch_bucket(b, bool(t)) for b, t in zip(df["bars_to_touch"], df["touched"])
    ]
    df["component_bucket"] = df["n_components"].map(component_bucket)

    # pool_width_atr from distance fields when possible
    atr = []
    for dist, datr in zip(df["distance_from_price"], df["distance_from_price_atr"]):
        if datr is None or (isinstance(datr, float) and (np.isnan(datr) or datr == 0)):
            atr.append(np.nan)
        else:
            atr.append(abs(float(dist) / float(datr)) if float(datr) != 0 else np.nan)
    df["atr_at_known"] = atr
    df["pool_width_atr"] = df["pool_width"] / df["atr_at_known"]
    df["width_atr_bucket"] = pd.cut(
        df["pool_width_atr"].abs(),
        bins=[-np.inf, 0.25, 0.5, 1.0, 2.0, np.inf],
        labels=["0-0.25", "0.25-0.5", "0.5-1", "1-2", ">2"],
    ).astype(str)

    # EMA confluence flags (at CREATED / known_at): pool box overlaps EMA level
    half = df["pool_width"] / 2.0
    df["overlaps_ema20"] = (df["dist_pool_ema20"].abs() * df["ema20"].abs()) <= half
    df["overlaps_ema59"] = (df["dist_pool_ema59"].abs() * df["ema59"].abs()) <= half
    df["overlaps_ema200"] = (df["dist_pool_ema200"].abs() * df["ema200"].abs()) <= half
    df["above_ema200"] = df["pool_vs_ema200"] == "above"
    df["below_ema200"] = df["pool_vs_ema200"] == "below"
    df["between_ema20_59"] = df["pool_between_ema20_59"].fillna(False).astype(bool)
    df["bullish_stack"] = df["ema_regime"].astype(str).str.startswith("bullish") | (
        df["ema_order"].astype(str).str.contains("bullish", na=False)
    )
    df["bearish_stack"] = df["ema_regime"].astype(str).str.startswith("bearish") | (
        df["ema_order"].astype(str).str.contains("bearish", na=False)
    )
    df["mixed_stack"] = ~(df["bullish_stack"] | df["bearish_stack"])
    # compression vs expansion relative median
    med_c = df["ema_compression"].median()
    df["ema_compressed"] = df["ema_compression"] <= med_c
    df["ema_expanded"] = df["ema_compression"] > med_c

    # reaction-aligned slope: BID expects bounce (positive ema20 slope helpful); ASK rejection (negative)
    df["ema20_slope_with_reaction"] = np.where(
        df["side"] == "BID",
        df["ema20_slope"] > 0,
        df["ema20_slope"] < 0,
    )
    df["ema20_slope_against_reaction"] = np.where(
        df["side"] == "BID",
        df["ema20_slope"] < 0,
        df["ema20_slope"] > 0,
    )

    df["multi_pool"] = df["n_components"] >= 2
    df["multi_4plus"] = df["n_components"] >= 4
    df["multi_6plus"] = df["n_components"] >= 6
    n_ema_bands = (
        df["overlaps_ema20"].astype(int)
        + df["overlaps_ema59"].astype(int)
        + df["overlaps_ema200"].astype(int)
    )
    df["n_ema_overlaps"] = n_ema_bands
    df["multi_no_ema"] = df["multi_pool"] & (n_ema_bands == 0)
    df["multi_ema20"] = df["multi_pool"] & df["overlaps_ema20"]
    df["multi_ema59"] = df["multi_pool"] & df["overlaps_ema59"]
    df["multi_ema200"] = df["multi_pool"] & df["overlaps_ema200"]
    df["multi_multi_ema"] = df["multi_pool"] & (n_ema_bands >= 2)

    return df

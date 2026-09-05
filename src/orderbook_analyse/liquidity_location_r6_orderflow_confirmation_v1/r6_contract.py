"""Freeze R6 episodes from V2 with parity checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import PRIMARY_VARIANT, R6_CONTRACT

DEFAULT_V2 = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_pool_edge_validation_v2"
)
DEFAULT_V1 = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_pool_lifecycle_v1"
)


def _ts(x: Any) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def load_primary_outcomes(v1: Path) -> pd.DataFrame:
    out = pd.read_csv(v1 / "pool_outcomes.csv", low_memory=False)
    prim = out[
        (out["acceptance_bars"] == PRIMARY_VARIANT["acceptance_bars"])
        & (out["reclaim_horizon_bars"] == PRIMARY_VARIANT["reclaim_horizon_bars"])
        & (
            out["reaction_atr_mult"].isna()
            | (out["reaction_atr_mult"] == PRIMARY_VARIANT["reaction_atr_mult"])
            | (out["swept"] == True)  # noqa: E712
        )
    ].copy()
    return prim.sort_values(["entity_id", "reaction_atr_mult"], na_position="first").groupby(
        "entity_id", as_index=False
    ).first()


def select_r6_entities(v2: Path) -> pd.DataFrame:
    ent = pd.read_csv(v2 / "entity_enriched.csv", low_memory=False)
    buckets = R6_CONTRACT["distance_atr_buckets"]
    r6 = ent[
        (ent["multi_6plus"] == True)  # noqa: E712
        & (ent["touch_timing"] == "delayed_touch")
        & (ent["distance_atr_bucket"].isin(buckets))
    ].copy()
    return r6


def build_r6_episodes(
    *,
    v2_dir: Path = DEFAULT_V2,
    v1_dir: Path = DEFAULT_V1,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One row per independent episode that contains ≥1 V2-R6 entity."""
    r6_ent = select_r6_entities(v2_dir)
    ep_all = pd.read_csv(v2_dir / "independent_episodes.csv", low_memory=False)
    ep_ids = sorted(r6_ent["episode_id"].dropna().astype(str).unique())
    ep = ep_all[ep_all["episode_id"].astype(str).isin(ep_ids)].copy()

    prim = load_primary_outcomes(v1_dir)
    # leader = R6 entity with max n_components, then earliest known_at
    leaders = r6_ent.sort_values(
        ["episode_id", "n_components", "known_at"], ascending=[True, False, True]
    ).groupby("episode_id", as_index=False).first()

    # attach outcome times from primary
    leaders = leaders.merge(
        prim[
            [
                c
                for c in [
                    "entity_id",
                    "primary_outcome",
                    "defended",
                    "swept_reclaimed",
                    "consumed_accepted",
                    "swept",
                    "touched",
                    "first_touch_time",
                    "sweep_time",
                    "reclaim_time",
                    "defend_time",
                    "minutes_to_touch",
                    "mfe_frac",
                    "mae_frac",
                    "lower_price",
                    "upper_price",
                    "strength",
                ]
                if c in prim.columns
            ]
        ],
        left_on="entity_id",
        right_on="entity_id",
        how="left",
        suffixes=("", "_prim"),
    )

    # approach index / first touch from v1 instances/clusters
    pools = pd.read_csv(
        v1_dir / "pool_instances.csv",
        usecols=lambda c: c
        in {
            "pool_id",
            "known_at",
            "first_approach_index",
            "first_touch_index",
            "analysis_start_index",
            "bars_to_touch",
            "minutes_to_touch",
            "distance_from_price_atr",
            "lower_price",
            "upper_price",
            "strength",
            "timeframe",
        },
        low_memory=False,
    )
    clusters = pd.read_csv(
        v1_dir / "pool_clusters.csv",
        usecols=lambda c: c
        in {
            "cluster_id",
            "known_at",
            "first_approach_index",
            "first_touch_index",
            "analysis_start_index",
            "bars_to_touch",
            "minutes_to_touch",
            "distance_from_price_atr",
            "lower_price",
            "upper_price",
            "total_strength",
            "number_of_component_pools",
            "timeframe",
            "component_pools",
        },
        low_memory=False,
    )

    rows = []
    for _, lead in leaders.iterrows():
        eid = str(lead["episode_id"])
        ep_row = ep[ep["episode_id"].astype(str) == eid]
        if ep_row.empty:
            continue
        epr = ep_row.iloc[0]
        entity_id = str(lead["entity_id"])
        # geometry / indices
        if entity_id.startswith("lldc:"):
            src = clusters[clusters["cluster_id"] == entity_id]
            strength0 = float(src.iloc[0]["total_strength"]) if len(src) and pd.notna(src.iloc[0].get("total_strength")) else lead.get("strength")
            n_comp = int(src.iloc[0]["number_of_component_pools"]) if len(src) else int(lead["n_components"])
            components = str(src.iloc[0].get("component_pools") or "") if len(src) else ""
            lower = float(src.iloc[0]["lower_price"]) if len(src) else float(lead.get("lower_price") or np.nan)
            upper = float(src.iloc[0]["upper_price"]) if len(src) else float(lead.get("upper_price") or np.nan)
            ap_i = src.iloc[0].get("first_approach_index") if len(src) else None
            ft_i = src.iloc[0].get("first_touch_index") if len(src) else None
            as_i = src.iloc[0].get("analysis_start_index") if len(src) else None
            dist = src.iloc[0].get("distance_from_price_atr") if len(src) else lead.get("distance_from_price_atr")
        else:
            src = pools[pools["pool_id"] == entity_id]
            strength0 = float(src.iloc[0]["strength"]) if len(src) and pd.notna(src.iloc[0].get("strength")) else lead.get("strength")
            n_comp = int(lead["n_components"])
            components = entity_id
            lower = float(src.iloc[0]["lower_price"]) if len(src) else float(lead.get("lower_price") or np.nan)
            upper = float(src.iloc[0]["upper_price"]) if len(src) else float(lead.get("upper_price") or np.nan)
            ap_i = src.iloc[0].get("first_approach_index") if len(src) else None
            ft_i = src.iloc[0].get("first_touch_index") if len(src) else None
            as_i = src.iloc[0].get("analysis_start_index") if len(src) else None
            dist = src.iloc[0].get("distance_from_price_atr") if len(src) else None

        known_at = _ts(lead["known_at"])
        ft_time = lead.get("first_touch_time")
        if pd.isna(ft_time) and pd.notna(lead.get("minutes_to_touch")):
            ft_time = known_at + pd.Timedelta(minutes=float(lead["minutes_to_touch"]))
        else:
            ft_time = _ts(ft_time) if pd.notna(ft_time) else pd.NaT

        # approach time: if approach index available, estimate from known_at + bars
        tf = str(lead["timeframe"])
        tfm = 5 if tf.startswith("5") else 15 if tf.startswith("15") else 30
        approach_at = pd.NaT
        if pd.notna(ap_i) and pd.notna(as_i):
            approach_at = known_at + pd.Timedelta(minutes=tfm * max(0, int(ap_i) - int(as_i)))
        elif pd.notna(ft_time):
            approach_at = ft_time - pd.Timedelta(minutes=tfm)  # conservative proxy

        primary = str(lead.get("primary_outcome") or "NONE")
        label = primary
        if primary not in {"DEFENDED", "SWEPT_RECLAIMED", "CONSUMED_ACCEPTED"}:
            # map from flags
            if bool(lead.get("defended")):
                label = "DEFENDED"
            elif bool(lead.get("swept_reclaimed")):
                label = "SWEPT_RECLAIMED"
            elif bool(lead.get("consumed_accepted")):
                label = "CONSUMED_ACCEPTED"
            else:
                label = "unresolved"

        rows.append(
            {
                "episode_id": eid,
                "leader_entity_id": entity_id,
                "symbol": lead["symbol"],
                "timeframe": tf,
                "side": lead["side"],
                "n_members": int(epr["n_members"]),
                "member_ids": epr["member_ids"],
                "n_components": n_comp,
                "component_ids": components,
                "known_at": known_at.isoformat(),
                "approach_at": None if pd.isna(approach_at) else approach_at.isoformat(),
                "first_touch_at": None if pd.isna(ft_time) else ft_time.isoformat(),
                "sweep_at": None if pd.isna(lead.get("sweep_time")) else str(lead.get("sweep_time")),
                "reclaim_at": None if pd.isna(lead.get("reclaim_time")) else str(lead.get("reclaim_time")),
                "defend_at": None if pd.isna(lead.get("defend_time")) else str(lead.get("defend_time")),
                "lower_price": lower,
                "upper_price": upper,
                "center_price": (lower + upper) / 2.0 if pd.notna(lower) and pd.notna(upper) else np.nan,
                "strength_at_known": strength0,
                "distance_from_price_atr": float(dist) if pd.notna(dist) else np.nan,
                "distance_atr_bucket": lead["distance_atr_bucket"],
                "touch_timing": lead["touch_timing"],
                "approach_regime": lead.get("approach_regime"),
                "v2_temporal_split": lead.get("temporal_split"),
                "label_primary": label,
                "defended": bool(lead.get("defended")),
                "swept_reclaimed": bool(lead.get("swept_reclaimed")),
                "consumed_accepted": bool(lead.get("consumed_accepted")),
                "swept": bool(lead.get("swept")),
                "touched": bool(lead.get("touched")),
                "minutes_to_touch": lead.get("minutes_to_touch"),
                "bars_to_touch": lead.get("bars_to_touch") if "bars_to_touch" in lead else np.nan,
                "first_approach_index": ap_i,
                "first_touch_index": ft_i,
                "analysis_start_index": as_i,
                "mfe_frac": lead.get("mfe_frac"),
                "mae_frac": lead.get("mae_frac"),
                "r6_contract": "V2_R6",
            }
        )

    df = pd.DataFrame(rows)
    # parity report
    parity = {
        "v2_r6_entity_count": int(len(r6_ent)),
        "v2_r6_unique_episode_ids": int(len(ep_ids)),
        "phase3_episode_count": int(len(df)),
        "episode_id_set_equal": set(df["episode_id"].astype(str)) == set(ep_ids),
        "missing_episode_ids": sorted(set(ep_ids) - set(df["episode_id"].astype(str))),
        "extra_episode_ids": sorted(set(df["episode_id"].astype(str)) - set(ep_ids)),
        "touch_timing_all_delayed": bool((df["touch_timing"] == "delayed_touch").all()) if len(df) else False,
        "min_components_ge_6": bool((df["n_components"] >= 6).all()) if len(df) else False,
        "distance_atr_ge_0_5": bool(
            df["distance_atr_bucket"].isin(R6_CONTRACT["distance_atr_buckets"]).all()
        )
        if len(df)
        else False,
        "contract": R6_CONTRACT,
    }
    return df, parity

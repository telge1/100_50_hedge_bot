"""Deterministic matched controls for aggregate proxy discovery."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import FlushCluster


def _flush_minutes_set(clusters: list[FlushCluster]) -> set[str]:
    minutes: set[str] = set()
    for cluster in clusters:
        start = pd.Timestamp(cluster.cluster_start)
        end = pd.Timestamp(cluster.cluster_end)
        for minute in pd.date_range(start, end, freq="1min", tz="UTC"):
            minutes.add(minute.isoformat().replace("+00:00", "Z"))
    return minutes


def build_matched_controls(
    minute_features: pd.DataFrame,
    flush_candidates: pd.DataFrame,
    clusters: list[FlushCluster],
    *,
    max_controls: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match non-flush minutes causally by direction, hour, and prior displacement."""
    flush_minutes = _flush_minutes_set(clusters)
    flush_ids = {str(r["candidate_id"]) for _, r in flush_candidates.iterrows()}
    cluster_primary = {c.primary_candidate_id for c in clusters}

    cluster_profiles: list[dict[str, Any]] = []
    for cluster in clusters:
        primary = flush_candidates[
            flush_candidates["candidate_id"] == cluster.primary_candidate_id
        ]
        if primary.empty:
            continue
        row = primary.iloc[0]
        minute = str(row["minute"])
        direction = str(row["direction"])
        feat = minute_features[
            (minute_features["minute"] == minute) & (minute_features["direction"] == direction)
        ]
        if feat.empty:
            cluster_profiles.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "matchable": False,
                    "reason": "missing_minute_features",
                }
            )
            continue
        f = feat.iloc[0]
        prev_minute = (
            pd.Timestamp(minute) - pd.Timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        prev_feat = minute_features[
            (minute_features["minute"] == prev_minute)
            & (minute_features["direction"] == direction)
        ]
        prev_disp = (
            abs(float(prev_feat.iloc[0]["price_displacement_pct"]))
            if not prev_feat.empty
            else 0.0
        )
        cluster_profiles.append(
            {
                "cluster_id": cluster.cluster_id,
                "matchable": True,
                "direction": direction,
                "hour": pd.Timestamp(minute).hour,
                "target_displacement": abs(float(f.get("price_displacement_pct") or 0)),
                "prev_displacement": prev_disp,
            }
        )

    candidates_pool = minute_features[
        (~minute_features["minute"].isin(flush_minutes))
        & (minute_features["directional_flush_observed"] == False)  # noqa: E712
        & (minute_features["technical_gap"] == False)  # noqa: E712
        & (minute_features["trades_present"] == True)  # noqa: E712
        & (minute_features["oi_state_valid"] == True)  # noqa: E712
        & (minute_features["orderbook_present"] == True)  # noqa: E712
    ].copy()

    controls: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    used_minutes: set[str] = set()

    matchable_clusters = [p for p in cluster_profiles if p.get("matchable")]
    for profile in matchable_clusters:
        direction = profile["direction"]
        hour = profile["hour"]
        target = profile["target_displacement"]
        prev_target = profile["prev_displacement"]
        pool = candidates_pool[
            (candidates_pool["direction"] == direction)
            & (~candidates_pool["minute"].isin(used_minutes))
        ].copy()
        if pool.empty:
            unmatched.append(
                {"cluster_id": profile["cluster_id"], "reason": "empty_control_pool"}
            )
            continue
        pool["hour"] = pool["minute"].apply(lambda m: pd.Timestamp(m).hour)
        pool = pool[pool["hour"] == hour]
        if pool.empty:
            unmatched.append(
                {"cluster_id": profile["cluster_id"], "reason": "no_same_hour_control"}
            )
            continue
        pool["prev_minute"] = pool["minute"].apply(
            lambda m: (pd.Timestamp(m) - pd.Timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        )
        prev_lookup = minute_features.set_index(["minute", "direction"])["price_displacement_pct"]
        pool["prev_disp"] = pool.apply(
            lambda r: abs(
                float(prev_lookup.get((r["prev_minute"], r["direction"]), 0) or 0)
            ),
            axis=1,
        )
        pool["disp_dist"] = (pool["price_displacement_pct"].abs() - target).abs()
        pool["prev_disp_dist"] = (pool["prev_disp"] - prev_target).abs()
        pool["match_distance"] = pool["disp_dist"] + pool["prev_disp_dist"]
        best = pool.sort_values(["match_distance", "minute"]).iloc[0]
        control_id = f"proxyctrl:{profile['cluster_id']}:{best['minute']}"
        used_minutes.add(str(best["minute"]))
        controls.append(
            {
                "control_id": control_id,
                "matched_cluster_id": profile["cluster_id"],
                "symbol": best["symbol"],
                "direction": direction,
                "control_minute": best["minute"],
                "match_distance": float(best["match_distance"]),
                "hour": hour,
                "control_displacement_pct": float(best["price_displacement_pct"]),
                "cluster_target_displacement_pct": target,
            }
        )

    if max_controls is not None:
        controls = controls[:max_controls]
    return controls, unmatched

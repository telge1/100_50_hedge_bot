"""Post-compression outcome metrics (read-only, no classification leakage)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def _ts(value: Any) -> pd.Timestamp | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return pd.Timestamp(text)


def adverse_extension(mid: float | None, anchor_mid: float | None, direction: str) -> float | None:
    if mid is None or anchor_mid is None:
        return None
    if direction == "LONG":
        return max(0.0, anchor_mid - mid)
    return max(0.0, mid - anchor_mid)


def episode_signed_move(start_mid: float, end_mid: float, direction: str) -> float:
    if direction == "LONG":
        return end_mid - start_mid
    return start_mid - end_mid


def adverse_signed_move(start_mid: float, end_mid: float, direction: str) -> float:
    if direction == "LONG":
        return start_mid - end_mid
    return end_mid - start_mid


def classification_window_end_second(
    timeline: pd.DataFrame,
    *,
    direction: str,
    window_size: int,
    last_prefix: str,
) -> str | None:
    if timeline.empty:
        return None
    post = timeline[
        (timeline["phase"] == "POST_CLUSTER")
        & timeline["is_genuine"].astype(str).str.lower().isin({"true", "1"})
    ].copy()
    if post.empty:
        return None
    post["second_ts"] = post["second"].map(_ts)
    post = post.dropna(subset=["second_ts"]).sort_values("second_ts")
    trade_active = post[post["aggressive_notional_1s"].fillna(0) > 0]
    if trade_active.empty:
        return None
    if last_prefix == "last5":
        size = 5
    elif last_prefix == "last10":
        size = 10
    elif last_prefix == "second_half":
        n = len(trade_active)
        half = max(1, n // 2)
        slice_frame = trade_active.iloc[half:]
        if slice_frame.empty:
            return None
        end_ts = slice_frame["second_ts"].max()
        return end_ts.isoformat().replace("+00:00", "Z")
    else:
        size = window_size
    tail = trade_active.tail(min(size, len(trade_active)))
    if tail.empty:
        return None
    end_ts = tail["second_ts"].max()
    return end_ts.isoformat().replace("+00:00", "Z")


def compute_post_compression_outcomes(
    classifications: list[dict[str, Any]],
    *,
    events: pd.DataFrame,
    reclaims: pd.DataFrame,
    timeline_by_cluster: dict[str, pd.DataFrame],
    horizons: tuple[int, ...],
) -> list[dict[str, Any]]:
    event_lookup = events.set_index("cluster_id", drop=False)
    reclaim_lookup = reclaims[
        reclaims["reclaim_anchor"] == "PRE_FLUSH_CLOSE"
    ].copy()
    reclaim_by_cluster: dict[str, pd.DataFrame] = {}
    for cluster_id, group in reclaim_lookup.groupby("cluster_id"):
        reclaim_by_cluster[str(cluster_id)] = group

    rows: list[dict[str, Any]] = []
    for item in classifications:
        cluster_id = str(item["cluster_id"])
        direction = str(item["direction"])
        comparison_pair = str(item["comparison_pair"])
        category = str(item["category"])
        if cluster_id not in event_lookup.index:
            continue
        event = event_lookup.loc[cluster_id]
        if isinstance(event, pd.DataFrame):
            event = event.iloc[0]
        timeline = timeline_by_cluster.get(cluster_id)
        if timeline is None or timeline.empty:
            continue

        _, _, last_prefix = next(
            (pair, first_p, last_p)
            for pair, first_p, last_p in (
                ("first5_last5", "first5", "last5"),
                ("first10_last10", "first10", "last10"),
                ("first_half_second_half", "first_half", "second_half"),
            )
            if pair == comparison_pair
        )
        window_end = classification_window_end_second(
            timeline,
            direction=direction,
            window_size=10,
            last_prefix=last_prefix,
        )
        if window_end is None:
            continue
        outcome_start = _ts(window_end)
        if outcome_start is None:
            continue

        anchor_mid = float(event["anchor_mid"]) if pd.notna(event.get("anchor_mid")) else None
        pre_flush_rows = reclaim_by_cluster.get(cluster_id)
        pre_flush_close = None
        if pre_flush_rows is not None and not pre_flush_rows.empty:
            pre_flush_close = float(pre_flush_rows.iloc[0]["anchor_price"])

        timeline_local = timeline.copy()
        timeline_local["second_ts"] = timeline_local["second"].map(_ts)
        timeline_local = timeline_local.dropna(subset=["second_ts"]).sort_values("second_ts")
        genuine = timeline_local[
            timeline_local["is_genuine"].astype(str).str.lower().isin({"true", "1"})
        ]
        start_row = genuine[genuine["second_ts"] <= outcome_start].tail(1)
        if start_row.empty:
            continue
        start_mid = float(start_row.iloc[0]["mid_price"])
        if pd.isna(start_mid):
            continue
        post = genuine[genuine["second_ts"] > outcome_start]
        if post.empty:
            continue

        for horizon in horizons:
            horizon_end = outcome_start + pd.Timedelta(minutes=horizon)
            window = post[post["second_ts"] <= horizon_end]
            if window.empty:
                continue
            mids = window["mid_price"].dropna().astype(float)
            if mids.empty:
                continue
            end_mid = float(mids.iloc[-1])
            forward_return = episode_signed_move(start_mid, end_mid, direction)
            mfe = max(episode_signed_move(start_mid, float(mid), direction) for mid in mids)
            mae = max(adverse_signed_move(start_mid, float(mid), direction) for mid in mids)
            further_adverse = None
            if anchor_mid is not None:
                if direction == "LONG":
                    further_adverse = max(max(0.0, start_mid - float(mid)) for mid in mids)
                else:
                    further_adverse = max(max(0.0, float(mid) - start_mid) for mid in mids)

            reclaimed = False
            minutes_to_reclaim = None
            if pre_flush_close is not None:
                for _, row in window.iterrows():
                    mid = row.get("mid_price")
                    if mid is None or pd.isna(mid):
                        continue
                    mid_f = float(mid)
                    if direction == "LONG" and mid_f >= pre_flush_close:
                        reclaimed = True
                        minutes_to_reclaim = (
                            pd.Timestamp(row["second_ts"]) - outcome_start
                        ).total_seconds() / 60.0
                        break
                    if direction == "SHORT" and mid_f <= pre_flush_close:
                        reclaimed = True
                        minutes_to_reclaim = (
                            pd.Timestamp(row["second_ts"]) - outcome_start
                        ).total_seconds() / 60.0
                        break

            rows.append(
                {
                    "cluster_id": cluster_id,
                    "direction": direction,
                    "comparison_pair": comparison_pair,
                    "category": category,
                    "horizon_minutes": horizon,
                    "outcome_start_second": window_end,
                    "pre_flush_close_reclaim": reclaimed,
                    "minutes_to_pre_flush_close_reclaim": minutes_to_reclaim,
                    "max_further_adverse_extension": further_adverse,
                    "forward_return_episode_direction": forward_return,
                    "mfe_episode_direction": mfe,
                    "mae_episode_direction": mae,
                }
            )
    return rows


def enrich_classification_context(
    row: dict[str, Any],
    *,
    event: pd.Series,
    recovery: pd.Series | None,
    flip: pd.Series | None,
    timeline: pd.DataFrame | None,
    comparison_pair: str,
) -> dict[str, Any]:
    direction = str(row["direction"])
    anchor_mid = float(event["anchor_mid"]) if pd.notna(event.get("anchor_mid")) else None
    adverse_mid = (
        float(event["adverse_extreme_mid"])
        if pd.notna(event.get("adverse_extreme_mid"))
        else None
    )
    row["adverse_extension_at_anchor"] = adverse_extension(
        adverse_mid, anchor_mid, direction
    )
    row["aggregate_depth_recovery_observed"] = (
        bool(recovery["aggregate_depth_recovery_observed"])
        if recovery is not None and pd.notna(recovery.get("aggregate_depth_recovery_observed"))
        else None
    )
    row["flip_tradeflow_second"] = (
        flip.get("flip_tradeflow_second") if flip is not None else None
    )
    row["flip_ofi_second"] = flip.get("flip_ofi_second") if flip is not None else None
    row["flip_microprice_second"] = (
        flip.get("flip_microprice_second") if flip is not None else None
    )
    if timeline is not None and not timeline.empty:
        _, _, last_prefix = next(
            (pair, first_p, last_p)
            for pair, first_p, last_p in (
                ("first5_last5", "first5", "last5"),
                ("first10_last10", "first10", "last10"),
                ("first_half_second_half", "first_half", "second_half"),
            )
            if pair == comparison_pair
        )
        row["classification_window_end_second"] = classification_window_end_second(
            timeline,
            direction=direction,
            window_size=10,
            last_prefix=last_prefix,
        )
    else:
        row["classification_window_end_second"] = None
    return row

"""Deterministic candidate selection (outcome-blind)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def effective_detection_available_at(
    trigger_ts: pd.Timestamp,
    detection_available_at: pd.Timestamp | None,
    *,
    bucket_seconds: int = 1,
) -> pd.Timestamp:
    """Normalize episode-v1 field to exclusive causal end = bucket close."""
    t = pd.to_datetime(trigger_ts, utc=True)
    d = pd.to_datetime(detection_available_at, utc=True) if detection_available_at is not None else t
    if d <= t:
        return t + pd.Timedelta(seconds=bucket_seconds)
    return d


def select_candidates(
    candidates: pd.DataFrame,
    *,
    cfg: dict[str, Any],
    max_candidates: int,
    candidate_type: str | None = None,
    candidate_id: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Return (selected, not_selected, manifest). Selection uses only pre-trigger fields."""
    df = candidates.copy()
    df["trigger_ts"] = pd.to_datetime(df["trigger_ts"], utc=True)
    df["detection_available_at"] = pd.to_datetime(df["detection_available_at"], utc=True)
    bucket = int(cfg.get("state_bucket_seconds", 1))
    df["detection_available_at_effective"] = [
        effective_detection_available_at(r.trigger_ts, r.detection_available_at, bucket_seconds=bucket)
        for r in df.itertuples()
    ]
    df["context_start_drilldown"] = df["trigger_ts"] - pd.Timedelta(seconds=int(cfg["context_lookback_seconds"]))

    if candidate_id:
        sel = df[df["candidate_id"] == candidate_id].copy()
        reason = "explicit_candidate_id"
        if sel.empty:
            raise ValueError(f"candidate_id not found: {candidate_id}")
        not_sel = df[df["candidate_id"] != candidate_id].copy()
        not_sel["selection_reason"] = "not_requested_id"
        sel["selection_reason"] = reason
        man = {"mode": reason, "n_selected": int(len(sel)), "max_candidates": max_candidates}
        return sel.reset_index(drop=True), not_sel.reset_index(drop=True), man

    if candidate_type:
        pool = df[df["candidate_type"] == candidate_type].copy()
    else:
        pool = df

    priority = {t: i for i, t in enumerate(cfg.get("selection_type_priority") or [])}
    pool = pool.copy()
    pool["_type_rank"] = pool["candidate_type"].map(lambda t: priority.get(t, 10_000))
    pool = pool.sort_values(
        ["_type_rank", "direction_hint", "trigger_ts", "candidate_id"],
        kind="mergesort",
    ).reset_index(drop=True)

    max_per = int(cfg.get("max_per_candidate_type", 3))
    selected_rows: list[pd.Series] = []
    per_type: dict[str, int] = {}
    selected_ids: set[str] = set()
    # diversify trigger seconds: prefer unused trigger_ts when same type quota remains
    used_ts: set[pd.Timestamp] = set()

    for _, row in pool.iterrows():
        if len(selected_rows) >= max_candidates:
            break
        ctype = str(row["candidate_type"])
        if per_type.get(ctype, 0) >= max_per:
            continue
        ts = row["trigger_ts"]
        # first pass prefer unused timestamps; second pass fills remainder
        if ts in used_ts and per_type.get(ctype, 0) < max_per:
            # allow if we still need diversity within type but timestamp already used by other type
            pass
        selected_rows.append(row)
        selected_ids.add(str(row["candidate_id"]))
        per_type[ctype] = per_type.get(ctype, 0) + 1
        used_ts.add(ts)

    # If under max due to type caps, fill remaining by global order ignoring per-type if needed
    if len(selected_rows) < max_candidates:
        for _, row in pool.iterrows():
            if len(selected_rows) >= max_candidates:
                break
            cid = str(row["candidate_id"])
            if cid in selected_ids:
                continue
            selected_rows.append(row)
            selected_ids.add(cid)

    sel = pd.DataFrame(selected_rows).reset_index(drop=True) if selected_rows else pool.iloc[0:0].copy()
    if not sel.empty:
        sel["selection_reason"] = "deterministic_stratified_priority_cap"
        sel = sel.drop(columns=[c for c in sel.columns if c.startswith("_")], errors="ignore")
    not_sel = df[~df["candidate_id"].isin(selected_ids)].copy()
    not_sel["selection_reason"] = "not_selected_cap_or_priority"
    man = {
        "mode": "deterministic_stratified",
        "n_pool": int(len(pool)),
        "n_selected": int(len(sel)),
        "max_candidates": int(max_candidates),
        "max_per_candidate_type": max_per,
        "per_type_selected": per_type,
        "sort_keys": cfg.get("selection_sort_keys"),
        "type_priority": cfg.get("selection_type_priority"),
        "outcome_blind": True,
    }
    return sel, not_sel, man

"""Causal candidate grouping (no retroactive start moves)."""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd


def group_candidates(selected: pd.DataFrame, *, grouping_window_seconds: int) -> pd.DataFrame:
    if selected is None or selected.empty:
        return pd.DataFrame(
            columns=[
                "group_id",
                "first_trigger_ts",
                "detection_available_at",
                "candidate_ids",
                "candidate_types",
                "primary_candidate_id",
                "trigger_count",
            ]
        )
    df = selected.sort_values(["trigger_ts", "candidate_id"], kind="mergesort").reset_index(drop=True)
    groups: list[dict[str, Any]] = []
    open_first: pd.Timestamp | None = None
    open_members: list[pd.Series] = []

    def flush() -> None:
        nonlocal open_first, open_members
        if not open_members:
            return
        ids = [str(m["candidate_id"]) for m in open_members]
        types = [str(m["candidate_type"]) for m in open_members]
        det = max(pd.to_datetime(m["detection_available_at_effective"], utc=True) for m in open_members)
        primary = sorted(open_members, key=lambda m: (str(m["candidate_type"]), str(m["candidate_id"])))[0]
        raw = f"{open_first.isoformat()}|{ids[0]}"
        gid = hashlib.sha256(raw.encode()).hexdigest()[:24]
        groups.append(
            {
                "group_id": gid,
                "first_trigger_ts": open_first,
                "detection_available_at": det,
                "candidate_ids": ids,
                "candidate_types": types,
                "primary_candidate_id": str(primary["candidate_id"]),
                "trigger_count": len(ids),
            }
        )
        open_first = None
        open_members = []

    win = pd.Timedelta(seconds=grouping_window_seconds)
    for _, row in df.iterrows():
        ts = pd.to_datetime(row["trigger_ts"], utc=True)
        if open_first is None:
            open_first = ts
            open_members = [row]
            continue
        if ts - open_first <= win:
            open_members.append(row)
        else:
            flush()
            open_first = ts
            open_members = [row]
    flush()
    return pd.DataFrame(groups)


def merge_replay_windows(selected: pd.DataFrame) -> list[dict[str, Any]]:
    """Merge overlapping [context_start, causal_end) into replay spans."""
    if selected is None or selected.empty:
        return []
    intervals = []
    for _, r in selected.iterrows():
        intervals.append(
            (
                pd.to_datetime(r["context_start_drilldown"], utc=True),
                pd.to_datetime(r["detection_available_at_effective"], utc=True),
                str(r["candidate_id"]),
            )
        )
    intervals.sort(key=lambda x: x[0])
    merged: list[dict[str, Any]] = []
    cur_s, cur_e, ids = intervals[0][0], intervals[0][1], [intervals[0][2]]
    for s, e, cid in intervals[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
            ids.append(cid)
        else:
            merged.append({"start": cur_s, "end": cur_e, "candidate_ids": list(ids)})
            cur_s, cur_e, ids = s, e, [cid]
    merged.append({"start": cur_s, "end": cur_e, "candidate_ids": list(ids)})
    return merged

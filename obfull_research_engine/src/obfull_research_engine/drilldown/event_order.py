"""Event ordering contract for drilldown timelines."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

SOURCE_RANK = {"checkpoint": 0, "snapshot": 1, "delta": 2, "level_change": 3, "trade": 4}


def _ts_key(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return pd.to_datetime(value, utc=True).timestamp()


def sort_timeline(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Stable sort. Returns (sorted_events, ordering_confidence)."""
    if not events:
        return [], "EMPTY"

    decorated = []
    for e in events:
        src = str(e.get("event_type") or e.get("source") or "")
        rank = SOURCE_RANK.get(src, 50)
        if src == "trade":
            rank = SOURCE_RANK["trade"]
        seq = e.get("seq")
        u = e.get("u")
        seq_key = int(seq) if seq is not None else (int(u) if u is not None else 0)
        eid = str(e.get("source_event_id") or "")
        decorated.append((_ts_key(e["event_time"]), rank, seq_key, eid, e))

    decorated.sort(key=lambda x: x[:4])
    sorted_e = [x[4] for x in decorated]

    ambiguous = False
    for i in range(1, len(sorted_e)):
        a, b = sorted_e[i - 1], sorted_e[i]
        if _ts_key(a["event_time"]) == _ts_key(b["event_time"]):
            sa = str(a.get("event_type") or a.get("source"))
            sb = str(b.get("event_type") or b.get("source"))
            if (sa == "trade") != (sb == "trade") or SOURCE_RANK.get(sa, 50) != SOURCE_RANK.get(sb, 50):
                ambiguous = True
                a["ordering_flag"] = "ORDERING_AMBIGUOUS"
                b["ordering_flag"] = "ORDERING_AMBIGUOUS"
    conf = "ORDERING_AMBIGUOUS" if ambiguous else "ORDERING_DETERMINISTIC_CONTRACT"
    return sorted_e, conf

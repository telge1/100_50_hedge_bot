"""Selection helpers: pilot, first-touch, non-overlapping confirmed 4h."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .params import MANUAL_CASE_TRIGGERS_UTC, NS, PILOT_EXTRA_MAX


def _parse_utc(s: str) -> datetime:
    # allow with/without Z and fractional seconds
    t = s.replace("Z", "")
    if "." in t:
        return datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(t).replace(tzinfo=timezone.utc)


def _dt_to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * NS)


MANUAL_TRIGGER_NS = tuple(_dt_to_ns(_parse_utc(s)) for s in MANUAL_CASE_TRIGGERS_UTC)


def is_manual_case(event: dict[str, Any], tol_ns: int = 2 * NS) -> bool:
    trig = int(event["trigger_ts_ns"])
    return any(abs(trig - m) <= tol_ns for m in MANUAL_TRIGGER_NS)


def find_manual_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for m in MANUAL_TRIGGER_NS:
        best = None
        best_d = None
        for e in events:
            if e.get("trade_side") not in ("LONG", "SHORT"):
                continue
            d = abs(int(e["trigger_ts_ns"]) - m)
            if best_d is None or d < best_d:
                best_d = d
                best = e
        if best is not None and best_d is not None and best_d <= 2 * NS:
            found.append(best)
    return found


def select_pilot_events(
    events: list[dict[str, Any]],
    wall_flags: dict[str, bool],
    *,
    extra_max: int = PILOT_EXTRA_MAX,
) -> list[dict[str, Any]]:
    manuals = find_manual_events(events)
    selected = list(manuals)
    selected_ids = {str(e["event_id"]) for e in selected}
    by_key: dict[tuple, list] = defaultdict(list)
    for e in events:
        eid = str(e["event_id"])
        if eid in selected_ids:
            continue
        if e.get("trade_side") not in ("LONG", "SHORT"):
            continue
        if e.get("label_price_only") not in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
            continue
        key = (
            str(e.get("label_price_only")),
            str(e.get("trade_side")),
            bool(wall_flags.get(eid, False)),
            str(e.get("event_role")),
        )
        by_key[key].append(e)
    keys = sorted(by_key.keys(), key=lambda x: str(x))
    # deterministic: sort each bucket by trigger_ts then event_id
    for k in keys:
        by_key[k].sort(key=lambda e: (int(e["trigger_ts_ns"]), str(e["event_id"])))
    while len(selected) < len(manuals) + extra_max and keys:
        progressed = False
        for k in list(keys):
            if not by_key[k]:
                keys.remove(k)
                continue
            e = by_key[k].pop(0)
            selected.append(e)
            progressed = True
            if len(selected) >= len(manuals) + extra_max:
                break
        if not progressed:
            break
    return selected


def build_non_overlapping_confirmed_4h(
    confirmed_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Chronological confirmed entries; after each keepout 4h (no outcome used)."""
    rows = sorted(
        [r for r in confirmed_rows if r.get("entry_ts_ns") is not None],
        key=lambda r: (int(r["entry_ts_ns"]), str(r["event_id"])),
    )
    keepout = 4 * 3600 * NS
    out: list[dict[str, Any]] = []
    last_end = None
    for r in rows:
        ts = int(r["entry_ts_ns"])
        if last_end is not None and ts < last_end:
            continue
        out.append(dict(r))
        last_end = ts + keepout
    return out


def first_touch_ids(selection_rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(r["event_id"])
        for r in selection_rows
        if r.get("policy") == "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION"
    }

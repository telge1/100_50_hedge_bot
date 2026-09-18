"""Causal selection policies including NON_OVERLAPPING_4H."""

from __future__ import annotations

from typing import Any

from .params import NS


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def build_non_overlapping_4h(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chronological causal selection: after pick, suppress 4h; no outcome use."""
    eligible = []
    for e in events:
        trig = e.get("trigger_ts_ns")
        if trig in (None, "", "None"):
            continue
        side = e.get("trade_side")
        if side not in ("LONG", "SHORT"):
            continue
        price = e.get("trigger_price")
        if price in (None, "", "None"):
            continue
        eligible.append(e)
    eligible.sort(key=lambda e: (int(e["trigger_ts_ns"]), e["event_id"]))
    selected: list[str] = []
    blocked_until = -1
    rows = []
    for e in eligible:
        ts = int(e["trigger_ts_ns"])
        ok = ts >= blocked_until
        if ok:
            selected.append(e["event_id"])
            blocked_until = ts + 4 * 3600 * NS
        rows.append(
            {
                "event_id": e["event_id"],
                "trigger_ts_ns": ts,
                "selected_non_overlapping_4h": ok,
                "blocked_until_ns": blocked_until if ok else None,
            }
        )
    # also mark non-eligible as false
    seen = {r["event_id"] for r in rows}
    for e in events:
        if e["event_id"] not in seen:
            rows.append(
                {
                    "event_id": e["event_id"],
                    "trigger_ts_ns": e.get("trigger_ts_ns"),
                    "selected_non_overlapping_4h": False,
                    "blocked_until_ns": None,
                }
            )
    return rows


def policy_ids(
    *,
    events: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    non_ov_4h: list[dict[str, Any]],
    name: str,
) -> set[str]:
    if name == "ALL_VALID_EVENTS":
        return {
            e["event_id"]
            for e in events
            if e.get("trade_side") in ("LONG", "SHORT")
            and e.get("trigger_price") not in (None, "", "None")
            and e.get("trigger_ts_ns") not in (None, "", "None")
        }
    if name == "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION":
        return {
            e["event_id"]
            for e in episodes
            if _truthy(e.get("is_first_touch_of_zone_version"))
        }
    if name == "NON_OVERLAPPING_30M_OUTCOMES":
        return {
            e["event_id"]
            for e in episodes
            if _truthy(e.get("selected_non_overlapping_30m"))
        }
    if name == "COOLDOWN_15M":
        return {e["event_id"] for e in episodes if _truthy(e.get("selected_cooldown_15m"))}
    if name == "NON_OVERLAPPING_4H":
        return {
            r["event_id"]
            for r in non_ov_4h
            if _truthy(r.get("selected_non_overlapping_4h"))
        }
    raise KeyError(name)

"""Touch episodes: one visit per cluster until a full 1m candle stays outside."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..timeparse import format_utc_z
from .bins import intervals_overlap, point_in_zone, range_overlaps_zone


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def candle_complete_at(open_ts: datetime) -> datetime:
    return parse_utc(open_ts) + timedelta(seconds=60)


def candle_fully_outside(candle: dict[str, Any], low: float, high: float) -> bool:
    c_low = float(candle["low"])
    c_high = float(candle["high"])
    return c_high < float(low) or c_low > float(high)


def occupancy(
    *,
    trade_price: float | None,
    mid_price: float | None,
    zone_low: float,
    zone_high: float,
) -> bool:
    if trade_price is not None and point_in_zone(trade_price, zone_low, zone_high):
        return True
    if mid_price is not None and point_in_zone(mid_price, zone_low, zone_high):
        return True
    return False


def range_occupancy(
    *,
    range_low: float | None,
    range_high: float | None,
    mid_price: float | None,
    zone_low: float,
    zone_high: float,
) -> bool:
    if range_low is not None and range_high is not None:
        if range_overlaps_zone(range_low, range_high, zone_low, zone_high):
            return True
    if mid_price is not None and point_in_zone(mid_price, zone_low, zone_high):
        return True
    return False


def side_of_zone(price: float, zone_low: float, zone_high: float) -> str:
    if price < float(zone_low):
        return "BELOW"
    if price > float(zone_high):
        return "ABOVE"
    return "INSIDE"


def episode_id(cluster_id: str, first_touch_ts: datetime) -> str:
    return f"ep:{cluster_id}:{int(parse_utc(first_touch_ts).timestamp())}"


def can_open_new_visit(
    *,
    last_exit_ts: datetime | None,
    candles_1m: list[dict[str, Any]],
    zone_low: float,
    zone_high: float,
    candidate_ts: datetime,
) -> bool:
    """New visit only after a completed 1m candle fully outside after the last exit."""
    if last_exit_ts is None:
        return True
    exit_at = parse_utc(last_exit_ts)
    cand_ts = parse_utc(candidate_ts)
    for candle in candles_1m:
        open_ts = parse_utc(candle["open_time"])
        close_ts = candle_complete_at(open_ts)
        if close_ts <= exit_at:
            continue
        if close_ts > cand_ts:
            continue
        if candle_fully_outside(candle, zone_low, zone_high):
            return True
    return False


def detect_visits_for_cluster(
    *,
    cluster: dict[str, Any],
    trade_events: list[dict[str, Any]],
    mid_events: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Walk merged causal prices. One episode per contiguous visit."""
    low = float(cluster["cluster_price_low"])
    high = float(cluster["cluster_price_high"])
    marks: list[tuple[datetime, str, float]] = []
    for tr in trade_events:
        ts = parse_utc(tr["ts"])
        if window_start <= ts < window_end:
            marks.append((ts, "trade", float(tr["price"])))
    for md in mid_events:
        ts = parse_utc(md["ts"])
        if window_start <= ts < window_end:
            marks.append((ts, "mid", float(md["price"])))
    marks.sort(key=lambda x: (x[0], 0 if x[1] == "trade" else 1, x[2]))

    episodes: list[dict[str, Any]] = []
    open_ep: dict[str, Any] | None = None
    last_exit: datetime | None = None
    last_exit_dir: str | None = None
    last_price: float | None = None
    last_outside_side: str | None = None

    def close_open(ts: datetime, direction: str | None, status: str) -> None:
        nonlocal open_ep, last_exit, last_exit_dir
        if open_ep is None:
            return
        open_ep["first_exit_ts"] = format_utc_z(ts)
        open_ep["first_exit_direction"] = direction
        open_ep["status"] = status
        last_exit = ts
        last_exit_dir = direction
        if open_ep.get("episode_close_ts") is None:
            open_ep["episode_close_ts"] = format_utc_z(ts)
        episodes.append(open_ep)
        open_ep = None

    for ts, kind, price in marks:
        inside = point_in_zone(price, low, high)
        if open_ep is None:
            if inside and can_open_new_visit(
                last_exit_ts=last_exit,
                candles_1m=candles_1m,
                zone_low=low,
                zone_high=high,
                candidate_ts=ts,
            ):
                approach_side = last_outside_side or (
                    "BELOW" if last_price is not None and last_price < low else
                    "ABOVE" if last_price is not None and last_price > high else
                    "INSIDE_OR_UNKNOWN"
                )
                open_ep = {
                    "episode_id": episode_id(cluster["level_cluster_id"], ts),
                    "level_cluster_id": cluster["level_cluster_id"],
                    "first_approach_ts": format_utc_z(ts),
                    "first_touch_ts": format_utc_z(ts),
                    "entry_into_zone_ts": format_utc_z(ts),
                    "first_trade_in_zone_ts": format_utc_z(ts) if kind == "trade" else None,
                    "first_mid_in_zone_ts": format_utc_z(ts) if kind == "mid" else None,
                    "approach_side": approach_side,
                    "first_exit_ts": None,
                    "first_exit_direction": None,
                    "first_return_ts": None,
                    "episode_close_ts": None,
                    "visit_count": 1,
                    "status": "FIRST_TOUCH",
                    "quality_status": "OK",
                }
            elif not inside:
                last_outside_side = side_of_zone(price, low, high)
        else:
            if kind == "trade" and open_ep.get("first_trade_in_zone_ts") is None and inside:
                open_ep["first_trade_in_zone_ts"] = format_utc_z(ts)
            if kind == "mid" and open_ep.get("first_mid_in_zone_ts") is None and inside:
                open_ep["first_mid_in_zone_ts"] = format_utc_z(ts)
            if inside:
                open_ep["status"] = "INSIDE_LEVEL_ZONE"
            else:
                direction = "UP" if price > high else "DOWN"
                status = "LEFT_LEVEL_ZONE_UP" if direction == "UP" else "LEFT_LEVEL_ZONE_DOWN"
                close_open(ts, direction, status)
                last_outside_side = side_of_zone(price, low, high)
        last_price = price

    if open_ep is not None:
        open_ep["status"] = "INSIDE_LEVEL_ZONE"
        open_ep["quality_status"] = "OPEN_AT_WINDOW_END"
        episodes.append(open_ep)

    # Attach return timestamps when a later visit is the same cluster after leave.
    by_cluster = [e for e in episodes]
    for i, ep in enumerate(by_cluster):
        if i == 0:
            continue
        prev = by_cluster[i - 1]
        if prev.get("first_exit_ts") and ep.get("first_touch_ts"):
            ep["first_return_ts"] = ep["first_touch_ts"]
            ep["visit_count"] = int(prev.get("visit_count") or 1) + 1
            ep["status"] = "RETURNED_TO_LEVEL"
    return episodes


def walk_cluster_visits(
    *,
    clusters_at,
    trade_events: list[dict[str, Any]],
    mid_events: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Single price walk. Overlapping live/frozen zones share one visit."""
    from .bins import intervals_overlap, point_in_zone
    from .levels import freeze_level

    marks: list[tuple[datetime, str, float]] = []
    for tr in trade_events:
        ts = parse_utc(tr["ts"])
        if window_start <= ts < window_end:
            marks.append((ts, "trade", float(tr["price"])))
    for md in mid_events:
        ts = parse_utc(md["ts"])
        if window_start <= ts < window_end:
            marks.append((ts, "mid", float(md["price"])))
    marks.sort(key=lambda x: (x[0], 0 if x[1] == "trade" else 1, x[2]))

    open_eps: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    last_exit_by_zone: list[tuple[float, float, datetime]] = []
    last_exit_by_persistent: dict[str, tuple[float, float, datetime]] = {}
    last_price: float | None = None
    last_outside: str | None = None

    def overlaps_open(low: float, high: float, persistent_id: str | None) -> bool:
        for ep in open_eps:
            cl = ep["frozen_cluster"]
            if persistent_id and cl.get("persistent_cluster_id") == persistent_id:
                return True
            if intervals_overlap(low, high, cl["cluster_price_low"], cl["cluster_price_high"]):
                return True
        return False

    def last_exit_for(low: float, high: float, persistent_id: str | None) -> tuple[datetime | None, float, float]:
        if persistent_id and persistent_id in last_exit_by_persistent:
            zlow, zhigh, ts = last_exit_by_persistent[persistent_id]
            return ts, zlow, zhigh
        best = None
        best_zone = (low, high)
        for zlow, zhigh, ts in last_exit_by_zone:
            if intervals_overlap(low, high, zlow, zhigh):
                if best is None or ts > best:
                    best = ts
                    best_zone = (zlow, zhigh)
        return best, best_zone[0], best_zone[1]

    for ts, kind, price in marks:
        still: list[dict[str, Any]] = []
        for ep in open_eps:
            cl = ep["frozen_cluster"]
            low, high = float(cl["cluster_price_low"]), float(cl["cluster_price_high"])
            if point_in_zone(price, low, high):
                if kind == "trade" and ep.get("first_trade_in_zone_ts") is None:
                    ep["first_trade_in_zone_ts"] = format_utc_z(ts)
                if kind == "mid" and ep.get("first_mid_in_zone_ts") is None:
                    ep["first_mid_in_zone_ts"] = format_utc_z(ts)
                ep["status"] = "INSIDE_LEVEL_ZONE"
                still.append(ep)
            else:
                direction = "UP" if price > high else "DOWN"
                ep["first_exit_ts"] = format_utc_z(ts)
                ep["first_exit_direction"] = direction
                ep["status"] = "LEFT_LEVEL_ZONE_UP" if direction == "UP" else "LEFT_LEVEL_ZONE_DOWN"
                ep["episode_close_ts"] = format_utc_z(ts)
                last_exit_by_zone.append((low, high, ts))
                pid = cl.get("persistent_cluster_id")
                if pid:
                    last_exit_by_persistent[str(pid)] = (low, high, ts)
                closed.append(ep)
        open_eps = still

        live = clusters_at(ts)
        for cl in live:
            low, high = float(cl["cluster_price_low"]), float(cl["cluster_price_high"])
            if not point_in_zone(price, low, high):
                continue
            pid = cl.get("persistent_cluster_id")
            if overlaps_open(low, high, pid):
                continue
            last_exit, exit_low, exit_high = last_exit_for(low, high, pid)
            if not can_open_new_visit(
                last_exit_ts=last_exit,
                candles_1m=candles_1m,
                zone_low=exit_low,
                zone_high=exit_high,
                candidate_ts=ts,
            ):
                continue
            approach = last_outside or (
                "BELOW" if last_price is not None and last_price < low else
                "ABOVE" if last_price is not None and last_price > high else
                "INSIDE_OR_UNKNOWN"
            )
            frozen_members = [freeze_level(m) for m in (cl.get("members") or [])]
            frozen_cl = {k: cl[k] for k in cl if k != "members"}
            ep = {
                "episode_id": episode_id(str(pid or cl["level_cluster_id"]), ts),
                "level_cluster_id": cl["level_cluster_id"],
                "persistent_cluster_id": pid,
                "first_approach_ts": format_utc_z(ts),
                "first_touch_ts": format_utc_z(ts),
                "entry_into_zone_ts": format_utc_z(ts),
                "first_trade_in_zone_ts": format_utc_z(ts) if kind == "trade" else None,
                "first_mid_in_zone_ts": format_utc_z(ts) if kind == "mid" else None,
                "approach_side": approach,
                "first_exit_ts": None,
                "first_exit_direction": None,
                "first_return_ts": None,
                "episode_close_ts": None,
                "visit_count": 1,
                "status": "FIRST_TOUCH",
                "quality_status": "OK",
                "frozen_cluster": frozen_cl,
                "frozen_members": frozen_members,
                "source_hashes": {
                    "cluster_id": cl["level_cluster_id"],
                    "member_payload_hashes": [m.get("payload_hash") for m in frozen_members],
                },
            }
            open_eps.append(ep)

        if not point_in_zone(price, -1e18, 1e18):
            pass
        inside_any = any(
            point_in_zone(price, ep["frozen_cluster"]["cluster_price_low"], ep["frozen_cluster"]["cluster_price_high"])
            for ep in open_eps
        )
        if not inside_any:
            # side vs nearest live cluster if any
            if live:
                cl0 = min(live, key=lambda c: abs(((c["cluster_price_low"] + c["cluster_price_high"]) / 2) - price))
                last_outside = side_of_zone(price, cl0["cluster_price_low"], cl0["cluster_price_high"])
        last_price = price

    for ep in open_eps:
        ep["status"] = "INSIDE_LEVEL_ZONE"
        ep["quality_status"] = "OPEN_AT_WINDOW_END"
        closed.append(ep)

    closed.sort(key=lambda e: (e["first_touch_ts"], e["episode_id"]))
    for i, ep in enumerate(closed):
        same_prev = None
        for prev in closed[:i]:
            same_persistent = (
                ep.get("persistent_cluster_id")
                and ep.get("persistent_cluster_id") == prev.get("persistent_cluster_id")
            )
            same_zone = intervals_overlap(
                ep["frozen_cluster"]["cluster_price_low"],
                ep["frozen_cluster"]["cluster_price_high"],
                prev["frozen_cluster"]["cluster_price_low"],
                prev["frozen_cluster"]["cluster_price_high"],
            )
            if prev.get("first_exit_ts") and (same_persistent or same_zone):
                same_prev = prev
        if same_prev is not None:
            ep["first_return_ts"] = ep["first_touch_ts"]
            ep["visit_count"] = int(same_prev.get("visit_count") or 1) + 1
            ep["status"] = "RETURNED_TO_LEVEL"
    return closed

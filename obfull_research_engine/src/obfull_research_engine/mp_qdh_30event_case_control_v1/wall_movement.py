"""Causal wall-movement features (decision-cutoff only; no look-ahead)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.mp_ob_feature_enrichment_v1.lc_book import BookState, apply_level_change
from obfull_research_engine.mp_qdh_canonical_integration_v1.wall_select import make_wall_id
from obfull_research_engine.mp_qdh_wall_linkage_audit_v1 import BAND_TICKS, TICK_SIZE

from . import DISTANCE_STABLE_TICKS, NEARBY_REAPPEAR_TICKS


def _snap(price: float, tick: float = TICK_SIZE) -> float:
    return round(round(float(price) / float(tick)) * float(tick), 10)


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    s = str(x).replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def wall_distance_ticks(*, wall_price: float, mid: float, wall_side: str, tick: float = TICK_SIZE) -> float:
    """ASK: (wall-mid)/tick; BID: (mid-wall)/tick. Positive = wall above/below mid on its side."""
    if str(wall_side).lower() == "ask":
        return (float(wall_price) - float(mid)) / float(tick)
    return (float(mid) - float(wall_price)) / float(tick)


def classify_wall_movement(
    *,
    wall_side: str,
    wall_price_first: float,
    wall_price_last: float,
    dist_first: float | None,
    dist_last: float | None,
    mid_first: float | None,
    mid_last: float | None,
    disappeared: bool,
    reappeared_nearby: bool,
    stable_ticks: float = DISTANCE_STABLE_TICKS,
) -> str:
    """Distance-to-mid based movement class (documented tolerance = 1 tick)."""
    if disappeared and not reappeared_nearby:
        return "WALL_DISAPPEARS"
    if reappeared_nearby and disappeared:
        return "WALL_REAPPEARS_NEARBY"
    if wall_price_first == wall_price_last and (dist_first is None or dist_last is None or abs(dist_last - dist_first) <= stable_ticks):
        return "WALL_STATIONARY"

    if dist_first is not None and dist_last is not None:
        d_change = dist_last - dist_first
        if d_change > stable_ticks:
            return "WALL_RETREATS_FROM_PRICE"
        if d_change < -stable_ticks:
            return "WALL_ADVANCES_TOWARD_PRICE"
        # ~stable distance
        if mid_first is not None and mid_last is not None and wall_price_first != wall_price_last:
            mid_dir = mid_last - mid_first
            wall_dir = wall_price_last - wall_price_first
            if abs(mid_dir) < float(TICK_SIZE) * 0.5:
                return "WALL_STATIONARY"
            if mid_dir * wall_dir > 0:
                return "WALL_FOLLOWS_PRICE"
            if mid_dir * wall_dir < 0:
                return "WALL_MOVES_AGAINST_PRICE"
        return "WALL_STATIONARY"

    # Fallback without mid: raw ask/bid direction (documented descriptive)
    dp = wall_price_last - wall_price_first
    if abs(dp) < float(TICK_SIZE) * 0.5:
        return "WALL_STATIONARY"
    return "WALL_MOVEMENT_AMBIGUOUS"


def normalize_vs_trade_direction(
    *,
    wall_side: str,
    movement_state: str,
    trade_side: str,
    wall_move_net_ticks: float,
) -> dict[str, Any]:
    """Descriptive flags only; uses info available at decision (movement_state already causal)."""
    # Trade LONG expects price up; SHORT expects price down.
    long = str(trade_side).upper() == "LONG"
    opens = blocks = follows = opposite = False
    ticks_opened = ticks_closed = 0.0

    if movement_state == "WALL_RETREATS_FROM_PRICE":
        # Ask retreats up / Bid retreats down → opens path in that direction
        if wall_side == "ask" and wall_move_net_ticks > 0:
            opens = long
            opposite = not long
            ticks_opened = abs(wall_move_net_ticks) if opens else 0.0
            ticks_closed = abs(wall_move_net_ticks) if not opens else 0.0
        elif wall_side == "bid" and wall_move_net_ticks < 0:
            opens = not long  # opens downside for SHORT
            opposite = long
            ticks_opened = abs(wall_move_net_ticks) if opens else 0.0
            ticks_closed = abs(wall_move_net_ticks) if not opens else 0.0
    elif movement_state == "WALL_ADVANCES_TOWARD_PRICE":
        blocks = True
        ticks_closed = abs(wall_move_net_ticks)
    elif movement_state == "WALL_FOLLOWS_PRICE":
        follows = True
    elif movement_state == "WALL_MOVES_AGAINST_PRICE":
        opposite = True

    return {
        "wall_opens_path_in_trade_direction": opens,
        "wall_blocks_trade_direction": blocks,
        "wall_follows_trade_direction": follows,
        "wall_moves_opposite_trade_direction": opposite,
        "ticks_path_opened": ticks_opened,
        "ticks_path_closed": ticks_closed,
    }


@dataclass
class WallMoveEvent:
    event_time_ns: int
    from_price: float
    to_price: float
    queue_from: float
    queue_to: float
    kind: str  # STEP | DISAPPEAR | REAPPEAR


def track_wall_movement(
    *,
    level_changes: Sequence[LevelChangeEvent],
    wall_side: str,
    wall_price_start: float,
    zone_id: str,
    start_ns: int,
    touch_ns: int,
    decision_ns: int,
    mids_by_ns: Sequence[tuple[int, float]] | None = None,
    tick_size: float = TICK_SIZE,
    nearby_ticks: int = NEARBY_REAPPEAR_TICKS,
) -> dict[str, Any]:
    """Track selected wall price and nearby reappearance up to decision_ns (inclusive).

    Never uses LC with event_time_ns > decision_ns.
    Wall identity for reporting: original make_wall_id(side, start_price, zone).
    Linked chain may move to nearby prices when size transfers.
    """
    side = str(wall_side).lower()
    cur_price = _snap(wall_price_start, tick_size)
    origin_price = cur_price
    wall_id = make_wall_id(wall_side=side, wall_price=origin_price, zone_id=zone_id)
    book = BookState()
    moves: list[WallMoveEvent] = []
    first_seen_ns: int | None = None
    last_seen_ns: int | None = None
    last_positive_ns: int | None = None
    stationary_ns = 0
    prev_price = cur_price
    prev_q = 0.0
    disappeared = False
    reappeared = False
    queue_transferred = 0.0
    reappear_delay_ms: float | None = None
    disappear_ns: int | None = None
    path_ticks = 0.0
    step_sizes: list[float] = []
    prices_at = {
        "first": origin_price,
        "touch": None,
        "decision": None,
        "last_causal": origin_price,
    }

    ordered = sorted(level_changes, key=lambda ev: (int(ev.event_time_ns), int(ev.apply_order or 0)))
    # Seed book before start_ns
    for ev in ordered:
        ts = int(ev.event_time_ns)
        if ts > int(decision_ns):
            break
        if str(ev.side).lower() != side:
            continue
        apply_level_change(book, ev)
        if ts < int(start_ns):
            continue
        px = _snap(float(ev.price), tick_size)
        q_cur = float(book.sizes.get((side, cur_price), 0.0))

        if q_cur > 0:
            if first_seen_ns is None:
                first_seen_ns = ts
            last_seen_ns = ts
            last_positive_ns = ts
            if disappeared and abs(px - cur_price) <= nearby_ticks * tick_size + 1e-12 and px == cur_price:
                reappeared = True
                if disappear_ns is not None and reappear_delay_ms is None:
                    reappear_delay_ms = (ts - disappear_ns) / 1e6

        # Detect depletion at current price + nearby increase
        if prev_q > 0 and q_cur <= 0:
            nearby = None
            best_d = None
            for (s2, p2), q2 in book.sizes.items():
                if str(s2).lower() != side or q2 <= 0:
                    continue
                d = abs(float(p2) - cur_price)
                if d <= nearby_ticks * tick_size + 1e-12 and d > 1e-12:
                    if best_d is None or d < best_d:
                        best_d = d
                        nearby = (float(p2), float(q2))
            if nearby is not None:
                to_p, to_q = nearby
                step = (to_p - cur_price) / tick_size
                moves.append(WallMoveEvent(ts, cur_price, to_p, prev_q, to_q, "STEP"))
                path_ticks += abs(step)
                step_sizes.append(abs(step))
                queue_transferred += min(prev_q, to_q)
                prev_price = cur_price
                cur_price = to_p
                reappeared = True
                disappeared = False
            else:
                moves.append(WallMoveEvent(ts, cur_price, cur_price, prev_q, 0.0, "DISAPPEAR"))
                disappeared = True
                disappear_ns = ts
        elif prev_q <= 0 and q_cur > 0 and disappeared:
            moves.append(WallMoveEvent(ts, cur_price, cur_price, 0.0, q_cur, "REAPPEAR"))
            reappeared = True
            if disappear_ns is not None and reappear_delay_ms is None:
                reappear_delay_ms = (ts - disappear_ns) / 1e6
            disappeared = False

        prev_q = q_cur
        prices_at["last_causal"] = cur_price
        if ts <= int(touch_ns):
            prices_at["touch"] = cur_price
        if ts <= int(decision_ns):
            prices_at["decision"] = cur_price

    if prices_at["touch"] is None:
        prices_at["touch"] = origin_price if int(touch_ns) >= int(start_ns) else None
    if prices_at["decision"] is None:
        prices_at["decision"] = prices_at["last_causal"]

    # Mid snapshots
    def mid_at(ns: int) -> float | None:
        if not mids_by_ns:
            return None
        best = None
        for mns, mid in mids_by_ns:
            if int(mns) <= int(ns):
                best = float(mid)
            else:
                break
        return best

    mid_first = mid_at(int(first_seen_ns or start_ns))
    mid_touch = mid_at(int(touch_ns))
    mid_dec = mid_at(int(decision_ns))
    wp_first = float(prices_at["first"])
    wp_touch = float(prices_at["touch"] if prices_at["touch"] is not None else wp_first)
    wp_dec = float(prices_at["decision"] if prices_at["decision"] is not None else wp_first)
    wp_last = float(prices_at["last_causal"])

    def dist(wp: float, mid: float | None) -> float | None:
        if mid is None:
            return None
        return wall_distance_ticks(wall_price=wp, mid=mid, wall_side=side, tick=tick_size)

    d_first = dist(wp_first, mid_first)
    d_touch = dist(wp_touch, mid_touch)
    d_dec = dist(wp_dec, mid_dec)

    state = classify_wall_movement(
        wall_side=side,
        wall_price_first=wp_first,
        wall_price_last=wp_dec,
        dist_first=d_first,
        dist_last=d_dec,
        mid_first=mid_first,
        mid_last=mid_dec,
        disappeared=disappeared and not reappeared,
        reappeared_nearby=reappeared and (disappeared or any(m.kind == "STEP" for m in moves)),
    )
    # Refine: if STEP moves happened, prefer REAPPEARS_NEARBY over DISAPPEARS
    if any(m.kind == "STEP" for m in moves) and state == "WALL_DISAPPEARS":
        state = "WALL_REAPPEARS_NEARBY"

    net_ticks = (wp_dec - wp_first) / tick_size
    visible_s = 0.0
    if first_seen_ns is not None and last_seen_ns is not None:
        visible_s = max(0.0, (last_seen_ns - first_seen_ns) / 1e9)
    move_count = sum(1 for m in moves if m.kind == "STEP")
    vel = (net_ticks / visible_s) if visible_s > 1e-9 else 0.0
    max_step = max(step_sizes) if step_sizes else 0.0
    med_step = sorted(step_sizes)[len(step_sizes) // 2] if step_sizes else 0.0

    # Wall vs mid lag (causal): count moves whose time is before next mid move of same sign
    wall_before = wall_after = 0
    lag_ms: list[float] = []
    if mids_by_ns and moves:
        mid_series = list(mids_by_ns)
        for m in moves:
            if m.kind != "STEP":
                continue
            # find mid change after / before move
            pre_mid = None
            post_mid = None
            for mns, mid in mid_series:
                if mns <= m.event_time_ns:
                    pre_mid = (mns, mid)
                elif post_mid is None:
                    post_mid = (mns, mid)
                    break
            if pre_mid and post_mid:
                # if mid moved after wall
                if abs(post_mid[1] - pre_mid[1]) >= tick_size * 0.5:
                    wall_before += 1
                    lag_ms.append((post_mid[0] - m.event_time_ns) / 1e6)
                else:
                    wall_after += 1

    transferred_frac = None
    if queue_transferred > 0 and moves:
        # vs first disappear queue
        first_q = next((m.queue_from for m in moves if m.kind in ("STEP", "DISAPPEAR")), None)
        if first_q and first_q > 0:
            transferred_frac = min(1.0, queue_transferred / first_q)

    max_vel = vel
    if moves and visible_s > 0:
        for m in moves:
            if m.kind == "STEP":
                max_vel = max(max_vel, abs(m.to_price - m.from_price) / tick_size / max(visible_s, 1e-9))

    dist_min = dist_max = None
    for d in (d_first, d_touch, d_dec):
        if d is None:
            continue
        dist_min = d if dist_min is None else min(dist_min, d)
        dist_max = d if dist_max is None else max(dist_max, d)

    wall_mid_vel_diff = None
    if mid_first is not None and mid_dec is not None and visible_s > 1e-9:
        mid_vel = ((mid_dec - mid_first) / tick_size) / visible_s
        wall_mid_vel_diff = vel - mid_vel

    corr = None
    # simple sign correlation of step vs mid increment
    if mids_by_ns and move_count:
        signs = []
        for m in moves:
            if m.kind != "STEP":
                continue
            pre = mid_at(m.event_time_ns)
            post = mid_at(m.event_time_ns + int(0.5e9))
            if pre is None or post is None:
                continue
            signs.append(1.0 if (m.to_price - m.from_price) * (post - pre) > 0 else -1.0)
        if signs:
            corr = sum(signs) / len(signs)

    summary = {
        "wall_id": wall_id,
        "wall_side": side,
        "wall_price_first": wp_first,
        "wall_price_at_touch": wp_touch,
        "wall_price_at_decision": wp_dec,
        "wall_price_last_causal": wp_last,
        "wall_move_raw_ticks": net_ticks,
        "wall_move_raw_pct": (100.0 * (wp_dec - wp_first) / wp_first) if wp_first else None,
        "wall_move_count": move_count,
        "wall_move_total_path_ticks": path_ticks,
        "wall_move_net_ticks": net_ticks,
        "wall_move_velocity_ticks_per_second": vel,
        "wall_move_max_velocity": max_vel,
        "wall_move_median_step_ticks": med_step,
        "wall_move_max_step_ticks": max_step,
        "first_wall_move_time_ns": next((m.event_time_ns for m in moves if m.kind == "STEP"), None),
        "last_wall_move_time_ns": next((m.event_time_ns for m in reversed(moves) if m.kind == "STEP"), None),
        "visible_duration_seconds": visible_s,
        "stationary_duration_seconds": max(0.0, visible_s - sum(
            abs(m.to_price - m.from_price) / tick_size * 0.0 for m in moves  # placeholder; use gaps
        )),
        "queue_transferred_to_new_price": queue_transferred,
        "transferred_queue_fraction": transferred_frac,
        "wall_reappearance_delay_ms": reappear_delay_ms,
        "distance_to_mid_first": d_first,
        "distance_to_mid_at_touch": d_touch,
        "distance_to_mid_at_decision": d_dec,
        "distance_to_mid_min": dist_min,
        "distance_to_mid_max": dist_max,
        "distance_to_mid_change": (None if d_first is None or d_dec is None else d_dec - d_first),
        "wall_mid_velocity_difference": wall_mid_vel_diff,
        "correlation_wall_move_vs_mid_move": corr,
        "median_wall_move_lag_ms": (sorted(lag_ms)[len(lag_ms) // 2] if lag_ms else None),
        "wall_moves_before_mid_count": wall_before,
        "wall_moves_after_mid_count": wall_after,
        "movement_state": state,
        "distance_stable_ticks_contract": DISTANCE_STABLE_TICKS,
        "n_move_events": len(moves),
        "used_post_decision_lc": False,
    }
    move_rows = [
        {
            "wall_id": wall_id,
            "event_time_ns": m.event_time_ns,
            "from_price": m.from_price,
            "to_price": m.to_price,
            "queue_from": m.queue_from,
            "queue_to": m.queue_to,
            "kind": m.kind,
            "post_decision": False,
        }
        for m in moves
    ]
    return {"summary": summary, "events": move_rows}

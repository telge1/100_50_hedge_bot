"""Target wall selection and live tracking."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery.models import (
    bps_between,
    sample_at,
    samples_between,
    ticks_between,
    wall_price,
    wall_qty,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow

MIN_TARGET_BPS = 0.5  # exclude BBO-noise; BTC 200-level book often spans only ~4–5bps
MIN_TARGET_TICKS = 5


def _far_wall_price(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    if side == "BID":
        return sample.bid_far_wall_price
    return sample.ask_far_wall_price


def _far_wall_qty(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    if side == "BID":
        return sample.bid_far_wall_qty
    return sample.ask_far_wall_qty


def select_target_wall(
    entry: dict[str, Any],
    *,
    samples: list[SampleRow],
    ts_index: list[int],
    lifecycles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pick nearest qualified opposite-side wall visible at entry_at."""
    entry_at = int(entry["entry_at"])
    mid = float(entry["entry_mid"])
    pos = entry["position_side"]
    symbol = entry["symbol"]
    target_side = "ASK" if pos == "LONG" else "BID"

    cands: list[dict[str, Any]] = []
    for lc in lifecycles:
        if lc.get("symbol") != symbol or lc.get("side") != target_side:
            continue
        appear_raw = lc.get("appear_ts")
        end_raw = lc.get("end_ts")
        appear = int(float(appear_raw)) if appear_raw not in (None, "") else None
        end = int(float(end_raw)) if end_raw not in (None, "") else None
        if appear is None or appear > entry_at:
            continue
        if end is not None and end < entry_at:
            continue
        px = float(lc["wall_price"])
        if pos == "LONG" and px <= mid:
            continue
        if pos == "SHORT" and px >= mid:
            continue
        dist_bps = bps_between(px, mid, mid)
        dist_ticks = ticks_between(px, mid, symbol)
        if dist_bps is None or dist_bps < MIN_TARGET_BPS:
            continue
        if dist_ticks is None or abs(dist_ticks) < MIN_TARGET_TICKS:
            continue
        cands.append({**lc, "_dist_bps": dist_bps})

    s_entry = sample_at(samples, ts_index, entry_at)
    for sp, sq, tag in (
        (_far_wall_price(s_entry, target_side), _far_wall_qty(s_entry, target_side), "sample_far"),
        (wall_price(s_entry, target_side), wall_qty(s_entry, target_side), "sample_dom"),
    ):
        if sp is None:
            continue
        if not ((pos == "LONG" and sp > mid) or (pos == "SHORT" and sp < mid)):
            continue
        dist_bps = bps_between(sp, mid, mid)
        dist_ticks = ticks_between(sp, mid, symbol)
        if dist_bps is None or dist_bps < MIN_TARGET_BPS:
            continue
        if dist_ticks is None or abs(dist_ticks) < MIN_TARGET_TICKS:
            continue
        cands.append(
            {
                "lifecycle_id": f"{tag}_{target_side}_{entry_at}",
                "symbol": symbol,
                "side": target_side,
                "wall_price": sp,
                "peak_qty": sq,
                "appear_ts": entry_at,
                "end_ts": None,
                "_dist_bps": dist_bps,
                "_from_sample": True,
            }
        )

    row = {
        "signal_id": entry["signal_id"],
        "attack_id": entry["attack_id"],
        "symbol": symbol,
        "position_side": pos,
        "entry_at": entry_at,
        "entry_mid": mid,
        "target_visible_at_entry": False,
        "target_wall_id": None,
        "target_side": target_side,
        "target_price_at_entry": None,
        "target_size_at_entry": None,
        "target_notional_at_entry": None,
        "target_quantile_at_entry": None,
        "target_age_at_entry": None,
        "target_distance_ticks": None,
        "target_distance_bps": None,
        "target_distance_pct": None,
        "target_expected_gross_move_bps": None,
        "no_target_wall": True,
    }
    if not cands:
        return row

    def _rank(x: dict[str, Any]) -> tuple:
        lid = str(x.get("lifecycle_id") or "")
        src = 0
        if lid.startswith("sample_dom_"):
            src = 2
        elif lid.startswith("sample_far_"):
            src = 1
        return (float(x["_dist_bps"]), src, lid)

    best = min(cands, key=_rank)
    px = float(best["wall_price"])
    qty = float(best["peak_qty"]) if best.get("peak_qty") not in (None, "") else None
    if s_entry is not None:
        live_q = _far_wall_qty(s_entry, target_side) or wall_qty(s_entry, target_side)
        live_p = _far_wall_price(s_entry, target_side) or wall_price(s_entry, target_side)
        if live_p is not None and abs(live_p - px) / px * 10000 <= 2.0 and live_q is not None:
            qty = live_q
            px = live_p
    dist_bps = bps_between(px, mid, mid)
    dist_ticks = ticks_between(px, mid, symbol)
    age = None
    appear_for_age = best.get("appear_ts")
    if appear_for_age not in (None, ""):
        age = entry_at - int(float(appear_for_age))
    row.update(
        {
            "target_visible_at_entry": True,
            "target_wall_id": best.get("lifecycle_id"),
            "target_price_at_entry": px,
            "target_size_at_entry": qty,
            "target_notional_at_entry": (qty * px) if qty is not None else None,
            "target_age_at_entry": age,
            "target_distance_ticks": dist_ticks,
            "target_distance_bps": dist_bps,
            "target_distance_pct": (dist_bps / 100.0) if dist_bps is not None else None,
            "target_expected_gross_move_bps": dist_bps,
            "no_target_wall": False,
        }
    )
    return row


def track_target(
    entry: dict[str, Any],
    target: dict[str, Any],
    samples: list[SampleRow],
    ts_index: list[int],
    *,
    max_horizon_ms: int = 14_400_000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Follow target wall until resolution or data end."""
    timeline: list[dict[str, Any]] = []
    entry_at = int(entry["entry_at"])
    pos = entry["position_side"]
    if target.get("no_target_wall"):
        res = {
            "signal_id": entry["signal_id"],
            "target_end_state": "NO_TARGET_WALL",
            "target_reached": False,
            "target_reached_at": None,
            "path_end_at": entry_at,
        }
        return timeline, res

    tpx = float(target["target_price_at_entry"])
    tside = target["target_side"]
    path = samples_between(samples, ts_index, entry_at, entry_at + max_horizon_ms)
    reached = False
    reached_at = None
    end_state = "TARGET_DATA_END"
    prev_qty = target.get("target_size_at_entry")
    defended = False
    broken = False
    reclaim_after_break = False

    for s in path:
        mid = s.mid
        dist = abs(mid - tpx) / tpx * 10000 if tpx else 999
        hit = dist <= 1.5 or (pos == "LONG" and mid >= tpx) or (pos == "SHORT" and mid <= tpx)
        live_p = _far_wall_price(s, tside) or wall_price(s, tside)
        live_q = _far_wall_qty(s, tside) or wall_qty(s, tside)
        state = "TARGET_ACTIVE"
        if live_p is None or (live_q is not None and prev_qty and live_q < 0.25 * float(prev_qty)):
            if not hit:
                state = "TARGET_PULLED_BEFORE_REACH"
        elif live_q is not None and prev_qty is not None:
            if live_q > float(prev_qty) * 1.1:
                state = "TARGET_GROWING"
            elif live_q < float(prev_qty) * 0.9:
                state = "TARGET_SHRINKING"
        if live_p is not None and abs(live_p - tpx) / tpx * 10000 > 2.0:
            state = "TARGET_MIGRATING"
            tpx = live_p

        if hit and not reached:
            reached = True
            reached_at = s.ts_ms
            state = "TARGET_REACHED"

        if reached:
            if pos == "LONG":
                if mid < tpx * (1 - 0.5 / 10000):
                    broken = True
                    state = "TARGET_BREAK_CONFIRMED"
                elif broken and mid >= tpx:
                    reclaim_after_break = True
                    state = "TARGET_BREAK_RECLAIMED"
                elif not broken and mid <= tpx * (1 + 2 / 10000):
                    defended = True
                    state = "TARGET_DEFENDED"
            else:
                if mid > tpx * (1 + 0.5 / 10000):
                    broken = True
                    state = "TARGET_BREAK_CONFIRMED"
                elif broken and mid <= tpx:
                    reclaim_after_break = True
                    state = "TARGET_BREAK_RECLAIMED"
                elif not broken and mid >= tpx * (1 - 2 / 10000):
                    defended = True
                    state = "TARGET_DEFENDED"

        timeline.append(
            {
                "signal_id": entry["signal_id"],
                "ts_ms": s.ts_ms,
                "mid": mid,
                "target_price": tpx,
                "target_qty": live_q,
                "state": state,
            }
        )
        if live_q is not None:
            prev_qty = live_q
        if state in {"TARGET_BREAK_RECLAIMED", "TARGET_DEFENDED"} and reached:
            end_state = state
            break
        if state == "TARGET_PULLED_BEFORE_REACH" and not reached:
            end_state = state
            break
        if state == "TARGET_BREAK_CONFIRMED" and broken:
            end_state = state
    else:
        if reached and defended:
            end_state = "TARGET_DEFENDED"
        elif reached and reclaim_after_break:
            end_state = "TARGET_BREAK_RECLAIMED"
        elif reached and broken:
            end_state = "TARGET_BREAK_CONFIRMED"
        elif reached:
            end_state = "TARGET_REACHED"
        elif end_state == "TARGET_DATA_END":
            end_state = "TARGET_DATA_END"

    res = {
        "signal_id": entry["signal_id"],
        "target_end_state": end_state,
        "target_reached": reached,
        "target_reached_at": reached_at,
        "path_end_at": timeline[-1]["ts_ms"] if timeline else entry_at,
        "target_defended": defended,
        "target_broken": broken,
        "target_break_reclaimed": reclaim_after_break,
    }
    return timeline, res

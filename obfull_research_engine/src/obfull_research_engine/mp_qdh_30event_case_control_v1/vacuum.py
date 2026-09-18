"""Vacuum / depth features from silver metrics (NOT_AVAILABLE when missing)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


NA = "NOT_AVAILABLE"


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def extract_vacuum_features(
    *,
    states: list[dict[str, Any]],
    wall_side: str,
    wall_price: float,
    touch_at: datetime,
    decision_at: datetime,
    move_times_ns: list[int] | None = None,
) -> dict[str, Any]:
    """Use bid_depth_2bps / ask_depth_2bps from metrics when present.

    1bp depth is not in the silver metrics adapter → NOT_AVAILABLE.
    depth_beyond_wall requires full ladder → NOT_AVAILABLE (no invented proxy).
    """
    side = str(wall_side).lower()
    same_key = "ask_depth_2bps" if side == "ask" else "bid_depth_2bps"
    opp_key = "bid_depth_2bps" if side == "ask" else "ask_depth_2bps"

    def at_or_before(ts: datetime) -> dict[str, Any] | None:
        chosen = None
        for st in states:
            avail = _as_dt(st.get("available_at") or st.get("bucket_end_exclusive"))
            if avail <= ts:
                chosen = st
            else:
                break
        return chosen

    # Sort states
    ordered = sorted(states, key=lambda s: _as_dt(s.get("bucket_start")))
    r_touch = at_or_before(touch_at) if ordered else None
    # rebuild with sorted
    states_s = ordered

    def pick(ts: datetime) -> dict[str, Any] | None:
        chosen = None
        for st in states_s:
            avail = _as_dt(st.get("available_at") or st.get("bucket_end_exclusive"))
            if avail <= ts:
                chosen = st
        return chosen

    r_touch = pick(touch_at)
    r_dec = pick(decision_at)

    def depth(row: dict[str, Any] | None, key: str) -> Any:
        if row is None:
            return NA
        v = row.get(key)
        return NA if v is None else float(v)

    same_2_touch = depth(r_touch, same_key)
    same_2_dec = depth(r_dec, same_key)
    opp_2_touch = depth(r_touch, opp_key)
    opp_2_dec = depth(r_dec, opp_key)

    mid_touch = r_touch.get("midprice") if r_touch else None
    mid_dec = r_dec.get("midprice") if r_dec else None
    spread_touch = None
    spread_dec = None
    try:
        if r_touch and r_touch.get("best_bid") is not None and r_touch.get("best_ask") is not None:
            mid = float(mid_touch) if mid_touch is not None else 0.5 * (
                float(r_touch["best_bid"]) + float(r_touch["best_ask"])
            )
            if mid > 0:
                spread_touch = 10_000.0 * (float(r_touch["best_ask"]) - float(r_touch["best_bid"])) / mid
        if r_dec and r_dec.get("best_bid") is not None and r_dec.get("best_ask") is not None:
            mid = float(mid_dec) if mid_dec is not None else 0.5 * (
                float(r_dec["best_bid"]) + float(r_dec["best_ask"])
            )
            if mid > 0:
                spread_dec = 10_000.0 * (float(r_dec["best_ask"]) - float(r_dec["best_bid"])) / mid
    except (TypeError, ValueError):
        pass

    mid_move_after = NA
    micro_move_after = NA
    time_move_to_price = NA
    if move_times_ns:
        # first move → mid change to decision (causal)
        m0 = move_times_ns[0]
        from datetime import timezone as tz

        move_at = datetime.fromtimestamp(m0 / 1e9, tz=tz.utc)
        if move_at <= decision_at:
            r_m = pick(move_at)
            if r_m and r_dec and r_m.get("midprice") is not None and mid_dec is not None:
                mid_move_after = float(mid_dec) - float(r_m["midprice"])
                micro_move_after = mid_move_after  # MICROPRICE_PROXY
                # time to first mid move of ≥1 tick after wall move
                time_move_to_price = NA
                for st in states_s:
                    avail = _as_dt(st.get("available_at") or st.get("bucket_end_exclusive"))
                    if avail <= move_at:
                        continue
                    if avail > decision_at:
                        break
                    if st.get("midprice") is None or r_m.get("midprice") is None:
                        continue
                    if abs(float(st["midprice"]) - float(r_m["midprice"])) >= 0.1:
                        time_move_to_price = (avail - move_at).total_seconds() * 1000.0
                        break

    depth_change = NA
    if same_2_touch != NA and same_2_dec != NA:
        depth_change = float(same_2_dec) - float(same_2_touch)

    return {
        "same_side_depth_inside_1bp": NA,
        "same_side_depth_inside_2bps": same_2_dec if same_2_dec != NA else same_2_touch,
        "same_side_depth_inside_2bps_at_touch": same_2_touch,
        "same_side_depth_inside_2bps_at_decision": same_2_dec,
        "opposite_side_depth_inside_1bp": NA,
        "opposite_side_depth_inside_2bps": opp_2_dec if opp_2_dec != NA else opp_2_touch,
        "depth_beyond_wall": NA,
        "baseline_depth_beyond_wall": NA,
        "depth_beyond_wall_fraction": NA,
        "depth_change_behind_wall": depth_change,
        "empty_price_levels_behind_wall": NA,
        "spread_change_bps": (
            None
            if spread_touch is None or spread_dec is None
            else spread_dec - spread_touch
        ),
        "mid_move_after_wall_move": mid_move_after,
        "microprice_move_after_wall_move": micro_move_after,
        "time_wall_move_to_price_move_ms": time_move_to_price,
        "vacuum_note": "1bp and beyond-wall require full ladder; marked NOT_AVAILABLE without proxy",
        "wall_price_ref": wall_price,
    }

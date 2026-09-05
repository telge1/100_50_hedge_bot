"""Episode-based level transition facts from deduplicated public trades."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import iso_z, utc

LEVEL_EVENTS_CONTRACT = "level_events_v1_1"
SIDE_BELOW = "BELOW"
SIDE_AT = "AT"
SIDE_ABOVE = "ABOVE"
TRANSITION_CROSS_UP = "CROSS_UP"
TRANSITION_CROSS_DOWN = "CROSS_DOWN"
TRANSITION_TOUCH = "TOUCH"


def _price_side(price: float, level: float) -> str:
    if price < level:
        return SIDE_BELOW
    if price > level:
        return SIDE_ABOVE
    return SIDE_AT


def compute_level_events(
    trades: list[dict[str, Any]],
    levels: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    *,
    anchor: datetime | None = None,
    price_source: str = "public_trades_canonical.price",
) -> list[dict[str, Any]]:
    window_start = utc(window_start)
    window_end = utc(window_end)
    anchor = utc(anchor) if anchor is not None else window_start
    # Loader output is already ordered by (ts, trade_id); avoid re-sorting 800k+ rows.
    full = [t for t in trades if window_start <= t["ts"] < window_end]
    post = [t for t in full if t["ts"] >= anchor]
    anchor_price = None
    for t in reversed(trades):
        if t["ts"] < anchor:
            anchor_price = t["price"]
            break
    if anchor_price is None and post:
        anchor_price = post[0]["price"]
    out: list[dict[str, Any]] = []
    for lvl in levels:
        level = float(lvl["price"])
        # Same semantics as full pre-scan: walk backward until a non-AT side appears.
        pre_for_level: list[dict[str, Any]] = []
        for t in reversed(trades):
            if t["ts"] >= anchor:
                continue
            pre_for_level.append(t)
            if _price_side(t["price"], level) != SIDE_AT:
                break
        pre_for_level.reverse()
        out.append(
            _analyze_level(
                lvl,
                level,
                full,
                post,
                pre_for_level,
                anchor,
                anchor_price,
                price_source=price_source,
            )
        )
    out.sort(key=lambda x: (x["level_id"], x["price"]))
    return out


def _analyze_level(
    lvl: dict[str, Any],
    level: float,
    full: list[dict[str, Any]],
    post: list[dict[str, Any]],
    pre: list[dict[str, Any]],
    anchor: datetime,
    anchor_price: float | None,
    *,
    price_source: str,
) -> dict[str, Any]:
    anchor_state = _anchor_state(pre, post, anchor, anchor_price, level)
    transitions: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    first_touch_ts = _first_touch(full, level)

    last_non_at = anchor_state["last_non_at_side_at_or_before_anchor"]
    open_above: dict[str, Any] | None = None
    open_below: dict[str, Any] | None = None
    above_idx = 0
    below_idx = 0
    transition_index = 0

    for t in post:
        side = _price_side(t["price"], level)
        if side == SIDE_AT:
            if first_touch_ts is None or t["ts"] == datetime.fromisoformat(first_touch_ts.replace("Z", "+00:00")):
                pass
            transitions.append(
                _transition(
                    transition_index,
                    lvl,
                    level,
                    TRANSITION_TOUCH,
                    t,
                    last_non_at,
                    SIDE_AT,
                )
            )
            transition_index += 1
            continue

        if last_non_at == SIDE_BELOW and side == SIDE_ABOVE:
            ttype = TRANSITION_CROSS_UP
            if open_below is not None:
                open_below = _close_episode(open_below, t, complete=True)
                episodes.append(open_below)
                open_below = None
            above_idx += 1
            open_above = _open_episode(lvl, level, "ABOVE", above_idx, t, ttype)
            transitions.append(
                _transition(transition_index, lvl, level, ttype, t, last_non_at, side)
            )
            transition_index += 1
            last_non_at = side
            continue

        if last_non_at == SIDE_ABOVE and side == SIDE_BELOW:
            ttype = TRANSITION_CROSS_DOWN
            if open_above is not None:
                _accumulate_episode(open_above, None, t, level)
                open_above = _close_episode(open_above, t, complete=True)
                episodes.append(open_above)
                open_above = None
            below_idx += 1
            open_below = _open_episode(lvl, level, "BELOW", below_idx, t, ttype)
            transitions.append(
                _transition(transition_index, lvl, level, ttype, t, last_non_at, side)
            )
            transition_index += 1
            last_non_at = side
            continue

        if last_non_at is None:
            last_non_at = side
        _accumulate_episode(open_above, open_below, t, level)

    if open_above is not None:
        episodes.append(_finalize_open(open_above))
    if open_below is not None:
        episodes.append(_finalize_open(open_below))

    if post:
        anchor_state["final_side_at_window_end"] = _price_side(post[-1]["price"], level)

    summary = _derive_summary(first_touch_ts, transitions, episodes, anchor_state)
    return {
        "contract_version": LEVEL_EVENTS_CONTRACT,
        "level_id": lvl["level_id"],
        "label": lvl.get("label"),
        "price": level,
        "price_source": price_source,
        "cross_return_scope": "anchor_to_window_end",
        "anchor_state": anchor_state,
        "transitions": transitions,
        "episodes": episodes,
        **summary,
    }


def _anchor_state(
    pre: list[dict[str, Any]],
    post: list[dict[str, Any]],
    anchor: datetime,
    anchor_price: float | None,
    level: float,
) -> dict[str, Any]:
    last_non_at = None
    for t in pre:
        side = _price_side(t["price"], level)
        if side != SIDE_AT:
            last_non_at = side
    initial = _price_side(anchor_price, level) if anchor_price is not None else None
    if initial == SIDE_AT and last_non_at is not None:
        initial_non_at = last_non_at
    elif initial == SIDE_AT:
        initial_non_at = None
    else:
        initial_non_at = initial
    dist_signed = None
    dist_abs = None
    if anchor_price is not None and level:
        dist_signed = (anchor_price - level) / level * 10000.0
        dist_abs = abs(dist_signed)
    return {
        "anchor_timestamp_utc": iso_z(anchor),
        "anchor_price": anchor_price,
        "level_price": level,
        "initial_side_at_anchor": initial,
        "last_non_at_side_at_or_before_anchor": initial_non_at,
        "distance_signed_bps": dist_signed,
        "distance_absolute_bps": dist_abs,
    }


def _first_touch(trades: list[dict[str, Any]], level: float) -> str | None:
    for t in trades:
        if _price_side(t["price"], level) == SIDE_AT:
            return iso_z(t["ts"])
    return None


def _transition(
    index: int,
    lvl: dict[str, Any],
    level: float,
    ttype: str,
    trade: dict[str, Any],
    prev_non_at: str | None,
    new_non_at: str,
) -> dict[str, Any]:
    return {
        "transition_index": index,
        "level_id": lvl["level_id"],
        "level_type": lvl.get("label"),
        "level_price": level,
        "transition_type": ttype,
        "transition_ts": iso_z(trade["ts"]),
        "trade_id": trade["trade_id"],
        "price": trade["price"],
        "previous_non_at_side": prev_non_at,
        "new_non_at_side": new_non_at if ttype != TRANSITION_TOUCH else SIDE_AT,
    }


def _open_episode(
    lvl: dict[str, Any],
    level: float,
    direction: str,
    episode_index: int,
    trade: dict[str, Any],
    start_transition: str,
) -> dict[str, Any]:
    eid = f"{lvl['level_id']}_{direction.lower()}_{episode_index:03d}"
    return {
        "episode_id": eid,
        "episode_index": episode_index,
        "level_id": lvl["level_id"],
        "label": lvl.get("label"),
        "level_price": level,
        "direction": direction,
        "start_transition": start_transition,
        "start_ts": iso_z(trade["ts"]),
        "start_trade_id": trade["trade_id"],
        "end_transition": None,
        "end_ts": None,
        "end_trade_id": None,
        "duration_seconds": None,
        "complete": False,
        "max_excursion_bps": 0.0,
        "trade_count": 1,
        "buy_notional": trade["notional"] if trade["side"] == "Buy" else 0.0,
        "sell_notional": trade["notional"] if trade["side"] == "Sell" else 0.0,
        "delta_notional": trade["notional"] if trade["side"] == "Buy" else -trade["notional"],
    }


def _accumulate_episode(
    open_above: dict[str, Any] | None,
    open_below: dict[str, Any] | None,
    trade: dict[str, Any],
    level: float,
) -> None:
    for ep in (open_above, open_below):
        if ep is None:
            continue
        ep["trade_count"] += 1
        if trade["side"] == "Buy":
            ep["buy_notional"] += trade["notional"]
            ep["delta_notional"] += trade["notional"]
        else:
            ep["sell_notional"] += trade["notional"]
            ep["delta_notional"] -= trade["notional"]
        if ep["direction"] == "ABOVE" and trade["price"] > level:
            exc = (trade["price"] - level) / level * 10000.0
            ep["max_excursion_bps"] = max(ep["max_excursion_bps"], exc)
        if ep["direction"] == "BELOW" and trade["price"] < level:
            exc = (level - trade["price"]) / level * 10000.0
            ep["max_excursion_bps"] = max(ep["max_excursion_bps"], exc)


def _close_episode(ep: dict[str, Any], trade: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    ep["end_transition"] = TRANSITION_CROSS_DOWN if ep["direction"] == "ABOVE" else TRANSITION_CROSS_UP
    ep["end_ts"] = iso_z(trade["ts"])
    ep["end_trade_id"] = trade["trade_id"]
    ep["complete"] = complete
    start = datetime.fromisoformat(ep["start_ts"].replace("Z", "+00:00"))
    ep["duration_seconds"] = (trade["ts"] - start).total_seconds()
    return ep


def _finalize_open(ep: dict[str, Any]) -> dict[str, Any]:
    ep["complete"] = False
    ep["duration_seconds"] = None
    return ep


def _derive_summary(
    first_touch_ts: str | None,
    transitions: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    anchor_state: dict[str, Any],
) -> dict[str, Any]:
    cross_ups = [t for t in transitions if t["transition_type"] == TRANSITION_CROSS_UP]
    cross_downs = [t for t in transitions if t["transition_type"] == TRANSITION_CROSS_DOWN]
    above_eps = [e for e in episodes if e["direction"] == "ABOVE"]
    below_eps = [e for e in episodes if e["direction"] == "BELOW"]
    complete_above = [e for e in above_eps if e.get("complete")]
    complete_below = [e for e in below_eps if e.get("complete")]
    first_complete_above = complete_above[0] if complete_above else None
    first_complete_below = complete_below[0] if complete_below else None

    first_return_below = first_complete_above["end_ts"] if first_complete_above else None
    first_return_above = first_complete_below["end_ts"] if first_complete_below else None
    seconds_outside = first_complete_above["duration_seconds"] if first_complete_above else None

    return {
        "first_touch_ts": first_touch_ts,
        "first_cross_up_ts": cross_ups[0]["transition_ts"] if cross_ups else None,
        "first_cross_down_ts": cross_downs[0]["transition_ts"] if cross_downs else None,
        "first_complete_above_episode": first_complete_above,
        "first_complete_below_episode": first_complete_below,
        "first_return_below_after_cross_up_ts": first_return_below,
        "first_return_above_after_cross_down_ts": first_return_above,
        "seconds_outside_before_first_return": seconds_outside,
        "max_excursion_bps_above": first_complete_above.get("max_excursion_bps") if first_complete_above else None,
        "max_excursion_bps_below": first_complete_below.get("max_excursion_bps") if first_complete_below else None,
        "anchor_state_summary": {
            "initial_side_at_anchor": anchor_state.get("initial_side_at_anchor"),
            "distance_absolute_bps": anchor_state.get("distance_absolute_bps"),
        },
    }

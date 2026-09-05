"""Deterministic anchor profile context + prior edge-cross facts (no trading rules)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .config import iso_z, utc
from .profile_edge_state import (
    STATE_BETWEEN_LOWER,
    STATE_BETWEEN_UPPER,
    STATE_INSIDE_BOTH,
    STATE_INVALID,
    STATE_OUTSIDE_ABOVE,
    STATE_OUTSIDE_BELOW,
    build_frozen_profile_edges,
    classify_price_state,
    price_to_tick,
)

ANCHOR_PROFILE_CONTEXT_CONTRACT = "anchor_profile_context_v1"

ANCHOR_INSIDE_BOTH_PROFILES = "ANCHOR_INSIDE_BOTH_PROFILES"
ANCHOR_IN_UPPER_EDGE_ZONE = "ANCHOR_IN_UPPER_EDGE_ZONE"
ANCHOR_IN_LOWER_EDGE_ZONE = "ANCHOR_IN_LOWER_EDGE_ZONE"
ANCHOR_OUTSIDE_BOTH_ABOVE = "ANCHOR_OUTSIDE_BOTH_ABOVE"
ANCHOR_OUTSIDE_BOTH_BELOW = "ANCHOR_OUTSIDE_BOTH_BELOW"
ANCHOR_BETWEEN_PROFILE_EDGES = "ANCHOR_BETWEEN_PROFILE_EDGES"
ANCHOR_CONTEXT_NOT_AVAILABLE = "ANCHOR_CONTEXT_NOT_AVAILABLE"

OBS_EDGE_CONTACT_AT_ANCHOR = "EDGE_CONTACT_AT_ANCHOR"
OBS_EDGE_TRANSITION_IN_PROGRESS = "EDGE_TRANSITION_IN_PROGRESS"
OBS_ALREADY_OUTSIDE_AT_ANCHOR = "ALREADY_OUTSIDE_AT_ANCHOR"
OBS_ALREADY_INSIDE_AT_ANCHOR = "ALREADY_INSIDE_AT_ANCHOR"
OBS_PRIOR_CROSS_NOT_OBSERVED = "PRIOR_CROSS_NOT_OBSERVED"

PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW = "PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW"
PRIOR_CROSS_OBSERVED = "PRIOR_CROSS_OBSERVED"
PRIOR_CROSS_AMBIGUOUS_SAME_TIMESTAMP = "PRIOR_CROSS_AMBIGUOUS_SAME_TIMESTAMP"


_STATE_TO_CONTEXT = {
    STATE_INSIDE_BOTH: ANCHOR_INSIDE_BOTH_PROFILES,
    STATE_BETWEEN_UPPER: ANCHOR_IN_UPPER_EDGE_ZONE,
    STATE_BETWEEN_LOWER: ANCHOR_IN_LOWER_EDGE_ZONE,
    STATE_OUTSIDE_ABOVE: ANCHOR_OUTSIDE_BOTH_ABOVE,
    STATE_OUTSIDE_BELOW: ANCHOR_OUTSIDE_BOTH_BELOW,
}


def build_anchor_profile_context(
    *,
    anchor_price: float | None,
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    trades: list[dict[str, Any]],
    anchor: datetime,
    before_minutes: int,
    symbol: str = "BTCUSDT",
) -> dict[str, Any]:
    """Build factual anchor context + prior outer-edge cross in [anchor-before, anchor)."""
    anchor = utc(anchor)
    edges = build_frozen_profile_edges(
        tpo_profile,
        volume_profile,
        anchor_cutoff_utc=iso_z(anchor),
    )
    levels = edges.get("levels") or {}
    if edges.get("profile_state") != "VALID" or anchor_price is None:
        return {
            "contract_version": ANCHOR_PROFILE_CONTEXT_CONTRACT,
            "anchor_context": ANCHOR_CONTEXT_NOT_AVAILABLE,
            "observation_context": OBS_PRIOR_CROSS_NOT_OBSERVED,
            "anchor_price": anchor_price,
            "levels": levels,
            "edges": edges,
            "prior_edge_cross": {
                "status": PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW,
                "reason": "PROFILE_OR_PRICE_UNAVAILABLE",
            },
            "interpretation_status": "NOT_EVALUATED",
            "rules_frozen": False,
            "trade_verdict_evaluated": False,
            "direction": None,
        }

    cls = classify_price_state(float(anchor_price), edges)
    state = cls["state"]
    if state == STATE_INVALID:
        ctx = ANCHOR_CONTEXT_NOT_AVAILABLE
    elif state in (STATE_BETWEEN_UPPER, STATE_BETWEEN_LOWER):
        ctx = _STATE_TO_CONTEXT[state]
    elif state in (STATE_OUTSIDE_ABOVE, STATE_OUTSIDE_BELOW, STATE_INSIDE_BOTH):
        ctx = _STATE_TO_CONTEXT[state]
    else:
        ctx = ANCHOR_BETWEEN_PROFILE_EDGES

    window_start = anchor - timedelta(minutes=int(before_minutes))
    prior = _prior_edge_cross(
        trades=trades,
        edges=edges,
        anchor=anchor,
        window_start=window_start,
        anchor_context=ctx,
        anchor_price=float(anchor_price),
    )
    observation = _observation_context(ctx, prior)

    return {
        "contract_version": ANCHOR_PROFILE_CONTEXT_CONTRACT,
        "symbol": symbol,
        "anchor_utc": iso_z(anchor),
        "anchor_price": float(anchor_price),
        "anchor_price_tick": price_to_tick(float(anchor_price)),
        "anchor_context": ctx,
        "observation_context": observation,
        "classify": cls,
        "levels": {
            "tpo_poc": levels.get("tpo_poc"),
            "tpo_vah": levels.get("tpo_vah"),
            "tpo_val": levels.get("tpo_val"),
            "volume_vpoc": levels.get("volume_vpoc"),
            "volume_vvah": levels.get("volume_vvah"),
            "volume_vval": levels.get("volume_vval"),
        },
        "edges": {
            "inner_upper_edge": edges.get("upper_inner_edge"),
            "outer_upper_edge": edges.get("upper_outer_edge"),
            "inner_lower_edge": edges.get("lower_inner_edge"),
            "outer_lower_edge": edges.get("lower_outer_edge"),
            "upper_inner_edge_tick": edges.get("upper_inner_edge_tick"),
            "upper_outer_edge_tick": edges.get("upper_outer_edge_tick"),
            "lower_inner_edge_tick": edges.get("lower_inner_edge_tick"),
            "lower_outer_edge_tick": edges.get("lower_outer_edge_tick"),
        },
        "prior_window": {
            "start_utc": iso_z(window_start),
            "end_utc": iso_z(anchor),
            "end_exclusive": True,
            "rule": "session_start_not_required; lookback = [anchor - before_minutes, anchor)",
        },
        "prior_edge_cross": prior,
        "interpretation_status": "NOT_EVALUATED",
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
    }


def _observation_context(anchor_context: str, prior: dict[str, Any]) -> str:
    if anchor_context in {ANCHOR_OUTSIDE_BOTH_ABOVE, ANCHOR_OUTSIDE_BOTH_BELOW}:
        if prior.get("status") == PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW:
            return OBS_PRIOR_CROSS_NOT_OBSERVED
        return OBS_ALREADY_OUTSIDE_AT_ANCHOR
    if anchor_context in {ANCHOR_IN_UPPER_EDGE_ZONE, ANCHOR_IN_LOWER_EDGE_ZONE}:
        return OBS_EDGE_CONTACT_AT_ANCHOR
    if anchor_context == ANCHOR_INSIDE_BOTH_PROFILES:
        return OBS_ALREADY_INSIDE_AT_ANCHOR
    if anchor_context == ANCHOR_BETWEEN_PROFILE_EDGES:
        return OBS_EDGE_TRANSITION_IN_PROGRESS
    return OBS_PRIOR_CROSS_NOT_OBSERVED


def _side_of_upper(tick: int, edge_tick: int) -> str:
    return "ABOVE" if tick > edge_tick else "AT_OR_BELOW"


def _side_of_lower(tick: int, edge_tick: int) -> str:
    return "BELOW" if tick < edge_tick else "AT_OR_ABOVE"


def _prior_edge_cross(
    *,
    trades: list[dict[str, Any]],
    edges: dict[str, Any],
    anchor: datetime,
    window_start: datetime,
    anchor_context: str,
    anchor_price: float,
) -> dict[str, Any]:
    if anchor_context not in {ANCHOR_OUTSIDE_BOTH_ABOVE, ANCHOR_OUTSIDE_BOTH_BELOW}:
        return {
            "status": "NOT_APPLICABLE",
            "reason": "ANCHOR_NOT_OUTSIDE_BOTH",
            "anchor_context": anchor_context,
        }

    upper = anchor_context == ANCHOR_OUTSIDE_BOTH_ABOVE
    inner_tick = int(edges["upper_inner_edge_tick"] if upper else edges["lower_inner_edge_tick"])
    outer_tick = int(edges["upper_outer_edge_tick"] if upper else edges["lower_outer_edge_tick"])
    inner_price = float(edges["upper_inner_edge"] if upper else edges["lower_inner_edge"])
    outer_price = float(edges["upper_outer_edge"] if upper else edges["lower_outer_edge"])
    edge_label = "UPPER" if upper else "LOWER"

    window_trades = [
        t
        for t in trades
        if window_start <= utc(t["ts"]) < anchor
    ]
    window_trades.sort(key=lambda t: (utc(t["ts"]), str(t.get("trade_id") or "")))

    inner_crosses, inner_amb = _crosses_for_edge(window_trades, inner_tick, upper=upper)
    outer_crosses, outer_amb = _crosses_for_edge(window_trades, outer_tick, upper=upper)

    # Outward cross: into outside (upper: AT_OR_BELOW → ABOVE; lower: AT_OR_ABOVE → BELOW)
    outward = [c for c in outer_crosses if c["direction"] == ("OUTWARD")]
    first_inner = inner_crosses[0] if inner_crosses else None
    first_outer = outer_crosses[0] if outer_crosses else None
    last_outer = outward[-1] if outward else (outer_crosses[-1] if outer_crosses else None)

    if not outward:
        return {
            "status": PRIOR_CROSS_NOT_OBSERVED_IN_WINDOW,
            "edge": edge_label,
            "inner_edge_price": inner_price,
            "outer_edge_price": outer_price,
            "inner_edge_tick": inner_tick,
            "outer_edge_tick": outer_tick,
            "first_inner_cross": first_inner,
            "first_outer_cross": first_outer,
            "last_outer_cross": None,
            "same_timestamp_ambiguity": bool(inner_amb or outer_amb),
            "ordering_quality": (
                PRIOR_CROSS_AMBIGUOUS_SAME_TIMESTAMP if (inner_amb or outer_amb) else "DETERMINISTIC_TRADE_ID_ORDER"
            ),
            "note": "No outward outer-edge cross in lookback; do not invent window-start cross.",
        }

    last = last_outer
    assert last is not None
    last_ts = datetime.fromisoformat(last["cross_ts"].replace("Z", "+00:00"))
    stayed_outside = _stayed_outside_until_anchor(
        window_trades, last_ts, outer_tick, upper=upper, anchor=anchor
    )
    delta = _window_trade_delta(window_trades, last_ts, anchor)
    seconds_to_anchor = (anchor - last_ts).total_seconds()

    status = PRIOR_CROSS_OBSERVED
    if outer_amb:
        status = PRIOR_CROSS_AMBIGUOUS_SAME_TIMESTAMP

    return {
        "status": status,
        "edge": edge_label,
        "inner_edge_price": inner_price,
        "outer_edge_price": outer_price,
        "inner_edge_tick": inner_tick,
        "outer_edge_tick": outer_tick,
        "first_inner_cross": first_inner,
        "first_outer_cross": first_outer,
        "last_outer_cross": last,
        "seconds_from_last_outer_cross_to_anchor": seconds_to_anchor,
        "state_before_cross": last.get("state_before"),
        "state_after_cross": last.get("state_after"),
        "remained_outside_until_anchor": stayed_outside,
        "public_trade_delta_quote_from_cross_to_anchor": delta["delta_quote"],
        "price_change_bps_from_cross_to_anchor": delta["price_change_bps"],
        "trade_count_from_cross_to_anchor": delta["trade_count"],
        "same_timestamp_ambiguity": bool(inner_amb or outer_amb),
        "ordering_quality": (
            PRIOR_CROSS_AMBIGUOUS_SAME_TIMESTAMP
            if (inner_amb or outer_amb)
            else "DETERMINISTIC_TRADE_ID_ORDER"
        ),
        "future_leakage": False,
        "lookback_start_utc": iso_z(window_start),
        "lookback_end_utc": iso_z(anchor),
    }


def _crosses_for_edge(
    trades: list[dict[str, Any]], edge_tick: int, *, upper: bool
) -> tuple[list[dict[str, Any]], bool]:
    """Detect edge crossings; flag same-timestamp ambiguity without inventing exchange order."""
    crosses: list[dict[str, Any]] = []
    ambiguous = False
    # Group by timestamp
    groups: dict[datetime, list[dict[str, Any]]] = {}
    order: list[datetime] = []
    for t in trades:
        ts = utc(t["ts"])
        if ts not in groups:
            groups[ts] = []
            order.append(ts)
        groups[ts].append(t)

    prev_side: str | None = None
    prev_trade: dict[str, Any] | None = None
    for ts in order:
        bucket = groups[ts]
        sides = set()
        for t in bucket:
            tick = price_to_tick(float(t["price"]))
            side = _side_of_upper(tick, edge_tick) if upper else _side_of_lower(tick, edge_tick)
            sides.add(side)
        if len(sides) > 1:
            ambiguous = True
            # Conservative: do not emit a cross from an ambiguous same-timestamp group.
            # Keep prev_side unchanged so we do not invent ordering.
            continue
        side = next(iter(sides))
        t0 = bucket[0]
        if prev_side is not None and side != prev_side:
            # Outward = entering outside
            if upper:
                outward = prev_side == "AT_OR_BELOW" and side == "ABOVE"
            else:
                outward = prev_side == "AT_OR_ABOVE" and side == "BELOW"
            crosses.append(
                {
                    "cross_ts": iso_z(ts),
                    "cross_trade_id": str(t0.get("trade_id")),
                    "cross_price": float(t0["price"]),
                    "edge_tick": edge_tick,
                    "direction": "OUTWARD" if outward else "INWARD",
                    "state_before": prev_side,
                    "state_after": side,
                    "prev_trade_id": str((prev_trade or {}).get("trade_id") or ""),
                    "prev_price": float((prev_trade or {}).get("price") or 0.0),
                }
            )
        prev_side = side
        prev_trade = bucket[-1]
    return crosses, ambiguous


def _stayed_outside_until_anchor(
    trades: list[dict[str, Any]],
    cross_ts: datetime,
    outer_tick: int,
    *,
    upper: bool,
    anchor: datetime,
) -> bool:
    for t in trades:
        ts = utc(t["ts"])
        if ts <= cross_ts or ts >= anchor:
            continue
        tick = price_to_tick(float(t["price"]))
        if upper and tick <= outer_tick:
            return False
        if (not upper) and tick >= outer_tick:
            return False
    return True


def _window_trade_delta(
    trades: list[dict[str, Any]], start_ts: datetime, end_ts: datetime
) -> dict[str, Any]:
    buy = sell = 0.0
    first = last = None
    n = 0
    for t in trades:
        ts = utc(t["ts"])
        if ts < start_ts or ts >= end_ts:
            continue
        n += 1
        px = float(t["price"])
        notion = float(t.get("quote_notional") or (px * float(t.get("base_size") or t.get("size") or 0)))
        side = str(t.get("side") or "").lower()
        if side in {"buy", "b"}:
            buy += notion
        elif side in {"sell", "s"}:
            sell += notion
        if first is None:
            first = px
        last = px
    bps = None
    if first and last and first > 0:
        bps = (last - first) / first * 10000.0
    return {
        "delta_quote": buy - sell,
        "price_change_bps": bps,
        "trade_count": n,
        "first_price": first,
        "last_price": last,
    }

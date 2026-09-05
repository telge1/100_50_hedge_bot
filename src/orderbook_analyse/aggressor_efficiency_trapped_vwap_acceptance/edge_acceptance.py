"""Pool-edge break acceptance / reclaim — causal checkpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, floor_second, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import (
    CHECKPOINTS_S,
    TrapAcceptConfig,
)
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size


def edge_band(edge: float, symbol: str, cfg: TrapAcceptConfig) -> tuple[float, float, float]:
    tick = float(tick_size(symbol))
    tol = cfg.edge_tolerance_ticks * tick
    return edge - tol, edge + tol, tick


def _relation_to_edge(
    *,
    wall_side: str,
    price: float,
    edge: float,
    lo: float,
    hi: float,
    policy: str,
) -> str:
    """Return BEYOND | ON_EDGE | INSIDE relative to attacked wall edge."""
    if lo <= price <= hi or abs(price - edge) <= 1e-15:
        return "ON_EDGE"
    if wall_side == "ASK":
        # bullish break = above ask edge
        return "BEYOND" if price > hi else "INSIDE"
    # BID wall: bearish break = below bid edge
    return "BEYOND" if price < lo else "INSIDE"


def evaluate_edge_acceptance(
    *,
    buckets: dict[datetime, SecondBucket],
    trades: list[Trade],
    symbol: str,
    wall_side: Optional[str],
    edge_price: Optional[float],
    edge_confidence: str,
    decision_ts: datetime,
    aggressor_side: str,
    cfg: TrapAcceptConfig,
    as_of: Optional[datetime] = None,
    checkpoints: tuple[int, ...] = (5, 10, 30, 60),
) -> dict[str, Any]:
    if edge_price is None or wall_side is None or edge_confidence in {"none", "low"} and edge_price is None:
        return {
            "acceptance_status": "UNKNOWN_EDGE",
            "final_acceptance_state": "UNKNOWN_EDGE",
            "checkpoints": {f"cp_{c}s": {"state": "UNKNOWN_EDGE"} for c in checkpoints},
        }
    if edge_confidence == "none":
        return {
            "acceptance_status": "UNKNOWN_EDGE",
            "final_acceptance_state": "UNKNOWN_EDGE",
            "checkpoints": {f"cp_{c}s": {"state": "UNKNOWN_EDGE"} for c in checkpoints},
            "note": "edge_confidence=none",
        }

    wall = wall_side.upper()
    lo, hi, tick = edge_band(float(edge_price), symbol, cfg)
    horizon = as_of if as_of is not None else decision_ts + timedelta(seconds=max(checkpoints))

    first_break_ts = None
    consecutive_beyond = 0
    time_beyond = 0
    closed_beyond = 0
    notional_beyond = 0.0
    aggr_beyond = 0.0
    max_ext = 0.0
    max_retrace = 0.0
    first_retest_ts = None
    retest_count = 0
    first_reclaim_ts = None
    was_beyond = False
    accepted = False
    reclaimed = False
    chop = False

    # trade notional beyond edge after decision
    def _accum_trades(start: datetime, end: datetime) -> tuple[float, float]:
        n_all = n_aggr = 0.0
        for tr in trades:
            ts = ensure_utc(tr.trade_ts)
            if ts < start or ts >= end:
                continue
            rel = _relation_to_edge(
                wall_side=wall, price=tr.price, edge=float(edge_price), lo=lo, hi=hi, policy=cfg.exact_on_edge_policy
            )
            if rel != "BEYOND":
                continue
            n_all += tr.notional
            if tr.side == aggressor_side:
                n_aggr += tr.notional
        return n_all, n_aggr

    out_cp: dict[str, Any] = {}
    cur = floor_second(decision_ts)
    scan_end = min(decision_ts + timedelta(seconds=max(checkpoints)), horizon)

    while cur + timedelta(seconds=1) <= scan_end:
        bucket_close = cur + timedelta(seconds=1)
        b = buckets.get(cur)
        if b is None or b.last_price is None:
            consecutive_beyond = 0
            cur += timedelta(seconds=1)
            continue
        px = b.last_price
        hi_px = b.high_price if b.high_price is not None else px
        lo_px = b.low_price if b.low_price is not None else px

        # extension / retrace using extremes
        if wall == "ASK":
            ext = max(0.0, (hi_px - float(edge_price)) / float(edge_price) * 1e4)
            retr = max(0.0, (float(edge_price) - lo_px) / float(edge_price) * 1e4)
            beyond = _relation_to_edge(wall_side=wall, price=px, edge=float(edge_price), lo=lo, hi=hi, policy=cfg.exact_on_edge_policy) == "BEYOND"
        else:
            ext = max(0.0, (float(edge_price) - lo_px) / float(edge_price) * 1e4)
            retr = max(0.0, (hi_px - float(edge_price)) / float(edge_price) * 1e4)
            beyond = _relation_to_edge(wall_side=wall, price=px, edge=float(edge_price), lo=lo, hi=hi, policy=cfg.exact_on_edge_policy) == "BEYOND"

        max_ext = max(max_ext, ext)
        if was_beyond:
            max_retrace = max(max_retrace, retr)

        if beyond:
            if first_break_ts is None:
                first_break_ts = bucket_close
            consecutive_beyond += 1
            time_beyond += 1
            closed_beyond += 1
            was_beyond = True
            if consecutive_beyond >= cfg.accept_min_consecutive_buckets and time_beyond >= cfg.accept_min_seconds:
                accepted = True
        else:
            if was_beyond and not beyond:
                # retest / reclaim
                if first_retest_ts is None:
                    first_retest_ts = bucket_close
                retest_count += 1
                # reclaim: confirmed back inside after break
                if consecutive_beyond > 0 or was_beyond:
                    if first_reclaim_ts is None:
                        first_reclaim_ts = bucket_close
                    # failed break if reclaim before acceptance locked long enough
                    if not accepted:
                        reclaimed = True
                    else:
                        # post-acceptance reclaim → CHOP or FAILED depending on persistence
                        chop = True
            consecutive_beyond = 0

        elapsed = (bucket_close - decision_ts).total_seconds()
        for cp in checkpoints:
            if abs(elapsed - float(cp)) < 1e-9:
                n_all, n_aggr = _accum_trades(decision_ts, bucket_close)
                state = _state_at(
                    wall=wall,
                    first_break_ts=first_break_ts,
                    accepted=accepted,
                    reclaimed=reclaimed,
                    chop=chop,
                    time_beyond=time_beyond,
                )
                out_cp[f"cp_{cp}s"] = {
                    "checkpoint_s": cp,
                    "checkpoint_ts": iso_z(bucket_close),
                    "state": state,
                    "first_break_ts": iso_z(first_break_ts),
                    "time_beyond_edge_seconds": time_beyond,
                    "consecutive_buckets_beyond_edge": consecutive_beyond,
                    "closed_buckets_beyond_edge": closed_beyond,
                    "trade_notional_beyond_edge": n_all,
                    "aggressor_notional_beyond_edge": n_aggr,
                    "max_extension_bps": max_ext,
                    "max_retrace_through_edge_bps": max_retrace,
                    "first_retest_ts": iso_z(first_retest_ts),
                    "retest_count": retest_count,
                    "retest_hold_flag": accepted and not reclaimed,
                    "first_reclaim_ts": iso_z(first_reclaim_ts),
                }
        cur += timedelta(seconds=1)

    for cp in checkpoints:
        key = f"cp_{cp}s"
        if key not in out_cp:
            need = decision_ts + timedelta(seconds=cp)
            if as_of is not None and need > as_of:
                out_cp[key] = {"state": "UNKNOWN_DATA", "reason": "checkpoint_beyond_as_of"}
            else:
                out_cp[key] = {"state": "UNKNOWN_DATA", "reason": "incomplete_scan"}

    final = _state_at(
        wall=wall,
        first_break_ts=first_break_ts,
        accepted=accepted,
        reclaimed=reclaimed,
        chop=chop,
        time_beyond=time_beyond,
    )
    return {
        "acceptance_status": "OK",
        "edge_price": edge_price,
        "wall_side": wall,
        "tick_size": tick,
        "edge_tolerance_abs": hi - float(edge_price),
        "exact_on_edge_policy": cfg.exact_on_edge_policy,
        "first_break_ts": iso_z(first_break_ts),
        "time_beyond_edge_seconds": time_beyond,
        "consecutive_buckets_beyond_edge": consecutive_beyond,
        "closed_buckets_beyond_edge": closed_beyond,
        "max_extension_bps": max_ext,
        "max_retrace_through_edge_bps": max_retrace,
        "first_retest_ts": iso_z(first_retest_ts),
        "retest_count": retest_count,
        "first_reclaim_ts": iso_z(first_reclaim_ts),
        "checkpoints": out_cp,
        "final_acceptance_state": final,
        "acceptance_state_at_5s": (out_cp.get("cp_5s") or {}).get("state"),
        "acceptance_state_at_10s": (out_cp.get("cp_10s") or {}).get("state"),
        "acceptance_state_at_30s": (out_cp.get("cp_30s") or {}).get("state"),
        "acceptance_state_at_60s": (out_cp.get("cp_60s") or {}).get("state"),
    }


def _state_at(
    *,
    wall: str,
    first_break_ts: Optional[datetime],
    accepted: bool,
    reclaimed: bool,
    chop: bool,
    time_beyond: int,
) -> str:
    if first_break_ts is None:
        return "NO_BREAK"
    if accepted and not reclaimed and not chop:
        return "ACCEPTED_ABOVE" if wall == "ASK" else "ACCEPTED_BELOW"
    if reclaimed and not accepted:
        return "BREAK_RECLAIMED"
    if reclaimed and accepted:
        return "CHOP_AROUND_EDGE"
    if time_beyond > 0 and not accepted:
        return "BREAK_UNCONFIRMED"
    return "FAILED_BREAK"

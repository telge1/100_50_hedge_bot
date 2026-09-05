"""Trade-level Trapped Aggressor VWAP — causal checkpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import sort_trades
from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, Trade
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, floor_second, iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import (
    CHECKPOINTS_S,
    TrapAcceptConfig,
)


def _aggressor_trades(trades: list[Trade], start: datetime, end: datetime, side: str) -> list[Trade]:
    start, end = ensure_utc(start), ensure_utc(end)
    out = []
    seen: set[str] = set()
    dup = 0
    missing_id = 0
    for tr in sort_trades(trades):
        ts = ensure_utc(tr.trade_ts)
        if ts < start or ts >= end:
            continue
        if tr.side != side:
            continue
        tid = str(tr.trade_id) if tr.trade_id is not None else ""
        if not tid:
            missing_id += 1
        if tid and tid in seen:
            dup += 1
            continue
        if tid:
            seen.add(tid)
        out.append(tr)
    return out  # type: ignore[return-value]


def compute_aggressor_vwap_block(
    trades: list[Trade],
    *,
    flow_start: datetime,
    flow_end: datetime,
    side: str,
) -> dict[str, Any]:
    start, end = ensure_utc(flow_start), ensure_utc(flow_end)
    seen: set[str] = set()
    dup = 0
    missing_id = 0
    selected: list[Trade] = []
    for tr in sort_trades(trades):
        ts = ensure_utc(tr.trade_ts)
        if ts < start or ts >= end:
            continue
        if tr.side != side:
            continue
        tid = str(tr.trade_id) if tr.trade_id is not None else ""
        if not tid:
            missing_id += 1
        if tid and tid in seen:
            dup += 1
            continue
        if tid:
            seen.add(tid)
        selected.append(tr)

    if not selected:
        return {
            "aggressor_vwap": None,
            "aggressor_low_price": None,
            "aggressor_high_price": None,
            "aggressor_notional": 0.0,
            "aggressor_trade_count": 0,
            "aggressor_vwap_valid": False,
            "vwap_data_coverage": 0.0,
            "duplicate_trade_count": dup,
            "missing_trade_id_count": missing_id,
            "aggressor_trades": [],
        }

    notion = sum(t.notional for t in selected)
    vwap = sum(t.price * t.notional for t in selected) / notion if notion > 0 else None
    prices = [t.price for t in selected]
    span_s = max(1.0, (end - start).total_seconds())
    # coverage: fraction of 1s buckets in flow with aggressor trades
    secs = {floor_second(t.trade_ts) for t in selected}
    coverage = len(secs) / span_s
    return {
        "aggressor_vwap": vwap,
        "aggressor_low_price": min(prices),
        "aggressor_high_price": max(prices),
        "aggressor_notional": notion,
        "aggressor_trade_count": len(selected),
        "aggressor_vwap_valid": vwap is not None and notion > 0,
        "vwap_data_coverage": coverage,
        "duplicate_trade_count": dup,
        "missing_trade_id_count": missing_id,
        "aggressor_trades": selected,
    }


def underwater_notional_at(
    aggressor_trades: list[Trade],
    *,
    side: str,
    current_price: float,
) -> tuple[float, float]:
    """Trade-exact underwater notional and share.

    BUY underwater if trade_price > current_price.
    SELL underwater if trade_price < current_price.
    """
    if not aggressor_trades or current_price <= 0:
        return 0.0, 0.0
    total = sum(t.notional for t in aggressor_trades)
    if total <= 0:
        return 0.0, 0.0
    if side == "Buy":
        uw = sum(t.notional for t in aggressor_trades if t.price > current_price)
    else:
        uw = sum(t.notional for t in aggressor_trades if t.price < current_price)
    return uw, uw / total


def _price_at_or_before(
    buckets: dict[datetime, SecondBucket],
    ts: datetime,
    *,
    lookback_s: int = 5,
) -> Optional[float]:
    """Last closed 1s last_price at floor(ts)-1s (bucket fully closed before ts)."""
    # Checkpoint T uses data with bucket_end <= T, i.e. bucket_start < T
    end_exclusive = floor_second(ts)
    cur = end_exclusive - timedelta(seconds=1)
    for _ in range(lookback_s + 30):
        b = buckets.get(cur)
        if b is not None and b.last_price is not None:
            return b.last_price
        cur -= timedelta(seconds=1)
        if cur.year < 2020:
            break
    return None


def evaluate_trap_checkpoints(
    *,
    buckets: dict[datetime, SecondBucket],
    aggressor_trades: list[Trade],
    side: str,
    vwap: Optional[float],
    decision_ts: datetime,
    cfg: TrapAcceptConfig,
    as_of: Optional[datetime] = None,
    checkpoints: tuple[int, ...] = CHECKPOINTS_S,
) -> dict[str, Any]:
    """Causal trap features at checkpoints relative to decision_ts (post-flow close)."""
    if not aggressor_trades or vwap is None:
        return {
            "trap_status": "UNKNOWN_DATA",
            "checkpoints": {},
            "final_trap_label": "UNKNOWN_DATA",
        }

    horizon = as_of if as_of is not None else decision_ts + timedelta(seconds=max(checkpoints))
    out_cp: dict[str, Any] = {}
    first_trap_ts = None
    consecutive = 0
    time_uw = 0
    cross_count = 0
    prev_side_vs: Optional[int] = None  # -1 below vwap for buy trap direction, etc.
    ever_trapped = False
    reclaimed = False

    # Scan 1s closed buckets from decision_ts to max checkpoint (causal)
    max_cp = max(checkpoints)
    scan_end = decision_ts + timedelta(seconds=max_cp)
    if as_of is not None:
        scan_end = min(scan_end, as_of)

    cur = floor_second(decision_ts)
    # first usable closed bucket ends at decision_ts if decision aligns to second
    while cur + timedelta(seconds=1) <= scan_end:
        bucket_close = cur + timedelta(seconds=1)
        b = buckets.get(cur)
        px = b.last_price if b else None
        if px is None:
            consecutive = 0
            cur += timedelta(seconds=1)
            continue

        if side == "Buy":
            # underwater when price below execution → vs VWAP: price < vwap
            uw_vs_vwap = px < vwap
            last_vs_vwap_bps = (px - vwap) / vwap * 1e4
            side_sign = -1 if px < vwap else (1 if px > vwap else 0)
        else:
            uw_vs_vwap = px > vwap
            last_vs_vwap_bps = (px - vwap) / vwap * 1e4
            side_sign = 1 if px > vwap else (-1 if px < vwap else 0)

        uw_n, uw_share = underwater_notional_at(aggressor_trades, side=side, current_price=px)
        if prev_side_vs is not None and side_sign != 0 and prev_side_vs != 0 and side_sign != prev_side_vs:
            cross_count += 1
        if side_sign != 0:
            prev_side_vs = side_sign

        if uw_vs_vwap and uw_share >= cfg.trap_min_underwater_share:
            consecutive += 1
            time_uw += 1
            if first_trap_ts is None:
                first_trap_ts = bucket_close
        else:
            if ever_trapped and not uw_vs_vwap:
                reclaimed = True
            consecutive = 0

        trap_confirmed_now = (
            consecutive >= cfg.trap_min_consecutive_buckets
            and time_uw >= cfg.trap_min_seconds
            and uw_share >= cfg.trap_min_underwater_share
        )
        if trap_confirmed_now:
            ever_trapped = True

        # record at exact checkpoints when bucket_close == decision + cp
        elapsed = (bucket_close - decision_ts).total_seconds()
        for cp in checkpoints:
            if abs(elapsed - cp) < 1e-9 or (elapsed == float(cp)):
                key = f"cp_{cp}s"
                if as_of is not None and bucket_close > as_of:
                    out_cp[key] = {"status": "UNKNOWN_DATA", "reason": "checkpoint_not_closed"}
                else:
                    label = (
                        "TRAP_CONFIRMED"
                        if trap_confirmed_now
                        else (
                            "TEMPORARY_UNDERWATER"
                            if uw_vs_vwap
                            else ("VWAP_RECLAIMED" if reclaimed else "NEVER_TRAPPED")
                        )
                    )
                    out_cp[key] = {
                        "checkpoint_s": cp,
                        "checkpoint_ts": iso_z(bucket_close),
                        "last_price": px,
                        "last_price_vs_aggressor_vwap_bps": last_vs_vwap_bps,
                        "mark_or_mid_vs_aggressor_vwap_bps": None,  # no mid/BBO in smoke
                        "underwater_notional": uw_n,
                        "underwater_notional_share": uw_share,
                        "time_underwater_seconds": time_uw,
                        "consecutive_buckets_underwater": consecutive,
                        "vwap_cross_count": cross_count,
                        "first_trap_ts": iso_z(first_trap_ts),
                        "trap_confirmed_at_checkpoint": trap_confirmed_now,
                        "trap_label": label,
                    }
        cur += timedelta(seconds=1)

    # Fill missing checkpoints if scan incomplete
    for cp in checkpoints:
        key = f"cp_{cp}s"
        if key not in out_cp:
            need = decision_ts + timedelta(seconds=cp)
            if as_of is not None and need > as_of:
                out_cp[key] = {"status": "UNKNOWN_DATA", "reason": "checkpoint_beyond_as_of"}
            else:
                out_cp[key] = {"status": "UNKNOWN_DATA", "reason": "no_closed_bucket_at_checkpoint"}

    if ever_trapped and reclaimed:
        final = "VWAP_RECLAIMED"
    elif ever_trapped:
        final = "TRAP_CONFIRMED"
    else:
        # any temporary?
        any_temp = any(
            isinstance(v, dict) and v.get("trap_label") == "TEMPORARY_UNDERWATER" for v in out_cp.values()
        )
        final = "TEMPORARY_UNDERWATER" if any_temp else "NEVER_TRAPPED"

    return {
        "trap_status": "OK",
        "checkpoints": out_cp,
        "final_trap_label": final,
        "first_trap_ts": iso_z(first_trap_ts),
        "vwap_cross_count_total": cross_count,
        "time_underwater_seconds_total": time_uw,
    }

"""Setup detection: pullback limit + terminal ladder (V2 contract)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size

from .config import (
    APPROACH_ATR_MULT,
    ENTRY_FRACTION_FROM_LOWER,
    MIN_TARGET_DISTANCE_ATR,
    NEARBY_POOL_DISTANCE_ATR,
    SMALL_POOL_WIDTH_ATR,
    STOP_ATR_BUFFER,
    TARGET_TICK_BUFFER,
    TF_ENTRY_POOL,
    TF_LIQUIDITY,
    TF_MACRO,
)
from .pools import eligible_target_pools, pool_valid_at
from .models import CandidateState, PoolRecord, ScannerCandidate


def _naive(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def _setup_id(
    *,
    symbol: str,
    setup_type: str,
    entry_pool_id: str,
    approach_at: datetime | None,
    confirmation_at: datetime | None,
) -> str:
    key = "|".join(
        [
            symbol,
            setup_type,
            entry_pool_id,
            "" if approach_at is None else approach_at.isoformat(),
            "" if confirmation_at is None else confirmation_at.isoformat(),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def atr_available(atr: float) -> bool:
    return atr == atr and atr > 0


def pools_overlap(a: PoolRecord, b: PoolRecord) -> bool:
    if a.side != b.side:
        return False
    lo = max(a.lower_edge, b.lower_edge)
    hi = min(a.upper_edge, b.upper_edge)
    if hi <= lo:
        return False
    overlap = hi - lo
    min_width = min(a.upper_edge - a.lower_edge, b.upper_edge - b.lower_edge)
    return overlap >= 0.35 * min_width


def _distance_atr(price: float, pool: PoolRecord, atr: float) -> float:
    if not atr_available(atr):
        return float("inf")
    edge = pool.near_edge
    return abs(price - edge) / atr


def _select_target_below(
    price: float, pools: list[PoolRecord], atr: float, *, as_of: datetime
) -> PoolRecord | None:
    if not atr_available(atr):
        return None
    eligible = eligible_target_pools(pools, as_of)
    cands = [
        p
        for p in eligible
        if p.side == "BID" and p.midpoint < price and _distance_atr(price, p, atr) >= MIN_TARGET_DISTANCE_ATR
    ]
    if not cands:
        return None
    return sorted(cands, key=lambda p: price - p.midpoint)[0]


def _select_target_above(
    price: float, pools: list[PoolRecord], atr: float, *, as_of: datetime
) -> PoolRecord | None:
    if not atr_available(atr):
        return None
    eligible = eligible_target_pools(pools, as_of)
    cands = [
        p
        for p in eligible
        if p.side == "ASK" and p.midpoint > price and _distance_atr(price, p, atr) >= MIN_TARGET_DISTANCE_ATR
    ]
    if not cands:
        return None
    return sorted(cands, key=lambda p: p.midpoint - price)[0]


def freeze_target_context(cand: ScannerCandidate, *, armed_at: datetime) -> None:
    """Snapshot entry+target plan geometry at arm time (immutable thereafter)."""
    ep = cand.entry_pool
    cand.htf_context.update(
        {
            "entry_pool_id": ep.pool_id,
            "entry_pool_known_at": ep.known_at.isoformat(),
            "entry_pool_edges_at_arm": {
                "lower": ep.lower_edge,
                "upper": ep.upper_edge,
            },
            "plan_frozen_at": armed_at.isoformat(),
            "entry_order_type": cand.entry_order_type,
            "entry_policy": cand.entry_policy,
            "frozen_entry_price": cand.entry_price,
            "frozen_stop_loss": cand.stop_price,
            "frozen_take_profit": cand.target_price,
            "frozen_gross_rr": cand.data_quality.get("gross_rr"),
            "frozen_net_rr": cand.data_quality.get("estimated_net_rr"),
        }
    )
    if cand.target_pool is None:
        return
    tp = cand.target_pool
    cand.htf_context.update(
        {
            "target_pool_id": tp.pool_id,
            "target_pool_known_at": tp.known_at.isoformat(),
            "target_pool_known_at_arm": tp.known_at.isoformat(),
            "target_pool_edges_at_arm": {
                "lower": tp.lower_edge,
                "upper": tp.upper_edge,
            },
            "target_pool_strength_at_arm": tp.strength,
            "target_pool_side": tp.side,
            "target_pool_timeframe": tp.timeframe,
            "target_selected_at": armed_at.isoformat(),
            "tp_policy": "pool_near_edge_with_front_run",
        }
    )


def _intermediate_blocks(
    entry: PoolRecord,
    target: PoolRecord,
    pools: list[PoolRecord],
    *,
    direction: str,
) -> bool:
    lo, hi = sorted((entry.midpoint, target.midpoint))
    for p in pools:
        if p.pool_id in {entry.pool_id, target.pool_id}:
            continue
        if lo < p.midpoint < hi and p.component_count >= entry.component_count:
            if direction == "SHORT" and p.side == "ASK":
                return True
            if direction == "LONG" and p.side == "BID":
                return True
    return False


def _liquidity_asymmetry_short(price: float, pools_30m: list[PoolRecord], atr: float) -> bool:
    if not atr_available(atr):
        return False
    below = [p for p in pools_30m if p.side == "BID" and p.midpoint < price]
    above = [p for p in pools_30m if p.side == "ASK" and p.midpoint > price]
    if not below:
        return False
    nearest_below = min(below, key=lambda p: price - p.midpoint)
    nearer_above = [p for p in above if (p.midpoint - price) < (price - nearest_below.midpoint)]
    return len(nearer_above) == 0 or (nearest_below.strength or 0) >= max((p.strength or 0) for p in nearer_above)


def _liquidity_asymmetry_long(price: float, pools_30m: list[PoolRecord], atr: float) -> bool:
    if not atr_available(atr):
        return False
    above = [p for p in pools_30m if p.side == "ASK" and p.midpoint > price]
    below = [p for p in pools_30m if p.side == "BID" and p.midpoint < price]
    if not above:
        return False
    nearest_above = min(above, key=lambda p: p.midpoint - price)
    nearer_below = [p for p in below if (price - p.midpoint) < (nearest_above.midpoint - price)]
    return len(nearer_below) == 0 or (nearest_above.strength or 0) >= max((p.strength or 0) for p in nearer_below)


def _lower_high_structure(row: pd.Series, prev_row: pd.Series | None) -> bool:
    psh = row.get("prior_swing_high")
    if psh is None or (isinstance(psh, float) and pd.isna(psh)):
        return True
    if prev_row is None or prev_row.empty:
        return True
    psh_prev = prev_row.get("prior_swing_high")
    if psh_prev is None or (isinstance(psh_prev, float) and pd.isna(psh_prev)):
        return True
    return float(psh) <= float(psh_prev)


def _higher_low_structure(row: pd.Series, prev_row: pd.Series | None) -> bool:
    psl = row.get("prior_swing_low")
    if psl is None or (isinstance(psl, float) and pd.isna(psl)):
        return True
    if prev_row is None or prev_row.empty:
        return True
    psl_prev = prev_row.get("prior_swing_low")
    if psl_prev is None or (isinstance(psl_prev, float) and pd.isna(psl_prev)):
        return True
    return float(psl) >= float(psl_prev)


def bearish_5m_structural(row: pd.Series, entry_pool: PoolRecord, prev_row: pd.Series | None = None) -> bool:
    close = float(row["close"])
    e9, e20, e59 = row.get("ema_9"), row.get("ema_20"), row.get("ema_59")
    s9, s20 = row.get("ema_9_slope_1"), row.get("ema_20_slope_1")
    if any(x is None or (isinstance(x, float) and pd.isna(x)) for x in (e9, e20, e59)):
        return False
    below_59 = close <= float(e59)
    ema_bear = float(e9) < float(e20) or (float(s9 or 0) < 0 and float(s20 or 0) < 0)
    swing_ok = _lower_high_structure(row, prev_row)
    no_accept_above_pool = close <= entry_pool.upper_edge
    return below_59 and ema_bear and swing_ok and no_accept_above_pool


def bullish_5m_structural(row: pd.Series, entry_pool: PoolRecord, prev_row: pd.Series | None = None) -> bool:
    close = float(row["close"])
    e9, e20, e59 = row.get("ema_9"), row.get("ema_20"), row.get("ema_59")
    s9, s20 = row.get("ema_9_slope_1"), row.get("ema_20_slope_1")
    if any(x is None or (isinstance(x, float) and pd.isna(x)) for x in (e9, e20, e59)):
        return False
    above_59 = close >= float(e59)
    ema_bull = float(e9) > float(e20) or (float(s9 or 0) > 0 and float(s20 or 0) > 0)
    swing_ok = _higher_low_structure(row, prev_row)
    no_accept_below_pool = close >= entry_pool.lower_edge
    return above_59 and ema_bull and swing_ok and no_accept_below_pool


# Legacy aliases used in replay/funnel
def _bearish_5m(row: pd.Series) -> bool:
    dummy = PoolRecord(
        pool_id="dummy",
        symbol="X",
        timeframe="15m",
        side="ASK",
        lower_edge=0.0,
        upper_edge=1e9,
        midpoint=0.5,
        component_count=1,
        strength=None,
        known_at=datetime(2020, 1, 1),
        invalidated_at=None,
        source_timestamp=datetime(2020, 1, 1),
    )
    return bearish_5m_structural(row, dummy)


def _bullish_5m(row: pd.Series) -> bool:
    dummy = PoolRecord(
        pool_id="dummy",
        symbol="X",
        timeframe="15m",
        side="BID",
        lower_edge=0.0,
        upper_edge=1e9,
        midpoint=0.5,
        component_count=1,
        strength=None,
        known_at=datetime(2020, 1, 1),
        invalidated_at=None,
        source_timestamp=datetime(2020, 1, 1),
    )
    return bullish_5m_structural(row, dummy)


def classify_pool_below_terminal(
    terminal: PoolRecord,
    all_pools: list[PoolRecord],
    *,
    price: float,
    atr: float,
) -> str:
    if not atr_available(atr):
        return "atr_unavailable"
    lower = [p for p in all_pools if p.side == "BID" and p.midpoint < terminal.midpoint - 1e-12]
    if not lower:
        return "none"
    nearest = max(lower, key=lambda p: p.midpoint)
    dist_atr = (terminal.midpoint - nearest.midpoint) / atr
    width_atr = (nearest.upper_edge - nearest.lower_edge) / atr
    strength = nearest.strength or 0
    comp = nearest.component_count
    if dist_atr <= NEARBY_POOL_DISTANCE_ATR and comp >= 1 and width_atr >= SMALL_POOL_WIDTH_ATR:
        if strength >= (terminal.strength or 0) * 0.5 or width_atr >= (terminal.upper_edge - terminal.lower_edge) / atr * 0.5:
            return "nearby_comparable_pool_below"
    if dist_atr <= 1.0 and width_atr < SMALL_POOL_WIDTH_ATR:
        return "small_residual_pool_below"
    return "distant_macro_pool_below"


def classify_pool_above_terminal(
    terminal: PoolRecord,
    all_pools: list[PoolRecord],
    *,
    price: float,
    atr: float,
) -> str:
    if not atr_available(atr):
        return "atr_unavailable"
    upper = [p for p in all_pools if p.side == "ASK" and p.midpoint > terminal.midpoint + 1e-12]
    if not upper:
        return "none"
    nearest = min(upper, key=lambda p: p.midpoint)
    dist_atr = (nearest.midpoint - terminal.midpoint) / atr
    width_atr = (nearest.upper_edge - nearest.lower_edge) / atr
    if dist_atr <= NEARBY_POOL_DISTANCE_ATR and width_atr >= SMALL_POOL_WIDTH_ATR:
        return "nearby_comparable_pool_above"
    if dist_atr <= 1.0 and width_atr < SMALL_POOL_WIDTH_ATR:
        return "small_residual_pool_above"
    return "distant_macro_pool_above"


def is_terminal_bid_pool(
    pools: list[PoolRecord],
    price: float,
    atr: float,
    *,
    approach_at: datetime,
) -> tuple[PoolRecord | None, str]:
    bids = [p for p in pools if p.side == "BID" and p.is_known_before(approach_at) and p.midpoint <= price + (atr or 0) * 0.25]
    if not bids:
        return None, "none"
    terminal = max(bids, key=lambda p: p.midpoint)
    below_class = classify_pool_below_terminal(terminal, pools, price=price, atr=atr)
    if below_class == "nearby_comparable_pool_below":
        return None, below_class
    return terminal, below_class


def is_terminal_ask_pool(
    pools: list[PoolRecord],
    price: float,
    atr: float,
    *,
    approach_at: datetime,
) -> tuple[PoolRecord | None, str]:
    asks = [p for p in pools if p.side == "ASK" and p.is_known_before(approach_at) and p.midpoint >= price - (atr or 0) * 0.25]
    if not asks:
        return None, "none"
    terminal = min(asks, key=lambda p: p.midpoint)
    above_class = classify_pool_above_terminal(terminal, pools, price=price, atr=atr)
    if above_class == "nearby_comparable_pool_above":
        return None, above_class
    return terminal, above_class


def _stop_target_levels(
    *,
    direction: str,
    symbol: str,
    entry: float,
    entry_pool: PoolRecord,
    target_pool: PoolRecord,
    atr: float,
    sweep_high: float | None,
    sweep_low: float | None,
) -> tuple[float, float]:
    tick = tick_size(symbol)
    buf = max(tick * TARGET_TICK_BUFFER, (atr if atr_available(atr) else 0) * STOP_ATR_BUFFER)
    if direction == "SHORT":
        stop = entry_pool.upper_edge + buf
        target = target_pool.near_edge + tick * TARGET_TICK_BUFFER
    else:
        pool_low = min(entry_pool.lower_edge, sweep_low or entry_pool.lower_edge)
        stop = pool_low - buf
        target = target_pool.near_edge - tick * TARGET_TICK_BUFFER
    return stop, target


def _build_pullback_candidate(
    *,
    symbol: str,
    setup_type: str,
    direction: str,
    entry_pool: PoolRecord,
    target: PoolRecord,
    approach_at: datetime,
    limit_px: float,
    selection_reason: str,
) -> ScannerCandidate:
    sid = _setup_id(
        symbol=symbol,
        setup_type=setup_type,
        entry_pool_id=entry_pool.pool_id,
        approach_at=approach_at,
        confirmation_at=None,
    )
    return ScannerCandidate(
        setup_id=sid,
        setup_type=setup_type,
        symbol=symbol,
        direction=direction,
        state=CandidateState.LIMIT_INTENT_ARMED,
        entry_pool=entry_pool,
        target_pool=target,
        approach_at=approach_at,
        armed_at=approach_at,
        limit_entry_price=limit_px,
        pool_selection_reason=selection_reason,
        episode_id=f"{setup_type}:{entry_pool.pool_id}",
        htf_context={
            "30m_asymmetry": "targets_below" if direction == "SHORT" else "targets_above",
            "5m_regime": "bearish" if direction == "SHORT" else "bullish",
            "entry_fraction_from_lower": ENTRY_FRACTION_FROM_LOWER,
        },
    )


def pullback_limit_price(pool: PoolRecord, *, direction: str) -> float:
    width = pool.upper_edge - pool.lower_edge
    if direction == "SHORT":
        return pool.lower_edge + ENTRY_FRACTION_FROM_LOWER * width
    return pool.upper_edge - ENTRY_FRACTION_FROM_LOWER * width


def select_pullback_entry_pools(
    pools_15m: list[PoolRecord],
    *,
    price: float,
    approach_at: datetime,
    direction: str,
    atr: float,
) -> list[tuple[PoolRecord, str, float]]:
    from .pool_selection import select_pullback_entry_pools as _sel

    return _sel(pools_15m, price=price, approach_at=approach_at, direction=direction, atr=atr)


def detect_pullback_short_candidates(
    *,
    symbol: str,
    price: float,
    approach_at: datetime,
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    row_5m: pd.Series,
    prev_row_5m: pd.Series | None,
    atr: float,
) -> list[ScannerCandidate]:
    if not atr_available(atr):
        return []
    selected = select_pullback_entry_pools(
        pools_15m, price=price, approach_at=approach_at, direction="SHORT", atr=atr
    )
    out: list[ScannerCandidate] = []
    for entry_pool, reason, limit_px in selected:
        if not _liquidity_asymmetry_short(price, pools_30m, atr):
            continue
        target = _select_target_below(
            limit_px, pools_30m + [p for p in pools_15m if p.side == "BID"], atr, as_of=approach_at
        )
        if target is None:
            continue
        if not pool_valid_at(target, approach_at):
            continue
        if _intermediate_blocks(entry_pool, target, pools_30m, direction="SHORT"):
            continue
        if not bearish_5m_structural(row_5m, entry_pool, prev_row_5m):
            continue
        out.append(
            _build_pullback_candidate(
                symbol=symbol,
                setup_type="A_PLUS_PULLBACK_SHORT",
                direction="SHORT",
                entry_pool=entry_pool,
                target=target,
                approach_at=approach_at,
                limit_px=limit_px,
                selection_reason=reason,
            )
        )
    return out


def detect_pullback_long_candidates(
    *,
    symbol: str,
    price: float,
    approach_at: datetime,
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    row_5m: pd.Series,
    prev_row_5m: pd.Series | None,
    atr: float,
) -> list[ScannerCandidate]:
    if not atr_available(atr):
        return []
    selected = select_pullback_entry_pools(
        pools_15m, price=price, approach_at=approach_at, direction="LONG", atr=atr
    )
    out: list[ScannerCandidate] = []
    for entry_pool, reason, limit_px in selected:
        if not _liquidity_asymmetry_long(price, pools_30m, atr):
            continue
        target = _select_target_above(
            limit_px, pools_30m + [p for p in pools_15m if p.side == "ASK"], atr, as_of=approach_at
        )
        if target is None:
            continue
        if not pool_valid_at(target, approach_at):
            continue
        if _intermediate_blocks(entry_pool, target, pools_30m, direction="LONG"):
            continue
        if not bullish_5m_structural(row_5m, entry_pool, prev_row_5m):
            continue
        out.append(
            _build_pullback_candidate(
                symbol=symbol,
                setup_type="A_PLUS_PULLBACK_LONG",
                direction="LONG",
                entry_pool=entry_pool,
                target=target,
                approach_at=approach_at,
                limit_px=limit_px,
                selection_reason=reason,
            )
        )
    return out


def detect_pullback_short_context(
    *,
    symbol: str,
    price: float,
    approach_at: datetime,
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    row_5m: pd.Series,
    atr: float,
    prev_row_5m: pd.Series | None = None,
) -> ScannerCandidate | None:
    cands = detect_pullback_short_candidates(
        symbol=symbol,
        price=price,
        approach_at=approach_at,
        pools_15m=pools_15m,
        pools_30m=pools_30m,
        row_5m=row_5m,
        prev_row_5m=prev_row_5m,
        atr=atr,
    )
    return cands[0] if cands else None


def detect_pullback_long_context(
    *,
    symbol: str,
    price: float,
    approach_at: datetime,
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    row_5m: pd.Series,
    atr: float,
    prev_row_5m: pd.Series | None = None,
) -> ScannerCandidate | None:
    cands = detect_pullback_long_candidates(
        symbol=symbol,
        price=price,
        approach_at=approach_at,
        pools_15m=pools_15m,
        pools_30m=pools_30m,
        row_5m=row_5m,
        prev_row_5m=prev_row_5m,
        atr=atr,
    )
    return cands[0] if cands else None


def detect_terminal_long_context(
    *,
    symbol: str,
    price: float,
    approach_at: datetime,
    pools_1h: list[PoolRecord],
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    atr: float,
    wick_low: float,
) -> ScannerCandidate | None:
    if not atr_available(atr):
        return None
    ladder_pools = pools_1h + pools_15m
    terminal, tclass = is_terminal_bid_pool(ladder_pools, price, atr, approach_at=approach_at)
    if terminal is None:
        return None
    target = _select_target_above(
        price, pools_15m + pools_30m + [p for p in pools_1h if p.side == "ASK"], atr, as_of=approach_at
    )
    if target is None or not pool_valid_at(target, approach_at):
        return None
    lower_wick = price - wick_low
    if lower_wick < atr * 0.25:
        return None
    sid = _setup_id(
        symbol=symbol,
        setup_type="A_PLUS_TERMINAL_POOL_LONG",
        entry_pool_id=terminal.pool_id,
        approach_at=approach_at,
        confirmation_at=None,
    )
    return ScannerCandidate(
        setup_id=sid,
        setup_type="A_PLUS_TERMINAL_POOL_LONG",
        symbol=symbol,
        direction="LONG",
        state=CandidateState.WAITING_FOR_1M_CONFIRMATION,
        entry_pool=terminal,
        target_pool=target,
        approach_at=approach_at,
        sweep_low=wick_low,
        terminal_ladder_state="WAIT_FOR_REACTION",
        htf_context={"terminal_pool_class": tclass, "lower_wick_atr": lower_wick / atr},
    )


def detect_terminal_short_context(
    *,
    symbol: str,
    price: float,
    approach_at: datetime,
    pools_1h: list[PoolRecord],
    pools_15m: list[PoolRecord],
    pools_30m: list[PoolRecord],
    atr: float,
    wick_high: float,
) -> ScannerCandidate | None:
    if not atr_available(atr):
        return None
    ladder_pools = pools_1h + pools_15m
    terminal, tclass = is_terminal_ask_pool(ladder_pools, price, atr, approach_at=approach_at)
    if terminal is None:
        return None
    target = _select_target_below(
        price, pools_15m + pools_30m + [p for p in pools_1h if p.side == "BID"], atr, as_of=approach_at
    )
    if target is None or not pool_valid_at(target, approach_at):
        return None
    upper_wick = wick_high - price
    if upper_wick < atr * 0.25:
        return None
    sid = _setup_id(
        symbol=symbol,
        setup_type="A_PLUS_TERMINAL_POOL_SHORT",
        entry_pool_id=terminal.pool_id,
        approach_at=approach_at,
        confirmation_at=None,
    )
    return ScannerCandidate(
        setup_id=sid,
        setup_type="A_PLUS_TERMINAL_POOL_SHORT",
        symbol=symbol,
        direction="SHORT",
        state=CandidateState.WAITING_FOR_1M_CONFIRMATION,
        entry_pool=terminal,
        target_pool=target,
        approach_at=approach_at,
        sweep_high=wick_high,
        terminal_ladder_state="WAIT_FOR_REACTION",
        htf_context={"terminal_pool_class": tclass, "upper_wick_atr": upper_wick / atr},
    )


def in_upper_half(pool: PoolRecord, price: float) -> bool:
    return pool.midpoint <= price <= pool.upper_edge


def in_lower_half(pool: PoolRecord, price: float) -> bool:
    return pool.lower_edge <= price <= pool.midpoint


def is_red_reaction(open_px: float, close_px: float) -> bool:
    return close_px < open_px


def is_green_reaction(open_px: float, close_px: float) -> bool:
    return close_px > open_px


def finalize_levels(cand: ScannerCandidate, *, symbol: str, atr: float) -> None:
    if cand.entry_price is None or cand.target_pool is None:
        return
    stop, target = _stop_target_levels(
        direction=cand.direction,
        symbol=symbol,
        entry=float(cand.entry_price),
        entry_pool=cand.entry_pool,
        target_pool=cand.target_pool,
        atr=atr,
        sweep_high=cand.sweep_high,
        sweep_low=cand.sweep_low,
    )
    cand.stop_price = stop
    cand.target_price = target


# Legacy terminal helpers for replay funnel
def _terminal_bid_pool(pools_1h: list[PoolRecord], price: float, atr: float) -> PoolRecord | None:
    terminal, _ = is_terminal_bid_pool(pools_1h, price, atr, approach_at=datetime(2099, 1, 1))
    return terminal


def _terminal_ask_pool(pools_1h: list[PoolRecord], price: float, atr: float) -> PoolRecord | None:
    terminal, _ = is_terminal_ask_pool(pools_1h, price, atr, approach_at=datetime(2099, 1, 1))
    return terminal

"""Level-change book reconstruction and HIT/PULL/ADD attribution."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import timezone
from typing import Sequence

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.breakout_xray_v1.trades import XRayTrade

from .params import NS, TICK_TOLERANCE_USD, TRADE_MATCH_TOLERANCE_MS


def trade_ts_ns(trade: XRayTrade) -> int:
    ts = trade.trade_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * NS)

@dataclass
class AttributedChange:
    event_time_ns: int
    side: str
    price: float
    old_size: float
    new_size: float
    kind: str  # HIT | PULL | ADD | NONE | DECREASE_UNCLASSIFIED
    qty: float
    notional: float


@dataclass
class BookState:
    sizes: dict[tuple[str, float], float] = field(default_factory=dict)
    max_feature_ts_ns: int = 0

    def snapshot(self) -> dict[tuple[str, float], float]:
        return dict(self.sizes)


@dataclass
class TradeIndex:
    """Sorted trade list with timestamp index for O(log n) window lookup."""

    trades: list[XRayTrade]
    ts_ns: list[int]

    @classmethod
    def build(cls, trades: Sequence[XRayTrade]) -> "TradeIndex":
        ordered = sorted(trades, key=lambda t: (t.trade_ts, t.trade_id))
        return cls(trades=ordered, ts_ns=[trade_ts_ns(t) for t in ordered])

    def window(self, lo_ns: int, hi_ns: int) -> list[XRayTrade]:
        if not self.ts_ns:
            return []
        i = bisect.bisect_left(self.ts_ns, int(lo_ns))
        j = bisect.bisect_right(self.ts_ns, int(hi_ns))
        return self.trades[i:j]


def apply_level_change(state: BookState, ev: LevelChangeEvent) -> tuple[float, float]:
    """Apply LC; return (old_size, new_size) used."""
    key = (str(ev.side), float(ev.price))
    known = state.sizes.get(key)
    if known is None:
        old = float(ev.old_size) if ev.old_size is not None else 0.0
    else:
        old = float(known)
    new = float(ev.new_size)
    if str(ev.change_type).upper() in ("DELETE", "REMOVE"):
        new = 0.0
    if new <= 1e-15:
        state.sizes.pop(key, None)
    else:
        state.sizes[key] = new
    state.max_feature_ts_ns = max(state.max_feature_ts_ns, int(ev.event_time_ns))
    return old, new


def _trade_hits_level(trade: XRayTrade, *, side: str, price: float, tick: float) -> bool:
    """Ask-wall hit by Buy; Bid-wall hit by Sell within tick tolerance of level price."""
    if side == "ask" and trade.side != "Buy":
        return False
    if side == "bid" and trade.side != "Sell":
        return False
    return abs(float(trade.price) - float(price)) <= float(tick)


def attribute_change(
    *,
    side: str,
    price: float,
    old_size: float,
    new_size: float,
    event_time_ns: int,
    trades: Sequence[XRayTrade],
    trades_available: bool,
    tick: float = TICK_TOLERANCE_USD,
    match_ms: int = TRADE_MATCH_TOLERANCE_MS,
    trade_index: TradeIndex | None = None,
) -> AttributedChange:
    delta = float(new_size) - float(old_size)
    qty = abs(delta)
    notional = qty * float(price)
    if qty <= 1e-15:
        return AttributedChange(
            event_time_ns, side, price, old_size, new_size, "NONE", 0.0, 0.0
        )
    if delta > 0:
        return AttributedChange(
            event_time_ns, side, price, old_size, new_size, "ADD", qty, notional
        )
    if not trades_available:
        return AttributedChange(
            event_time_ns,
            side,
            price,
            old_size,
            new_size,
            "DECREASE_UNCLASSIFIED",
            qty,
            notional,
        )
    tol_ns = int(match_ms) * 1_000_000
    lo = int(event_time_ns) - tol_ns
    hi = int(event_time_ns) + tol_ns
    if trade_index is not None:
        candidates = trade_index.window(lo, hi)
    else:
        candidates = [
            t
            for t in trades
            if lo <= trade_ts_ns(t) <= hi
        ]
    hit = any(
        _trade_hits_level(t, side=side, price=price, tick=tick) for t in candidates
    )
    kind = "HIT" if hit else "PULL"
    return AttributedChange(
        event_time_ns, side, price, old_size, new_size, kind, qty, notional
    )


def replay_and_attribute(
    events: Sequence[LevelChangeEvent],
    *,
    trades: Sequence[XRayTrade],
    trades_available: bool,
    cutoff_ns: int,
    start_ns: int,
    tick: float = TICK_TOLERANCE_USD,
    match_ms: int = TRADE_MATCH_TOLERANCE_MS,
    snapshot_stride_ns: int = 100_000_000,  # 100ms
) -> tuple[BookState, list[AttributedChange], list[tuple[int, dict[tuple[str, float], float]]]]:
    """Replay LC in [start_ns, cutoff_ns]; attribute HIT/PULL/ADD.

    Path stores sparse snapshots (every snapshot_stride_ns) plus last state.
    """
    state = BookState()
    attributed: list[AttributedChange] = []
    path: list[tuple[int, dict[tuple[str, float], float]]] = []
    trade_index = TradeIndex.build(trades) if trades_available and trades else None
    last_snap_ns = -1
    for ev in events:
        ts = int(ev.event_time_ns)
        if ts < int(start_ns):
            continue
        if ts > int(cutoff_ns):
            break
        old, new = apply_level_change(state, ev)
        attr = attribute_change(
            side=str(ev.side),
            price=float(ev.price),
            old_size=old,
            new_size=new,
            event_time_ns=ts,
            trades=trades,
            trades_available=trades_available,
            tick=tick,
            match_ms=match_ms,
            trade_index=trade_index,
        )
        if attr.kind != "NONE":
            attributed.append(attr)
        if last_snap_ns < 0 or ts - last_snap_ns >= snapshot_stride_ns:
            path.append((ts, state.snapshot()))
            last_snap_ns = ts
    if state.max_feature_ts_ns and (not path or path[-1][0] != state.max_feature_ts_ns):
        path.append((state.max_feature_ts_ns, state.snapshot()))
    return state, attributed, path


def snapshot_at_or_before(
    path: Sequence[tuple[int, dict[tuple[str, float], float]]],
    ts_ns: int,
) -> dict[tuple[str, float], float]:
    best: dict[tuple[str, float], float] = {}
    for t, snap in path:
        if t <= int(ts_ns):
            best = snap
        else:
            break
    return best

"""Dynamic bid/ask wall tracking — symbol tick-size, no BTC 0.1 hardcode."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrackedWall:
    wall_id: str
    side: str  # bid|ask
    price: float
    qty: float
    in_band: bool = True
    migrated: bool = False
    consumed: bool = False
    disappeared: bool = False
    refill_qty: float = 0.0
    pull_qty: float = 0.0


@dataclass
class DynamicWallTracker:
    """Selects walls relative to mid using tick_size + bps band (no MP)."""

    tick_size: float
    band_ticks: int = 5
    max_distance_bps: float = 15.0
    max_per_side: int = 8
    walls: dict[str, TrackedWall] = field(default_factory=dict)
    _next_id: int = 1

    def __post_init__(self) -> None:
        if self.tick_size is None or float(self.tick_size) <= 0:
            raise ValueError("tick_size_required_symbol_dependent")

    def _wid(self, side: str, price: float) -> str:
        # Stable id by side+tick-quantized price; migration updates price but keeps id if matched
        ticks = round(float(price) / float(self.tick_size))
        return f"{side}:{ticks}"

    def update_from_book(
        self, *, mid: float, bids: list[tuple[float, float]], asks: list[tuple[float, float]]
    ) -> list[dict[str, Any]]:
        if mid <= 0:
            return []
        band_abs = self.band_ticks * self.tick_size
        bps_abs = mid * self.max_distance_bps / 10000.0
        half = max(band_abs, bps_abs)
        lo, hi = mid - half, mid + half
        events: list[dict[str, Any]] = []
        seen: set[str] = set()

        def consider(side: str, levels: list[tuple[float, float]]) -> None:
            count = 0
            for price, qty in levels:
                if count >= self.max_per_side:
                    break
                if price < lo or price > hi:
                    continue
                wid = self._wid(side, price)
                seen.add(wid)
                count += 1
                prev = self.walls.get(wid)
                if prev is None:
                    # try match migration: same side nearest prior wall within 1 tick move
                    migrated_from = None
                    for oid, ow in list(self.walls.items()):
                        if ow.side != side or ow.disappeared:
                            continue
                        if abs(ow.price - price) <= self.tick_size * 1.5 and oid not in seen:
                            migrated_from = oid
                            break
                    if migrated_from:
                        old = self.walls.pop(migrated_from)
                        old.price = price
                        old.qty = qty
                        old.migrated = True
                        old.in_band = True
                        self.walls[wid] = old
                        events.append({"event": "wall_migrate", "wall_id": old.wall_id, "price": price})
                    else:
                        w = TrackedWall(wall_id=f"w{self._next_id}", side=side, price=price, qty=qty)
                        self._next_id += 1
                        self.walls[wid] = w
                        events.append({"event": "wall_enter", "wall_id": w.wall_id, "side": side, "price": price})
                else:
                    dq = float(qty) - float(prev.qty)
                    if dq > 1e-12:
                        prev.refill_qty += dq
                        events.append({"event": "wall_refill", "wall_id": prev.wall_id, "dq": dq})
                    elif dq < -1e-12:
                        prev.pull_qty += -dq
                        if qty <= 1e-12:
                            prev.consumed = True
                            events.append({"event": "wall_consumed", "wall_id": prev.wall_id})
                        else:
                            events.append({"event": "wall_pull", "wall_id": prev.wall_id, "dq": -dq})
                    prev.qty = qty
                    prev.price = price
                    prev.in_band = True

        consider("bid", bids)
        consider("ask", asks)
        for key, w in list(self.walls.items()):
            if key not in seen and w.in_band and not w.disappeared:
                w.in_band = False
                w.disappeared = True
                events.append({"event": "wall_leave", "wall_id": w.wall_id, "side": w.side, "price": w.price})
        return events

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "wall_id": w.wall_id,
                "side": w.side,
                "price": w.price,
                "qty": w.qty,
                "in_band": w.in_band,
                "migrated": w.migrated,
                "consumed": w.consumed,
                "disappeared": w.disappeared,
                "refill_qty": w.refill_qty,
                "pull_qty": w.pull_qty,
            }
            for w in self.walls.values()
        ]

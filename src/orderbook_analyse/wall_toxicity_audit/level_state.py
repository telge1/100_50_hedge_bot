"""Absolute quantity level-state reconstruction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from orderbook_analyse.wall_toxicity_audit.bucket import price_in_primary_bucket
from orderbook_analyse.wall_toxicity_audit.types import LevelQtyEvent


@dataclass
class LevelStateTracker:
    """Track absolute resting qty per price; qty_change = new - previous."""

    symbol: str
    side: str
    band_low: float
    band_high: float
    analysis_low: float
    analysis_high: float
    _qty: dict[float, float] = field(default_factory=dict)
    _known: set[float] = field(default_factory=set)
    _last_snapshot_key: tuple[int, int] | None = None
    events: list[LevelQtyEvent] = field(default_factory=list)

    def _in_analysis(self, price: float) -> bool:
        return self.analysis_low - 1e-15 <= price <= self.analysis_high + 1e-15

    def apply_level(
        self,
        *,
        ts: datetime,
        price: float,
        new_qty: float,
        message_type: str,
        update_id: int,
        cross_sequence: int,
    ) -> LevelQtyEvent | None:
        if not self._in_analysis(price):
            return None
        px = float(price)
        snap_key = (int(update_id), int(cross_sequence))
        snapshot_boundary = False
        if str(message_type) == "snapshot":
            if self._last_snapshot_key != snap_key:
                snapshot_boundary = True
                self._last_snapshot_key = snap_key
                # Snapshot replaces resting sizes; unknown levels stay unknown
                # until observed. Observed levels get updated below.

        incomplete = px not in self._known
        previous: float | None
        if incomplete:
            previous = None
            qty_change: float | None = None
        else:
            previous = float(self._qty.get(px, 0.0))
            qty_change = float(new_qty) - previous

        if float(new_qty) <= 0:
            self._qty.pop(px, None)
        else:
            self._qty[px] = float(new_qty)
        self._known.add(px)

        ev = LevelQtyEvent(
            ts=ts,
            symbol=self.symbol,
            side=self.side,
            price=px,
            previous_qty=previous,
            new_qty=float(new_qty),
            qty_change=qty_change,
            message_type=str(message_type),
            update_id=int(update_id),
            cross_sequence=int(cross_sequence),
            incomplete_initial=incomplete,
            snapshot_boundary=snapshot_boundary,
            in_primary_bucket=price_in_primary_bucket(
                px, band_low=self.band_low, band_high=self.band_high, side=self.side
            ),
        )
        self.events.append(ev)
        return ev


def qty_change_from_absolute(previous_qty: float | None, new_qty: float) -> float | None:
    """Documented absolute-quantity semantics helper."""
    if previous_qty is None:
        return None
    return float(new_qty) - float(previous_qty)


def iter_complete_changes(
    events: Sequence[LevelQtyEvent],
) -> Iterable[LevelQtyEvent]:
    for ev in events:
        if ev.incomplete_initial:
            continue
        if ev.qty_change is None or ev.qty_change == 0:
            continue
        yield ev

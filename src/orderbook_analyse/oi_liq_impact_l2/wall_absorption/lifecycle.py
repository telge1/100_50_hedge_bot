"""Wall lifecycle state machine from per-level causal observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from orderbook_analyse.orderbook_replay import OrderBookState

ZERO = Decimal("0")


@dataclass
class LevelDelta:
    added: Decimal = ZERO
    removed: Decimal = ZERO
    carried_forward: bool = False
    sequence_gap: bool = False


@dataclass
class WallLifecycleTracker:
    cluster_id: str
    direction: str
    wall_price: Decimal
    initial_qty: Decimal
    state: str = "WALL_UNTOUCHED"
    cumulative_added: Decimal = ZERO
    cumulative_removed: Decimal = ZERO
    traded_notional_at_level: Decimal = ZERO
    seconds_visible: int = 0
    seconds_at_best_level: int = 0
    seconds_after_first_touch: int = 0
    first_touch_at: str | None = None
    max_depletion_ratio: Decimal = ZERO
    aborted: bool = False
    abort_reason: str | None = None
    rows: list[dict[str, object]] = field(default_factory=list)

    def observe_second(
        self,
        *,
        second: str,
        book: OrderBookState,
        last_price: Decimal | None,
        delta: LevelDelta,
        aggressive_sell_notional: Decimal = ZERO,
        aggressive_buy_notional: Decimal = ZERO,
    ) -> None:
        if self.aborted:
            return
        if delta.sequence_gap:
            self.aborted = True
            self.abort_reason = "SEQUENCE_GAP"
            self.state = "WALL_DATA_ABORT"
            self._append_row(second, book, last_price, delta)
            return

        visible_qty = self._visible_qty(book)
        touched = self._price_touched(last_price, book)
        if visible_qty is None and not delta.carried_forward:
            if touched and last_price is not None:
                if self.direction == "LONG" and last_price < self.wall_price:
                    self.state = "WALL_TRADED_THROUGH"
                    self._append_row(second, book, last_price, delta)
                    return
                if self.direction == "SHORT" and last_price > self.wall_price:
                    self.state = "WALL_TRADED_THROUGH"
                    self._append_row(second, book, last_price, delta)
                    return
            self.aborted = True
            self.abort_reason = "LEVEL_MISSING"
            self.state = "WALL_DATA_ABORT"
            self._append_row(second, book, last_price, delta)
            return

        if delta.carried_forward:
            self._append_row(second, book, last_price, delta)
            return

        self.cumulative_added += delta.added
        self.cumulative_removed += delta.removed

        if visible_qty is not None and visible_qty > ZERO and self.initial_qty > ZERO:
            self.seconds_visible += 1
            depletion = (self.initial_qty - visible_qty) / self.initial_qty
            if depletion > self.max_depletion_ratio:
                self.max_depletion_ratio = depletion

        best = book.best_bid() if self.direction == "LONG" else book.best_ask()
        if visible_qty is not None and best == self.wall_price:
            self.seconds_at_best_level += 1

        if touched and self.first_touch_at is None:
            self.first_touch_at = second
        if self.first_touch_at is not None:
            self.seconds_after_first_touch += 1

        if self.direction == "LONG":
            self.traded_notional_at_level += aggressive_sell_notional
        else:
            self.traded_notional_at_level += aggressive_buy_notional

        self.state = self._classify_state(
            visible_qty=visible_qty,
            delta=delta,
            touched=touched,
            last_price=last_price,
        )
        self._append_row(second, book, last_price, delta, visible_qty=visible_qty)

    def _visible_qty(self, book: OrderBookState) -> Decimal | None:
        side = book.bids if self.direction == "LONG" else book.asks
        return side.get(self.wall_price)

    def _price_touched(self, last_price: Decimal | None, book: OrderBookState) -> bool:
        if last_price is None:
            return False
        if self.direction == "LONG":
            return last_price <= self.wall_price
        return last_price >= self.wall_price

    def _classify_state(
        self,
        *,
        visible_qty: Decimal | None,
        delta: LevelDelta,
        touched: bool,
        last_price: Decimal | None,
    ) -> str:
        if self.aborted:
            return "WALL_DATA_ABORT"
        if visible_qty is None:
            if touched and last_price is not None:
                if self.direction == "LONG" and last_price < self.wall_price:
                    return "WALL_TRADED_THROUGH"
                if self.direction == "SHORT" and last_price > self.wall_price:
                    return "WALL_TRADED_THROUGH"
            return "WALL_REMOVED"
        if visible_qty == ZERO:
            return "WALL_REMOVED"
        if delta.removed > ZERO and delta.added > ZERO and visible_qty >= self.initial_qty:
            return "WALL_REFILLED"
        if delta.removed > ZERO and visible_qty < self.initial_qty:
            return "WALL_PARTIALLY_CONSUMED"
        if touched:
            return "WALL_TOUCHED"
        if self._approached(last_price):
            return "WALL_APPROACHED"
        return "WALL_HELD" if visible_qty >= self.initial_qty else "WALL_UNTOUCHED"

    def _approached(self, last_price: Decimal | None) -> bool:
        if last_price is None:
            return False
        if self.direction == "LONG":
            return last_price <= self.wall_price * Decimal("1.001")
        return last_price >= self.wall_price * Decimal("0.999")

    def _append_row(
        self,
        second: str,
        book: OrderBookState,
        last_price: Decimal | None,
        delta: LevelDelta,
        *,
        visible_qty: Decimal | None = None,
    ) -> None:
        qty = visible_qty if visible_qty is not None else self._visible_qty(book)
        retained = None
        if qty is not None and self.initial_qty > ZERO:
            retained = qty / self.initial_qty
        refill_ratio = None
        if self.cumulative_removed > ZERO:
            refill_ratio = self.cumulative_added / self.cumulative_removed
        self.rows.append(
            {
                "cluster_id": self.cluster_id,
                "direction": self.direction,
                "second": second,
                "wall_price": float(self.wall_price),
                "visible_qty": float(qty) if qty is not None else None,
                "initial_qty": float(self.initial_qty),
                "retained_qty_ratio": float(retained) if retained is not None else None,
                "cumulative_refill_qty": float(self.cumulative_added),
                "refill_to_consumption_ratio": float(refill_ratio)
                if refill_ratio is not None
                else None,
                "removal_ratio": float(self.cumulative_removed / self.initial_qty)
                if self.initial_qty > ZERO
                else None,
                "max_depletion_ratio": float(self.max_depletion_ratio),
                "genuine_added": float(delta.added),
                "genuine_removed": float(delta.removed),
                "carried_forward": delta.carried_forward,
                "state": self.state,
                "last_price": float(last_price) if last_price is not None else None,
                "best_bid": float(book.best_bid()) if book.best_bid() is not None else None,
                "best_ask": float(book.best_ask()) if book.best_ask() is not None else None,
                "classification_frozen": True,
            }
        )

    def summary(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "direction": self.direction,
            "wall_price": float(self.wall_price),
            "initial_qty": float(self.initial_qty),
            "final_state": self.state,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "seconds_visible": self.seconds_visible,
            "seconds_at_best_level": self.seconds_at_best_level,
            "seconds_after_first_touch": self.seconds_after_first_touch,
            "first_touch_at": self.first_touch_at,
            "max_depletion_ratio": float(self.max_depletion_ratio),
            "traded_notional_at_level": float(self.traded_notional_at_level),
        }


def comparison_group_from_state(state: str, aborted: bool) -> str:
    if aborted or state == "WALL_DATA_ABORT":
        return "WALL_DATA_ABORT"
    if state in {"WALL_HELD", "WALL_REFILLED", "WALL_APPROACHED", "WALL_TOUCHED"}:
        return "WALL_HELD_OR_REFILLED"
    if state in {"WALL_REMOVED", "WALL_TRADED_THROUGH", "WALL_PARTIALLY_CONSUMED"}:
        return "WALL_REMOVED_OR_TRADED_THROUGH"
    if state == "WALL_UNTOUCHED":
        return "WALL_NEVER_TOUCHED"
    return "PARTIAL_UNRESOLVED"

"""Core vs Overlay ledgers with per-fill fees and VWAP averages."""

from __future__ import annotations

from dataclasses import dataclass, field

from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from research.backtests.emergency_lock.cost_model import (
    fee_usdt,
    informative_slippage_cost_usdt,
)


def round_qty(qty: float, qty_step: float) -> float:
    step = float(qty_step)
    if step <= 0.0:
        raise ValueError("qty_step must be > 0")
    steps = round(float(qty) / step)
    return max(0.0, steps * step)


def round_price(price: float, tick_size: float) -> float:
    tick = float(tick_size)
    if tick <= 0.0:
        raise ValueError("tick_size must be > 0")
    ticks = round(float(price) / tick)
    return max(tick, ticks * tick)


def weighted_avg(qty_a: float, avg_a: float, qty_b: float, avg_b: float) -> float:
    total = float(qty_a) + float(qty_b)
    if total <= 0.0:
        return 0.0
    return (float(qty_a) * float(avg_a) + float(qty_b) * float(avg_b)) / total


@dataclass
class SidePosition:
    qty: float = 0.0
    avg: float = 0.0

    def open_add(self, qty: float, fill_price: float) -> None:
        q = float(qty)
        px = float(fill_price)
        if q <= 0.0 or px <= 0.0:
            raise ValueError("open_add requires positive qty and price")
        if self.qty <= 0.0:
            self.qty = q
            self.avg = px
            return
        new_qty = self.qty + q
        self.avg = (self.qty * self.avg + q * px) / new_qty
        self.qty = new_qty

    def close_all(self, exit_price: float, side: str) -> float:
        if self.qty <= 0.0:
            return 0.0
        pnl = calculate_pnl(self.avg, float(exit_price), self.qty, side)
        self.qty = 0.0
        self.avg = 0.0
        return pnl

    def close_qty(self, qty: float, exit_price: float, side: str) -> float:
        q = float(qty)
        if q <= 0.0:
            return 0.0
        if q - self.qty > 1e-12:
            raise ValueError(f"over-close: requested {q} > available {self.qty}")
        if abs(q - self.qty) <= 1e-12:
            return self.close_all(exit_price, side)
        pnl = calculate_pnl(self.avg, float(exit_price), q, side)
        self.qty -= q
        return pnl


@dataclass
class CoberturaLedger:
    """Strictly separated core and overlay positions."""

    core_long: SidePosition = field(default_factory=SidePosition)
    core_short: SidePosition = field(default_factory=SidePosition)
    overlay_long: SidePosition = field(default_factory=SidePosition)
    overlay_short: SidePosition = field(default_factory=SidePosition)

    realized_overlay_pnl: float = 0.0
    cumulative_entry_fees: float = 0.0
    cumulative_close_fees: float = 0.0
    cumulative_slippage_costs: float = 0.0  # informative; embedded in fill prices

    overlay_entry_fees: float = 0.0
    overlay_close_fees: float = 0.0
    overlay_add_count_round: int = 0

    def seed_core(
        self,
        *,
        long_qty: float,
        long_avg: float,
        short_qty: float,
        short_avg: float,
    ) -> None:
        self.core_long = SidePosition(qty=float(long_qty), avg=float(long_avg))
        self.core_short = SidePosition(qty=float(short_qty), avg=float(short_avg))

    def core_snapshot(self) -> tuple[float, float, float, float]:
        return (
            self.core_long.qty,
            self.core_long.avg,
            self.core_short.qty,
            self.core_short.avg,
        )

    def assert_core_unchanged(self, snapshot: tuple[float, float, float, float]) -> None:
        cur = self.core_snapshot()
        if any(abs(a - b) > 1e-12 for a, b in zip(cur, snapshot)):
            raise AssertionError(f"core mutated: before={snapshot} after={cur}")

    def total_long_qty(self) -> float:
        return self.core_long.qty + self.overlay_long.qty

    def total_short_qty(self) -> float:
        return self.core_short.qty + self.overlay_short.qty

    def total_long_avg(self) -> float:
        return weighted_avg(
            self.core_long.qty,
            self.core_long.avg,
            self.overlay_long.qty,
            self.overlay_long.avg,
        )

    def total_short_avg(self) -> float:
        return weighted_avg(
            self.core_short.qty,
            self.core_short.avg,
            self.overlay_short.qty,
            self.overlay_short.avg,
        )

    def net_qty(self) -> float:
        return self.total_long_qty() - self.total_short_qty()

    def open_pnl_at(self, price: float) -> dict[str, float]:
        px = float(price)
        return {
            "core_long_open_pnl": calculate_pnl(
                self.core_long.avg, px, self.core_long.qty, "long"
            )
            if self.core_long.qty > 0
            else 0.0,
            "core_short_open_pnl": calculate_pnl(
                self.core_short.avg, px, self.core_short.qty, "short"
            )
            if self.core_short.qty > 0
            else 0.0,
            "overlay_long_open_pnl": calculate_pnl(
                self.overlay_long.avg, px, self.overlay_long.qty, "long"
            )
            if self.overlay_long.qty > 0
            else 0.0,
            "overlay_short_open_pnl": calculate_pnl(
                self.overlay_short.avg, px, self.overlay_short.qty, "short"
            )
            if self.overlay_short.qty > 0
            else 0.0,
        }

    def _book_open_fee(self, fill_price: float, qty: float, fee_rate: float) -> float:
        fee = fee_usdt(fill_price=fill_price, qty=qty, fee_rate=fee_rate)
        self.cumulative_entry_fees += fee
        self.overlay_entry_fees += fee
        return fee

    def _book_close_fee(self, fill_price: float, qty: float, fee_rate: float) -> float:
        fee = fee_usdt(fill_price=fill_price, qty=qty, fee_rate=fee_rate)
        self.cumulative_close_fees += fee
        self.overlay_close_fees += fee
        return fee

    def open_overlay_short(
        self,
        *,
        qty: float,
        fill_price: float,
        reference_price: float,
        fee_rate_open: float,
    ) -> dict[str, float]:
        self.overlay_short.open_add(qty, fill_price)
        fee = self._book_open_fee(fill_price, qty, fee_rate_open)
        slip = informative_slippage_cost_usdt(
            side="short",
            reference_price=reference_price,
            fill_price=fill_price,
            qty=qty,
        )
        self.cumulative_slippage_costs += slip
        self.overlay_add_count_round += 1
        return {"fee": fee, "slippage_cost": slip, "realized_pnl_delta": 0.0}

    def open_overlay_long(
        self,
        *,
        qty: float,
        fill_price: float,
        reference_price: float,
        fee_rate_open: float,
    ) -> dict[str, float]:
        self.overlay_long.open_add(qty, fill_price)
        fee = self._book_open_fee(fill_price, qty, fee_rate_open)
        slip = informative_slippage_cost_usdt(
            side="long",
            reference_price=reference_price,
            fill_price=fill_price,
            qty=qty,
        )
        self.cumulative_slippage_costs += slip
        self.overlay_add_count_round += 1
        return {"fee": fee, "slippage_cost": slip, "realized_pnl_delta": 0.0}

    def close_overlay_short_qty(
        self,
        *,
        qty: float,
        fill_price: float,
        reference_price: float,
        fee_rate_close: float,
        open_fee_release: float = 0.0,
    ) -> dict[str, float]:
        """Reduce overlay short; remaining average is unchanged (no avg improvement)."""
        q = float(qty)
        if q <= 0.0:
            return {"fee": 0.0, "slippage_cost": 0.0, "realized_pnl_delta": 0.0, "qty": 0.0}
        pnl = self.overlay_short.close_qty(q, fill_price, "short")
        self.realized_overlay_pnl += pnl
        fee = self._book_close_fee(fill_price, q, fee_rate_close)
        slip = informative_slippage_cost_usdt(
            side="long",
            reference_price=reference_price,
            fill_price=fill_price,
            qty=q,
        )
        self.cumulative_slippage_costs += slip
        if open_fee_release > 0.0:
            self.overlay_entry_fees = max(
                0.0, float(self.overlay_entry_fees) - float(open_fee_release)
            )
        if self.overlay_short.qty <= 1e-12:
            self.overlay_add_count_round = 0
            self.overlay_entry_fees = 0.0
        return {
            "fee": fee,
            "slippage_cost": slip,
            "realized_pnl_delta": pnl,
            "qty": q,
        }

    def close_all_overlay_short(
        self,
        *,
        fill_price: float,
        reference_price: float,
        fee_rate_close: float,
    ) -> dict[str, float]:
        qty = self.overlay_short.qty
        if qty <= 0.0:
            return {"fee": 0.0, "slippage_cost": 0.0, "realized_pnl_delta": 0.0, "qty": 0.0}
        return self.close_overlay_short_qty(
            qty=qty,
            fill_price=fill_price,
            reference_price=reference_price,
            fee_rate_close=fee_rate_close,
            open_fee_release=float(self.overlay_entry_fees),
        )

    def close_all_overlay_long(
        self,
        *,
        fill_price: float,
        reference_price: float,
        fee_rate_close: float,
    ) -> dict[str, float]:
        qty = self.overlay_long.qty
        if qty <= 0.0:
            return {"fee": 0.0, "slippage_cost": 0.0, "realized_pnl_delta": 0.0, "qty": 0.0}
        pnl = self.overlay_long.close_all(fill_price, "long")
        self.realized_overlay_pnl += pnl
        fee = self._book_close_fee(fill_price, qty, fee_rate_close)
        slip = informative_slippage_cost_usdt(
            side="short",  # sell to close long
            reference_price=reference_price,
            fill_price=fill_price,
            qty=qty,
        )
        self.cumulative_slippage_costs += slip
        self.overlay_add_count_round = 0
        self.overlay_entry_fees = 0.0
        return {
            "fee": fee,
            "slippage_cost": slip,
            "realized_pnl_delta": pnl,
            "qty": qty,
        }

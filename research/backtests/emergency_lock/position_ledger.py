"""Neutral hedge-mode position ledger for Emergency-Lock research.

VWAP updates match ``SimulatedOrderBook.apply_fill`` for non-reduce opens:

    new_avg = (prev_qty * prev_avg + fill_qty * fill_price) / (prev_qty + fill_qty)

Gross unrealized / realized PnL uses ``fixed_cycle_hedge_bot.math_utils.calculate_pnl``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from .cost_model import (
    conservative_long_close_fill_price,
    conservative_short_close_fill_price,
    fee_usdt,
    informative_slippage_cost_usdt,
)


@dataclass
class PositionLedger:
    """Strategy-independent long/short ledger with immediate fee booking."""

    long_qty: float = 0.0
    long_avg: float = 0.0
    short_qty: float = 0.0
    short_avg: float = 0.0

    realized_long_pnl: float = 0.0
    realized_short_pnl: float = 0.0

    opening_fees: float = 0.0
    closing_fees: float = 0.0
    lock_fees: float = 0.0
    unlock_closing_fees: float = 0.0
    relock_opening_fees: float = 0.0
    final_exit_fees: float = 0.0
    total_fees: float = 0.0

    slippage_cost: float = 0.0  # informational; already in fill prices
    funding_cost: float = 0.0

    events: list[dict[str, float | str]] = field(default_factory=list)

    def net_qty(self) -> float:
        return float(self.long_qty) - float(self.short_qty)

    def _book_fee(self, amount: float, *, bucket: str) -> None:
        fee = float(amount)
        if fee <= 0.0:
            return
        self.total_fees += fee
        if bucket == "opening":
            self.opening_fees += fee
        elif bucket == "lock":
            self.lock_fees += fee
            self.opening_fees += fee
        elif bucket == "relock":
            self.relock_opening_fees += fee
            self.opening_fees += fee
        elif bucket == "closing":
            self.closing_fees += fee
        elif bucket == "unlock_closing":
            self.unlock_closing_fees += fee
            self.closing_fees += fee
        elif bucket == "final_exit":
            self.final_exit_fees += fee
            self.closing_fees += fee
        else:
            raise ValueError(f"unknown fee bucket: {bucket}")

    def _vwap_open(self, *, side: str, qty: float, fill_price: float) -> None:
        qty_f = float(qty)
        px = float(fill_price)
        if qty_f <= 0.0:
            raise ValueError("open qty must be positive")
        if px <= 0.0:
            raise ValueError("fill_price must be positive")

        if side == "long":
            prev_qty = float(self.long_qty)
            new_qty = prev_qty + qty_f
            self.long_avg = (
                (prev_qty * self.long_avg + qty_f * px) / new_qty
                if prev_qty > 0.0
                else px
            )
            self.long_qty = new_qty
        elif side == "short":
            prev_qty = float(self.short_qty)
            new_qty = prev_qty + qty_f
            self.short_avg = (
                (prev_qty * self.short_avg + qty_f * px) / new_qty
                if prev_qty > 0.0
                else px
            )
            self.short_qty = new_qty
        else:
            raise ValueError(f"unsupported side: {side}")

    def open_long(
        self,
        *,
        qty: float,
        fill_price: float,
        fee_rate: float,
        reference_price: float | None = None,
    ) -> dict[str, float]:
        """Open / add long; book fee immediately."""
        ref = float(reference_price) if reference_price is not None else float(fill_price)
        self._vwap_open(side="long", qty=qty, fill_price=fill_price)
        fee = fee_usdt(fill_price=fill_price, qty=qty, fee_rate=fee_rate)
        self._book_fee(fee, bucket="opening")
        slip = informative_slippage_cost_usdt(
            side="long",
            reference_price=ref,
            fill_price=fill_price,
            qty=qty,
        )
        self.slippage_cost += slip
        event = {
            "action": "open_long",
            "qty": float(qty),
            "fill_price": float(fill_price),
            "fee": fee,
            "slippage_cost": slip,
            "realized_pnl_delta": 0.0,
        }
        self.events.append(event)
        return event

    def open_short(
        self,
        *,
        qty: float,
        fill_price: float,
        fee_rate: float,
        reference_price: float | None = None,
        fee_bucket: str = "opening",
    ) -> dict[str, float]:
        """Open / add short; book fee immediately."""
        ref = float(reference_price) if reference_price is not None else float(fill_price)
        self._vwap_open(side="short", qty=qty, fill_price=fill_price)
        fee = fee_usdt(fill_price=fill_price, qty=qty, fee_rate=fee_rate)
        self._book_fee(fee, bucket=fee_bucket)
        slip = informative_slippage_cost_usdt(
            side="short",
            reference_price=ref,
            fill_price=fill_price,
            qty=qty,
        )
        self.slippage_cost += slip
        action = {
            "opening": "open_short",
            "lock": "emergency_short",
            "relock": "relock_short",
        }.get(fee_bucket, "open_short")
        event = {
            "action": action,
            "qty": float(qty),
            "fill_price": float(fill_price),
            "fee": fee,
            "slippage_cost": slip,
            "realized_pnl_delta": 0.0,
        }
        self.events.append(event)
        return event

    def emergency_short_top_up(
        self,
        *,
        fill_price: float,
        fee_rate: float,
        reference_price: float | None = None,
        qty_tolerance: float = 1e-12,
    ) -> dict[str, float]:
        """Add short qty so ``short_qty`` matches ``long_qty`` (no overhedge)."""
        needed = max(float(self.long_qty) - float(self.short_qty), 0.0)
        if needed <= qty_tolerance:
            return {
                "action": "emergency_short_noop",
                "qty": 0.0,
                "fill_price": float(fill_price),
                "fee": 0.0,
                "slippage_cost": 0.0,
                "realized_pnl_delta": 0.0,
            }
        return self.open_short(
            qty=needed,
            fill_price=fill_price,
            fee_rate=fee_rate,
            reference_price=reference_price,
            fee_bucket="lock",
        )

    def close_short(
        self,
        *,
        qty: float,
        fill_price: float,
        fee_rate: float,
        reference_price: float | None = None,
        fee_bucket: str = "closing",
    ) -> dict[str, float]:
        """Reduce short (buy-to-close). Remaining short_avg is unchanged."""
        qty_f = float(qty)
        if qty_f <= 0.0:
            raise ValueError("close qty must be positive")
        if qty_f > self.short_qty + 1e-12:
            raise ValueError(
                f"close_short qty {qty_f} exceeds short_qty {self.short_qty}"
            )
        close_qty = min(qty_f, float(self.short_qty))
        avg = float(self.short_avg)
        px = float(fill_price)
        realized = float(calculate_pnl(avg, px, close_qty, "short"))
        self.realized_short_pnl += realized
        self.short_qty = max(0.0, float(self.short_qty) - close_qty)
        if self.short_qty <= 1e-12:
            self.short_qty = 0.0
            self.short_avg = 0.0

        fee = fee_usdt(fill_price=px, qty=close_qty, fee_rate=fee_rate)
        self._book_fee(fee, bucket=fee_bucket)
        ref = float(reference_price) if reference_price is not None else px
        # Buy-to-close: treat as long-side adverse slippage for informative cost.
        slip = informative_slippage_cost_usdt(
            side="long",
            reference_price=ref,
            fill_price=px,
            qty=close_qty,
        )
        self.slippage_cost += slip
        event = {
            "action": "close_short",
            "qty": close_qty,
            "fill_price": px,
            "fee": fee,
            "slippage_cost": slip,
            "realized_pnl_delta": realized,
        }
        self.events.append(event)
        return event

    def close_long(
        self,
        *,
        qty: float,
        fill_price: float,
        fee_rate: float,
        reference_price: float | None = None,
        fee_bucket: str = "closing",
    ) -> dict[str, float]:
        """Reduce long (sell-to-close). Remaining long_avg is unchanged."""
        qty_f = float(qty)
        if qty_f <= 0.0:
            raise ValueError("close qty must be positive")
        if qty_f > self.long_qty + 1e-12:
            raise ValueError(f"close_long qty {qty_f} exceeds long_qty {self.long_qty}")
        close_qty = min(qty_f, float(self.long_qty))
        avg = float(self.long_avg)
        px = float(fill_price)
        realized = float(calculate_pnl(avg, px, close_qty, "long"))
        self.realized_long_pnl += realized
        self.long_qty = max(0.0, float(self.long_qty) - close_qty)
        if self.long_qty <= 1e-12:
            self.long_qty = 0.0
            self.long_avg = 0.0

        fee = fee_usdt(fill_price=px, qty=close_qty, fee_rate=fee_rate)
        self._book_fee(fee, bucket=fee_bucket)
        ref = float(reference_price) if reference_price is not None else px
        slip = informative_slippage_cost_usdt(
            side="short",
            reference_price=ref,
            fill_price=px,
            qty=close_qty,
        )
        self.slippage_cost += slip
        event = {
            "action": "close_long",
            "qty": close_qty,
            "fill_price": px,
            "fee": fee,
            "slippage_cost": slip,
            "realized_pnl_delta": realized,
        }
        self.events.append(event)
        return event

    def unrealized_long_pnl(self, mark_price: float) -> float:
        if self.long_qty <= 0.0 or self.long_avg <= 0.0:
            return 0.0
        return float(
            calculate_pnl(self.long_avg, float(mark_price), self.long_qty, "long")
        )

    def unrealized_short_pnl(self, mark_price: float) -> float:
        if self.short_qty <= 0.0 or self.short_avg <= 0.0:
            return 0.0
        return float(
            calculate_pnl(self.short_avg, float(mark_price), self.short_qty, "short")
        )

    def apply_funding(self, amount_usdt: float) -> None:
        """Accumulate funding cost (positive = paid)."""
        self.funding_cost += float(amount_usdt)

    def basket_net_pnl(self, mark_price: float) -> float:
        """Net basket PnL at ``mark_price``.

        Slippage is already embedded in average entry / realized fill prices and
        is therefore **not** subtracted again. Fees and funding are explicit.
        """
        return (
            self.unrealized_long_pnl(mark_price)
            + self.unrealized_short_pnl(mark_price)
            + float(self.realized_long_pnl)
            + float(self.realized_short_pnl)
            - float(self.total_fees)
            - float(self.funding_cost)
        )

    def project_full_close_net_pnl(
        self,
        *,
        reference_price: float,
        fee_rate: float,
        slippage_bps: float,
    ) -> dict[str, float]:
        """Project net PnL after closing both sides at conservative fills."""
        ref = float(reference_price)
        long_fill = conservative_long_close_fill_price(
            reference_price=ref, slippage_bps=slippage_bps
        )
        short_fill = conservative_short_close_fill_price(
            trigger_price=ref, candle_high=ref, slippage_bps=slippage_bps
        )
        realized_long_delta = 0.0
        realized_short_delta = 0.0
        fee_long = 0.0
        fee_short = 0.0
        slip_long = 0.0
        slip_short = 0.0
        if self.long_qty > 0.0 and self.long_avg > 0.0:
            realized_long_delta = float(
                calculate_pnl(self.long_avg, long_fill, self.long_qty, "long")
            )
            fee_long = fee_usdt(
                fill_price=long_fill, qty=self.long_qty, fee_rate=fee_rate
            )
            slip_long = informative_slippage_cost_usdt(
                side="short",
                reference_price=ref,
                fill_price=long_fill,
                qty=self.long_qty,
            )
        if self.short_qty > 0.0 and self.short_avg > 0.0:
            realized_short_delta = float(
                calculate_pnl(self.short_avg, short_fill, self.short_qty, "short")
            )
            fee_short = fee_usdt(
                fill_price=short_fill, qty=self.short_qty, fee_rate=fee_rate
            )
            slip_short = informative_slippage_cost_usdt(
                side="long",
                reference_price=ref,
                fill_price=short_fill,
                qty=self.short_qty,
            )
        projected_fees = fee_long + fee_short
        projected_slip = slip_long + slip_short
        projected_final = (
            float(self.realized_long_pnl)
            + float(self.realized_short_pnl)
            + realized_long_delta
            + realized_short_delta
            - float(self.total_fees)
            - projected_fees
            - float(self.funding_cost)
        )
        return {
            "long_fill": long_fill,
            "short_fill": short_fill,
            "projected_closing_fees": projected_fees,
            "projected_exit_slippage": projected_slip,
            "projected_final_net_pnl_after_closing_costs": projected_final,
            "basket_pnl_before_exit": self.basket_net_pnl(ref),
        }

    def snapshot(self, mark_price: float) -> dict[str, float]:
        return {
            "long_qty": float(self.long_qty),
            "long_avg": float(self.long_avg),
            "short_qty": float(self.short_qty),
            "short_avg": float(self.short_avg),
            "net_qty": self.net_qty(),
            "unrealized_long_pnl": self.unrealized_long_pnl(mark_price),
            "unrealized_short_pnl": self.unrealized_short_pnl(mark_price),
            "realized_long_pnl": float(self.realized_long_pnl),
            "realized_short_pnl": float(self.realized_short_pnl),
            "opening_fees": float(self.opening_fees),
            "closing_fees": float(self.closing_fees),
            "lock_fees": float(self.lock_fees),
            "unlock_closing_fees": float(self.unlock_closing_fees),
            "relock_opening_fees": float(self.relock_opening_fees),
            "final_exit_fees": float(self.final_exit_fees),
            "total_fees": float(self.total_fees),
            "slippage_cost": float(self.slippage_cost),
            "funding_cost": float(self.funding_cost),
            "basket_net_pnl": self.basket_net_pnl(mark_price),
        }


def qty_from_notional(*, notional_usdt: float, price: float) -> float:
    if price <= 0.0:
        raise ValueError("price must be positive")
    return float(notional_usdt) / float(price)


def emergency_trigger_price(*, long_avg: float, emergency_trigger_pct: float) -> float:
    return float(long_avg) * (1.0 - float(emergency_trigger_pct))

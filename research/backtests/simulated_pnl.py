"""Bot-aligned closed PnL for backtest fills (Phase 3.5).

Reuses ``fixed_cycle_hedge_bot.math_utils.calculate_pnl`` for gross PnL and
mirrors the fee-aware reduce-only path from ``runtime.py`` when ``fee_rate`` is
provided.

Intracandle note (5m V1): multiple resting orders may fill on the same candle.
Phase 4 will define a conservative fill ordering model; Phase 3.5 only aligns PnL.
"""

from __future__ import annotations

from typing import Any

from fixed_cycle_hedge_bot.math_utils import calculate_pnl


def calculate_simulated_closed_pnl(
    *,
    side: str,
    avg_entry_price: float,
    fill_price: float,
    qty: float,
    reduce_only: bool = True,
    fee_rate: float | None = None,
    fee_quote: float = 0.0,
) -> tuple[float, dict[str, float | None]]:
    """Return net closed PnL and calculation details for a simulated fill.

    ``side`` is the position leg (``long`` or ``short``), matching bot/runtime
    semantics for reduce-only exits.

    Opening fills (``reduce_only=False``) always return ``0.0``.
    Reduce/exit fills use the same gross formula as ``math_utils.calculate_pnl``:

    - long reduced by sell: ``(fill_price - avg_entry_price) * qty``
    - short reduced by buy: ``(avg_entry_price - fill_price) * qty``

    When ``fee_rate`` is set (decimal, e.g. ``0.00055`` for 0.055%), entry and
    exit fees are subtracted like ``runtime.py``:

    ``net = gross - abs(avg * qty) * fee_rate - abs(fill * qty) * fee_rate - fee_quote``
    """
    details: dict[str, float | None] = {
        "gross_pnl": 0.0,
        "entry_fee": None,
        "exit_fee": None,
        "fee_rate": fee_rate,
        "fee_quote": float(fee_quote),
        "pnl_calc_source": "simulated_opening_zero",
    }

    if not reduce_only:
        return 0.0, details

    if qty <= 0 or avg_entry_price <= 0:
        details["pnl_calc_source"] = "simulated_reduce_missing_entry"
        return 0.0, details

    gross_pnl = float(
        calculate_pnl(float(avg_entry_price), float(fill_price), float(qty), str(side))
    )
    details["gross_pnl"] = gross_pnl

    if fee_rate is not None and float(fee_rate) > 0:
        rate = float(fee_rate)
        entry_fee = abs(float(avg_entry_price) * float(qty)) * rate
        exit_fee = abs(float(fill_price) * float(qty)) * rate
        net_pnl = gross_pnl - entry_fee - exit_fee - float(fee_quote)
        details.update(
            {
                "entry_fee": entry_fee,
                "exit_fee": exit_fee,
                "pnl_calc_source": "simulated_calculate_pnl_with_fees",
            }
        )
        return float(net_pnl), details

    details["pnl_calc_source"] = "simulated_calculate_pnl"
    return gross_pnl - float(fee_quote), details


def attach_closed_pnl_metadata(
    metadata: dict[str, Any],
    closed_pnl: float,
    *,
    pnl_details: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Set confirmed/closed/runtime PnL fields consistently on fill metadata."""
    pnl = float(closed_pnl)
    metadata["confirmed_closed_pnl"] = pnl
    metadata["closed_pnl"] = pnl
    metadata["runtime_calculated_pnl"] = pnl
    metadata["exec_pnl"] = pnl

    if pnl_details:
        for key in (
            "gross_pnl",
            "entry_fee",
            "exit_fee",
            "fee_rate",
            "fee_quote",
            "pnl_calc_source",
        ):
            value = pnl_details.get(key)
            if value is not None:
                metadata[key] = value
        if pnl_details.get("entry_fee") is not None:
            metadata["runtime_entry_fee"] = pnl_details.get("entry_fee")
        if pnl_details.get("exit_fee") is not None:
            metadata["runtime_exit_fee"] = pnl_details.get("exit_fee")
        if pnl_details.get("gross_pnl") is not None:
            metadata["runtime_gross_pnl"] = pnl_details.get("gross_pnl")
        if pnl_details.get("fee_rate") is not None:
            metadata["runtime_fee_rate"] = pnl_details.get("fee_rate")
        source = pnl_details.get("pnl_calc_source")
        if source:
            metadata["pnl_calc_source"] = source

    return metadata


def closed_pnl_for_virtual_order_fill(
    *,
    side: str,
    reduce_only: bool,
    avg_entry_price: float,
    fill_price: float,
    qty: float,
    fee_rate: float | None = None,
) -> tuple[float, dict[str, float | None]]:
    """Compute closed PnL for a virtual order fill against book average entry."""
    close_qty = float(qty)
    if reduce_only and avg_entry_price > 0:
        # Cap at available position is handled by caller; PnL uses actual close qty.
        pass
    return calculate_simulated_closed_pnl(
        side=str(side).lower(),
        avg_entry_price=float(avg_entry_price),
        fill_price=float(fill_price),
        qty=close_qty,
        reduce_only=bool(reduce_only),
        fee_rate=fee_rate,
    )

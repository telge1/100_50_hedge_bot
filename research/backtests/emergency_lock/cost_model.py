"""Fee, slippage, and funding helpers for Emergency-Lock Phase A.

Fee semantics
-------------
On every actual fill (initial long, initial short, emergency short top-up):

    fee_usdt = abs(fill_price * qty) * fee_rate

Fees are booked immediately into the ledger cost fields and subtracted once
from ``basket_net_pnl``. Opening fills do not create realized PnL.

Slippage semantics
------------------
Slippage is applied **inside the fill price**, not as a second PnL debit.

For a short opening fill in ``conservative`` mode the reference price is the
emergency trigger (never the candle low when the low is below the trigger):

    raw_short_fill = trigger_price
    short_fill = raw_short_fill * (1 - slippage_bps / 10_000)

A lower short entry is worse for the short (more adverse). The informative
``slippage_cost_usdt`` is:

    max(raw_short_fill - short_fill, 0) * qty

and must **not** be subtracted again from ``basket_net_pnl``.

Long opening fills use the start close with the same bps as adverse slippage
(higher buy price):

    long_fill = entry_ref * (1 + slippage_bps / 10_000)

Funding semantics
-----------------
When ``funding_enabled`` is true, every ``funding_interval_hours`` of elapsed
candle time applies:

    funding_usdt = (long_qty - short_qty) * mark_price * funding_rate_per_interval

For a full lock (``long_qty == short_qty``) this contribution is zero even if
funding is enabled. Non-zero post-lock funding drift only appears if the
ledger is intentionally unbalanced or the rate model is extended later.
"""

from __future__ import annotations

BPS_DIVISOR = 10_000.0


def fee_usdt(*, fill_price: float, qty: float, fee_rate: float) -> float:
    """Return absolute fee in quote currency for a fill."""
    if fee_rate <= 0.0 or qty == 0.0:
        return 0.0
    return abs(float(fill_price) * float(qty)) * float(fee_rate)


def apply_long_open_slippage(*, reference_price: float, slippage_bps: float) -> float:
    """Worse long fill: pay a higher price."""
    return float(reference_price) * (1.0 + float(slippage_bps) / BPS_DIVISOR)


def apply_short_open_slippage(*, reference_price: float, slippage_bps: float) -> float:
    """Worse short fill: receive a lower price."""
    return float(reference_price) * (1.0 - float(slippage_bps) / BPS_DIVISOR)


def informative_slippage_cost_usdt(
    *,
    side: str,
    reference_price: float,
    fill_price: float,
    qty: float,
) -> float:
    """Non-negative informational slippage cost already embedded in fill_price."""
    side_l = str(side).lower()
    qty_f = abs(float(qty))
    if side_l == "long":
        return max(float(fill_price) - float(reference_price), 0.0) * qty_f
    if side_l == "short":
        return max(float(reference_price) - float(fill_price), 0.0) * qty_f
    raise ValueError(f"unsupported side for slippage cost: {side}")


def conservative_emergency_short_fill_price(
    *,
    trigger_price: float,
    candle_low: float,
    slippage_bps: float,
) -> float:
    """Conservative short top-up fill for Emergency Lock.

    Trigger detection uses ``candle.low <= trigger_price``. The fill itself
    must not be better than the trigger: never use a lower candle low as a
    more favourable short entry. Slippage then worsens the short further.
    """
    _ = candle_low  # detection only; deliberately unused for fill price
    raw = float(trigger_price)
    return apply_short_open_slippage(reference_price=raw, slippage_bps=slippage_bps)


def funding_payment_usdt(
    *,
    long_qty: float,
    short_qty: float,
    mark_price: float,
    funding_rate: float,
) -> float:
    """Net funding paid (positive = cost) for one funding interval.

    Convention: long pays short when rate > 0 on net long exposure.
    ``(long_qty - short_qty) * mark * rate``.
    """
    net_qty = float(long_qty) - float(short_qty)
    return net_qty * float(mark_price) * float(funding_rate)


def conservative_short_close_fill_price(
    *,
    trigger_price: float,
    candle_high: float,
    slippage_bps: float,
) -> float:
    """Buy-to-close short: fill not better than trigger; buy slippage raises price.

    Detection may use ``candle.high >= trigger``. The fill never uses a higher
    candle high as a more favourable (lower) buy. Fill model:

        fill = trigger_price * (1 + slippage_bps / 10_000)
    """
    _ = candle_high
    return apply_long_open_slippage(
        reference_price=float(trigger_price), slippage_bps=slippage_bps
    )


def conservative_long_close_fill_price(
    *,
    reference_price: float,
    slippage_bps: float,
) -> float:
    """Sell-to-close long: sell slippage lowers the fill price."""
    return apply_short_open_slippage(
        reference_price=float(reference_price), slippage_bps=slippage_bps
    )


def conservative_relock_short_fill_price(
    *,
    trigger_price: float,
    candle_low: float,
    slippage_bps: float,
) -> float:
    """Re-open short: same conservative short-open rule as emergency lock."""
    return conservative_emergency_short_fill_price(
        trigger_price=trigger_price,
        candle_low=candle_low,
        slippage_bps=slippage_bps,
    )

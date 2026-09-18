"""Direction-normalized percent geometry (no bps in outputs)."""

from __future__ import annotations


def favorable_pct(*, trade_side: str, trigger_price: float, future_price: float) -> float:
    side = str(trade_side).upper()
    tp = float(trigger_price)
    fp = float(future_price)
    if tp <= 0:
        raise ValueError("non-positive trigger_price")
    if side == "LONG":
        return 100.0 * (fp - tp) / tp
    if side == "SHORT":
        return 100.0 * (tp - fp) / tp
    raise ValueError(f"unknown trade_side:{trade_side}")


def adverse_pct(*, trade_side: str, trigger_price: float, future_price: float) -> float:
    # adverse is opposite of favorable; store as positive magnitude via max(0, -favorable)
    return -favorable_pct(
        trade_side=trade_side, trigger_price=trigger_price, future_price=future_price
    )


def signed_return_pct(*, trade_side: str, trigger_price: float, future_close: float) -> float:
    return favorable_pct(
        trade_side=trade_side, trigger_price=trigger_price, future_price=future_close
    )


def mfe_from_extremes(*, trade_side: str, trigger_price: float, high: float, low: float) -> float:
    side = str(trade_side).upper()
    if side == "LONG":
        return max(0.0, favorable_pct(trade_side=side, trigger_price=trigger_price, future_price=high))
    if side == "SHORT":
        return max(0.0, favorable_pct(trade_side=side, trigger_price=trigger_price, future_price=low))
    raise ValueError(f"unknown trade_side:{trade_side}")


def mae_from_extremes(*, trade_side: str, trigger_price: float, high: float, low: float) -> float:
    side = str(trade_side).upper()
    if side == "LONG":
        return max(0.0, -favorable_pct(trade_side=side, trigger_price=trigger_price, future_price=low))
    if side == "SHORT":
        return max(0.0, -favorable_pct(trade_side=side, trigger_price=trigger_price, future_price=high))
    raise ValueError(f"unknown trade_side:{trade_side}")

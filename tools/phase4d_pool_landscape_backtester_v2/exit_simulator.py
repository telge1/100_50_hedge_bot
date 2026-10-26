"""SL/TP exit on 5m OHLC after fill."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ExitResult:
    exit_utc: str | None
    exit_reason: str
    exit_price: float | None
    gross_pnl_pct: float | None
    net_pnl_pct: float | None
    bars: int
    trace: list[dict]
    conservative_sl_first: bool = False


def simulate_exit(
    direction: str,
    entry_utc: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    prices: dict[str, dict],
    pilot_to: str,
    fees_pct: float,
) -> ExitResult:
    times = sorted(prices.keys())
    idx = next((i for i, k in enumerate(times) if k >= entry_utc), len(times))
    trace = []
    bars = 0
    for k in times[idx:]:
        if k < entry_utc:
            continue
        if k > pilot_to:
            break
        bar = prices[k]
        bars += 1
        h, l = bar["h"], bar["l"]
        if direction == "SHORT":
            hit_sl = h >= sl_price
            hit_tp = l <= tp_price
        else:
            hit_sl = l <= sl_price
            hit_tp = h >= tp_price
        trace.append({"bar_utc": k, "high": h, "low": l, "hit_sl": hit_sl, "hit_tp": hit_tp})
        if hit_sl and hit_tp:
            reason = "AMBIGUOUS_ENTRY_EXIT_SEQUENCE" if k == entry_utc else "AMBIGUOUS_INTRABAR_EXIT"
            return ExitResult(
                exit_utc=k,
                exit_reason=reason,
                exit_price=None,
                gross_pnl_pct=None,
                net_pnl_pct=None,
                bars=bars,
                trace=trace,
                conservative_sl_first=True,
            )
        if hit_sl:
            gross = _gross(direction, entry_price, sl_price)
            return ExitResult(
                exit_utc=k,
                exit_reason="SL",
                exit_price=sl_price,
                gross_pnl_pct=gross,
                net_pnl_pct=gross - fees_pct,
                bars=bars,
                trace=trace,
            )
        if hit_tp:
            gross = _gross(direction, entry_price, tp_price)
            return ExitResult(
                exit_utc=k,
                exit_reason="TP",
                exit_price=tp_price,
                gross_pnl_pct=gross,
                net_pnl_pct=gross - fees_pct,
                bars=bars,
                trace=trace,
            )
    return ExitResult(
        exit_utc=None,
        exit_reason="POSITION_OPEN_AT_DATA_END",
        exit_price=None,
        gross_pnl_pct=None,
        net_pnl_pct=None,
        bars=bars,
        trace=trace,
    )


def _gross(direction: str, entry: float, exit_p: float) -> float:
    if direction == "SHORT":
        return (entry - exit_p) / entry * 100.0
    return (exit_p - entry) / entry * 100.0

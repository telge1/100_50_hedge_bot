"""V3Y exit: V3U SL/TP/channel + support-break confirmation (isolated)."""

from __future__ import annotations

from typing import Any, Literal

from line_geometry import BAR_SEC_5M, LineDef

from v3y_config import CONFIRM_DELTA, EXIT_KIND

ExitKind = Literal[
    "TP",
    "CHANNEL_UPPER",
    "SL",
    "AMBIGUOUS",
    "OPEN_AT_WINDOW_END",
    "SUPPORT_BREAK_EXIT",
    "NO_TRADE",
]


def channel_line_at_bar(channel_line: LineDef | None, candle: dict[str, Any]) -> float | None:
    if channel_line is None:
        return None
    t = float(candle["time"])
    if not channel_line.active_for_candle(t):
        return None
    p = channel_line.price_at(t)
    if p is None:
        return None
    return float(p)


def line_price_at_close(line: LineDef, candle: dict[str, Any]) -> float | None:
    if not line.active_for_candle(float(candle["time"])):
        return None
    return line.price_at(float(candle["time"]) + BAR_SEC_5M)


def simulate_exit_v3y(
    candles: list[dict],
    start_idx: int,
    entry: float,
    sl: float,
    pool_tp: float | None,
    channel_line: LineDef | None,
    support_line: LineDef | None,
    *,
    d: float = CONFIRM_DELTA,
) -> tuple[ExitKind, int | None, float | None, float | None, int | None]:
    if pool_tp is not None and pool_tp <= entry:
        pool_tp = None
    has_channel = channel_line is not None
    if pool_tp is None and not has_channel and support_line is None:
        return "NO_TRADE", None, None, None, None

    state = "MONITOR"
    break_idx = -1
    break_close = 0.0
    schedule_fill_at_open = False

    for j in range(start_idx, len(candles)):
        c = candles[j]

        if schedule_fill_at_open:
            pnl = (float(c["open"]) - entry) / entry * 100.0
            return EXIT_KIND, c["time"], float(c["open"]), pnl, j

        channel_px = channel_line_at_bar(channel_line, c)
        hit_sl = float(c["low"]) <= sl
        profit_targets: list[tuple[str, float]] = []
        if pool_tp is not None and float(c["high"]) >= pool_tp:
            profit_targets.append(("TP", float(pool_tp)))
        if channel_px is not None and float(c["high"]) >= channel_px:
            profit_targets.append(("CHANNEL_UPPER", channel_px))
        hit_profit = bool(profit_targets)

        if hit_sl and hit_profit:
            return "AMBIGUOUS", c["time"], sl, (sl - entry) / entry * 100.0, j
        if hit_sl:
            pnl = (sl - entry) / entry * 100.0
            return "SL", c["time"], sl, pnl, j
        if profit_targets:
            kind, exit_px = min(profit_targets, key=lambda x: x[1])
            pnl = (exit_px - entry) / entry * 100.0
            return kind, c["time"], exit_px, pnl, j  # type: ignore[return-value]

        if support_line is None:
            continue

        if not support_line.active_for_candle(float(c["time"])):
            state = "MONITOR"
            break_idx = -1
            continue

        lp = line_price_at_close(support_line, c)
        if lp is None:
            continue

        if state == "MONITOR":
            if float(c["close"]) < lp:
                state = "BREAK_PENDING"
                break_close = float(c["close"])
                break_idx = j
        elif state == "BREAK_PENDING" and j > break_idx:
            nc = float(c["close"])
            if nc <= break_close * (1.0 - d):
                schedule_fill_at_open = True
            elif nc >= break_close * (1.0 + d):
                state = "MONITOR"
                break_idx = -1
            elif nc >= lp:
                state = "MONITOR"
                break_idx = -1
            else:
                break_close = nc
                break_idx = j

    last = candles[-1]
    pnl = (float(last["close"]) - entry) / entry * 100.0
    return "OPEN_AT_WINDOW_END", last["time"], float(last["close"]), pnl, len(candles) - 1

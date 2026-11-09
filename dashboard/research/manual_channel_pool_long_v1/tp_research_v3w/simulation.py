"""OHLC exit simulation for V3W (isolated from production engine)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import run_l1_pilot as l1

ExitKind = Literal["TP", "CHANNEL_UPPER", "SL", "AMBIGUOUS", "OPEN_AT_WINDOW_END", "PARTIAL_DONE"]


@dataclass
class SimResult:
    gross_pnl_pct: float
    exit_kind: str
    exit_utc: str
    exit_price: float | None
    exit_idx: int | None
    mfe_pct: float
    mae_pct: float
    ambiguous: bool
    leg_fills: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False


def _mfe_mae(candles: list[dict], start_idx: int, end_idx: int, entry: float) -> tuple[float, float]:
    mfe, mae = 0.0, 0.0
    for j in range(start_idx, min(end_idx + 1, len(candles))):
        hi, lo = float(candles[j]["high"]), float(candles[j]["low"])
        mfe = max(mfe, (hi - entry) / entry * 100.0)
        mae = min(mae, (lo - entry) / entry * 100.0)
    return round(mfe, 4), round(mae, 4)


def simulate_standard(
    candles: list[dict],
    start_idx: int,
    entry: float,
    sl: float,
    pool_tp: float | None,
    channel_line,
) -> SimResult:
    kind, exit_ts, exit_px, pnl, exit_idx = l1.simulate_exit(
        candles, start_idx, entry, sl, pool_tp, channel_line
    )
    if kind == "NO_TRADE":
        return SimResult(0.0, "NO_TRADE", "", None, None, 0.0, 0.0, False, incomplete=True)
    mfe, mae = _mfe_mae(candles, start_idx, exit_idx or start_idx, entry)
    amb = kind == "AMBIGUOUS"
    return SimResult(
        float(pnl) if pnl is not None else 0.0,
        str(kind),
        l1.fmt_utc(exit_ts) if exit_ts else "",
        float(exit_px) if exit_px is not None else None,
        exit_idx,
        mfe,
        mae,
        amb,
        [
            {
                "leg_idx": 0,
                "weight": 1.0,
                "tp_price": pool_tp,
                "exit_utc": l1.fmt_utc(exit_ts) if exit_ts else "",
                "exit_price": exit_px,
                "exit_kind": kind,
                "leg_pnl_pct": pnl,
            }
        ],
    )


def simulate_multi_legs(
    candles: list[dict],
    start_idx: int,
    entry: float,
    sl: float,
    tp_prices: list[float],
    weights: list[float],
    *,
    channel_line=None,
    breakeven_after_leg: int | None = None,
) -> SimResult:
    if abs(sum(weights) - 1.0) > 1e-6:
        return SimResult(0.0, "NO_TRADE", "", None, None, 0.0, 0.0, False, incomplete=True)
    if len(tp_prices) != len(weights):
        return SimResult(0.0, "NO_TRADE", "", None, None, 0.0, 0.0, False, incomplete=True)

    legs = list(zip(weights, tp_prices))
    filled: list[dict[str, Any]] = []
    remaining = 1.0
    leg_i = 0
    current_sl = sl
    last_idx = start_idx
    gross = 0.0
    amb = False
    final_kind = ""

    for j in range(start_idx, len(candles)):
        c = candles[j]
        hi, lo = float(c["high"]), float(c["low"])
        channel_px = l1.channel_line_at_bar(channel_line, c) if channel_line else None

        # fill legs in order at fixed TPs
        while leg_i < len(legs) and hi >= legs[leg_i][1]:
            w, tp = legs[leg_i]
            part_pnl = (tp - entry) / entry * 100.0
            gross += w * part_pnl
            remaining -= w
            filled.append(
                {
                    "leg_idx": leg_i,
                    "weight": w,
                    "tp_price": tp,
                    "exit_utc": l1.fmt_utc(int(c["time"])),
                    "exit_price": tp,
                    "exit_kind": "TP",
                    "leg_pnl_pct": round(part_pnl, 4),
                }
            )
            if breakeven_after_leg is not None and leg_i == breakeven_after_leg:
                current_sl = entry
            leg_i += 1

        hit_sl = lo <= current_sl and remaining > 1e-12
        hit_ch = channel_px is not None and hi >= channel_px and remaining > 1e-12
        if hit_sl and (hit_ch or leg_i < len(legs)):
            amb = True
        if hit_sl:
            px = current_sl
            part_pnl = (px - entry) / entry * 100.0
            gross += remaining * part_pnl
            filled.append(
                {
                    "leg_idx": leg_i,
                    "weight": remaining,
                    "tp_price": "",
                    "exit_utc": l1.fmt_utc(int(c["time"])),
                    "exit_price": px,
                    "exit_kind": "AMBIGUOUS" if amb else "SL",
                    "leg_pnl_pct": round(part_pnl, 4),
                }
            )
            remaining = 0.0
            last_idx = j
            final_kind = "AMBIGUOUS" if amb else "SL"
            break
        if hit_ch:
            px = channel_px
            part_pnl = (px - entry) / entry * 100.0
            gross += remaining * part_pnl
            filled.append(
                {
                    "leg_idx": leg_i,
                    "weight": remaining,
                    "tp_price": "",
                    "exit_utc": l1.fmt_utc(int(c["time"])),
                    "exit_price": px,
                    "exit_kind": "CHANNEL_UPPER",
                    "leg_pnl_pct": round(part_pnl, 4),
                }
            )
            remaining = 0.0
            last_idx = j
            final_kind = "CHANNEL_UPPER"
            break
        if leg_i >= len(legs) and remaining <= 1e-12:
            last_idx = j
            final_kind = "TP"
            break

    if remaining > 1e-12:
        last = candles[-1]
        px = float(last["close"])
        part_pnl = (px - entry) / entry * 100.0
        gross += remaining * part_pnl
        filled.append(
            {
                "leg_idx": leg_i,
                "weight": remaining,
                "exit_utc": l1.fmt_utc(int(last["time"])),
                "exit_price": px,
                "exit_kind": "OPEN_AT_WINDOW_END",
                "leg_pnl_pct": round(part_pnl, 4),
            }
        )
        last_idx = len(candles) - 1
        final_kind = "OPEN_AT_WINDOW_END"

    mfe, mae = _mfe_mae(candles, start_idx, last_idx, entry)
    exit_px = filled[-1]["exit_price"] if filled else None
    exit_ts = filled[-1]["exit_utc"] if filled else ""
    return SimResult(
        round(gross, 4),
        final_kind or "TP",
        exit_ts,
        float(exit_px) if exit_px is not None else None,
        last_idx,
        mfe,
        mae,
        amb,
        filled,
    )

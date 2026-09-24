"""Frozen-level partial-exit simulation on 1m OHLC. Price hits, SL_FIRST."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .candles import ensure_utc
from .config import FEE_PCT, hold_minutes_for_tf
from .schema import OUTCOME_OPEN, OUTCOME_SL, OUTCOME_TP1, OUTCOME_TP1_SL, OUTCOME_TP1_TP2


def first_outcome_open(entry_time: datetime) -> datetime:
    et = ensure_utc(entry_time)
    if et.second == 0 and et.microsecond == 0:
        return et
    floored = et.replace(second=0, microsecond=0)
    return floored + timedelta(minutes=1)


def _long_ret(entry: float, exit_px: float) -> float:
    return (exit_px - entry) / entry


def _short_ret(entry: float, exit_px: float) -> float:
    return (entry - exit_px) / entry


def _hit_long(high: float, low: float, level: float, *, is_tp: bool) -> bool:
    if is_tp:
        return high >= level
    return low <= level


def _hit_short(high: float, low: float, level: float, *, is_tp: bool) -> bool:
    if is_tp:
        return low <= level
    return high >= level


def simulate_partial_exits(
    *,
    direction: str,
    entry_time: datetime,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    tp1_size: float,
    tp2_price: float | None,
    tp2_size: float | None,
    candles_1m: pd.DataFrame,
    timeframe: str | None,
    fee_pct: float = FEE_PCT,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    side = str(direction).upper()
    ret = _long_ret if side == "LONG" else _short_ret
    hit = _hit_long if side == "LONG" else _hit_short
    start_open = first_outcome_open(entry_time)
    hold_min = hold_minutes_for_tf(timeframe)
    hold_end = start_open + timedelta(minutes=hold_min)
    if as_of is not None:
        hold_end = min(hold_end, ensure_utc(as_of))

    df = candles_1m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.loc[(df["timestamp"] >= pd.Timestamp(start_open)) & (df["timestamp"] < pd.Timestamp(hold_end))]
    df = df.sort_values("timestamp").reset_index(drop=True)

    remaining = 1.0
    legs: list[dict[str, Any]] = []
    sl_first = False
    two_targets = tp2_price is not None and tp2_size and float(tp2_size) > 0
    s1 = float(tp1_size)

    def close_leg(kind: str, px: float, ts, size: float, amb: bool) -> None:
        nonlocal remaining
        remaining = max(0.0, remaining - size)
        legs.append(
            {
                "kind": kind,
                "price": float(px),
                "time": pd.Timestamp(ts).tz_convert("UTC").isoformat().replace("+00:00", "Z"),
                "size": size,
                "gross_frac": ret(float(entry_price), float(px)) * 100.0 * size,
                "sl_first": amb,
            }
        )

    for _, row in df.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        ts = row["timestamp"]
        if remaining <= 1e-12:
            break
        tp_level = tp1_price if remaining > s1 - 1e-9 or not two_targets or abs(remaining - 1.0) < 1e-9 else tp2_price
        # remaining 1.0 → TP1; remaining 0.5 after TP1 → TP2 if two targets else nothing
        if abs(remaining - 1.0) < 1e-9:
            hit_tp = hit(high, low, float(tp1_price), is_tp=True)
            hit_sl = hit(high, low, float(sl_price), is_tp=False)
            if hit_tp and hit_sl:
                sl_first = True
                close_leg("SL", sl_price, ts, 1.0, True)
                break
            if hit_sl:
                close_leg("SL", sl_price, ts, 1.0, False)
                break
            if hit_tp:
                close_leg("TP1", tp1_price, ts, s1, False)
                if abs(s1 - 1.0) < 1e-9:
                    break
                continue
        else:
            hit_sl = hit(high, low, float(sl_price), is_tp=False)
            hit_tp2 = two_targets and hit(high, low, float(tp2_price), is_tp=True)
            if hit_tp2 and hit_sl:
                sl_first = True
                close_leg("SL", sl_price, ts, remaining, True)
                break
            if hit_sl:
                close_leg("SL", sl_price, ts, remaining, False)
                break
            if hit_tp2:
                close_leg("TP2", float(tp2_price), ts, remaining, False)
                break

    kinds = [leg["kind"] for leg in legs]
    if not kinds:
        outcome = OUTCOME_OPEN
    elif kinds == ["SL"] or (kinds[0] == "SL" and abs(legs[0]["size"] - 1.0) < 1e-9):
        outcome = OUTCOME_SL
    elif kinds == ["TP1"] and abs(s1 - 1.0) < 1e-9:
        outcome = OUTCOME_TP1
    elif kinds == ["TP1", "TP2"]:
        outcome = OUTCOME_TP1_TP2
    elif kinds == ["TP1", "SL"]:
        outcome = OUTCOME_TP1_SL
    elif kinds == ["TP1"]:
        outcome = OUTCOME_OPEN  # remainder still open
    else:
        outcome = kinds[-1]

    closed_size = sum(leg["size"] for leg in legs)
    gross = sum(leg["gross_frac"] for leg in legs)
    fee_entry = fee_pct / 2.0
    fee_exits = sum((fee_pct / 2.0) * leg["size"] for leg in legs)
    fees = fee_entry + fee_exits if legs else None
    net = (gross - fees) if fees is not None and outcome != OUTCOME_OPEN else None
    if outcome == OUTCOME_OPEN:
        net = None
        if not legs:
            fees = None
            gross = None

    last_time = legs[-1]["time"] if legs else None
    return {
        "outcome": outcome,
        "legs": legs,
        "gross_pnl_pct": None if gross is None else float(gross),
        "fees_pct": None if fees is None else float(fees),
        "net_pnl_pct": None if net is None else float(net),
        "sl_first": sl_first,
        "exit_time": last_time,
        "closed_size": closed_size,
        "remaining_size": remaining if remaining > 1e-12 else 0.0,
        "outcome_start_open": start_open.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hold_minutes": hold_min,
        "outcome_as_of": None if as_of is None else ensure_utc(as_of).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

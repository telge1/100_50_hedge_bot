"""Frozen Short-only RR 1:2 + Break-even Lock semantics (research-only).

Primary lock mode: conservative_next_bar_lock
- Lock trigger observed on bar T activates stop replacement from bar T+1.
- Same-bar trigger + original SL → original SL (no retroactive rescue).
- Same-bar active BE-stop + TP → BE-stop (adverse/conservative first).
- Matches existing TP/SL same-bar rule: unknown intrabar order → SL/stop first.

No A6 / Pine / signal changes. No trailing beyond BE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    COST_ROUNDTRIP_PCT,
    fav_adv_from_bar,
    path_arrays,
    signed_return_pct,
)

HORIZON_BARS = 192
COST_PCT = COST_ROUNDTRIP_PCT  # 0.20 roundtrip flat (frozen holdout model)
SLIPPAGE_PCT = 0.0  # not separately modeled in holdout; documented as 0

TICK_SIZE: dict[str, float] = {
    "BTCUSDT": 0.1,
    "ETHUSDT": 0.01,
    "BNBUSDT": 0.01,
}

CONSERVATIVE_LOCK_MODE = "conservative_next_bar_lock"

# ---------------------------------------------------------------------------
# Frozen profile registry (exactly these; no extras)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitProfile:
    name: str
    tp_pct: float
    sl_pct: float  # negative
    lock_enabled: bool
    lock_threshold: float | None  # 0.60 / 0.70 / 0.80 or None


PROFILES: tuple[ExitProfile, ...] = (
    ExitProfile("reference_tp3_sl2", 3.0, -2.0, False, None),
    ExitProfile("rr2_tp3_sl1_5_no_lock", 3.0, -1.5, False, None),
    ExitProfile("rr2_tp3_sl1_5_lock60_be", 3.0, -1.5, True, 0.60),
    ExitProfile("rr2_tp3_sl1_5_lock70_be", 3.0, -1.5, True, 0.70),
    ExitProfile("rr2_tp3_sl1_5_lock80_be", 3.0, -1.5, True, 0.80),
    ExitProfile("rr2_tp2_5_sl1_25_no_lock", 2.5, -1.25, False, None),
    ExitProfile("rr2_tp2_5_sl1_25_lock60_be", 2.5, -1.25, True, 0.60),
    ExitProfile("rr2_tp2_5_sl1_25_lock70_be", 2.5, -1.25, True, 0.70),
    ExitProfile("rr2_tp2_5_sl1_25_lock80_be", 2.5, -1.25, True, 0.80),
    ExitProfile("rr2_tp2_sl1_no_lock", 2.0, -1.0, False, None),
    ExitProfile("rr2_tp2_sl1_lock60_be", 2.0, -1.0, True, 0.60),
    ExitProfile("rr2_tp2_sl1_lock70_be", 2.0, -1.0, True, 0.70),
    ExitProfile("rr2_tp2_sl1_lock80_be", 2.0, -1.0, True, 0.80),
)

PROFILE_BY_NAME: dict[str, ExitProfile] = {p.name: p for p in PROFILES}


def trade_key(row: Mapping[str, Any]) -> str:
    return f"{row['symbol']}|{row['side']}|{int(row['fill_bar'])}|{row['fill_timestamp']}|{row.get('setup_id', '')}"


def tick_size_for(symbol: str, entry_price: float) -> float:
    if symbol in TICK_SIZE:
        return TICK_SIZE[symbol]
    # fallback heuristic
    if entry_price >= 1000:
        return 0.1
    if entry_price >= 10:
        return 0.01
    return 0.001


def round_short_stop_conservative(price: float, tick: float) -> float:
    """Short stop is above favorable zone; round UP so stop is hit earlier (worse)."""
    if tick <= 0:
        return float(price)
    return math.ceil(price / tick - 1e-15) * tick


def round_short_tp_conservative(price: float, tick: float) -> float:
    """Short TP below entry; round DOWN so TP is slightly harder (worse)."""
    if tick <= 0:
        return float(price)
    return math.floor(price / tick + 1e-15) * tick


def short_tp_price(entry: float, tp_pct: float) -> float:
    # Matches multicoin exit_price_for_result: entry / (1 + tp/100)
    return float(entry) / (1.0 + float(tp_pct) / 100.0)


def short_sl_price(entry: float, sl_pct: float) -> float:
    # sl_pct negative, e.g. -1.5 → factor 0.985 → entry/0.985
    return float(entry) / (1.0 + float(sl_pct) / 100.0)


def short_lock_trigger_price(entry: float, tp_price: float, threshold: float) -> float:
    """Price where progress == threshold (short favorable = lower price)."""
    return float(entry) - float(threshold) * (float(entry) - float(tp_price))


def short_progress(entry: float, favorable_price: float, tp_price: float) -> float:
    denom = float(entry) - float(tp_price)
    if abs(denom) < 1e-15:
        return 0.0
    return (float(entry) - float(favorable_price)) / denom


def net_break_even_stop_short(
    entry: float,
    *,
    cost_pct: float = COST_PCT,
    slippage_pct: float = SLIPPAGE_PCT,
    symbol: str = "BTCUSDT",
) -> tuple[float, float]:
    """Cost-adjusted net BE stop for short.

    Frozen holdout model: single roundtrip deduction ``cost_pct`` (0.20) and
    no separate per-side fee/slippage. Net BE requires gross signed return
    equal to cost_pct + slippage_pct so net ≈ 0.

    Short signed_return_pct = entry/px - 1:
        entry/px - 1 = buffer/100
        px = entry / (1 + buffer/100)

    Tick rounding is conservative (ceil) for the short stop.
    Returns (be_stop_price, buffer_pct).
    """
    buffer = float(cost_pct) + float(slippage_pct)
    raw = float(entry) / (1.0 + buffer / 100.0)
    tick = tick_size_for(symbol, entry)
    return round_short_stop_conservative(raw, tick), buffer


def simulate_short_exit(
    *,
    profile: ExitProfile,
    symbol: str,
    entry: float,
    fill_i: int,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: Any,
    n_bars: int,
    horizon_bars: int = HORIZON_BARS,
    cost_pct: float = COST_PCT,
    slippage_pct: float = SLIPPAGE_PCT,
    lock_mode: str = CONSERVATIVE_LOCK_MODE,
) -> dict[str, Any]:
    """Simulate one short trade exit under profile. Side must be short."""
    if lock_mode != CONSERVATIVE_LOCK_MODE:
        raise ValueError(f"unsupported lock mode: {lock_mode}")

    side = -1
    tp = float(profile.tp_pct)
    sl = float(profile.sl_pct)
    tick = tick_size_for(symbol, entry)
    tp_px = round_short_tp_conservative(short_tp_price(entry, tp), tick)
    sl_px = round_short_stop_conservative(short_sl_price(entry, sl), tick)
    be_px, be_buffer = net_break_even_stop_short(
        entry, cost_pct=cost_pct, slippage_pct=slippage_pct, symbol=symbol
    )
    thr = profile.lock_threshold
    trig_px = (
        None
        if thr is None
        else round_short_tp_conservative(short_lock_trigger_price(entry, short_tp_price(entry, tp), thr), tick)
    )

    end_data = n_bars - 1
    end_h = min(end_data, fill_i + int(horizon_bars) - 1)
    truncated = end_h < fill_i + int(horizon_bars) - 1

    lock_activated = False
    lock_active_from: int | None = None
    lock_trigger_bar: int | None = None
    lock_trigger_time = None

    exit_bar: int | None = None
    exit_reason: str | None = None
    exit_px: float | None = None
    same_bar_ambiguous = False

    for bar in range(fill_i, end_h + 1):
        hi = float(highs[bar])
        lo = float(lows[bar])
        cl = float(closes[bar])
        fav, adv = fav_adv_from_bar(side, entry, hi, lo)

        active = lock_activated and lock_active_from is not None and bar >= lock_active_from

        if not active:
            hit_sl = adv <= sl + 1e-15 or hi >= sl_px - 1e-12
            hit_tp = fav >= tp - 1e-15 or lo <= tp_px + 1e-12
            hit_trig = False
            if profile.lock_enabled and thr is not None and trig_px is not None:
                prog = short_progress(entry, lo, short_tp_price(entry, tp))
                hit_trig = prog >= thr - 1e-15 or lo <= trig_px + 1e-12

            if hit_sl and (hit_tp or hit_trig):
                # unknown order: conservative SL; no retroactive lock rescue
                exit_bar, exit_reason, exit_px = bar, ("same_bar_conservative_sl" if hit_tp else "SL"), sl_px
                same_bar_ambiguous = bool(hit_tp)
                break
            if hit_sl:
                exit_bar, exit_reason, exit_px = bar, "SL", sl_px
                break
            if hit_tp:
                exit_bar, exit_reason, exit_px = bar, "TP", tp_px
                break
            if hit_trig:
                lock_activated = True
                lock_trigger_bar = bar
                lock_trigger_time = timestamps[bar]
                lock_active_from = bar + 1  # next-bar activation
                continue
            continue

        # lock active: BE stop replaces original SL
        hit_be = hi >= be_px - 1e-12
        hit_tp = fav >= tp - 1e-15 or lo <= tp_px + 1e-12
        if hit_be and hit_tp:
            exit_bar, exit_reason, exit_px = bar, "lock_be", be_px
            same_bar_ambiguous = True
            break
        if hit_be:
            exit_bar, exit_reason, exit_px = bar, "lock_be", be_px
            break
        if hit_tp:
            exit_bar, exit_reason, exit_px = bar, "TP", tp_px
            break

    if exit_bar is None:
        exit_bar = end_h
        exit_px = float(closes[exit_bar])
        exit_reason = "data_end" if truncated else "time_exit"

    # Gross PnL: level exits use level %; BE uses signed return at stop; time/data use close
    if exit_reason in {"TP"}:
        gross = tp
    elif exit_reason in {"SL", "same_bar_conservative_sl"}:
        gross = sl
    elif exit_reason == "lock_be":
        gross = signed_return_pct(side, entry, float(exit_px))
    else:
        gross = signed_return_pct(side, entry, float(exit_px))

    fees = float(cost_pct)
    slip = float(slippage_pct)
    net = gross - fees - slip
    bars_held = int(exit_bar - fill_i)
    path = path_arrays(side, entry, highs, lows, closes, fill_i, exit_bar)

    # Hypothetical TP after lock_be: did TP touch after exit?
    hyp_tp_after = False
    if exit_reason == "lock_be" and exit_bar < end_h:
        for b in range(exit_bar + 1, end_h + 1):
            fav_b, _ = fav_adv_from_bar(side, entry, float(highs[b]), float(lows[b]))
            if fav_b >= tp - 1e-15:
                hyp_tp_after = True
                break

    return {
        "tp_pct": tp,
        "sl_pct": sl,
        "tp_price": tp_px,
        "original_sl_price": sl_px,
        "lock_enabled": bool(profile.lock_enabled),
        "lock_threshold": thr,
        "lock_trigger_price": trig_px,
        "lock_trigger_time": lock_trigger_time,
        "lock_trigger_candle": lock_trigger_bar,
        "lock_activated": bool(lock_activated),
        "lock_active_from_bar": lock_active_from,
        "break_even_stop_price": be_px,
        "break_even_cost_buffer_pct": be_buffer,
        "lock_exit": exit_reason == "lock_be",
        "lock_exit_time": timestamps[exit_bar] if exit_reason == "lock_be" else None,
        "lock_exit_price": exit_px if exit_reason == "lock_be" else None,
        "final_exit_type": exit_reason,
        "exit_bar": int(exit_bar),
        "exit_timestamp": timestamps[exit_bar],
        "exit_price": float(exit_px),
        "same_bar_ambiguous": same_bar_ambiguous,
        "gross_pnl_pct": float(gross),
        "fees_pct": fees,
        "slippage_pct": slip,
        "final_pnl_pct": float(net),
        "holding_bars": bars_held,
        "mfe_pct": path.get("maximum_favorable_excursion_pct"),
        "mae_pct": path.get("maximum_adverse_excursion_pct"),
        "hypothetical_tp_after_lock_exit": hyp_tp_after,
        "lock_mode": lock_mode,
        "truncated": bool(truncated and exit_reason == "data_end"),
    }


def simulate_short_no_lock_reference_pair(
    *,
    tp_pct: float,
    sl_pct: float,
    symbol: str,
    entry: float,
    fill_i: int,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: Any,
    n_bars: int,
) -> dict[str, Any]:
    """Convenience: no-lock profile with given TP/SL."""
    p = ExitProfile(name="tmp", tp_pct=tp_pct, sl_pct=sl_pct, lock_enabled=False, lock_threshold=None)
    return simulate_short_exit(
        profile=p,
        symbol=symbol,
        entry=entry,
        fill_i=fill_i,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=timestamps,
        n_bars=n_bars,
    )

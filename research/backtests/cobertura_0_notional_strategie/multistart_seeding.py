"""Deterministic multi-start seeding for Cobertura APT validation.

Core-position normalization
---------------------------
Variants considered (not guessed at runtime):

1. **Absolute prices** from ``apt_example.json`` on every start  
   → Rejected: locked loss / notionals distort as spot moves.

2. **Ratio avgs, fixed qty**  
   → Avgs scale with start price; locked USDT loss scales with price  
   → Rejected: economic severity not comparable across starts.

3. **Ratio avgs + qty scaled to hold USDT notionals / locked loss** (chosen)  
   ``long_mult = ref_long_avg / ref_start_price``  
   ``short_mult = ref_short_avg / ref_start_price``  
   ``qty = round(ref_qty * ref_start_price / start_price)``  
   ``long_avg = long_mult * start_price``  
   ``short_avg = short_mult * start_price``  

   Then long notional, short notional, and locked loss USDT match the
   reference (up to qty_step rounding). Matches „gleiche wirtschaftliche
   Ausgangslage“.

The audited reference timestamp keeps the exact absolute seed from
``default_apt_example()`` so the fingerprint is preserved when that start
is executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .config import CoberturaConfig, default_apt_example
from .engine import _parse_ts
from .ledger import round_price, round_qty

REFERENCE_START_TS = "2026-01-19T03:55:00+00:00"
BARS_PER_DAY = 288  # 5m
BARS_PER_HOUR = 12


@dataclass(frozen=True)
class ReferenceSeedGeometry:
    ref_start_price: float
    ref_long_avg: float
    ref_short_avg: float
    ref_qty: float
    long_mult: float
    short_mult: float
    locked_loss_usdt: float
    locked_spread_pct: float
    long_notional_usdt: float
    short_notional_usdt: float


def reference_geometry(cfg: CoberturaConfig | None = None) -> ReferenceSeedGeometry:
    cfg = cfg or default_apt_example()
    sp = float(cfg.start_price)
    la = float(cfg.core_long_avg)
    sa = float(cfg.core_short_avg)
    qty = float(cfg.core_long_qty)
    locked = qty * (la - sa)
    return ReferenceSeedGeometry(
        ref_start_price=sp,
        ref_long_avg=la,
        ref_short_avg=sa,
        ref_qty=qty,
        long_mult=la / sp,
        short_mult=sa / sp,
        locked_loss_usdt=locked,
        locked_spread_pct=(la - sa) / sa if sa > 0 else 0.0,
        long_notional_usdt=qty * la,
        short_notional_usdt=qty * sa,
    )


@dataclass(frozen=True)
class StartSeed:
    start_index: int
    start_timestamp: str
    start_price: float
    core_long_qty: float
    core_short_qty: float
    core_long_avg: float
    core_short_avg: float
    initial_locked_spread_pct: float
    initial_locked_loss_usdt: float
    is_reference_start: bool
    seeding_mode: str


def build_relative_core_seed(
    *,
    start_price: float,
    qty_step: float,
    tick_size: float,
    geom: ReferenceSeedGeometry | None = None,
) -> dict[str, float]:
    """Notional-invariant relative core seed (variant 3)."""
    geom = geom or reference_geometry()
    sp = float(start_price)
    if sp <= 0:
        raise ValueError("start_price must be > 0")
    qty = round_qty(geom.ref_qty * geom.ref_start_price / sp, qty_step)
    if qty <= 0:
        raise ValueError("scaled qty rounds to zero")
    # Keep full-precision averages so USDT locked loss / notionals stay comparable;
    # tick rounding would systematically bias locked loss.
    long_avg = geom.long_mult * sp
    short_avg = geom.short_mult * sp
    if long_avg <= short_avg:
        raise ValueError("long_avg must exceed short_avg after scaling")
    locked = qty * (long_avg - short_avg)
    locked_pct = (long_avg - short_avg) / short_avg
    return {
        "start_price": sp,
        "core_long_qty": qty,
        "core_short_qty": qty,
        "core_long_avg": long_avg,
        "core_short_avg": short_avg,
        "initial_locked_spread_pct": locked_pct,
        "initial_locked_loss_usdt": locked,
    }


def build_reference_absolute_seed(cfg: CoberturaConfig | None = None) -> dict[str, float]:
    cfg = cfg or default_apt_example()
    qty = float(cfg.core_long_qty)
    la = float(cfg.core_long_avg)
    sa = float(cfg.core_short_avg)
    return {
        "start_price": float(cfg.start_price),
        "core_long_qty": qty,
        "core_short_qty": float(cfg.core_short_qty),
        "core_long_avg": la,
        "core_short_avg": sa,
        "initial_locked_spread_pct": (la - sa) / sa,
        "initial_locked_loss_usdt": qty * (la - sa),
    }


def select_start_indices(
    candles: Sequence[dict[str, Any]],
    *,
    spacing_hours: int = 24,
    min_forward_days: int = 30,
    max_starts: int | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    reference_ts: str = REFERENCE_START_TS,
) -> list[int]:
    """Deterministic start indices; always includes the audited reference if eligible."""
    if spacing_hours < 1:
        raise ValueError("spacing_hours must be >= 1")
    if min_forward_days < 1:
        raise ValueError("min_forward_days must be >= 1")
    n = len(candles)
    if n < 2:
        return []
    step = int(spacing_hours) * BARS_PER_HOUR
    min_fwd = int(min_forward_days) * BARS_PER_DAY
    t_from = _parse_ts(start_from) if start_from else None
    t_to = _parse_ts(start_to) if start_to else None

    indices: list[int] = []
    i = 0
    while i + min_fwd < n:
        ts = _parse_ts(candles[i]["timestamp"])
        if t_from is not None and ts < t_from:
            i += step
            continue
        if t_to is not None and ts > t_to:
            break
        indices.append(i)
        i += step
        if max_starts is not None and len(indices) >= int(max_starts):
            break

    # Force-include reference start when eligible.
    ref = _parse_ts(reference_ts)
    ref_i = None
    for j, row in enumerate(candles):
        if _parse_ts(row["timestamp"]) == ref:
            ref_i = j
            break
    if ref_i is not None and ref_i + min_fwd < n:
        if t_from is not None and ref < t_from:
            pass
        elif t_to is not None and ref > t_to:
            pass
        elif ref_i not in indices:
            indices.append(ref_i)
            indices.sort()
            if max_starts is not None and len(indices) > int(max_starts):
                # Keep reference; drop farthest non-ref from the end/start grid
                # Prefer dropping last grid starts first, never drop ref.
                trimmed = [x for x in indices if x != ref_i]
                need = int(max_starts) - 1
                trimmed = trimmed[:need]
                indices = sorted(trimmed + [ref_i])

    return indices


def materialize_start(
    candles: Sequence[dict[str, Any]],
    start_index: int,
    *,
    cfg_template: CoberturaConfig | None = None,
) -> StartSeed:
    cfg = cfg_template or default_apt_example()
    row = candles[start_index]
    ts = _parse_ts(row["timestamp"]).isoformat()
    ref_ts = _parse_ts(REFERENCE_START_TS)
    is_ref = _parse_ts(row["timestamp"]) == ref_ts
    if is_ref:
        seed = build_reference_absolute_seed(cfg)
        mode = "reference_absolute"
        # Audited start_price is config value, not necessarily candle open.
        start_price = float(seed["start_price"])
    else:
        start_price = float(row["open"])
        seed = build_relative_core_seed(
            start_price=start_price,
            qty_step=float(cfg.qty_step),
            tick_size=float(cfg.tick_size),
        )
        mode = "relative_notional_invariant"
    return StartSeed(
        start_index=start_index,
        start_timestamp=ts,
        start_price=float(seed["start_price"]),
        core_long_qty=float(seed["core_long_qty"]),
        core_short_qty=float(seed["core_short_qty"]),
        core_long_avg=float(seed["core_long_avg"]),
        core_short_avg=float(seed["core_short_avg"]),
        initial_locked_spread_pct=float(seed["initial_locked_spread_pct"]),
        initial_locked_loss_usdt=float(seed["initial_locked_loss_usdt"]),
        is_reference_start=is_ref,
        seeding_mode=mode,
    )


def horizon_end_index(
    candles: Sequence[dict[str, Any]],
    start_index: int,
    *,
    max_horizon_days: int | None,
) -> int:
    """Last inclusive candle index for this start."""
    last = len(candles) - 1
    if max_horizon_days is None:
        return last
    start_ts = _parse_ts(candles[start_index]["timestamp"])
    end_ts = start_ts + timedelta(days=int(max_horizon_days))
    end_i = start_index
    for i in range(start_index, len(candles)):
        if _parse_ts(candles[i]["timestamp"]) <= end_ts:
            end_i = i
        else:
            break
    return end_i


def classify_market_path(
    candles: Sequence[dict[str, Any]],
    start_index: int,
    end_index: int,
    *,
    start_price: float,
) -> str:
    """Outcome-only path bucket from start to window end (not used for decisions)."""
    if start_price <= 0 or end_index <= start_index:
        return "unknown"
    window = candles[start_index : end_index + 1]
    lows = [float(c["low"]) for c in window]
    highs = [float(c["high"]) for c in window]
    last_close = float(window[-1]["close"])
    min_ret = (min(lows) - start_price) / start_price
    max_ret = (max(highs) - start_price) / start_price
    end_ret = (last_close - start_price) / start_price
    # Prefer characterizing by worst adverse vs recovery using end move.
    if min_ret <= -0.10:
        return "drop_ge_10pct"
    if min_ret <= -0.05:
        return "drop_5_to_10pct"
    if abs(end_ret) <= 0.05 and max_ret < 0.10 and min_ret > -0.05:
        return "sideways_pm_5pct"
    if end_ret >= 0.10 or max_ret >= 0.10:
        return "rally_ge_10pct"
    if end_ret >= 0.05 or max_ret >= 0.05:
        return "rally_5_to_10pct"
    return "sideways_pm_5pct"

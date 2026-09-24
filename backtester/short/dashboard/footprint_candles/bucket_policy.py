"""Per-symbol footprint bucket step policy (INJUSDT pilot → later roll-out).

Formula (price-relative, BTC-calibrated)
---------------------------------------
BTC reference lock: step=5 at ~price 77000
  relative = 5 / 77000 ≈ 6.4935e-5 of mid price

For any coin:
  raw = ref_price * (BTC_REF_STEP / BTC_REF_PRICE)
  step = nice_multiple(raw, tick)   # round UP to 1|2|5 × 10^k, ≥ tick

Optional range check (readability):
  range_step = nice_multiple(median_5m_range / TARGET_LEVELS, tick)
  final = max(price_step, range_step)   # coarser wins → fewer, clearer rows

INJUSDT pilot (2026-09-18 sample):
  tick=0.001, price≈6.2 → price_step≈0.00040 → nice → 0.001
  median 5m range≈0.021 → range_step≈0.00105 → nice → 0.001
  → locked pilot step = 0.001
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# --- BTC calibration anchor (existing MVP lock) ---
BTC_REF_PRICE = Decimal("77000")
BTC_REF_STEP = Decimal("5")
TARGET_LEVELS_PER_5M = 20

# Exchange ticks used by the pilot (trade-grid observed / known).
PILOT_TICKS: dict[str, Decimal] = {
    "BTCUSDT": Decimal("0.1"),
    "INJUSDT": Decimal("0.001"),
}

# Explicit locked steps for supported pilots (deterministic API contract).
# INJUSDT matches formula output; BTCUSDT keeps the historical MVP value.
PILOT_STEPS: dict[str, Decimal] = {
    "BTCUSDT": Decimal("5"),
    "INJUSDT": Decimal("0.001"),
}

SUPPORTED_SYMBOLS: frozenset[str] = frozenset(PILOT_STEPS.keys())

# Back-compat alias used across older imports.
SUPPORTED_SYMBOL = "BTCUSDT"
BUCKET_STEP = PILOT_STEPS["BTCUSDT"]


def _dec(value: Decimal | float | str | int) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def nice_multiple(raw: Decimal | float | str, tick: Decimal | float | str) -> Decimal:
    """Round *up* to the next 1|2|5 × 10^k grid, at least one tick."""
    r = _dec(raw)
    t = _dec(tick)
    if t <= 0:
        raise ValueError("tick must be > 0")
    if r <= 0:
        return t
    # Work in tick units.
    units = r / t
    # Find magnitude in tick-units.
    # units → ceil to 1|2|5 × 10^exp
    from math import floor, log10

    u = float(units)
    if u <= 1:
        return t
    exp = floor(log10(u))
    base = Decimal(10) ** exp
    for mult in (Decimal(1), Decimal(2), Decimal(5), Decimal(10)):
        candidate_units = mult * base
        if candidate_units >= units:
            return (candidate_units * t).quantize(t)
    return ((Decimal(10) * base) * t).quantize(t)


def bucket_step_from_price(
    price: Decimal | float | str,
    tick: Decimal | float | str,
    *,
    ref_price: Decimal = BTC_REF_PRICE,
    ref_step: Decimal = BTC_REF_STEP,
) -> Decimal:
    """Price-relative step: raw = price * (BTC_REF_STEP / BTC_REF_PRICE)."""
    p = _dec(price)
    raw = p * (ref_step / ref_price)
    return nice_multiple(raw, tick)


def bucket_step_from_range(
    median_5m_range: Decimal | float | str,
    tick: Decimal | float | str,
    *,
    target_levels: int = TARGET_LEVELS_PER_5M,
) -> Decimal:
    """Range-relative step aiming for ~target_levels rows inside a typical 5m candle."""
    if target_levels <= 0:
        raise ValueError("target_levels must be > 0")
    raw = _dec(median_5m_range) / Decimal(target_levels)
    return nice_multiple(raw, tick)


def derive_bucket_step(
    *,
    price: Decimal | float | str | None = None,
    median_5m_range: Decimal | float | str | None = None,
    tick: Decimal | float | str,
) -> Decimal:
    """Combine price + optional range (max = coarser / clearer)."""
    steps: list[Decimal] = []
    if price is not None:
        steps.append(bucket_step_from_price(price, tick))
    if median_5m_range is not None:
        steps.append(bucket_step_from_range(median_5m_range, tick))
    if not steps:
        return nice_multiple(_dec(tick), tick)
    return max(steps)


def resolve_bucket_step(symbol: str) -> Decimal:
    """Deterministic step for API/UI. Pilot symbols only for now."""
    sym = str(symbol or "").strip().upper()
    if sym not in PILOT_STEPS:
        raise KeyError(f"no pilot bucket step for {sym}")
    return PILOT_STEPS[sym]


def is_supported_symbol(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in SUPPORTED_SYMBOLS


def validate_bucket_step(symbol: str, bucket_step: float | Decimal | str) -> Decimal:
    """Ensure caller step matches the locked pilot step for the symbol."""
    want = resolve_bucket_step(symbol)
    got = _dec(bucket_step)
    if abs(got - want) > Decimal("1e-12"):
        raise ValueError(f"bucket_step must be {want} for {symbol.upper()}")
    return want


def pilot_meta() -> dict:
    return {
        "symbols": sorted(SUPPORTED_SYMBOLS),
        "steps": {k: float(v) for k, v in sorted(PILOT_STEPS.items())},
        "ticks": {k: float(v) for k, v in sorted(PILOT_TICKS.items())},
        "formula": {
            "ref_price": float(BTC_REF_PRICE),
            "ref_step": float(BTC_REF_STEP),
            "target_levels_per_5m": TARGET_LEVELS_PER_5M,
            "rule": "nice_multiple(price * ref_step/ref_price, tick); "
            "optional max with nice_multiple(median_5m_range/target_levels, tick)",
        },
    }


def display_steps_for_raw(raw_step: Decimal | float | str) -> list[float]:
    """UI display aggregation ladder: 1×,2×,3×,4×,5×,10× raw (BTC: 5→50)."""
    raw = _dec(raw_step)
    return [float(raw * m) for m in (1, 2, 3, 4, 5, 10)]

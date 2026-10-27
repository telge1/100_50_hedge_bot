"""Research-only EMA permission variants (BT-D65 frozen classifier unchanged)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
D65 = ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1"
if str(D65) not in sys.path:
    sys.path.insert(0, str(D65))

from classification_v1 import classify_pure_ema  # noqa: E402

EmaDecision = Literal["SHORT_OK", "SHORT_NO", "UNCERTAIN"]

# Re-export helpers from frozen module via duplicate import pattern
from classification_v1 import (  # noqa: E402
    _falling_slow,
    _flat_slow,
    _rising_slow,
    _slope_sign,
)


def _vetoes_before_short_ok(row: dict[str, Any]) -> tuple[EmaDecision, str] | None:
    if not row.get("ema_ready"):
        return "UNCERTAIN", "EMA_WARMUP_INCOMPLETE"
    if row.get("ema_bull_stack_9_20_59_200"):
        return "SHORT_NO", "BULL_EMA_STACK"
    if _rising_slow(row):
        return "SHORT_NO", "EMA59_TURNING_UP"
    if row.get("ema_tight_cluster") and not row.get("ema_bear_stack_9_20_59_200"):
        return "SHORT_NO", "EMA_COMPRESSION_SIDEWAYS"
    return None


def _uncertain_tail(row: dict[str, Any], bear: bool, below: bool, falling: bool) -> tuple[EmaDecision, str]:
    if _flat_slow(row) or (
        _slope_sign(row.get("ema59_slope_12b_pct")) == 0
        and _slope_sign(row.get("ema200_slope_12b_pct")) in (0, None)
    ):
        return "SHORT_NO", "FLAT_EMA59_200"
    s6 = _slope_sign(row.get("ema59_slope_6b_pct"))
    s12 = _slope_sign(row.get("ema59_slope_12b_pct"))
    if s6 is not None and s12 is not None and s6 != s12:
        return "UNCERTAIN", "MIXED_EMA59_SLOPE_SIGNS"
    if bear and not falling:
        return "UNCERTAIN", "BEAR_STACK_BUT_SLOW_FLAT"
    if not below and falling:
        return "UNCERTAIN", "FALLING_EMAS_PRICE_RECLAIM"
    return "UNCERTAIN", "NO_CLEAR_EMA_TREND"


def classify_variant(row: dict[str, Any], variant_id: str) -> tuple[EmaDecision, str]:
    if variant_id == "v1_bt_d65_frozen":
        d, r, _ = classify_pure_ema(row)
        return d, r

    veto = _vetoes_before_short_ok(row)
    if veto:
        return veto

    bear = bool(row.get("ema_bear_stack_9_20_59_200"))
    below = bool(row.get("close_below_ema20") and row.get("close_below_ema59"))
    below9 = bool(row.get("close_below_ema9"))
    falling = _falling_slow(row)
    e200_down = _slope_sign(row.get("ema200_slope_12b_pct")) == -1

    if variant_id == "v2_bear_stack_only":
        if bear and below:
            return "SHORT_OK", "RESEARCH_BEAR_STACK_ONLY"
        return _uncertain_tail(row, bear, below, falling)

    if variant_id == "v3_bear_stack_falling_59_200":
        if bear and below and falling and e200_down:
            return "SHORT_OK", "RESEARCH_BEAR_STACK_FALLING_59_200"
        return _uncertain_tail(row, bear, below, falling)

    if variant_id == "v4_bear_stack_falling_plus_below_ema9":
        if bear and below and below9 and falling and e200_down:
            return "SHORT_OK", "RESEARCH_STRICT_BELOW_EMA9"
        return _uncertain_tail(row, bear, below, falling)

    raise ValueError(variant_id)


VARIANTS = [
    ("v1_bt_d65_frozen", "Frozen BT-D65 (includes PRICE_BELOW_FALLING_EMA59_200 fallback)"),
    ("v2_bear_stack_only", "SHORT_OK only with full bear stack + close below EMA20/59"),
    ("v3_bear_stack_falling_59_200", "SHORT_OK only BEAR_STACK_FALLING_59_200 path (no fallback)"),
    ("v4_bear_stack_falling_plus_below_ema9", "V3 plus close below EMA9"),
]

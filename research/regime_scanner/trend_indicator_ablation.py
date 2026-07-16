"""Phase C3.2B-D indicator-pattern ablation for regime research.

Architecture
------------
This module layers indicator-pattern logic on top of the existing C3.1 regime
replay. The underlying structure classifier still comes from
``trend_regime_classifier``:

* ``step_regime_classifier`` determines the C3.1 candidate state.
* ``build_bar_features`` and ``compute_range_score`` provide the causal price /
  structure backbone used by the replay.
* 30m indicator features are aligned onto 5m decision bars and evaluated by
  this module's EMA / ADX / DI pattern scores.

The ablation variants are intentionally research-only:

* ``C3.2B_baseline`` - passthrough of the C3.1 candidate state.
* ``C3.2C_ema`` - EMA-pattern gate only.
* ``C3.2D_ema_adx_di`` - EMA gate plus DI / ADX confirmation.

No production module is modified here. The module is safe to import from tests
and to run as a standalone research audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    INDICATOR_FEATURE_VERSION,
    load_or_build_indicator_features,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_audit_shared_replay import (
    PreparedBar,
    build_shared_structure_timeline,
    load_or_build_shared_context,
)
from research.regime_scanner.trend_pine_export import validate_pine_script
from research.regime_scanner.trend_regime_classifier import (
    RegimeClassifierConfig,
    RegimeRuntime,
    build_bar_features,
    compute_range_score,
    config_c3,
    precompute_regime_arrays,
    replay_regime_variant,
    step_regime_classifier,
)
from research.regime_scanner.trend_robustness_audit import (
    ANALYZE_END,
    ANALYZE_START,
    LOAD_END,
    LOAD_START,
    load_analysis_frame,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

VARIANT_BASELINE = "C3.2B_baseline"
VARIANT_EMA = "C3.2C_ema"
VARIANT_EMA_ADX_DI = "C3.2D_ema_adx_di"
ALL_VARIANTS: tuple[str, ...] = (
    VARIANT_BASELINE,
    VARIANT_EMA,
    VARIANT_EMA_ADX_DI,
)


@dataclass(frozen=True)
class IndicatorPatternConfig:
    ema_fast_compression_max: float = 0.45
    ema_fast_expansion_min: float = 0.35
    ema_fast_cross_count_range_min: float = 2.0
    ema_fast_slope_flat_max: float = 0.12
    ema_59_slope_confirm_min: float = 0.04
    ema_200_context_tolerance_atr: float = 1.5
    ema_range_score_min: float = 0.55
    ema_breakout_score_min: float = 0.45
    ema_trend_score_min: float = 0.50
    ema_ordered_bonus: float = 0.15
    breakout_buffer_atr: float = 0.15
    breakout_acceptance_bars: int = 2
    breakout_max_confirmation_bars: int = 12
    breakout_ema_confirmation_min: float = 0.40
    breakout_structure_confirmation_required: bool = True
    breakout_quick_reentry_bars: int = 4
    di_spread_confirm_min: float = 5.0
    di_confirmation_bars: int = 2
    adx_weak_max: float = 15.0
    adx_strong_min: float = 25.0
    adx_rising_min: float = 0.5
    adx_confirmation_grace_bars: int = 6
    adx_component_weight: float = 0.12
    di_component_weight: float = 0.18
    pullback_ema20_distance_atr: float = 0.35
    pullback_ema59_distance_atr: float = 0.80
    pullback_max_structure_depth: float = 2.5
    reacceleration_expansion_min: float = 0.30
    trend_follow_confirmation_bars: int = 2
    trend_follow_score_min: float = 0.45
    regime_min_hold_bars: int = 3
    transition_max_bars: int = 16
    gate_range_hold_score: float = 0.60
    gate_confirm_min_baseline_passthrough: bool = True
    variant_id: str = VARIANT_BASELINE
    mode: Literal["baseline", "ema", "ema_adx_di"] = "baseline"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    return _ts(value).isoformat()


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _finite(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _truthy(value: object | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, np.integer, np.floating)):
        if math.isnan(float(value)):
            return default
        return float(value) != 0.0
    text = str(value).strip().lower()
    if text in {"", "none", "nan"}:
        return default
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _lookup(container: object, key: str, default: object = None) -> object:
    if container is None:
        return default
    if isinstance(container, Mapping):
        return container.get(key, default)
    if isinstance(container, pd.Series):
        return container.get(key, default)
    return getattr(container, key, default)


def _as_row_dict(item: object) -> dict[str, Any]:
    if item is None:
        return {}
    if isinstance(item, dict):
        return dict(item)
    if isinstance(item, pd.Series):
        return item.to_dict()
    return dict(getattr(item, "__dict__", {}))


def _mode_from_variant(variant_id: str) -> tuple[str, str]:
    key = str(variant_id).strip()
    if key == VARIANT_BASELINE:
        return VARIANT_BASELINE, "baseline"
    if key == VARIANT_EMA:
        return VARIANT_EMA, "ema"
    if key == VARIANT_EMA_ADX_DI:
        return VARIANT_EMA_ADX_DI, "ema_adx_di"
    lowered = key.lower()
    if lowered in {"baseline", "c3.2b_baseline"}:
        return VARIANT_BASELINE, "baseline"
    if lowered in {"ema", "c3.2c_ema"}:
        return VARIANT_EMA, "ema"
    if lowered in {"ema_adx_di", "c3.2d_ema_adx_di"}:
        return VARIANT_EMA_ADX_DI, "ema_adx_di"
    raise ValueError(f"unknown indicator pattern variant: {variant_id!r}")


def config_for_variant(variant_id: str, **overrides: Any) -> IndicatorPatternConfig:
    variant_id, mode = _mode_from_variant(variant_id)
    payload = dict(overrides)
    payload.setdefault("variant_id", variant_id)
    payload.setdefault("mode", mode)
    return IndicatorPatternConfig(**payload)


def _critical_ready(ind: object) -> bool:
    fields = (
        "ema_9",
        "ema_20",
        "ema_59",
        "ema_200",
        "atr_14",
        "adx_14",
        "plus_di_14",
        "minus_di_14",
    )
    explicit = _lookup(ind, "features_ready", None)
    if explicit is not None and not _truthy(explicit):
        return False
    for field in fields:
        val = _lookup(ind, field, None)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return False
        try:
            if pd.isna(val):
                return False
        except Exception:
            pass
    return True


def compute_ema_band_scores(ind: object, cfg: IndicatorPatternConfig) -> dict[str, Any]:
    ready = _critical_ready(ind)
    if not ready:
        return {
            "features_ready": False,
            "ema_state": "ema_unclear",
            "ema_range_score": 0.0,
            "ema_bullish_trend_score": 0.0,
            "ema_bearish_trend_score": 0.0,
            "ema_bullish_breakout_score": 0.0,
            "ema_bearish_breakout_score": 0.0,
            "ema_pullback_support_score": 0.0,
            "ema_bullish_ordered_score": 0.0,
            "ema_bearish_ordered_score": 0.0,
            "ema_expansion_score": 0.0,
            "ema_compression_score": 0.0,
        }

    ema9 = _finite(_lookup(ind, "ema_9"))
    ema20 = _finite(_lookup(ind, "ema_20"))
    ema59 = _finite(_lookup(ind, "ema_59"))
    ema200 = _finite(_lookup(ind, "ema_200"))
    atr = max(_finite(_lookup(ind, "atr_14"), 1.0), 1e-9)
    close = _finite(_lookup(ind, "close", ema9))

    spread_9_20 = _finite(_lookup(ind, "ema_9_20_spread_atr"), ema9 - ema20)
    spread_20_59 = _finite(_lookup(ind, "ema_20_59_spread_atr"), ema20 - ema59)
    spread_59_200 = _finite(_lookup(ind, "ema_59_200_spread_atr"), ema59 - ema200)
    abs_spread = abs(spread_9_20)
    cross_count = _finite(_lookup(ind, "ema_fast_cross_count_24"), 0.0)
    compression_raw = _finite(_lookup(ind, "ema_fast_compression_score"), 0.0)
    expansion_raw = _finite(_lookup(ind, "ema_fast_expansion_score"), 0.0)
    ordered_bull = _truthy(_lookup(ind, "ema_bullish_ordered"), False)
    ordered_bear = _truthy(_lookup(ind, "ema_bearish_ordered"), False)
    price_above_200 = _truthy(_lookup(ind, "price_above_ema_200"), close >= ema200)
    price_below_200 = _truthy(_lookup(ind, "price_below_ema_200"), close <= ema200)
    dist_200_atr = abs(_finite(_lookup(ind, "close_to_ema_200_atr"), (close - ema200) / atr))
    slope9 = _finite(_lookup(ind, "ema_9_slope_3_atr"), 0.0)
    slope20 = _finite(_lookup(ind, "ema_20_slope_3_atr"), 0.0)
    slope59 = _finite(_lookup(ind, "ema_59_slope_3_atr"), 0.0)
    slope200 = _finite(_lookup(ind, "ema_200_slope_3_atr"), 0.0)

    slope_flat = cfg.ema_fast_slope_flat_max
    slope_confirm = cfg.ema_59_slope_confirm_min
    cross_score = _clip01(cross_count / max(cfg.ema_fast_cross_count_range_min, 1.0))
    flat_score = _clip01(1.0 - (abs(slope9) + abs(slope20)) / max(2.0 * slope_flat, 1e-6))
    slope59_score = _clip01(abs(slope59) / max(slope_confirm, 1e-6))
    context_score = _clip01(1.0 - dist_200_atr / max(cfg.ema_200_context_tolerance_atr, 1e-6))

    compression_score = _clip01(
        0.45 * _clip01(compression_raw / max(cfg.ema_fast_compression_max, 1e-6))
        + 0.30 * flat_score
        + 0.25 * cross_score
    )
    expansion_score = _clip01(
        0.50 * _clip01(expansion_raw / max(cfg.ema_fast_expansion_min, 1e-6))
        + 0.25 * _clip01((abs(slope9) + abs(slope20)) / max(2.0 * slope_flat, 1e-6))
        + 0.25 * _clip01((cfg.ema_fast_cross_count_range_min - cross_count) / max(cfg.ema_fast_cross_count_range_min, 1.0))
    )

    bullish_ordered_score = _clip01(
        (1.0 if ordered_bull else 0.0)
        + (cfg.ema_ordered_bonus if ordered_bull else 0.0)
        + 0.20 * _clip01(max(slope9, 0.0) / max(slope_flat, 1e-6))
        + 0.20 * _clip01(max(slope20, 0.0) / max(slope_flat, 1e-6))
        + 0.15 * _clip01(max(slope59, 0.0) / max(slope_confirm, 1e-6))
        + 0.15 * _clip01(max(0.0, 1.0 - dist_200_atr / max(cfg.ema_200_context_tolerance_atr, 1e-6)))
    )
    bearish_ordered_score = _clip01(
        (1.0 if ordered_bear else 0.0)
        + (cfg.ema_ordered_bonus if ordered_bear else 0.0)
        + 0.20 * _clip01(max(-slope9, 0.0) / max(slope_flat, 1e-6))
        + 0.20 * _clip01(max(-slope20, 0.0) / max(slope_flat, 1e-6))
        + 0.15 * _clip01(max(-slope59, 0.0) / max(slope_confirm, 1e-6))
        + 0.15 * _clip01(max(0.0, 1.0 - dist_200_atr / max(cfg.ema_200_context_tolerance_atr, 1e-6)))
    )

    bullish_trend_score = _clip01(
        0.24 * bullish_ordered_score
        + 0.18 * _clip01(max(slope9, 0.0) / max(slope_flat, 1e-6))
        + 0.18 * _clip01(max(slope20, 0.0) / max(slope_flat, 1e-6))
        + 0.14 * _clip01(max(slope59, 0.0) / max(slope_confirm, 1e-6))
        + 0.12 * _clip01(1.0 if price_above_200 else 0.0)
        + 0.14 * context_score
        + 0.10 * _clip01(1.0 - compression_score)
    )
    bearish_trend_score = _clip01(
        0.24 * bearish_ordered_score
        + 0.18 * _clip01(max(-slope9, 0.0) / max(slope_flat, 1e-6))
        + 0.18 * _clip01(max(-slope20, 0.0) / max(slope_flat, 1e-6))
        + 0.14 * _clip01(max(-slope59, 0.0) / max(slope_confirm, 1e-6))
        + 0.12 * _clip01(1.0 if price_below_200 else 0.0)
        + 0.14 * context_score
        + 0.10 * _clip01(1.0 - compression_score)
    )

    bullish_breakout_score = _clip01(
        0.35 * bullish_trend_score
        + 0.30 * expansion_score
        + 0.15 * _clip01(max(slope59, 0.0) / max(slope_confirm, 1e-6))
        + 0.10 * _clip01(1.0 if price_above_200 else 0.0)
        + 0.10 * _clip01(1.0 - compression_score)
    )
    bearish_breakout_score = _clip01(
        0.35 * bearish_trend_score
        + 0.30 * expansion_score
        + 0.15 * _clip01(max(-slope59, 0.0) / max(slope_confirm, 1e-6))
        + 0.10 * _clip01(1.0 if price_below_200 else 0.0)
        + 0.10 * _clip01(1.0 - compression_score)
    )

    pullback_support_score = _clip01(
        0.50
        * _clip01(
            1.0
            - abs(_finite(_lookup(ind, "close_to_ema_20_atr"), (close - ema20) / atr))
            / max(cfg.pullback_ema20_distance_atr, 1e-6)
        )
        + 0.30
        * _clip01(
            1.0
            - abs(_finite(_lookup(ind, "close_to_ema_59_atr"), (close - ema59) / atr))
            / max(cfg.pullback_ema59_distance_atr, 1e-6)
        )
        + 0.20 * context_score
    )

    range_score = _clip01(
        0.40 * compression_score
        + 0.20 * flat_score
        + 0.20 * cross_score
        + 0.20 * context_score
    )

    bull_dom = bullish_trend_score - bearish_trend_score
    if range_score >= cfg.ema_range_score_min and max(bullish_trend_score, bearish_trend_score) < cfg.ema_trend_score_min:
        ema_state = "ema_range_like"
    elif bullish_trend_score >= cfg.ema_trend_score_min and bull_dom > 0.06:
        if ordered_bull and bullish_trend_score >= cfg.ema_trend_score_min + 0.10:
            ema_state = "ema_bullish_ordered"
        elif expansion_score >= cfg.ema_fast_expansion_min or bullish_breakout_score >= cfg.ema_breakout_score_min:
            ema_state = "ema_bullish_expanding"
        else:
            ema_state = "ema_bullish_compressed"
    elif bearish_trend_score >= cfg.ema_trend_score_min and bull_dom < -0.06:
        if ordered_bear and bearish_trend_score >= cfg.ema_trend_score_min + 0.10:
            ema_state = "ema_bearish_ordered"
        elif expansion_score >= cfg.ema_fast_expansion_min or bearish_breakout_score >= cfg.ema_breakout_score_min:
            ema_state = "ema_bearish_expanding"
        else:
            ema_state = "ema_bearish_compressed"
    elif bullish_trend_score >= cfg.ema_trend_score_min and bearish_trend_score >= cfg.ema_trend_score_min:
        ema_state = "ema_mixed"
    elif range_score >= cfg.ema_range_score_min:
        ema_state = "ema_range_like"
    else:
        ema_state = "ema_unclear"

    return {
        "features_ready": True,
        "ema_state": ema_state,
        "ema_range_score": float(range_score),
        "ema_bullish_trend_score": float(bullish_trend_score),
        "ema_bearish_trend_score": float(bearish_trend_score),
        "ema_bullish_breakout_score": float(bullish_breakout_score),
        "ema_bearish_breakout_score": float(bearish_breakout_score),
        "ema_pullback_support_score": float(pullback_support_score),
        "ema_bullish_ordered_score": float(bullish_ordered_score),
        "ema_bearish_ordered_score": float(bearish_ordered_score),
        "ema_expansion_score": float(expansion_score),
        "ema_compression_score": float(compression_score),
        "ema_context_score": float(context_score),
        "ema_price_above_200": bool(price_above_200),
        "ema_price_below_200": bool(price_below_200),
        "ema_dist_200_atr": float(dist_200_atr),
        "ema_cross_count_24": float(cross_count),
        "ema_slope9_atr": float(slope9),
        "ema_slope20_atr": float(slope20),
        "ema_slope59_atr": float(slope59),
        "ema_slope200_atr": float(slope200),
        "ema_abs_spread_9_20_atr": float(abs_spread),
    }


def compute_adx_di_scores(ind: object, cfg: IndicatorPatternConfig) -> dict[str, Any]:
    ready = _critical_ready(ind)
    if not ready:
        return {
            "features_ready": False,
            "di_bullish_confirmation": 0.0,
            "di_bearish_confirmation": 0.0,
            "di_neutral": 0.0,
            "adx_weak": 0.0,
            "adx_strengthening": 0.0,
            "adx_strong": 0.0,
            "adx_falling": 0.0,
            "di_component_bull": 0.0,
            "di_component_bear": 0.0,
            "adx_component": 0.0,
            "dominant_di": "neutral",
        }

    plus_di = _finite(_lookup(ind, "plus_di_14"))
    minus_di = _finite(_lookup(ind, "minus_di_14"))
    adx = _finite(_lookup(ind, "adx_14"))
    adx_slope = _finite(_lookup(ind, "adx_slope_3"), _finite(_lookup(ind, "adx_slope_3_atr"), 0.0))
    spread = plus_di - minus_di
    abs_spread = abs(spread)

    di_confirm = max(cfg.di_spread_confirm_min, 1e-6)
    di_bull = _clip01((spread - di_confirm) / max(di_confirm * 2.0, 1e-6))
    di_bear = _clip01((-spread - di_confirm) / max(di_confirm * 2.0, 1e-6))
    di_neutral = _clip01(1.0 - abs_spread / max(di_confirm * 2.0, 1e-6))

    adx_weak = _clip01((cfg.adx_weak_max - adx) / max(cfg.adx_weak_max, 1e-6))
    adx_strong = _clip01((adx - cfg.adx_strong_min) / max(cfg.adx_strong_min, 1e-6))
    adx_strengthening = _clip01((adx_slope - cfg.adx_rising_min) / max(cfg.adx_rising_min, 1e-6))
    adx_falling = _clip01((-adx_slope - cfg.adx_rising_min) / max(cfg.adx_rising_min, 1e-6))
    adx_component = _clip01(
        0.45 * adx_strong + 0.30 * adx_strengthening + 0.15 * (1.0 - adx_weak) + 0.10 * (1.0 - adx_falling)
    )

    dominant_di = "neutral"
    if spread > cfg.di_spread_confirm_min:
        dominant_di = "plus"
    elif spread < -cfg.di_spread_confirm_min:
        dominant_di = "minus"

    return {
        "features_ready": True,
        "di_bullish_confirmation": float(di_bull),
        "di_bearish_confirmation": float(di_bear),
        "di_neutral": float(di_neutral),
        "adx_weak": float(adx_weak),
        "adx_strengthening": float(adx_strengthening),
        "adx_strong": float(adx_strong),
        "adx_falling": float(adx_falling),
        "di_component_bull": float(_clip01(di_bull * (1.0 - di_neutral))),
        "di_component_bear": float(_clip01(di_bear * (1.0 - di_neutral))),
        "adx_component": float(adx_component),
        "dominant_di": dominant_di,
        "di_spread": float(spread),
        "adx_14": float(adx),
        "adx_slope_3": float(adx_slope),
    }


def _direction_components(
    direction: Literal["up", "down"],
    ema: Mapping[str, Any],
    adxdi: Mapping[str, Any],
) -> tuple[float, float, float, float]:
    if direction == "up":
        return (
            _finite(ema.get("ema_bullish_breakout_score"), 0.0),
            _finite(adxdi.get("di_component_bull"), 0.0),
            _finite(adxdi.get("adx_component"), 0.0),
            _finite(ema.get("ema_bullish_trend_score"), 0.0),
        )
    return (
        _finite(ema.get("ema_bearish_breakout_score"), 0.0),
        _finite(adxdi.get("di_component_bear"), 0.0),
        _finite(adxdi.get("adx_component"), 0.0),
        _finite(ema.get("ema_bearish_trend_score"), 0.0),
    )


def compose_breakout_score(
    direction: Literal["up", "down"],
    ema: Mapping[str, Any],
    adxdi: Mapping[str, Any],
    price_acceptance: Mapping[str, Any],
    structure: Mapping[str, Any],
    cfg: IndicatorPatternConfig,
    mode: Literal["baseline", "ema", "ema_adx_di"],
) -> tuple[float, dict[str, Any]]:
    """Compose a breakout score from price, structure and indicator evidence."""
    price_score = _clip01(_finite(price_acceptance.get("price_score"), 0.0))
    structure_score = _clip01(_finite(structure.get("structure_score"), 0.0))
    ema_score, di_score, adx_score, trend_score = _direction_components(direction, ema, adxdi)

    if mode == "baseline":
        weights = {"price": 0.60, "structure": 0.40, "ema": 0.0, "di": 0.0, "adx": 0.0}
    elif mode == "ema":
        weights = {"price": 0.42, "structure": 0.23, "ema": 0.35, "di": 0.0, "adx": 0.0}
    else:
        weights = {
            "price": 0.34,
            "structure": 0.18,
            "ema": 0.18,
            "di": cfg.di_component_weight,
            "adx": cfg.adx_component_weight,
        }
    total = sum(weights.values()) or 1.0
    score = (
        weights["price"] * price_score
        + weights["structure"] * structure_score
        + weights["ema"] * ema_score
        + weights["di"] * di_score
        + weights["adx"] * adx_score
    ) / total
    score = _clip01(score)
    parts = {
        "direction": direction,
        "mode": mode,
        "score": float(score),
        "price_score": float(price_score),
        "structure_score": float(structure_score),
        "ema_score": float(ema_score),
        "di_score": float(di_score),
        "adx_score": float(adx_score),
        "trend_score": float(trend_score),
        "weights": dict(weights),
        "price_acceptance": dict(price_acceptance),
        "structure": dict(structure),
    }
    return score, parts


def _truthy_structure_confirm(structure: Mapping[str, Any]) -> bool:
    if "structure_confirmed" in structure:
        return _truthy(structure.get("structure_confirmed"))
    if "range_score" in structure:
        return _finite(structure.get("range_score"), 0.0) > 0.5
    return False


def _score_price_acceptance(
    *,
    direction: Literal["up", "down"],
    close: float,
    high: float,
    low: float,
    range_high: float | None,
    range_low: float | None,
    atr: float,
    cfg: IndicatorPatternConfig,
    outside_bars: int,
    quick_reentry: bool,
) -> tuple[float, dict[str, Any]]:
    if range_high is None or range_low is None:
        return 0.0, {
            "price_score": 0.0,
            "close_outside": False,
            "outside_distance_atr": 0.0,
            "bars_outside": outside_bars,
            "quick_reentry": quick_reentry,
        }
    buffer_atr = max(cfg.breakout_buffer_atr, 1e-6) * max(atr, 1e-9)
    if direction == "up":
        breakout_level = range_high + buffer_atr
        close_outside = close > breakout_level
        outside_distance_atr = max(0.0, (close - range_high) / max(atr, 1e-9))
    else:
        breakout_level = range_low - buffer_atr
        close_outside = close < breakout_level
        outside_distance_atr = max(0.0, (range_low - close) / max(atr, 1e-9))
    price_score = _clip01(
        0.45 * _clip01(outside_distance_atr / max(cfg.breakout_buffer_atr, 1e-6))
        + 0.25 * _clip01(outside_bars / max(cfg.breakout_acceptance_bars, 1))
        + 0.20 * (1.0 if close_outside else 0.0)
        + 0.10 * (0.0 if quick_reentry else 1.0)
    )
    return price_score, {
        "price_score": float(price_score),
        "close_outside": bool(close_outside),
        "outside_distance_atr": float(outside_distance_atr),
        "breakout_level": float(breakout_level),
        "bars_outside": int(outside_bars),
        "quick_reentry": bool(quick_reentry),
    }


def _score_structure_for_breakout(
    *,
    direction: Literal["up", "down"],
    range_score: float,
    c31_state: str,
    parent_trend: str | None,
    cfg: IndicatorPatternConfig,
) -> dict[str, Any]:
    if direction == "up":
        trend_like = c31_state in {"transition_up", "confirmed_uptrend", "bullish_pullback"}
        parent_ok = parent_trend in {None, "up", "none"}
    else:
        trend_like = c31_state in {"transition_down", "confirmed_downtrend", "bearish_pullback"}
        parent_ok = parent_trend in {None, "down", "none"}
    structure_score = _clip01(
        0.50 * (1.0 if trend_like else 0.0)
        + 0.30 * _clip01(range_score)
        + 0.20 * (1.0 if parent_ok else 0.0)
    )
    return {
        "structure_score": float(structure_score),
        "structure_confirmed": bool(trend_like),
        "parent_ok": bool(parent_ok),
        "range_score": float(range_score),
        "c31_state": c31_state,
        "breakout_structure_confirmation_required": bool(cfg.breakout_structure_confirmation_required),
    }


def _mode_breakout_acceptance_min(cfg: IndicatorPatternConfig) -> float:
    return cfg.breakout_ema_confirmation_min


def apply_indicator_gate(
    prev_shadow: str,
    c31_prev: str,
    c31_state: str,
    bars_in_shadow: int,
    ind: object,
    cfg: IndicatorPatternConfig,
    parent_trend: str | None,
) -> tuple[str, str]:
    """Gate the C3.1 candidate with indicator-pattern evidence."""
    if cfg.mode == "baseline":
        if cfg.gate_confirm_min_baseline_passthrough:
            return c31_state, "baseline_passthrough"
        return c31_state, "baseline_passthrough_disabled"

    ema = compute_ema_band_scores(ind, cfg)
    adxdi = compute_adx_di_scores(ind, cfg)
    if not ema["features_ready"]:
        # Without ready indicators, never invent a gated override — stay on C3.1.
        return c31_state, "features_not_ready_passthrough"

    def direction_of(state: str) -> str | None:
        if state in {"confirmed_uptrend", "transition_up", "bullish_pullback", "ema_bullish_ordered", "ema_bullish_expanding", "ema_bullish_compressed"}:
            return "up"
        if state in {"confirmed_downtrend", "transition_down", "bearish_pullback", "ema_bearish_ordered", "ema_bearish_expanding", "ema_bearish_compressed"}:
            return "down"
        if state in {"range_sideways", "ema_range_like", "ema_mixed", "ema_unclear"}:
            return "range"
        return None

    shadow_dir = direction_of(prev_shadow)
    candidate_dir = direction_of(c31_state)
    range_score = _finite(ema.get("ema_range_score"), 0.0)
    bullish_trend = _finite(ema.get("ema_bullish_trend_score"), 0.0)
    bearish_trend = _finite(ema.get("ema_bearish_trend_score"), 0.0)
    bullish_breakout = _finite(ema.get("ema_bullish_breakout_score"), 0.0)
    bearish_breakout = _finite(ema.get("ema_bearish_breakout_score"), 0.0)
    dominant_di = str(adxdi.get("dominant_di") or "neutral")
    di_bull = _finite(adxdi.get("di_component_bull"), 0.0)
    di_bear = _finite(adxdi.get("di_component_bear"), 0.0)
    adx_component = _finite(adxdi.get("adx_component"), 0.0)
    current = c31_state
    reasons: list[str] = []

    if c31_state == "range_sideways":
        if range_score >= cfg.gate_range_hold_score and max(bullish_breakout, bearish_breakout) < cfg.breakout_ema_confirmation_min:
            if prev_shadow in {"confirmed_uptrend", "confirmed_downtrend", "range_sideways"}:
                return prev_shadow, "soft_range_hold"
            return "range_sideways", "soft_range_hold"
        if prev_shadow in {"confirmed_uptrend", "confirmed_downtrend"} and max(bullish_breakout, bearish_breakout) < cfg.breakout_ema_confirmation_min:
            return prev_shadow, "range_hold"
        return "range_sideways", "range_gate"

    if c31_state in {"confirmed_uptrend", "transition_up", "bullish_pullback"}:
        if parent_trend == "down":
            reasons.append("parent_down_protect")
        if c31_state == "confirmed_uptrend":
            strong = bullish_trend >= cfg.ema_trend_score_min and bullish_breakout >= cfg.breakout_ema_confirmation_min
            if cfg.mode == "ema_adx_di":
                if dominant_di == "minus" and di_bear > di_bull:
                    reasons.append("di_conflict")
                    strong = False
                if adx_component < 0.25 and not strong:
                    reasons.append("adx_soft_delay")
            if strong and not reasons:
                return "confirmed_uptrend", "ema_gate_up"
            if strong and reasons == ["adx_soft_delay"]:
                return "confirmed_uptrend", "ema_gate_up"
            if strong and reasons == ["di_conflict"]:
                return prev_shadow if prev_shadow else "bullish_pullback", "di_conflict_hold"
            if prev_shadow in {"confirmed_uptrend", "bullish_pullback", "transition_up"}:
                return prev_shadow, "ema_hold_up"
            if parent_trend == "down" and c31_state != "bullish_pullback":
                return prev_shadow if prev_shadow else "unclear", "parent_regime_protect"
            if adx_component < 0.20 and bullish_trend < cfg.ema_trend_score_min + 0.08:
                return prev_shadow if prev_shadow else "transition_up", "adx_soft_delay"
            return "transition_up" if c31_state != "bullish_pullback" else "bullish_pullback", "ema_soft_up"
        if prev_shadow in {"confirmed_uptrend", "bullish_pullback", "transition_up"}:
            return prev_shadow, "trend_continuation_hold"
        return c31_state, "trend_up_passthrough"

    if c31_state in {"confirmed_downtrend", "transition_down", "bearish_pullback"}:
        if parent_trend == "up":
            reasons.append("parent_up_protect")
        if c31_state == "confirmed_downtrend":
            strong = bearish_trend >= cfg.ema_trend_score_min and bearish_breakout >= cfg.breakout_ema_confirmation_min
            if cfg.mode == "ema_adx_di":
                if dominant_di == "plus" and di_bull > di_bear:
                    reasons.append("di_conflict")
                    strong = False
                if adx_component < 0.25 and not strong:
                    reasons.append("adx_soft_delay")
            if strong and not reasons:
                return "confirmed_downtrend", "ema_gate_down"
            if strong and reasons == ["adx_soft_delay"]:
                return "confirmed_downtrend", "ema_gate_down"
            if strong and reasons == ["di_conflict"]:
                return prev_shadow if prev_shadow else "bearish_pullback", "di_conflict_hold"
            if prev_shadow in {"confirmed_downtrend", "bearish_pullback", "transition_down"}:
                return prev_shadow, "ema_hold_down"
            if parent_trend == "up" and c31_state != "bearish_pullback":
                return prev_shadow if prev_shadow else "unclear", "parent_regime_protect"
            if adx_component < 0.20 and bearish_trend < cfg.ema_trend_score_min + 0.08:
                return prev_shadow if prev_shadow else "transition_down", "adx_soft_delay"
            return "transition_down" if c31_state != "bearish_pullback" else "bearish_pullback", "ema_soft_down"
        if prev_shadow in {"confirmed_downtrend", "bearish_pullback", "transition_down"}:
            return prev_shadow, "trend_continuation_hold"
        return c31_state, "trend_down_passthrough"

    if c31_state == "ema_mixed":
        if shadow_dir == "range" and range_score >= cfg.gate_range_hold_score:
            return "range_sideways", "mixed_range_hold"
        if prev_shadow in {"confirmed_uptrend", "confirmed_downtrend"}:
            return prev_shadow, "mixed_hold"
        return "ema_mixed", "mixed_passthrough"

    if c31_state == "ema_range_like":
        if prev_shadow in {"confirmed_uptrend", "confirmed_downtrend"} and range_score >= cfg.gate_range_hold_score:
            return prev_shadow, "soft_range_hold"
        return "range_sideways", "range_like"

    if c31_state == "ema_unclear":
        if prev_shadow in {"confirmed_uptrend", "confirmed_downtrend", "range_sideways"}:
            return prev_shadow, "unclear_hold"
        return "unclear", "unclear_hold"

    if prev_shadow and prev_shadow != "unclear":
        return prev_shadow, "shadow_hold"
    return c31_state, "pass_through"


def align_30m_features_to_5m_bars(
    prepared_bars: Sequence[PreparedBar],
    features_30m: pd.DataFrame | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any] | None]:
    """Align 30m indicator rows to 5m decision bars.

    The 30m candle that is fully closed as of a 5m decision time is the candle
    with open time ``floor(decision_time, 30min) - 30min``.
    """
    if len(prepared_bars) == 0:
        return []

    if isinstance(features_30m, pd.DataFrame):
        feat = features_30m.copy()
    else:
        feat = pd.DataFrame(list(features_30m))
    if feat.empty:
        return [None for _ in prepared_bars]

    time_col = "timestamp" if "timestamp" in feat.columns else "decision_time"
    if time_col not in feat.columns:
        return [None for _ in prepared_bars]
    feat = feat.copy()
    feat[time_col] = pd.to_datetime(feat[time_col], utc=True)
    feat = feat.sort_values(time_col).drop_duplicates(subset=[time_col], keep="last")
    by_ts = {pd.Timestamp(row[time_col]): row.to_dict() for _, row in feat.iterrows()}

    aligned: list[dict[str, Any] | None] = []
    for prep in prepared_bars:
        decision_time = _ts(prep.decision_time)
        target = decision_time.floor("30min") - pd.Timedelta(minutes=30)
        row = by_ts.get(target)
        aligned.append(None if row is None else dict(row))
    return aligned


def _indicator_row_at(
    ind_by_prep_index: Sequence[Mapping[str, Any] | None] | Mapping[int, Mapping[str, Any] | None],
    bar_index: int,
) -> dict[str, Any]:
    if isinstance(ind_by_prep_index, Mapping):
        row = ind_by_prep_index.get(bar_index)
    else:
        row = ind_by_prep_index[bar_index] if bar_index < len(ind_by_prep_index) else None
    return _as_row_dict(row)


def _range_bounds_for_row(row: Mapping[str, Any]) -> tuple[float | None, float | None, float | None]:
    hi = row.get("range_high")
    lo = row.get("range_low")
    mid = row.get("range_mid")
    return (
        None if hi is None or pd.isna(hi) else float(hi),
        None if lo is None or pd.isna(lo) else float(lo),
        None if mid is None or pd.isna(mid) else float(mid),
    )


def _store_breakout_scores(
    row: dict[str, Any],
    *,
    prefix: str,
    score: float,
    parts: Mapping[str, Any],
) -> None:
    row[f"breakout_{prefix}_score"] = float(score)
    row[f"breakout_{prefix}_price_score"] = float(parts.get("price_score") or 0.0)
    row[f"breakout_{prefix}_structure_score"] = float(parts.get("structure_score") or 0.0)
    row[f"breakout_{prefix}_ema_score"] = float(parts.get("ema_score") or 0.0)
    row[f"breakout_{prefix}_di_score"] = float(parts.get("di_score") or 0.0)
    row[f"breakout_{prefix}_adx_score"] = float(parts.get("adx_score") or 0.0)


def _store_follow_scores(
    row: dict[str, Any],
    *,
    prefix: str,
    score: float,
    parts: Mapping[str, Any],
) -> None:
    row[f"trend_follow_{prefix}_score"] = float(score)
    row[f"trend_follow_{prefix}_support_score"] = float(parts.get("support_score") or 0.0)
    row[f"trend_follow_{prefix}_reaccel_score"] = float(parts.get("reaccel_score") or 0.0)
    row[f"trend_follow_{prefix}_structure_score"] = float(parts.get("structure_score") or 0.0)
    row[f"trend_follow_{prefix}_adx_score"] = float(parts.get("adx_score") or 0.0)


def _trend_follow_score(
    *,
    direction: Literal["up", "down"],
    ema: Mapping[str, Any],
    adxdi: Mapping[str, Any],
    structure_score: float,
    cfg: IndicatorPatternConfig,
) -> tuple[float, dict[str, Any]]:
    if direction == "up":
        support = _finite(ema.get("ema_pullback_support_score"), 0.0)
        reaccel = _finite(ema.get("ema_bullish_breakout_score"), 0.0)
        trend = _finite(ema.get("ema_bullish_trend_score"), 0.0)
    else:
        support = _finite(ema.get("ema_pullback_support_score"), 0.0)
        reaccel = _finite(ema.get("ema_bearish_breakout_score"), 0.0)
        trend = _finite(ema.get("ema_bearish_trend_score"), 0.0)
    adx_score = _finite(adxdi.get("adx_component"), 0.0)
    score = _clip01(
        0.30 * support + 0.30 * reaccel + 0.25 * trend + 0.15 * adx_score
    )
    return score, {
        "support_score": float(support),
        "reaccel_score": float(reaccel),
        "structure_score": float(structure_score),
        "adx_score": float(adx_score),
        "score": float(score),
    }


def replay_indicator_variant(
    prepared_bars: Sequence[PreparedBar],
    arrays: dict[str, Any],
    c31_cfg: RegimeClassifierConfig,
    pattern_cfg: IndicatorPatternConfig,
    ind_by_prep_index: Sequence[Mapping[str, Any] | None] | Mapping[int, Mapping[str, Any] | None],
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
) -> dict[str, Any]:
    """Policy-only replay for an indicator-pattern ablation variant."""
    rt = RegimeRuntime()
    timeline: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    shadow_state = "unclear"
    shadow_bars = 0
    c31_prev = "unclear"
    up_outside_streak = 0
    down_outside_streak = 0

    for prep in prepared_bars:
        ts = _ts(prep.decision_time)
        if ts < analyze_start or ts > analyze_end:
            continue

        bar_index = int(prep.bar_index)
        ind_row = _indicator_row_at(ind_by_prep_index, bar_index)
        feat = build_bar_features(
            prep,
            arrays,
            net_move_window=c31_cfg.net_move_window,
            efficiency_window=c31_cfg.efficiency_window,
            overlap_window=c31_cfg.overlap_window,
        )
        rt = step_regime_classifier(rt, feat, cfg=c31_cfg)
        c31_state = str(rt.state)
        ema_scores = compute_ema_band_scores(ind_row, pattern_cfg)
        adxdi_scores = compute_adx_di_scores(ind_row, pattern_cfg)
        range_parts = compute_range_score(
            feat,
            cfg=c31_cfg,
            sustained_bos_up=rt.sustained_bos_up,
            sustained_bos_down=rt.sustained_bos_down,
        )
        range_score = float(range_parts["range_score"])
        range_high, range_low, range_mid = _range_bounds_for_row(
            {
                "range_high": rt.range_high,
                "range_low": rt.range_low,
                "range_mid": rt.range_mid,
            }
        )
        close = feat.close
        high = feat.high
        low = feat.low
        atr = max(feat.atr, 1e-9)

        up_outside = range_high is not None and close > range_high + pattern_cfg.breakout_buffer_atr * atr
        down_outside = range_low is not None and close < range_low - pattern_cfg.breakout_buffer_atr * atr
        if up_outside:
            up_outside_streak += 1
        else:
            up_outside_streak = 0
        if down_outside:
            down_outside_streak += 1
        else:
            down_outside_streak = 0

        price_up_score, price_up = _score_price_acceptance(
            direction="up",
            close=close,
            high=high,
            low=low,
            range_high=range_high,
            range_low=range_low,
            atr=atr,
            cfg=pattern_cfg,
            outside_bars=up_outside_streak,
            quick_reentry=up_outside_streak > 0 and up_outside_streak <= pattern_cfg.breakout_quick_reentry_bars and not up_outside,
        )
        price_down_score, price_down = _score_price_acceptance(
            direction="down",
            close=close,
            high=high,
            low=low,
            range_high=range_high,
            range_low=range_low,
            atr=atr,
            cfg=pattern_cfg,
            outside_bars=down_outside_streak,
            quick_reentry=down_outside_streak > 0 and down_outside_streak <= pattern_cfg.breakout_quick_reentry_bars and not down_outside,
        )

        structure_up = _score_structure_for_breakout(
            direction="up",
            range_score=range_score,
            c31_state=c31_state,
            parent_trend=rt.parent_trend_label(),
            cfg=pattern_cfg,
        )
        structure_down = _score_structure_for_breakout(
            direction="down",
            range_score=range_score,
            c31_state=c31_state,
            parent_trend=rt.parent_trend_label(),
            cfg=pattern_cfg,
        )
        breakout_up_score, breakout_up_parts = compose_breakout_score(
            "up",
            ema_scores,
            adxdi_scores,
            price_up,
            structure_up,
            pattern_cfg,
            pattern_cfg.mode,
        )
        breakout_down_score, breakout_down_parts = compose_breakout_score(
            "down",
            ema_scores,
            adxdi_scores,
            price_down,
            structure_down,
            pattern_cfg,
            pattern_cfg.mode,
        )
        trend_up_score, trend_up_parts = _trend_follow_score(
            direction="up",
            ema=ema_scores,
            adxdi=adxdi_scores,
            structure_score=range_score,
            cfg=pattern_cfg,
        )
        trend_down_score, trend_down_parts = _trend_follow_score(
            direction="down",
            ema=ema_scores,
            adxdi=adxdi_scores,
            structure_score=range_score,
            cfg=pattern_cfg,
        )

        c31_prev_state = c31_prev
        gated_state, gate_reason = apply_indicator_gate(
            shadow_state,
            c31_prev,
            c31_state,
            shadow_bars,
            ind_row,
            pattern_cfg,
            rt.parent_trend_label(),
        )
        if gated_state == shadow_state:
            shadow_bars += 1
        else:
            shadow_bars = 1
        prev_shadow = shadow_state
        shadow_state = gated_state
        c31_prev = c31_state

        row = {
            "decision_time": ts.isoformat(),
            "bar_index": bar_index,
            "state": shadow_state,
            "previous_state": prev_shadow,
            "c31_state": c31_state,
            "c31_previous_state": c31_prev_state,
            "parent_trend": rt.parent_trend_label(),
            "parent_trend_direction": rt.parent_trend_label(),
            "gate_reason": gate_reason,
            "bars_in_shadow": shadow_bars,
            "transition": shadow_state != prev_shadow,
            "features_ready": bool(ema_scores.get("features_ready")) and bool(adxdi_scores.get("features_ready")),
            "indicator_version": INDICATOR_FEATURE_VERSION,
            "variant_id": pattern_cfg.variant_id,
            "mode": pattern_cfg.mode,
            "close": float(close),
            "high": float(high),
            "low": float(low),
            "atr": float(atr),
            "range_high": range_high,
            "range_low": range_low,
            "range_mid": range_mid,
            "range_width_atr": float(feat.range_width_atr),
            "range_score": float(range_score),
            "range_de": float(range_parts.get("part_de", 0.0)),
            "range_net_move_atr": float(range_parts.get("part_net", 0.0)),
            "range_box_efficiency": float(range_parts.get("part_box", 0.0)),
            "range_bound_drift": float(range_parts.get("part_drift", 0.0)),
            "ema_state": ema_scores.get("ema_state"),
            "ema_range_score": float(ema_scores.get("ema_range_score") or 0.0),
            "ema_bullish_trend_score": float(ema_scores.get("ema_bullish_trend_score") or 0.0),
            "ema_bearish_trend_score": float(ema_scores.get("ema_bearish_trend_score") or 0.0),
            "ema_bullish_breakout_score": float(ema_scores.get("ema_bullish_breakout_score") or 0.0),
            "ema_bearish_breakout_score": float(ema_scores.get("ema_bearish_breakout_score") or 0.0),
            "ema_pullback_support_score": float(ema_scores.get("ema_pullback_support_score") or 0.0),
            "ema_bullish_ordered_score": float(ema_scores.get("ema_bullish_ordered_score") or 0.0),
            "ema_bearish_ordered_score": float(ema_scores.get("ema_bearish_ordered_score") or 0.0),
            "ema_expansion_score": float(ema_scores.get("ema_expansion_score") or 0.0),
            "ema_compression_score": float(ema_scores.get("ema_compression_score") or 0.0),
            "adx_dominant_di": adxdi_scores.get("dominant_di"),
            "di_bullish_confirmation": float(adxdi_scores.get("di_bullish_confirmation") or 0.0),
            "di_bearish_confirmation": float(adxdi_scores.get("di_bearish_confirmation") or 0.0),
            "di_neutral": float(adxdi_scores.get("di_neutral") or 0.0),
            "adx_weak": float(adxdi_scores.get("adx_weak") or 0.0),
            "adx_strengthening": float(adxdi_scores.get("adx_strengthening") or 0.0),
            "adx_strong": float(adxdi_scores.get("adx_strong") or 0.0),
            "adx_falling": float(adxdi_scores.get("adx_falling") or 0.0),
            "di_component_bull": float(adxdi_scores.get("di_component_bull") or 0.0),
            "di_component_bear": float(adxdi_scores.get("di_component_bear") or 0.0),
            "adx_component": float(adxdi_scores.get("adx_component") or 0.0),
            "breakout_up_score": float(breakout_up_score),
            "breakout_down_score": float(breakout_down_score),
            "trend_follow_up_score": float(trend_up_score),
            "trend_follow_down_score": float(trend_down_score),
            "price_acceptance_up": bool(price_up.get("close_outside")),
            "price_acceptance_down": bool(price_down.get("close_outside")),
            "breakout_up_outside_bars": int(price_up.get("bars_outside") or 0),
            "breakout_down_outside_bars": int(price_down.get("bars_outside") or 0),
            "breakout_up_breakout_level": float(price_up.get("breakout_level") or 0.0),
            "breakout_down_breakout_level": float(price_down.get("breakout_level") or 0.0),
            "pullback_depth_atr": float(abs(close - _finite(_lookup(ind_row, "ema_20"), close)) / atr),
            "pullback_ema20_distance_atr": float(abs(close - _finite(_lookup(ind_row, "ema_20"), close)) / atr),
            "pullback_ema59_distance_atr": float(abs(close - _finite(_lookup(ind_row, "ema_59"), close)) / atr),
            "reacceleration_expansion_min": float(pattern_cfg.reacceleration_expansion_min),
            "trend_follow_confirmation_bars": int(pattern_cfg.trend_follow_confirmation_bars),
            "trend_follow_score_min": float(pattern_cfg.trend_follow_score_min),
            "breakout_acceptance_bars": int(pattern_cfg.breakout_acceptance_bars),
            "breakout_max_confirmation_bars": int(pattern_cfg.breakout_max_confirmation_bars),
        }
        row.update(
            {
                "breakout_up_structure_confirmed": bool(structure_up.get("structure_confirmed")),
                "breakout_down_structure_confirmed": bool(structure_down.get("structure_confirmed")),
                "breakout_up_parent_ok": bool(structure_up.get("parent_ok")),
                "breakout_down_parent_ok": bool(structure_down.get("parent_ok")),
                "breakout_up_price_score": float(breakout_up_parts.get("price_score") or 0.0),
                "breakout_up_structure_score": float(breakout_up_parts.get("structure_score") or 0.0),
                "breakout_up_ema_score": float(breakout_up_parts.get("ema_score") or 0.0),
                "breakout_up_di_score": float(breakout_up_parts.get("di_score") or 0.0),
                "breakout_up_adx_score": float(breakout_up_parts.get("adx_score") or 0.0),
                "breakout_down_price_score": float(breakout_down_parts.get("price_score") or 0.0),
                "breakout_down_structure_score": float(breakout_down_parts.get("structure_score") or 0.0),
                "breakout_down_ema_score": float(breakout_down_parts.get("ema_score") or 0.0),
                "breakout_down_di_score": float(breakout_down_parts.get("di_score") or 0.0),
                "breakout_down_adx_score": float(breakout_down_parts.get("adx_score") or 0.0),
                "trend_follow_up_support_score": float(trend_up_parts.get("support_score") or 0.0),
                "trend_follow_up_reaccel_score": float(trend_up_parts.get("reaccel_score") or 0.0),
                "trend_follow_up_structure_score": float(trend_up_parts.get("structure_score") or 0.0),
                "trend_follow_up_adx_score": float(trend_up_parts.get("adx_score") or 0.0),
                "trend_follow_down_support_score": float(trend_down_parts.get("support_score") or 0.0),
                "trend_follow_down_reaccel_score": float(trend_down_parts.get("reaccel_score") or 0.0),
                "trend_follow_down_structure_score": float(trend_down_parts.get("structure_score") or 0.0),
                "trend_follow_down_adx_score": float(trend_down_parts.get("adx_score") or 0.0),
            }
        )
        timeline.append(row)
        if shadow_state != prev_shadow:
            transitions.append(
                {
                    "decision_time": ts.isoformat(),
                    "bar_index": bar_index,
                    "previous_state": prev_shadow,
                    "new_state": shadow_state,
                    "c31_state": c31_state,
                    "parent_trend": rt.parent_trend_label(),
                    "gate_reason": gate_reason,
                    "range_score": float(range_score),
                    "ema_state": ema_scores.get("ema_state"),
                    "breakout_up_score": float(breakout_up_score),
                    "breakout_down_score": float(breakout_down_score),
                    "trend_follow_up_score": float(trend_up_score),
                    "trend_follow_down_score": float(trend_down_score),
                }
            )

    return {
        "variant": pattern_cfg.variant_id,
        "mode": pattern_cfg.mode,
        "config": pattern_cfg.to_dict(),
        "timeline": timeline,
        "transitions": transitions,
    }


def extract_breakout_events(
    timeline: Sequence[Mapping[str, Any]],
    variant: str,
    symbol: str,
    timeframe: str,
    cfg: IndicatorPatternConfig,
) -> list[dict[str, Any]]:
    """Extract breakout lifecycle events from the replay timeline."""
    events: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    event_id = 0

    def finish(outcome: str, row: Mapping[str, Any], reason: str) -> None:
        nonlocal active, event_id
        if active is None:
            return
        active["lifecycle_outcome"] = outcome
        active["result"] = outcome
        active["end_time"] = str(row.get("decision_time") or active["attempt_time"])
        active["end_bar_index"] = int(row.get("bar_index") or active["attempt_bar_index"])
        active["end_state"] = str(row.get("state") or "")
        active["end_c31_state"] = str(row.get("c31_state") or "")
        active["exit_reason"] = reason
        active["bars_active"] = int(active.get("bars_active", 0))
        events.append(active)
        active = None

    for row in timeline:
        close = _finite(row.get("close"))
        high = _finite(row.get("high"))
        low = _finite(row.get("low"))
        range_high = row.get("range_high")
        range_low = row.get("range_low")
        if range_high is None or range_low is None:
            if active is not None:
                active["bars_active"] = int(active.get("bars_active", 0)) + 1
            continue
        range_high = _finite(range_high)
        range_low = _finite(range_low)
        atr = max(_finite(row.get("atr"), 1.0), 1e-9)
        upper = range_high + cfg.breakout_buffer_atr * atr
        lower = range_low - cfg.breakout_buffer_atr * atr
        outside_up = close > upper
        outside_down = close < lower
        inside = not outside_up and not outside_down
        direction = "up" if outside_up else "down" if outside_down else None

        if active is None:
            if direction is None:
                continue
            breakout_score = float(row.get(f"breakout_{direction}_score") or 0.0)
            event_id += 1
            active = {
                "event_id": f"{variant}:{symbol}:{timeframe}:breakout:{event_id}",
                "breakout_id": f"{variant}:{symbol}:{timeframe}:breakout:{event_id}",
                "symbol": symbol,
                "timeframe": timeframe,
                "variant": variant,
                "direction": direction,
                "attempt_time": str(row.get("decision_time") or ""),
                "attempt_bar_index": int(row.get("bar_index") or 0),
                "attempt_state": str(row.get("state") or ""),
                "attempt_c31_state": str(row.get("c31_state") or ""),
                "parent_trend": row.get("parent_trend"),
                "range_high": float(range_high),
                "range_low": float(range_low),
                "range_mid": _finite(row.get("range_mid"), (range_high + range_low) / 2.0),
                "breakout_level": float(row.get(f"breakout_{direction}_breakout_level") or (upper if direction == "up" else lower)),
                "attempt_close": float(close),
                "attempt_high": float(high),
                "attempt_low": float(low),
                "attempt_score": breakout_score,
                "attempt_price_score": float(row.get(f"breakout_{direction}_price_score") or 0.0),
                "attempt_structure_score": float(row.get(f"breakout_{direction}_structure_score") or 0.0),
                "attempt_ema_score": float(row.get(f"breakout_{direction}_ema_score") or 0.0),
                "attempt_di_score": float(row.get(f"breakout_{direction}_di_score") or 0.0),
                "attempt_adx_score": float(row.get(f"breakout_{direction}_adx_score") or 0.0),
                "bars_outside": 1,
                "bars_active": 1,
                "confirm_bars_required": int(cfg.breakout_acceptance_bars),
                "max_confirmation_bars": int(cfg.breakout_max_confirmation_bars),
                "quick_reentry_bars": int(cfg.breakout_quick_reentry_bars),
                "features_ready": bool(row.get("features_ready")),
                "ema_state": row.get("ema_state"),
                "ema_range_score": float(row.get("ema_range_score") or 0.0),
                "di_dominant": row.get("adx_dominant_di"),
                "adx_component": float(row.get("adx_component") or 0.0),
                "lifecycle_outcome": "active",
                "exit_reason": "",
            }
            continue

        if direction == active["direction"]:
            active["bars_outside"] = int(active.get("bars_outside", 0)) + 1
            active["bars_active"] = int(active.get("bars_active", 0)) + 1
            score = float(row.get(f"breakout_{direction}_score") or 0.0)
            active["attempt_score"] = max(float(active["attempt_score"]), score)
            if active["bars_outside"] >= cfg.breakout_acceptance_bars and score >= cfg.breakout_ema_confirmation_min:
                if not cfg.breakout_structure_confirmation_required or _truthy(row.get(f"breakout_{direction}_structure_confirmed"), False):
                    active["confirm_time"] = str(row.get("decision_time") or "")
                    active["confirm_bar_index"] = int(row.get("bar_index") or 0)
                    active["confirm_close"] = float(close)
                    active["confirm_score"] = score
                    active["confirm_state"] = str(row.get("state") or "")
                    active["confirm_c31_state"] = str(row.get("c31_state") or "")
                    finish("confirmed", row, "accepted_after_confirmation_bars")
                    continue
            if active["bars_active"] >= cfg.breakout_max_confirmation_bars:
                finish("timeout", row, "max_confirmation_bars")
                continue
            if row.get("state") == "range_sideways" and active["bars_outside"] < cfg.breakout_acceptance_bars:
                # still inside the lifecycle
                pass
            continue

        # opposite side or re-entry
        if inside:
            if int(active.get("bars_active", 0)) <= cfg.breakout_quick_reentry_bars:
                finish("reentered", row, "returned_inside_range")
            else:
                finish("failed", row, "returned_inside_range")
            continue

        # Opposite direction before confirmation.
        finish("failed", row, "opposite_break_before_confirmation")
        if direction is not None:
            event_id += 1
            active = {
                "event_id": f"{variant}:{symbol}:{timeframe}:breakout:{event_id}",
                "breakout_id": f"{variant}:{symbol}:{timeframe}:breakout:{event_id}",
                "symbol": symbol,
                "timeframe": timeframe,
                "variant": variant,
                "direction": direction,
                "attempt_time": str(row.get("decision_time") or ""),
                "attempt_bar_index": int(row.get("bar_index") or 0),
                "attempt_state": str(row.get("state") or ""),
                "attempt_c31_state": str(row.get("c31_state") or ""),
                "parent_trend": row.get("parent_trend"),
                "range_high": float(range_high),
                "range_low": float(range_low),
                "range_mid": _finite(row.get("range_mid"), (range_high + range_low) / 2.0),
                "breakout_level": float(row.get(f"breakout_{direction}_breakout_level") or (upper if direction == "up" else lower)),
                "attempt_close": float(close),
                "attempt_high": float(high),
                "attempt_low": float(low),
                "attempt_score": float(row.get(f"breakout_{direction}_score") or 0.0),
                "attempt_price_score": float(row.get(f"breakout_{direction}_price_score") or 0.0),
                "attempt_structure_score": float(row.get(f"breakout_{direction}_structure_score") or 0.0),
                "attempt_ema_score": float(row.get(f"breakout_{direction}_ema_score") or 0.0),
                "attempt_di_score": float(row.get(f"breakout_{direction}_di_score") or 0.0),
                "attempt_adx_score": float(row.get(f"breakout_{direction}_adx_score") or 0.0),
                "bars_outside": 1,
                "bars_active": 1,
                "confirm_bars_required": int(cfg.breakout_acceptance_bars),
                "max_confirmation_bars": int(cfg.breakout_max_confirmation_bars),
                "quick_reentry_bars": int(cfg.breakout_quick_reentry_bars),
                "features_ready": bool(row.get("features_ready")),
                "ema_state": row.get("ema_state"),
                "ema_range_score": float(row.get("ema_range_score") or 0.0),
                "di_dominant": row.get("adx_dominant_di"),
                "adx_component": float(row.get("adx_component") or 0.0),
                "lifecycle_outcome": "active",
                "exit_reason": "",
            }

    if active is not None:
        finish("timeout", timeline[-1], "analyze_window_end")
    return events


def extract_trend_follow_events(
    timeline: Sequence[Mapping[str, Any]],
    variant: str,
    symbol: str,
    timeframe: str,
    cfg: IndicatorPatternConfig,
) -> list[dict[str, Any]]:
    """Extract bullish/bearish pullback -> reacceleration -> confirmation lifecycles."""
    events: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    event_id = 0

    def finish(outcome: str, row: Mapping[str, Any], reason: str) -> None:
        nonlocal active, event_id
        if active is None:
            return
        active["lifecycle_outcome"] = outcome
        active["end_time"] = str(row.get("decision_time") or active["start_time"])
        active["end_bar_index"] = int(row.get("bar_index") or active["start_bar_index"])
        active["end_state"] = str(row.get("state") or "")
        active["end_c31_state"] = str(row.get("c31_state") or "")
        active["exit_reason"] = reason
        active["bars_active"] = int(active.get("bars_active", 0))
        events.append(active)
        active = None

    for row in timeline:
        state = str(row.get("state") or "")
        parent_trend = str(row.get("parent_trend") or "none")
        c31_state = str(row.get("c31_state") or "")
        close = _finite(row.get("close"))
        atr = max(_finite(row.get("atr"), 1.0), 1e-9)
        ema_support = _finite(row.get("ema_pullback_support_score"), 0.0)
        up_score = _finite(row.get("trend_follow_up_score"), 0.0)
        down_score = _finite(row.get("trend_follow_down_score"), 0.0)
        up_reaccel = _finite(row.get("trend_follow_up_reaccel_score"), 0.0)
        down_reaccel = _finite(row.get("trend_follow_down_reaccel_score"), 0.0)
        up_struct = _finite(row.get("trend_follow_up_structure_score"), 0.0)
        down_struct = _finite(row.get("trend_follow_down_structure_score"), 0.0)
        up_adx = _finite(row.get("trend_follow_up_adx_score"), 0.0)
        down_adx = _finite(row.get("trend_follow_down_adx_score"), 0.0)
        ema20 = _finite(row.get("ema_20"), close)
        ema59 = _finite(row.get("ema_59"), close)
        pullback_depth = abs(close - ema20) / atr
        ema59_depth = abs(close - ema59) / atr
        side = "up" if state == "bullish_pullback" or parent_trend == "up" else "down" if state == "bearish_pullback" or parent_trend == "down" else None

        if active is None:
            if state not in {"bullish_pullback", "bearish_pullback"}:
                continue
            event_id += 1
            direction = "up" if state == "bullish_pullback" else "down"
            active = {
                "event_id": f"{variant}:{symbol}:{timeframe}:trend_follow:{event_id}",
                "symbol": symbol,
                "timeframe": timeframe,
                "variant": variant,
                "direction": direction,
                "parent_trend": parent_trend,
                "start_time": str(row.get("decision_time") or ""),
                "start_bar_index": int(row.get("bar_index") or 0),
                "start_state": state,
                "start_c31_state": c31_state,
                "start_close": float(close),
                "start_ema_state": row.get("ema_state"),
                "start_ema_support_score": float(ema_support),
                "start_pullback_depth_atr": float(pullback_depth),
                "start_pullback_ema20_distance_atr": float(abs(close - ema20) / atr),
                "start_pullback_ema59_distance_atr": float(abs(close - ema59) / atr),
                "structure_max_depth_atr": float(cfg.pullback_max_structure_depth),
                "trend_follow_score_min": float(cfg.trend_follow_score_min),
                "confirmation_bars_required": int(cfg.trend_follow_confirmation_bars),
                "bars_active": 1,
                "reacceleration_seen": False,
                "reacceleration_time": None,
                "reacceleration_bar_index": None,
                "reacceleration_score": 0.0,
                "confirm_time": None,
                "confirm_bar_index": None,
                "confirm_score": 0.0,
                "confirm_state": None,
                "lifecycle_outcome": "active",
                "exit_reason": "",
                "ema_state": row.get("ema_state"),
                "adx_dominant_di": row.get("adx_dominant_di"),
                "adx_component": float(row.get("adx_component") or 0.0),
                "range_score": float(row.get("range_score") or 0.0),
            }
            continue

        active["bars_active"] = int(active.get("bars_active", 0)) + 1
        direction = str(active.get("direction") or "up")
        score = up_score if direction == "up" else down_score
        reaccel = up_reaccel if direction == "up" else down_reaccel
        struct_score = up_struct if direction == "up" else down_struct
        adx_score = up_adx if direction == "up" else down_adx
        trend_score = _finite(row.get("ema_bullish_trend_score" if direction == "up" else "ema_bearish_trend_score"), 0.0)

        if not active["reacceleration_seen"] and reaccel >= cfg.reacceleration_expansion_min and trend_score >= cfg.trend_follow_score_min:
            active["reacceleration_seen"] = True
            active["reacceleration_time"] = str(row.get("decision_time") or "")
            active["reacceleration_bar_index"] = int(row.get("bar_index") or 0)
            active["reacceleration_score"] = float(score)

        if pullback_depth > cfg.pullback_max_structure_depth:
            finish("failed", row, "pullback_depth_exceeded")
            continue

        if score >= cfg.trend_follow_score_min and active["reacceleration_seen"]:
            confirmations = int(active.get("confirmations", 0))
            confirmations += 1
            active["confirmations"] = confirmations
            if confirmations >= cfg.trend_follow_confirmation_bars:
                active["confirm_time"] = str(row.get("decision_time") or "")
                active["confirm_bar_index"] = int(row.get("bar_index") or 0)
                active["confirm_score"] = float(score)
                active["confirm_state"] = c31_state
                finish("confirmed", row, "trend_follow_confirmed")
                continue
        else:
            active["confirmations"] = 0

        if direction == "up" and c31_state == "confirmed_downtrend":
            finish("failed", row, "opposite_trend_confirmed")
            continue
        if direction == "down" and c31_state == "confirmed_uptrend":
            finish("failed", row, "opposite_trend_confirmed")
            continue

        if active["bars_active"] > cfg.transition_max_bars:
            finish("timeout", row, "max_lifecycle_bars")
            continue

        active["ema_state"] = row.get("ema_state")
        active["adx_dominant_di"] = row.get("adx_dominant_di")
        active["adx_component"] = float(row.get("adx_component") or 0.0)
        active["range_score"] = float(row.get("range_score") or 0.0)
        active["current_state"] = state
        active["current_c31_state"] = c31_state
        active["current_score"] = float(score)
        active["current_reaccel_score"] = float(reaccel)
        active["current_structure_score"] = float(struct_score)
        active["current_adx_score"] = float(adx_score)
        active["current_close"] = float(close)
        active["current_pullback_depth_atr"] = float(pullback_depth)
        active["current_pullback_ema20_distance_atr"] = float(abs(close - ema20) / atr)
        active["current_pullback_ema59_distance_atr"] = float(ema59_depth)

    if active is not None:
        finish("timeout", timeline[-1], "analyze_window_end")
    return events


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(list(rows)).to_csv(path, index=False)


def _deterministic_hash(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(json_safe(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def run_audit(
    *,
    symbol: str = "APTUSDT",
    timeframe: str = "5m",
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    output_dir: Path = Path("research/regime_scanner/results/phase_c3_2_indicator_ablation"),
    variants: Sequence[str] = ALL_VARIANTS,
) -> dict[str, Any]:
    """Run the research-only ablation and write audit artifacts."""
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)
    t0 = time.perf_counter()
    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    shared = load_or_build_shared_context(frame, cache_dir=output_dir / ".cache")
    features_30m = load_or_build_indicator_features(
        symbol=symbol,
        timeframe="30m",
        analyze_start=load_start,
        analyze_end=load_end,
        cache_dir=output_dir / ".cache" / "indicator_features",
    )
    from research.regime_scanner.trend_audit_shared_replay import attach_c32a_indicator_features

    attach_c32a_indicator_features(shared, features_30m)
    aligned_30m = align_30m_features_to_5m_bars(shared.prepared_bars, features_30m)
    ind_by_index = {prep.bar_index: aligned_30m[i] for i, prep in enumerate(shared.prepared_bars)}

    # C3.2B must reproduce C3.1 conservative exactly.
    c31_cfg = config_c3("conservative")
    arrays = precompute_regime_arrays(
        frame,
        efficiency_window=c31_cfg.efficiency_window,
        net_move_window=c31_cfg.net_move_window,
        overlap_window=c31_cfg.overlap_window,
        range_width_window=c31_cfg.range_width_window,
        range_lookback=c31_cfg.range_lookback,
        failed_breakout_window=c31_cfg.failed_breakout_window,
        alternating_window=c31_cfg.alternating_window,
    )
    variant_results: dict[str, dict[str, Any]] = {}
    for variant_id in variants:
        cfg = config_for_variant(variant_id)
        variant_results[variant_id] = replay_indicator_variant(
            shared.prepared_bars,
            arrays,
            c31_cfg,
            cfg,
            ind_by_index,
            a0,
            a1,
        )

    comparison_rows: list[dict[str, Any]] = []
    breakout_rows: list[dict[str, Any]] = []
    follow_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    for variant_id, result in variant_results.items():
        tl = list(result["timeline"])
        timeline_rows.extend([{**r, "variant": variant_id} for r in tl])
        cfg = config_for_variant(variant_id)
        breakout = extract_breakout_events(tl, variant_id, symbol, timeframe, cfg)
        trend_follow = extract_trend_follow_events(tl, variant_id, symbol, timeframe, cfg)
        breakout_rows.extend(breakout)
        follow_rows.extend(trend_follow)
        comparison_rows.append(
            {
                "variant": variant_id,
                "mode": result["mode"],
                "n_timeline_bars": len(tl),
                "n_transitions": len(result["transitions"]),
                "n_breakout_events": len(breakout),
                "n_trend_follow_events": len(trend_follow),
                "share_range_sideways": (
                    sum(1 for r in tl if r.get("state") == "range_sideways") / max(1, len(tl))
                ),
                "share_confirmed_uptrend": (
                    sum(1 for r in tl if r.get("state") == "confirmed_uptrend") / max(1, len(tl))
                ),
                "share_confirmed_downtrend": (
                    sum(1 for r in tl if r.get("state") == "confirmed_downtrend") / max(1, len(tl))
                ),
                "mean_breakout_up_score": float(np.nanmean([r.get("breakout_up_score", 0.0) for r in tl])) if tl else 0.0,
                "mean_breakout_down_score": float(np.nanmean([r.get("breakout_down_score", 0.0) for r in tl])) if tl else 0.0,
                "mean_trend_follow_up_score": float(np.nanmean([r.get("trend_follow_up_score", 0.0) for r in tl])) if tl else 0.0,
                "mean_trend_follow_down_score": float(np.nanmean([r.get("trend_follow_down_score", 0.0) for r in tl])) if tl else 0.0,
            }
        )

    _write_csv(output_dir / "timeline.csv", timeline_rows)
    _write_csv(output_dir / "variant_comparison.csv", comparison_rows)
    _write_csv(output_dir / "breakout_events.csv", breakout_rows)
    _write_csv(output_dir / "trend_follow_events.csv", follow_rows)

    summary_core = {
        "phase": "C3_2_indicator_ablation",
        "symbol": symbol,
        "timeframe": timeframe,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "variants": {k: v["config"] for k, v in variant_results.items()},
        "comparison": comparison_rows,
        "n_breakout_events": len(breakout_rows),
        "n_trend_follow_events": len(follow_rows),
        "performance": {
            "elapsed_seconds": round(time.perf_counter() - t0, 3),
            "shared_structure_passes": shared.structure_pass_count,
            "shared_cache_key": shared.cache_key,
        },
        "safety": {
            "production_unchanged": True,
            "no_live_bot_changes": True,
        },
    }
    summary = {**summary_core, "deterministic_hash": _deterministic_hash(summary_core)}
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "metadata.json").write_text(
        json.dumps(
            json_safe(
                {
                    "indicator_feature_version": INDICATOR_FEATURE_VERSION,
                    "all_variants": list(ALL_VARIANTS),
                    "config_template": IndicatorPatternConfig().to_dict(),
                    "csv_artifacts": [
                        "timeline.csv",
                        "variant_comparison.csv",
                        "breakout_events.csv",
                        "trend_follow_events.csv",
                        "summary.json",
                        "metadata.json",
                    ],
                }
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.2 indicator-pattern ablation")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--load-start", default=LOAD_START)
    p.add_argument("--load-end", default=LOAD_END)
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    p.add_argument("--output-dir", type=Path, default=Path("research/regime_scanner/results/phase_c3_2_indicator_ablation"))
    p.add_argument("--variants", nargs="+", default=list(ALL_VARIANTS))
    args = p.parse_args(argv)

    summary = run_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        output_dir=args.output_dir,
        variants=args.variants,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "n_breakout_events": summary["n_breakout_events"],
                "n_trend_follow_events": summary["n_trend_follow_events"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic analytical regime / entry-risk classifier (backtest-only).

No machine learning, no orders, no strategy execution. All scores are bounded,
component-weighted, and emitted with named reason codes.

Score ranges
------------
* ``trend_direction_score``: [-100, +100]  (bearish … bullish)
* ``trend_strength_score``: [0, 100]
* ``trend_acceleration_score``: [-100, +100]  (decelerating … accelerating)
* ``overextension_score_{long,short}``: [0, 100]
* ``reversal_risk_score_{long,short}``: [0, 100]
* ``data_quality_score``: [0, 100]

Weights live in :class:`RegimeScannerConfig` and are not hidden.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from .config import RegimeScannerConfig, default_regime_scanner_config

RegimeLabel = Literal[
    "strong_bullish_expansion",
    "bullish_trend",
    "bullish_weakening",
    "neutral_range",
    "bearish_trend",
    "strong_bearish_expansion",
    "bearish_weakening",
    "transition",
]
EntryRisk = Literal["low", "moderate", "high", "extreme", "unavailable"]
AccelerationLabel = Literal[
    "accelerating",
    "steady",
    "decelerating",
    "mixed",
    "unavailable",
]
StrengthLabel = Literal[
    "very_weak",
    "weak",
    "moderate",
    "strong",
    "very_strong",
    "unavailable",
]

REASON_CODE_HELP: dict[str, str] = {
    "EMA_FULL_BULLISH_ALIGNMENT": "EMA9 > EMA20 > EMA59 > EMA200",
    "EMA_FULL_BEARISH_ALIGNMENT": "EMA9 < EMA20 < EMA59 < EMA200",
    "EMA_MIXED_ALIGNMENT": "EMA stack is mixed / transitional",
    "CLOSE_ABOVE_ALL_EMAS": "Close is above EMA9/20/59/200",
    "CLOSE_BELOW_ALL_EMAS": "Close is below EMA9/20/59/200",
    "ADX_VERY_STRONG": "ADX above the very-strong heuristic threshold",
    "ADX_STRONG": "ADX in the strong heuristic band",
    "ADX_MODERATE": "ADX in the moderate heuristic band",
    "ADX_WEAK": "ADX in the weak / very-weak heuristic band",
    "ADX_RISING": "ADX rising over short lookbacks",
    "ADX_FALLING": "ADX falling over short lookbacks",
    "DI_SPREAD_STRONGLY_POSITIVE": "DI-spread strongly positive",
    "DI_SPREAD_STRONGLY_NEGATIVE": "DI-spread strongly negative",
    "DI_SPREAD_NEAR_ZERO": "DI-spread near zero",
    "MEDIUM_TERM_SLOPES_ACCELERATING": "EMA20/59 medium-window slopes strengthening",
    "LONG_TERM_SLOPES_ACCELERATING": "EMA200 long-window slopes strengthening",
    "SHORT_TERM_SLOPE_DECELERATION": "EMA9 short-window slope weakening while still directional",
    "MEDIUM_TERM_SLOPES_DECELERATING": "EMA20/59 slopes weakening vs prior windows",
    "EMA_BANDS_EXPANDING": "Primary EMA bands expanding",
    "EMA_BANDS_CONTRACTING": "Primary EMA bands contracting",
    "HH_HL_STRUCTURE": "Last confirmed swings form higher-high / higher-low",
    "LH_LL_STRUCTURE": "Last confirmed swings form lower-high / lower-low",
    "CLOSE_OVER_3_ATR_ABOVE_EMA20": "Close more than 3 ATR above EMA20 (v1 heuristic)",
    "CLOSE_OVER_6_ATR_ABOVE_EMA59": "Close more than 6 ATR above EMA59 (v1 heuristic)",
    "CLOSE_OVER_10_ATR_ABOVE_EMA200": "Close more than 10 ATR above EMA200 (v1 heuristic)",
    "CLOSE_OVER_3_ATR_BELOW_EMA20": "Close more than 3 ATR below EMA20 (v1 heuristic)",
    "CLOSE_OVER_10_ATR_BELOW_EMA200": "Close more than 10 ATR below EMA200 (v1 heuristic)",
    "NO_CONFIRMED_BEARISH_DIVERGENCE": "No confirmed bearish divergence on eligible swings",
    "NO_CONFIRMED_BULLISH_DIVERGENCE": "No confirmed bullish divergence on eligible swings",
    "CONFIRMED_BEARISH_DIVERGENCE": "Confirmed bearish divergence present",
    "CONFIRMED_BULLISH_DIVERGENCE": "Confirmed bullish divergence present",
    "HIGH_VOLATILITY_EXPANSION": "ATR% above recent mean ratio threshold",
    "WEAKENING_SIGNALS_PRESENT": "Current weakening signals present (not confirmed divergence)",
    "INSUFFICIENT_HISTORY": "Warmup / history insufficient for full classification",
    "COUNTER_TREND_ENTRY": "Entry direction opposes the classified regime bias",
    "TREND_ALIGNED_ENTRY": "Entry direction aligns with classified regime bias",
}


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _score_bundle(components: dict[str, float], *, lo: float, hi: float) -> dict[str, Any]:
    clean: dict[str, float] = {}
    for key, value in components.items():
        number = float(value)
        clean[key] = number if math.isfinite(number) else 0.0
    summed = float(sum(clean.values()))
    return {
        "score": float(_clamp(summed, lo, hi)),
        "components": clean,
    }


def _ema_alignment_sign(ema: dict[str, Any]) -> int:
    e9 = _finite(ema.get("ema_9"))
    e20 = _finite(ema.get("ema_20"))
    e59 = _finite(ema.get("ema_59"))
    e200 = _finite(ema.get("ema_200"))
    if None in (e9, e20, e59, e200):
        return 0
    if e9 > e20 > e59 > e200:
        return 1
    if e9 < e20 < e59 < e200:
        return -1
    return 0


def _close_vs_all_sign(close_vs: dict[str, Any]) -> int:
    vals = [
        _finite(close_vs.get("close_vs_ema_9_pct")),
        _finite(close_vs.get("close_vs_ema_20_pct")),
        _finite(close_vs.get("close_vs_ema_59_pct")),
        _finite(close_vs.get("close_vs_ema_200_pct")),
    ]
    if any(v is None for v in vals):
        return 0
    if all(v is not None and v > 0 for v in vals):
        return 1
    if all(v is not None and v < 0 for v in vals):
        return -1
    return 0


def _structure_bias(pivots: dict[str, Any]) -> int:
    highs = pivots.get("last_two_highs") or []
    lows = pivots.get("last_two_lows") or []
    bias = 0
    if len(highs) >= 2 and _finite(highs[-1].get("price")) is not None and _finite(highs[-2].get("price")) is not None:
        if highs[-1]["price"] > highs[-2]["price"]:
            bias += 1
        elif highs[-1]["price"] < highs[-2]["price"]:
            bias -= 1
    if len(lows) >= 2 and _finite(lows[-1].get("price")) is not None and _finite(lows[-2].get("price")) is not None:
        if lows[-1]["price"] > lows[-2]["price"]:
            bias += 1
        elif lows[-1]["price"] < lows[-2]["price"]:
            bias -= 1
    if bias >= 2:
        return 1
    if bias <= -2:
        return -1
    return 0


def _slope_direction_fraction(slopes: dict[str, Any], periods: tuple[str, ...], windows: tuple[str, ...]) -> float | None:
    ups = 0
    downs = 0
    total = 0
    for period in periods:
        for window in windows:
            item = (slopes.get(period) or {}).get(window) or {}
            direction = item.get("direction")
            if direction == "up":
                ups += 1
                total += 1
            elif direction == "down":
                downs += 1
                total += 1
            elif direction == "flat":
                total += 1
    if total == 0:
        return None
    return (ups - downs) / total


def _slope_accel_fraction(slopes: dict[str, Any], periods: tuple[str, ...], windows: tuple[str, ...]) -> float | None:
    strengthen = 0
    weaken = 0
    total = 0
    for period in periods:
        for window in windows:
            item = (slopes.get(period) or {}).get(window) or {}
            status = item.get("status")
            if status == "strengthening":
                strengthen += 1
                total += 1
            elif status == "weakening":
                weaken += 1
                total += 1
            elif status == "stable":
                total += 1
    if total == 0:
        return None
    return (strengthen - weaken) / total


def _band_expansion_bias(bands: dict[str, Any], window: str = "12") -> float | None:
    expand = 0
    contract = 0
    total = 0
    for payload in bands.values():
        status = ((payload.get("windows") or {}).get(window) or {}).get("status")
        if status == "expanding":
            expand += 1
            total += 1
        elif status == "contracting":
            contract += 1
            total += 1
        elif status == "stable":
            total += 1
    if total == 0:
        return None
    return (expand - contract) / total


def _band_orientation_bias(bands: dict[str, Any]) -> float | None:
    bull = 0
    bear = 0
    total = 0
    for payload in bands.values():
        orientation = payload.get("orientation")
        if orientation == "bullish":
            bull += 1
            total += 1
        elif orientation == "bearish":
            bear += 1
            total += 1
        elif orientation == "flat":
            total += 1
    if total == 0:
        return None
    return (bull - bear) / total


def _has_confirmed(divergences: list[dict[str, Any]], status: str) -> bool:
    return any(d.get("status") == status for d in divergences)


def _has_confirmed_prefix(divergences: list[dict[str, Any]], prefix: str) -> bool:
    return any(str(d.get("status") or "").startswith(prefix) for d in divergences)


def _atr_bucket(distance: float | None, low: float, elevated: float, high: float) -> float:
    """Map ATR distance to 0..1 overextension intensity."""
    if distance is None:
        return 0.0
    d = abs(float(distance))
    if d < low:
        return 0.15 * (d / low if low else 0.0)
    if d < elevated:
        return 0.35 + 0.25 * ((d - low) / max(elevated - low, 1e-12))
    if d < high:
        return 0.60 + 0.25 * ((d - elevated) / max(high - elevated, 1e-12))
    return _clamp(0.85 + 0.15 * ((d - high) / max(high, 1e-12)), 0.0, 1.0)


def compute_trend_direction_score(
    *,
    ema: dict[str, Any],
    close_vs_ema_pct: dict[str, Any],
    di_spread: float | None,
    slopes: dict[str, Any],
    pivots: dict[str, Any],
    config: RegimeScannerConfig,
) -> dict[str, Any]:
    cfg = config
    align = _ema_alignment_sign(ema)
    close_sign = _close_vs_all_sign(close_vs_ema_pct)
    structure = _structure_bias(pivots)
    slope_frac = _slope_direction_fraction(slopes, ("20", "59", "200"), ("12", "48", "144"))
    if slope_frac is None:
        slope_comp = 0.0
    else:
        slope_comp = slope_frac * cfg.w_dir_slopes

    if di_spread is None:
        di_comp = 0.0
    else:
        scale = max(cfg.di_spread_strong, 1e-9)
        di_comp = _clamp(di_spread / scale, -1.0, 1.0) * cfg.w_dir_di_spread

    components = {
        "ema_alignment": align * cfg.w_dir_ema_alignment,
        "close_vs_ema": close_sign * cfg.w_dir_close_vs_ema,
        "di_spread": di_comp,
        "slope_consistency": slope_comp,
        "swing_structure": structure * cfg.w_dir_structure,
    }
    return _score_bundle(components, lo=-100.0, hi=100.0)


def compute_trend_strength_score(
    *,
    adx: float | None,
    di_spread: float | None,
    ema: dict[str, Any],
    bands: dict[str, Any],
    slopes: dict[str, Any],
    config: RegimeScannerConfig,
) -> dict[str, Any]:
    cfg = config
    if adx is None:
        adx_comp = 0.0
    elif adx >= cfg.adx_strong:
        adx_comp = cfg.w_str_adx
    elif adx >= cfg.adx_moderate:
        adx_comp = cfg.w_str_adx * 0.75
    elif adx >= cfg.adx_weak:
        adx_comp = cfg.w_str_adx * 0.45
    elif adx >= cfg.adx_very_weak:
        adx_comp = cfg.w_str_adx * 0.25
    else:
        adx_comp = cfg.w_str_adx * 0.1

    if di_spread is None:
        di_comp = 0.0
    else:
        di_comp = _clamp(abs(di_spread) / max(cfg.di_spread_strong, 1e-9), 0.0, 1.0) * cfg.w_str_di_spread

    align = abs(_ema_alignment_sign(ema))
    align_comp = align * cfg.w_str_ema_alignment

    band_exp = _band_expansion_bias(bands, "12")
    if band_exp is None:
        band_comp = 0.0
    else:
        band_comp = _clamp((band_exp + 1.0) / 2.0, 0.0, 1.0) * cfg.w_str_band_expansion

    slope_frac = _slope_direction_fraction(slopes, ("9", "20", "59", "200"), ("6", "12", "48"))
    if slope_frac is None:
        slope_comp = 0.0
    else:
        slope_comp = abs(slope_frac) * cfg.w_str_slope_consistency

    components = {
        "adx": adx_comp,
        "di_spread": di_comp,
        "ema_alignment": align_comp,
        "band_expansion": band_comp,
        "slope_consistency": slope_comp,
    }
    return _score_bundle(components, lo=0.0, hi=100.0)


def compute_trend_acceleration_score(
    *,
    slopes: dict[str, Any],
    bands: dict[str, Any],
    summary: dict[str, Any],
    config: RegimeScannerConfig,
) -> dict[str, Any]:
    cfg = config
    med = _slope_accel_fraction(slopes, ("20", "59"), ("12", "48"))
    lng = _slope_accel_fraction(slopes, ("200",), ("48", "144"))
    sht = _slope_accel_fraction(slopes, ("9",), ("3", "6", "12"))
    band = _band_expansion_bias(bands, "12")

    adx_change = summary.get("adx_change") or {}
    di_change = summary.get("di_spread_change") or {}

    def _chg_score(mapping: dict[str, Any]) -> float:
        vals = [mapping.get(k) for k in ("3", "6", "12")]
        score = 0.0
        n = 0
        for v in vals:
            if v == "rising":
                score += 1.0
                n += 1
            elif v == "falling":
                score -= 1.0
                n += 1
            elif v == "stable":
                n += 1
        return 0.0 if n == 0 else score / n

    adx_di = 0.5 * (_chg_score(adx_change) + _chg_score(di_change))

    components = {
        "medium_term_slopes": (0.0 if med is None else med) * cfg.w_acc_medium_slopes,
        "long_term_slopes": (0.0 if lng is None else lng) * cfg.w_acc_long_slopes,
        "band_expansion": (0.0 if band is None else band) * cfg.w_acc_bands,
        "adx_di_change": adx_di * cfg.w_acc_adx_di,
        "short_term_slopes": (0.0 if sht is None else sht) * cfg.w_acc_short_slopes,
    }
    return _score_bundle(components, lo=-100.0, hi=100.0)


def acceleration_label(score: float, *, short_decel_only: bool) -> AccelerationLabel:
    if not math.isfinite(score):
        return "unavailable"
    if score >= 15:
        return "accelerating"
    if score <= -15:
        # If only short-term decelerates while medium/long still positive, prefer mixed.
        if short_decel_only and score > -35:
            return "mixed"
        return "decelerating"
    if abs(score) < 5:
        return "steady"
    return "mixed"


def strength_label(adx: float | None, strength_score: float, config: RegimeScannerConfig) -> StrengthLabel:
    if adx is None and strength_score <= 0:
        return "unavailable"
    # Prefer ADX bands, but require some corroboration from strength_score.
    if adx is not None:
        if adx >= config.adx_strong and strength_score >= 55:
            return "very_strong"
        if adx >= config.adx_moderate and strength_score >= 45:
            return "strong"
        if adx >= config.adx_weak:
            return "moderate"
        if adx >= config.adx_very_weak:
            return "weak"
        return "very_weak"
    if strength_score >= 70:
        return "strong"
    if strength_score >= 45:
        return "moderate"
    if strength_score >= 25:
        return "weak"
    return "very_weak"


def compute_overextension_score(
    *,
    side: Literal["long", "short"],
    overextension: dict[str, Any],
    config: RegimeScannerConfig,
) -> dict[str, Any]:
    cfg = config
    atr_units = overextension.get("close_vs_ema_atr_units") or {}
    d9 = _finite(atr_units.get("ema_9"))
    d20 = _finite(atr_units.get("ema_20"))
    d59 = _finite(atr_units.get("ema_59"))
    d200 = _finite(atr_units.get("ema_200"))

    def _side_dist(value: float | None) -> float | None:
        if value is None:
            return None
        return value if side == "long" else -value

    s9, s20, s59, s200 = _side_dist(d9), _side_dist(d20), _side_dist(d59), _side_dist(d200)
    # Only count distances on the overextended side (positive after side transform).
    def _pos(v: float | None) -> float | None:
        if v is None:
            return None
        return v if v > 0 else 0.0

    c20 = _atr_bucket(_pos(s20), cfg.oe_ema20_low, cfg.oe_ema20_elevated, cfg.oe_ema20_high)
    c59 = _atr_bucket(_pos(s59), cfg.oe_ema59_low, cfg.oe_ema59_elevated, cfg.oe_ema59_high)
    c200 = _atr_bucket(_pos(s200), cfg.oe_ema200_normal, cfg.oe_ema200_elevated, cfg.oe_ema200_high)
    c9 = _atr_bucket(_pos(s9), 0.5, 1.0, 2.0)

    atr_means = overextension.get("atr_pct_vs_means") or {}
    ratio = _finite(((atr_means.get("48") or {}).get("ratio")))
    if ratio is None:
        atr_comp = 0.0
    elif ratio >= cfg.atr_pct_above_ratio:
        atr_comp = cfg.w_oe_atr_ratio * _clamp((ratio - 1.0) / 0.5, 0.0, 1.0)
    else:
        atr_comp = 0.0

    components = {
        "ema20_atr": c20 * cfg.w_oe_ema20,
        "ema59_atr": c59 * cfg.w_oe_ema59,
        "ema200_atr": c200 * cfg.w_oe_ema200,
        "ema9_atr": c9 * cfg.w_oe_ema9,
        "atr_pct_ratio": atr_comp,
    }
    return _score_bundle(components, lo=0.0, hi=100.0)


def compute_reversal_risk_score(
    *,
    side: Literal["long", "short"],
    divergences: list[dict[str, Any]],
    weakening_signals: list[dict[str, Any]],
    overextension_score: float,
    summary: dict[str, Any],
    bands: dict[str, Any],
    config: RegimeScannerConfig,
) -> dict[str, Any]:
    cfg = config
    if side == "long":
        div_hit = _has_confirmed_prefix(divergences, "confirmed_bearish")
    else:
        div_hit = _has_confirmed_prefix(divergences, "confirmed_bullish")
    div_comp = cfg.w_rev_divergence if div_hit else 0.0

    # Count relevant weakening signals.
    relevant = 0
    for item in weakening_signals:
        metric = str(item.get("metric") or "")
        if side == "long" and (
            metric.startswith("ema_")
            or metric in {"adx", "plus_di", "di_spread"}
            or "band" in metric
        ):
            relevant += 1
        if side == "short" and (
            metric.startswith("ema_")
            or metric in {"adx", "minus_di", "di_spread"}
            or "band" in metric
        ):
            relevant += 1
    weak_comp = _clamp(relevant / 5.0, 0.0, 1.0) * cfg.w_rev_weakening
    oe_comp = _clamp(overextension_score / 100.0, 0.0, 1.0) * cfg.w_rev_overextension

    adx_change = summary.get("adx_change") or {}
    di_change = summary.get("di_spread_change") or {}
    fall = 0
    for mapping in (adx_change, di_change):
        for key in ("3", "6", "12"):
            if mapping.get(key) == "falling":
                fall += 1
    adx_di_comp = _clamp(fall / 6.0, 0.0, 1.0) * cfg.w_rev_adx_di_fall

    band_bias = _band_expansion_bias(bands, "12")
    if band_bias is None:
        band_comp = 0.0
    elif band_bias < 0:
        band_comp = abs(band_bias) * cfg.w_rev_band_contract
    else:
        band_comp = 0.0

    components = {
        "confirmed_divergence": div_comp,
        "weakening_signals": weak_comp,
        "overextension": oe_comp,
        "adx_di_falling": adx_di_comp,
        "band_contraction": band_comp,
    }
    return _score_bundle(components, lo=0.0, hi=100.0)


def compute_data_quality_score(
    *,
    candles_used: int,
    warmup_sufficient: bool,
    ema: dict[str, Any],
    adx: float | None,
    di_spread: float | None,
    pivots: dict[str, Any],
    slopes: dict[str, Any],
    config: RegimeScannerConfig,
) -> dict[str, Any]:
    cfg = config
    warmup_comp = cfg.w_dq_warmup if warmup_sufficient else cfg.w_dq_warmup * min(
        candles_used / max(cfg.min_warmup_candles, 1), 1.0
    ) * 0.5

    required = [
        ema.get("ema_9"),
        ema.get("ema_20"),
        ema.get("ema_59"),
        ema.get("ema_200"),
        adx,
        di_spread,
    ]
    present = sum(1 for v in required if _finite(v) is not None)
    feature_comp = (present / len(required)) * cfg.w_dq_features

    high_n = int(pivots.get("high_count") or 0)
    low_n = int(pivots.get("low_count") or 0)
    swing_comp = cfg.w_dq_swings if (high_n >= 2 and low_n >= 2) else (
        cfg.w_dq_swings * 0.5 if (high_n + low_n) >= 2 else 0.0
    )

    slope_frac = _slope_direction_fraction(slopes, ("9", "20", "59", "200"), ("12", "48"))
    consistency = 0.0 if slope_frac is None else abs(slope_frac) * cfg.w_dq_consistency

    components = {
        "warmup": warmup_comp,
        "features_present": feature_comp,
        "confirmed_swings": swing_comp,
        "signal_consistency": consistency,
    }
    return _score_bundle(components, lo=0.0, hi=100.0)


def _entry_risk_label(score: float, *, available: bool, config: RegimeScannerConfig) -> EntryRisk:
    if not available:
        return "unavailable"
    if score >= config.risk_extreme:
        return "extreme"
    if score >= config.risk_high:
        return "high"
    if score >= config.risk_moderate:
        return "moderate"
    return "low"


def classify_regime_label(
    *,
    direction: float,
    strength: float,
    acceleration: float,
    accel_label: AccelerationLabel,
    strength_lbl: StrengthLabel,
    data_quality: float,
    bearish_div: bool,
    bullish_div: bool,
    config: RegimeScannerConfig,
) -> RegimeLabel:
    cfg = config
    if data_quality < cfg.min_data_quality_for_regime:
        return "transition"

    bullish = direction >= cfg.regime_trend_dir
    bearish = direction <= -cfg.regime_trend_dir
    strong_bull = direction >= cfg.regime_strong_dir and strength >= cfg.regime_strong_strength
    strong_bear = direction <= -cfg.regime_strong_dir and strength >= cfg.regime_strong_strength
    weakening = acceleration <= cfg.regime_weakening_acc or accel_label == "decelerating"

    if strong_bull and not weakening and strength_lbl in {"strong", "very_strong"} and not bearish_div:
        if accel_label in {"accelerating", "steady", "mixed"}:
            return "strong_bullish_expansion"
    if strong_bear and not weakening and strength_lbl in {"strong", "very_strong"} and not bullish_div:
        if accel_label in {"accelerating", "steady", "mixed"}:
            return "strong_bearish_expansion"

    if bullish and (weakening or bearish_div or accel_label == "decelerating"):
        return "bullish_weakening"
    if bearish and (weakening or bullish_div or accel_label == "decelerating"):
        return "bearish_weakening"

    if bullish and strength >= cfg.regime_trend_strength:
        return "bullish_trend"
    if bearish and strength >= cfg.regime_trend_strength:
        return "bearish_trend"

    if abs(direction) < cfg.regime_trend_dir and strength < cfg.regime_trend_strength:
        return "neutral_range"
    return "transition"


def compute_confidence(
    *,
    data_quality: float,
    direction: float,
    strength: float,
    conflicting: bool,
    config: RegimeScannerConfig,
) -> float:
    base = data_quality / 100.0
    separation = _clamp(abs(direction) / 100.0, 0.0, 1.0)
    strength_term = _clamp(strength / 100.0, 0.0, 1.0)
    conf = 0.45 * base + 0.30 * separation + 0.25 * strength_term
    if conflicting:
        conf *= 0.75
    if data_quality < config.min_data_quality_for_regime:
        conf *= 0.6
    return float(_clamp(conf, 0.0, 1.0))


def collect_reason_codes(
    *,
    ema: dict[str, Any],
    close_vs_ema_pct: dict[str, Any],
    adx: float | None,
    di_spread: float | None,
    slopes: dict[str, Any],
    bands: dict[str, Any],
    overextension: dict[str, Any],
    pivots: dict[str, Any],
    divergences: list[dict[str, Any]],
    weakening_signals: list[dict[str, Any]],
    summary: dict[str, Any],
    warmup_sufficient: bool,
    config: RegimeScannerConfig,
) -> list[dict[str, str]]:
    cfg = config
    codes: list[str] = []
    align = _ema_alignment_sign(ema)
    if align > 0:
        codes.append("EMA_FULL_BULLISH_ALIGNMENT")
    elif align < 0:
        codes.append("EMA_FULL_BEARISH_ALIGNMENT")
    else:
        codes.append("EMA_MIXED_ALIGNMENT")

    close_sign = _close_vs_all_sign(close_vs_ema_pct)
    if close_sign > 0:
        codes.append("CLOSE_ABOVE_ALL_EMAS")
    elif close_sign < 0:
        codes.append("CLOSE_BELOW_ALL_EMAS")

    if adx is not None:
        if adx >= cfg.adx_strong:
            codes.append("ADX_VERY_STRONG")
        elif adx >= cfg.adx_moderate:
            codes.append("ADX_STRONG")
        elif adx >= cfg.adx_weak:
            codes.append("ADX_MODERATE")
        else:
            codes.append("ADX_WEAK")

    adx_change = summary.get("adx_change") or {}
    if any(adx_change.get(k) == "rising" for k in ("3", "6", "12")):
        codes.append("ADX_RISING")
    if any(adx_change.get(k) == "falling" for k in ("3", "6", "12")):
        codes.append("ADX_FALLING")

    if di_spread is not None:
        if di_spread >= cfg.di_spread_strong:
            codes.append("DI_SPREAD_STRONGLY_POSITIVE")
        elif di_spread <= -cfg.di_spread_strong:
            codes.append("DI_SPREAD_STRONGLY_NEGATIVE")
        elif abs(di_spread) <= cfg.di_spread_near_zero:
            codes.append("DI_SPREAD_NEAR_ZERO")

    med = _slope_accel_fraction(slopes, ("20", "59"), ("12", "48"))
    lng = _slope_accel_fraction(slopes, ("200",), ("48", "144"))
    sht = _slope_accel_fraction(slopes, ("9",), ("3", "6", "12"))
    if med is not None and med > 0:
        codes.append("MEDIUM_TERM_SLOPES_ACCELERATING")
    if med is not None and med < 0:
        codes.append("MEDIUM_TERM_SLOPES_DECELERATING")
    if lng is not None and lng > 0:
        codes.append("LONG_TERM_SLOPES_ACCELERATING")
    if sht is not None and sht < 0:
        codes.append("SHORT_TERM_SLOPE_DECELERATION")

    band = _band_expansion_bias(bands, "12")
    if band is not None and band > 0:
        codes.append("EMA_BANDS_EXPANDING")
    if band is not None and band < 0:
        codes.append("EMA_BANDS_CONTRACTING")

    structure = _structure_bias(pivots)
    if structure > 0:
        codes.append("HH_HL_STRUCTURE")
    elif structure < 0:
        codes.append("LH_LL_STRUCTURE")

    atr_units = overextension.get("close_vs_ema_atr_units") or {}
    d20 = _finite(atr_units.get("ema_20"))
    d59 = _finite(atr_units.get("ema_59"))
    d200 = _finite(atr_units.get("ema_200"))
    if d20 is not None and d20 > cfg.oe_ema20_high:
        codes.append("CLOSE_OVER_3_ATR_ABOVE_EMA20")
    if d59 is not None and d59 > cfg.oe_ema59_high:
        codes.append("CLOSE_OVER_6_ATR_ABOVE_EMA59")
    if d200 is not None and d200 > cfg.oe_ema200_high:
        codes.append("CLOSE_OVER_10_ATR_ABOVE_EMA200")
    if d20 is not None and d20 < -cfg.oe_ema20_high:
        codes.append("CLOSE_OVER_3_ATR_BELOW_EMA20")
    if d200 is not None and d200 < -cfg.oe_ema200_high:
        codes.append("CLOSE_OVER_10_ATR_BELOW_EMA200")

    if _has_confirmed_prefix(divergences, "confirmed_bearish"):
        codes.append("CONFIRMED_BEARISH_DIVERGENCE")
    else:
        codes.append("NO_CONFIRMED_BEARISH_DIVERGENCE")
    if _has_confirmed_prefix(divergences, "confirmed_bullish"):
        codes.append("CONFIRMED_BULLISH_DIVERGENCE")
    else:
        codes.append("NO_CONFIRMED_BULLISH_DIVERGENCE")

    atr_means = overextension.get("atr_pct_vs_means") or {}
    if any((atr_means.get(k) or {}).get("label") == "above_recent_volatility" for k in ("12", "48", "144")):
        codes.append("HIGH_VOLATILITY_EXPANSION")
    if weakening_signals:
        codes.append("WEAKENING_SIGNALS_PRESENT")
    if not warmup_sufficient:
        codes.append("INSUFFICIENT_HISTORY")

    # Deduplicate preserving order.
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "explanation": REASON_CODE_HELP.get(code, code)})
    return out


def classify_market_state(
    *,
    candles_used: int,
    warmup_sufficient: bool,
    ema: dict[str, Any],
    ema_order: str | None,
    close_vs_ema_pct: dict[str, Any],
    atr: float | None,
    atr_pct: float | None,
    plus_di: float | None,
    minus_di: float | None,
    di_spread: float | None,
    adx: float | None,
    ema_slope_comparisons: dict[str, Any],
    ema_bands: dict[str, Any],
    overextension: dict[str, Any],
    confirmed_pivots: dict[str, Any],
    confirmed_divergences: list[dict[str, Any]],
    weakening_signals: list[dict[str, Any]],
    summary: dict[str, Any],
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Classify regime and directional entry risk from already-computed features."""
    cfg = config or default_regime_scanner_config()
    _ = (atr, atr_pct, plus_di, minus_di, ema_order)  # reserved for reason enrichment / future use

    direction = compute_trend_direction_score(
        ema=ema,
        close_vs_ema_pct=close_vs_ema_pct,
        di_spread=di_spread,
        slopes=ema_slope_comparisons,
        pivots=confirmed_pivots,
        config=cfg,
    )
    strength = compute_trend_strength_score(
        adx=adx,
        di_spread=di_spread,
        ema=ema,
        bands=ema_bands,
        slopes=ema_slope_comparisons,
        config=cfg,
    )
    acceleration = compute_trend_acceleration_score(
        slopes=ema_slope_comparisons,
        bands=ema_bands,
        summary=summary,
        config=cfg,
    )
    data_quality = compute_data_quality_score(
        candles_used=candles_used,
        warmup_sufficient=warmup_sufficient,
        ema=ema,
        adx=adx,
        di_spread=di_spread,
        pivots=confirmed_pivots,
        slopes=ema_slope_comparisons,
        config=cfg,
    )

    short_accel = _slope_accel_fraction(ema_slope_comparisons, ("9",), ("3", "6", "12"))
    med_accel = _slope_accel_fraction(ema_slope_comparisons, ("20", "59"), ("12", "48"))
    long_accel = _slope_accel_fraction(ema_slope_comparisons, ("200",), ("48", "144"))
    short_decel_only = (
        short_accel is not None
        and short_accel < 0
        and (med_accel is None or med_accel >= 0)
        and (long_accel is None or long_accel >= 0)
    )
    accel_lbl = acceleration_label(acceleration["score"], short_decel_only=short_decel_only)
    strength_lbl = strength_label(adx, strength["score"], cfg)

    oe_long = compute_overextension_score(side="long", overextension=overextension, config=cfg)
    oe_short = compute_overextension_score(side="short", overextension=overextension, config=cfg)
    rev_long = compute_reversal_risk_score(
        side="long",
        divergences=confirmed_divergences,
        weakening_signals=weakening_signals,
        overextension_score=oe_long["score"],
        summary=summary,
        bands=ema_bands,
        config=cfg,
    )
    rev_short = compute_reversal_risk_score(
        side="short",
        divergences=confirmed_divergences,
        weakening_signals=weakening_signals,
        overextension_score=oe_short["score"],
        summary=summary,
        bands=ema_bands,
        config=cfg,
    )

    bearish_div = _has_confirmed_prefix(confirmed_divergences, "confirmed_bearish")
    bullish_div = _has_confirmed_prefix(confirmed_divergences, "confirmed_bullish")
    regime = classify_regime_label(
        direction=direction["score"],
        strength=strength["score"],
        acceleration=acceleration["score"],
        accel_label=accel_lbl,
        strength_lbl=strength_lbl,
        data_quality=data_quality["score"],
        bearish_div=bearish_div,
        bullish_div=bullish_div,
        config=cfg,
    )

    # Entry risk combines overextension + reversal + counter-trend penalty.
    # Overextension dominates same-direction risk so a strong trend can still
    # show high long-entry risk when price is stretched in ATR terms.
    long_raw = 0.70 * oe_long["score"] + 0.30 * rev_long["score"]
    short_raw = 0.70 * oe_short["score"] + 0.30 * rev_short["score"]
    if regime in {
        "strong_bullish_expansion",
        "bullish_trend",
        "bullish_weakening",
    }:
        # Counter-trend short entries are structurally risky in bull regimes.
        if regime == "strong_bullish_expansion":
            short_raw = max(short_raw + 45.0, cfg.risk_extreme)
        else:
            short_raw = max(short_raw + 35.0, cfg.risk_high)
        if regime == "bullish_weakening" or bearish_div:
            long_raw = max(long_raw + 25.0, cfg.risk_high if bearish_div else long_raw + 25.0)
            if bearish_div:
                long_raw = max(long_raw, cfg.risk_extreme)
    if regime in {
        "strong_bearish_expansion",
        "bearish_trend",
        "bearish_weakening",
    }:
        if regime == "strong_bearish_expansion":
            long_raw = max(long_raw + 45.0, cfg.risk_extreme)
        else:
            long_raw = max(long_raw + 35.0, cfg.risk_high)
        if regime == "bearish_weakening" or bullish_div:
            short_raw = max(short_raw + 25.0, cfg.risk_high if bullish_div else short_raw + 25.0)
            if bullish_div:
                short_raw = max(short_raw, cfg.risk_extreme)

    long_raw = _clamp(long_raw, 0.0, 100.0)
    short_raw = _clamp(short_raw, 0.0, 100.0)

    available = data_quality["score"] >= cfg.min_data_quality_for_regime * 0.5
    long_risk = _entry_risk_label(long_raw, available=available, config=cfg)
    short_risk = _entry_risk_label(short_raw, available=available, config=cfg)

    conflicting = (
        (_ema_alignment_sign(ema) == 0)
        or (abs(direction["score"]) < cfg.regime_trend_dir and strength["score"] >= cfg.regime_trend_strength)
        or accel_lbl == "mixed"
    )
    confidence = compute_confidence(
        data_quality=data_quality["score"],
        direction=direction["score"],
        strength=strength["score"],
        conflicting=conflicting,
        config=cfg,
    )

    reason_codes = collect_reason_codes(
        ema=ema,
        close_vs_ema_pct=close_vs_ema_pct,
        adx=adx,
        di_spread=di_spread,
        slopes=ema_slope_comparisons,
        bands=ema_bands,
        overextension=overextension,
        pivots=confirmed_pivots,
        divergences=confirmed_divergences,
        weakening_signals=weakening_signals,
        summary=summary,
        warmup_sufficient=warmup_sufficient,
        config=cfg,
    )
    if regime.startswith("bull"):
        reason_codes.append(
            {
                "code": "TREND_ALIGNED_ENTRY",
                "explanation": REASON_CODE_HELP["TREND_ALIGNED_ENTRY"] + " for long bias",
            }
        )
        reason_codes.append(
            {
                "code": "COUNTER_TREND_ENTRY",
                "explanation": REASON_CODE_HELP["COUNTER_TREND_ENTRY"] + " for short bias",
            }
        )
    elif regime.startswith("bear"):
        reason_codes.append(
            {
                "code": "TREND_ALIGNED_ENTRY",
                "explanation": REASON_CODE_HELP["TREND_ALIGNED_ENTRY"] + " for short bias",
            }
        )
        reason_codes.append(
            {
                "code": "COUNTER_TREND_ENTRY",
                "explanation": REASON_CODE_HELP["COUNTER_TREND_ENTRY"] + " for long bias",
            }
        )

    primary_reasons = [item["explanation"] for item in reason_codes[:12]]

    return {
        "regime": regime,
        "confidence": confidence,
        "trend_strength_label": strength_lbl,
        "acceleration_label": accel_lbl,
        "long_entry_risk": long_risk,
        "short_entry_risk": short_risk,
        "long_entry_risk_score": float(_clamp(long_raw, 0.0, 100.0)),
        "short_entry_risk_score": float(_clamp(short_raw, 0.0, 100.0)),
        "scores": {
            "trend_direction_score": direction,
            "trend_strength_score": strength,
            "trend_acceleration_score": acceleration,
            "overextension_score_long": oe_long,
            "overextension_score_short": oe_short,
            "reversal_risk_score_long": rev_long,
            "reversal_risk_score_short": rev_short,
            "data_quality_score": data_quality,
        },
        "reason_codes": reason_codes,
        "primary_reasons": primary_reasons,
        "notes": {
            "thresholds": "Scanner-v1 heuristics only; not trading advice or final strategy rules.",
            "separation": (
                "Regime describes market state; entry risk is a separate new-entry overextension/"
                "reversal assessment and may be high even in a strong trend."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Simple human-readable regime summary (final layer; keeps technical signal names)
# ---------------------------------------------------------------------------

SimpleRegime = Literal[
    "strong_bullish_trend",
    "bullish_trend",
    "bullish_trend_with_trend_weakness",
    "neutral",
    "transition",
    "bearish_trend",
    "bearish_trend_with_trend_weakness",
    "strong_bearish_trend",
    "unavailable",
]

SIMPLE_REASON_HELP: dict[str, str] = {
    "BULLISH_TREND_INTACT": "Bullish trend structure remains intact",
    "BEARISH_TREND_INTACT": "Bearish trend structure remains intact",
    "FULL_BULLISH_EMA_ALIGNMENT": "EMA9 > EMA20 > EMA59 > EMA200",
    "FULL_BEARISH_EMA_ALIGNMENT": "EMA9 < EMA20 < EMA59 < EMA200",
    "STRONG_POSITIVE_DI_SPREAD": "DI-spread is clearly positive",
    "STRONG_NEGATIVE_DI_SPREAD": "DI-spread is clearly negative",
    "MULTI_TIMEFRAME_TREND_WEAKNESS": "Trend weakness appears across multiple timeframes",
    "DEVELOPING_EQUAL_HIGH_EXHAUSTION": "Developing equal-high / retest exhaustion is present",
    "CONFIRMED_EQUAL_HIGH_EXHAUSTION": "Confirmed equal-high / retest exhaustion is present",
    "MULTI_METRIC_EXHAUSTION": "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION (or mirror) is present",
    "LAST_BAR_MOMENTUM_ROLLOVER": "Last-bar momentum / volatility rollover is present",
    "NO_STRUCTURAL_WEAKNESS": "No structural exhaustion or confirmed reverse divergence",
    "MIXED_TREND_SIGNALS": "EMA / DI / slope signals are mixed or transitional",
    "INSUFFICIENT_DATA": "Warmup or feature coverage is insufficient",
}


def _reason(code: str) -> dict[str, str]:
    return {"code": code, "explanation": SIMPLE_REASON_HELP.get(code, code)}


def _slope_positive(slopes: dict[str, Any], key: str) -> bool | None:
    value = _finite((slopes or {}).get(key))
    if value is None:
        return None
    return value > 0.0


def _slope_negative(slopes: dict[str, Any], key: str) -> bool | None:
    value = _finite((slopes or {}).get(key))
    if value is None:
        return None
    return value < 0.0


def _band_contracting(bands: dict[str, Any]) -> bool:
    for item in (bands or {}).values():
        for win in ((item or {}).get("windows") or {}).values():
            if str((win or {}).get("status") or "").lower() == "contracting":
                return True
    return False


def _signal_codes(payload: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    retest = payload.get("retest_high_candidate") or payload.get(
        "developing_structural_exhaustion"
    )
    if isinstance(retest, dict):
        for item in retest.get("signals") or []:
            code = str(item.get("code") or "")
            if code:
                codes.add(code)
    for bucket in (
        "equal_high_retest_exhaustion",
        "lower_high_momentum_weakness",
        "classic_pivot_divergence",
    ):
        for item in payload.get(bucket) or []:
            for sig in item.get("signals") or []:
                code = str(sig.get("code") or "")
                if code:
                    codes.add(code)
    for item in (payload.get("last_bar_rollover") or {}).get("signals") or []:
        code = str(item.get("metric") or item.get("code") or "")
        if code:
            codes.add(code)
    for item in payload.get("weakening_signals") or []:
        metric = str(item.get("metric") or "")
        if "LAST_BAR_ROLLOVER" in metric:
            codes.add(metric)
    return codes


def _has_developing_equal_high(payload: dict[str, Any]) -> bool:
    item = payload.get("developing_structural_exhaustion")
    if not isinstance(item, dict):
        return False
    status = str(item.get("confirmation_status") or "")
    return status == "developing_equal_high_exhaustion" or (
        (item.get("structure") or {}).get("structure_type") == "equal_high_exhaustion"
        and not item.get("is_confirmed_pivot")
    )


def _has_confirmed_equal_high(payload: dict[str, Any]) -> bool:
    for item in payload.get("equal_high_retest_exhaustion") or []:
        status = str(item.get("confirmation_status") or "")
        if "confirmed_equal_high" in status:
            return True
        if (item.get("structure") or {}).get("structure_type") == "equal_high_exhaustion":
            if item.get("is_confirmed_pivot") or status.startswith("confirmed_"):
                return True
    return False


def _has_lower_high_weakness(payload: dict[str, Any]) -> bool:
    if payload.get("lower_high_momentum_weakness"):
        return True
    item = payload.get("developing_structural_exhaustion")
    if isinstance(item, dict):
        st = (item.get("structure") or {}).get("structure_type")
        if st == "lower_high_momentum_weakness":
            return True
    return False


def _has_confirmed_bearish_divergence(payload: dict[str, Any]) -> bool:
    for item in payload.get("confirmed_divergences") or []:
        status = str(item.get("status") or "")
        if status.startswith("confirmed_bearish"):
            return True
    multi = payload.get("confirmed_multi_metric_divergences") or {}
    return bool(multi.get("confirmed_bearish"))


def _has_confirmed_bullish_divergence(payload: dict[str, Any]) -> bool:
    for item in payload.get("confirmed_divergences") or []:
        status = str(item.get("status") or "")
        if status.startswith("confirmed_bullish"):
            return True
    multi = payload.get("confirmed_multi_metric_divergences") or {}
    return bool(multi.get("confirmed_bullish"))


def _has_confirmed_bearish_reversal(payload: dict[str, Any]) -> bool:
    """Confirmed bearish structure flip — not equal-high exhaustion alone."""
    multi = payload.get("confirmed_multi_metric_divergences") or {}
    if multi.get("confirmed_bearish"):
        # Full multi-metric HH bearish divergence counts as structure reversal evidence.
        return True
    for item in payload.get("confirmed_divergences") or []:
        if item.get("status") == "confirmed_bearish_divergence" and item.get("indicator") == "adx":
            # Single-indicator alone is weaker; still count classic ADX HH divergence.
            return True
    return False


def _momentum_weakness_families(payload: dict[str, Any], *, side: str) -> list[str]:
    """Count weakened momentum/volatility families (ADX, ATR|ATR%, +DI, DI, slopes, bands)."""
    families: list[str] = []
    codes = _signal_codes(payload)
    retest = payload.get("retest_high_candidate") or payload.get(
        "developing_structural_exhaustion"
    )
    comps = (retest or {}).get("indicator_comparisons") if isinstance(retest, dict) else None

    def _weak_comp(metric: str) -> bool:
        if not comps:
            return False
        return bool((comps.get(metric) or {}).get("weakening"))

    if side == "bullish":
        if _weak_comp("adx") or "ADX_EQUAL_HIGH_EXHAUSTION" in codes or "ADX_LOWER_HIGH_EXHAUSTION" in codes:
            families.append("adx")
        if (
            _weak_comp("atr")
            or _weak_comp("atr_pct")
            or "ATR_EQUAL_HIGH_EXHAUSTION" in codes
            or "ATR_PERCENT_EQUAL_HIGH_EXHAUSTION" in codes
            or "ATR_LOWER_HIGH_EXHAUSTION" in codes
            or "ATR_PERCENT_LOWER_HIGH_EXHAUSTION" in codes
        ):
            families.append("atr_family")
        if _weak_comp("plus_di") or "PLUS_DI_EQUAL_HIGH_EXHAUSTION" in codes:
            families.append("plus_di")
        if _weak_comp("di_spread") or "DI_SPREAD_EQUAL_HIGH_EXHAUSTION" in codes:
            families.append("di_spread")
    else:
        # Bearish trend weakness uses mirror signal codes when present.
        if _weak_comp("adx"):
            families.append("adx")
        if _weak_comp("atr") or _weak_comp("atr_pct"):
            families.append("atr_family")
        if _weak_comp("plus_di"):
            families.append("plus_di")
        if _weak_comp("di_spread"):
            families.append("di_spread")

    slopes = payload.get("ema_slopes_pct") or {}
    short_keys = ("ema_9_slope_3_pct", "ema_9_slope_6_pct", "ema_20_slope_6_pct")
    if side == "bullish":
        if any(_slope_negative(slopes, k) for k in short_keys):
            families.append("short_ema_slopes")
    else:
        if any(_slope_positive(slopes, k) for k in short_keys):
            families.append("short_ema_slopes")

    if _band_contracting(payload.get("ema_bands") or {}):
        families.append("ema_band_contraction")

    # Deduplicate while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for name in families:
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out


def _bullish_trend_intact(payload: dict[str, Any], cfg: RegimeScannerConfig) -> tuple[bool, list[dict[str, str]]]:
    reasons: list[dict[str, str]] = []
    checks = 0
    hits = 0

    ema_order = str(payload.get("ema_order") or "")
    if ema_order == "EMA9 > EMA20 > EMA59 > EMA200":
        hits += 1
        reasons.append(_reason("FULL_BULLISH_EMA_ALIGNMENT"))
    checks += 1

    cvs = payload.get("close_vs_ema_pct") or {}
    above = [
        _finite(cvs.get("close_vs_ema_20_pct")),
        _finite(cvs.get("close_vs_ema_59_pct")),
        _finite(cvs.get("close_vs_ema_200_pct")),
    ]
    if all(v is not None for v in above):
        checks += 1
        if all(v is not None and v > 0 for v in above):
            hits += 1

    di = _finite(payload.get("di_spread"))
    if di is not None:
        checks += 1
        if di > 0:
            hits += 1
            if di >= cfg.di_spread_strong:
                reasons.append(_reason("STRONG_POSITIVE_DI_SPREAD"))

    slopes = payload.get("ema_slopes_pct") or {}
    slope_flags = [
        _slope_positive(slopes, "ema_20_slope_12_pct"),
        _slope_positive(slopes, "ema_59_slope_48_pct"),
        _slope_positive(slopes, "ema_200_slope_48_pct"),
    ]
    known = [f for f in slope_flags if f is not None]
    if known:
        checks += 1
        if all(known) and len(known) >= 2:
            hits += 1

    if _has_confirmed_bearish_reversal(payload):
        return False, reasons

    intact = checks > 0 and hits >= max(2, (checks + 1) // 2) and hits >= 3
    if intact:
        reasons.insert(0, _reason("BULLISH_TREND_INTACT"))
    return intact, reasons


def _bearish_trend_intact(payload: dict[str, Any], cfg: RegimeScannerConfig) -> tuple[bool, list[dict[str, str]]]:
    reasons: list[dict[str, str]] = []
    checks = 0
    hits = 0

    ema_order = str(payload.get("ema_order") or "")
    if ema_order == "EMA9 < EMA20 < EMA59 < EMA200":
        hits += 1
        reasons.append(_reason("FULL_BEARISH_EMA_ALIGNMENT"))
    checks += 1

    cvs = payload.get("close_vs_ema_pct") or {}
    below = [
        _finite(cvs.get("close_vs_ema_20_pct")),
        _finite(cvs.get("close_vs_ema_59_pct")),
        _finite(cvs.get("close_vs_ema_200_pct")),
    ]
    if all(v is not None for v in below):
        checks += 1
        if all(v is not None and v < 0 for v in below):
            hits += 1

    di = _finite(payload.get("di_spread"))
    if di is not None:
        checks += 1
        if di < 0:
            hits += 1
            if di <= -cfg.di_spread_strong:
                reasons.append(_reason("STRONG_NEGATIVE_DI_SPREAD"))

    slopes = payload.get("ema_slopes_pct") or {}
    slope_flags = [
        _slope_negative(slopes, "ema_20_slope_12_pct"),
        _slope_negative(slopes, "ema_59_slope_48_pct"),
        _slope_negative(slopes, "ema_200_slope_48_pct"),
    ]
    known = [f for f in slope_flags if f is not None]
    if known:
        checks += 1
        if all(known) and len(known) >= 2:
            hits += 1

    # Confirmed bullish divergences are handled as trend-weakness evidence, not
    # as an automatic invalidation of an otherwise intact bearish base trend.
    intact = checks > 0 and hits >= max(2, (checks + 1) // 2) and hits >= 3
    if intact:
        reasons.insert(0, _reason("BEARISH_TREND_INTACT"))
    return intact, reasons


def _structural_weakness_bullish(payload: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    reasons: list[dict[str, str]] = []
    found = False
    if _has_developing_equal_high(payload):
        found = True
        reasons.append(_reason("DEVELOPING_EQUAL_HIGH_EXHAUSTION"))
    if _has_confirmed_equal_high(payload):
        found = True
        reasons.append(_reason("CONFIRMED_EQUAL_HIGH_EXHAUSTION"))
    if _has_confirmed_bearish_divergence(payload):
        found = True
    if _has_lower_high_weakness(payload):
        found = True
    codes = _signal_codes(payload)
    if "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" in codes or "MULTI_METRIC_LOWER_HIGH_MOMENTUM_WEAKNESS" in codes:
        found = True
        reasons.append(_reason("MULTI_METRIC_EXHAUSTION"))
    return found, reasons


def _structural_weakness_bearish(payload: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    reasons: list[dict[str, str]] = []
    found = False
    # Mirror using confirmed bullish divergences / equal-low style signals if present.
    if _has_confirmed_bullish_divergence(payload):
        found = True
    developing = payload.get("developing_structural_exhaustion")
    if isinstance(developing, dict) and "equal_low" in str(developing.get("confirmation_status") or ""):
        found = True
    return found, reasons


def _has_last_bar_rollover(payload: dict[str, Any]) -> bool:
    rollover = payload.get("last_bar_rollover") or {}
    return any(
        bool(rollover.get(key))
        for key in (
            "adx_last_bar_rollover",
            "plus_di_last_bar_rollover",
            "di_spread_last_bar_rollover",
            "atr_pct_last_bar_rollover",
            "multi_metric_last_bar_rollover",
        )
    )


def summarize_timeframe_regime(
    payload: dict[str, Any],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Classify one timeframe into a simple human-readable regime name."""
    cfg = config or default_regime_scanner_config()
    timeframe = str(payload.get("timeframe") or "")
    if not payload or payload.get("warmup_sufficient") is False and int(payload.get("candles_loaded") or 0) < 20:
        if int(payload.get("candles_loaded") or 0) <= 0 or payload.get("ema") is None:
            return {
                "timeframe": timeframe,
                "regime": "unavailable",
                "confidence": "low",
                "reason_codes": [_reason("INSUFFICIENT_DATA")],
                "primary_reasons": ["Insufficient data for regime summary"],
                "bullish_trend_intact": False,
                "bearish_trend_intact": False,
                "structural_weakness": False,
                "momentum_weakness_families": [],
            }

    if payload.get("warmup_sufficient") is False and _finite((payload.get("ema") or {}).get("ema_200")) is None:
        # Still allow classification if core EMAs exist; only mark unavailable when empty.
        pass

    codes = _signal_codes(payload)
    bull_intact, bull_reasons = _bullish_trend_intact(payload, cfg)
    bear_intact, bear_reasons = _bearish_trend_intact(payload, cfg)
    struct_bull, struct_reasons = _structural_weakness_bullish(payload)
    struct_bear, struct_bear_reasons = _structural_weakness_bearish(payload)
    mom_bull = _momentum_weakness_families(payload, side="bullish")
    mom_bear = _momentum_weakness_families(payload, side="bearish")
    multi_metric = "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" in codes or (
        "MULTI_METRIC_LOWER_HIGH_MOMENTUM_WEAKNESS" in codes
    )
    last_bar = _has_last_bar_rollover(payload)

    reason_codes: list[dict[str, str]] = []
    primary: list[str] = []
    regime: SimpleRegime

    # Strong evidence shortcut: multi-metric exhaustion forces trend-weakness label
    # when the bullish base trend is still intact.
    if bull_intact and (struct_bull or multi_metric) and (len(mom_bull) >= 2 or multi_metric):
        regime = "bullish_trend_with_trend_weakness"
        reason_codes.extend(bull_reasons)
        reason_codes.extend(struct_reasons)
        if multi_metric and not any(r["code"] == "MULTI_METRIC_EXHAUSTION" for r in reason_codes):
            reason_codes.append(_reason("MULTI_METRIC_EXHAUSTION"))
        if last_bar:
            reason_codes.append(_reason("LAST_BAR_MOMENTUM_ROLLOVER"))
        primary = [
            "Bullish trend remains intact",
            "Structural exhaustion / weaker momentum is present",
        ]
    elif bear_intact and (struct_bear or len(mom_bear) >= 2) and len(mom_bear) >= 2:
        regime = "bearish_trend_with_trend_weakness"
        reason_codes.extend(bear_reasons)
        reason_codes.extend(struct_bear_reasons)
        primary = [
            "Bearish trend remains intact",
            "Structural / momentum weakness against the bearish trend is present",
        ]
    elif bull_intact and not struct_bull and not multi_metric:
        adx = _finite(payload.get("adx"))
        di = _finite(payload.get("di_spread"))
        slopes = payload.get("ema_slopes_pct") or {}
        med_long_ok = all(
            flag is True
            for flag in (
                _slope_positive(slopes, "ema_20_slope_12_pct"),
                _slope_positive(slopes, "ema_59_slope_48_pct"),
                _slope_positive(slopes, "ema_200_slope_48_pct"),
            )
            if flag is not None
        )
        strong = (
            adx is not None
            and adx >= cfg.adx_strong
            and di is not None
            and di >= cfg.di_spread_strong
            and med_long_ok
        )
        if strong:
            regime = "strong_bullish_trend"
            reason_codes.extend(bull_reasons)
            reason_codes.append(_reason("NO_STRUCTURAL_WEAKNESS"))
            primary = ["Strong bullish trend without structural weakness"]
        else:
            regime = "bullish_trend"
            reason_codes.extend(bull_reasons)
            if not struct_bull:
                reason_codes.append(_reason("NO_STRUCTURAL_WEAKNESS"))
            # Lone last-bar rollover does not upgrade to trend-weakness.
            if last_bar:
                reason_codes.append(_reason("LAST_BAR_MOMENTUM_ROLLOVER"))
            primary = ["Bullish trend intact"]
    elif bear_intact and not struct_bear:
        adx = _finite(payload.get("adx"))
        di = _finite(payload.get("di_spread"))
        slopes = payload.get("ema_slopes_pct") or {}
        med_long_ok = all(
            flag is True
            for flag in (
                _slope_negative(slopes, "ema_20_slope_12_pct"),
                _slope_negative(slopes, "ema_59_slope_48_pct"),
                _slope_negative(slopes, "ema_200_slope_48_pct"),
            )
            if flag is not None
        )
        strong = (
            adx is not None
            and adx >= cfg.adx_strong
            and di is not None
            and di <= -cfg.di_spread_strong
            and med_long_ok
        )
        regime = "strong_bearish_trend" if strong else "bearish_trend"
        reason_codes.extend(bear_reasons)
        reason_codes.append(_reason("NO_STRUCTURAL_WEAKNESS"))
        primary = ["Bearish trend intact"]
    else:
        ema_order = str(payload.get("ema_order") or "")
        if "mixed" in ema_order.lower() or ">" in ema_order and "<" in ema_order.replace("EMA", ""):
            regime = "transition"
            reason_codes.append(_reason("MIXED_TREND_SIGNALS"))
            primary = ["Mixed EMA / trend signals"]
        elif bull_intact and bear_intact:
            regime = "transition"
            reason_codes.append(_reason("MIXED_TREND_SIGNALS"))
            primary = ["Conflicting bullish and bearish intact signals"]
        elif not bull_intact and not bear_intact:
            # Distinguish transition vs neutral via DI-spread / ADX.
            di = _finite(payload.get("di_spread"))
            adx = _finite(payload.get("adx"))
            if (
                di is not None
                and abs(di) < cfg.di_spread_near_zero
                and (adx is None or adx < cfg.adx_weak)
            ):
                regime = "neutral"
                primary = ["No clear directional trend"]
            else:
                regime = "transition"
                reason_codes.append(_reason("MIXED_TREND_SIGNALS"))
                primary = ["Transitional / incomplete trend alignment"]
        else:
            regime = "transition"
            reason_codes.append(_reason("MIXED_TREND_SIGNALS"))
            primary = ["Transitional regime"]

    # Deduplicate reason codes.
    dedup: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in reason_codes:
        code = item["code"]
        if code in seen:
            continue
        seen.add(code)
        dedup.append(item)

    confidence = "medium"
    if regime in {"strong_bullish_trend", "strong_bearish_trend"}:
        confidence = "high"
    if regime.endswith("with_trend_weakness") and multi_metric:
        confidence = "high"
    if regime in {"transition", "neutral", "unavailable"}:
        confidence = "low"

    return {
        "timeframe": timeframe,
        "regime": regime,
        "confidence": confidence,
        "reason_codes": dedup,
        "primary_reasons": primary,
        "bullish_trend_intact": bull_intact,
        "bearish_trend_intact": bear_intact,
        "structural_weakness": struct_bull if bull_intact else struct_bear,
        "momentum_weakness_families": mom_bull if bull_intact else mom_bear,
        "multi_metric_exhaustion": multi_metric,
        "last_bar_rollover": last_bar,
    }


def combine_timeframe_regimes(
    by_timeframe: dict[str, dict[str, Any]],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Combine 5m/15m/30m summaries. 15m is primary structure; 5m timing; 30m confirmation."""
    cfg = config or default_regime_scanner_config()
    per_tf: dict[str, dict[str, Any]] = {}
    for tf, payload in by_timeframe.items():
        per_tf[tf] = summarize_timeframe_regime(payload, config=cfg)

    if not per_tf:
        return {
            "regime": "unavailable",
            "confidence": "low",
            "reason_codes": [_reason("INSUFFICIENT_DATA")],
            "primary_reasons": ["No timeframe payloads available"],
            "by_timeframe": {},
        }

    primary_tf = "15m" if "15m" in per_tf else next(iter(per_tf))
    primary = per_tf[primary_tf]
    tf5 = per_tf.get("5m")
    tf30 = per_tf.get("30m")

    regime: SimpleRegime = primary["regime"]  # type: ignore[assignment]
    reason_codes = list(primary.get("reason_codes") or [])
    primary_reasons = list(primary.get("primary_reasons") or [])

    payload15 = by_timeframe.get("15m") or {}
    codes15 = _signal_codes(payload15)
    multi15 = "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" in codes15
    weak5 = bool(tf5 and (tf5.get("last_bar_rollover") or tf5.get("structural_weakness")))
    weak30 = bool(
        tf30
        and (
            tf30.get("structural_weakness")
            or tf30.get("multi_metric_exhaustion")
            or (
                tf30.get("momentum_weakness_families")
                and len(tf30.get("momentum_weakness_families") or []) >= 1
            )
        )
    )
    # 30m weaker +DI / DI-spread on retest counts even if TF regime stayed bullish_trend.
    if "30m" in by_timeframe:
        retest30 = (by_timeframe["30m"].get("retest_high_candidate") or {})
        comps = retest30.get("indicator_comparisons") or {}
        if (comps.get("plus_di") or {}).get("weakening") or (comps.get("di_spread") or {}).get(
            "weakening"
        ):
            weak30 = True

    bull_base = bool(primary.get("bullish_trend_intact")) or any(
        s.get("bullish_trend_intact") for s in per_tf.values()
    )

    if bull_base and multi15 and (weak5 or weak30):
        regime = "bullish_trend_with_trend_weakness"
        reason_codes.append(_reason("MULTI_TIMEFRAME_TREND_WEAKNESS"))
        reason_codes.append(_reason("MULTI_METRIC_EXHAUSTION"))
        if _has_developing_equal_high(payload15):
            reason_codes.append(_reason("DEVELOPING_EQUAL_HIGH_EXHAUSTION"))
        if weak5:
            reason_codes.append(_reason("LAST_BAR_MOMENTUM_ROLLOVER"))
        if not any(r["code"] == "BULLISH_TREND_INTACT" for r in reason_codes):
            reason_codes.insert(0, _reason("BULLISH_TREND_INTACT"))
        primary_reasons = [
            "Bullish trend remains intact",
            "15m retested the previous major high",
            "ADX, ATR, +DI and DI spread were substantially weaker",
        ]
        if weak5:
            primary_reasons.append("5m showed last-bar momentum rollover")
        if weak30:
            primary_reasons.append("30m showed weaker buying pressure")
        primary_reasons.append("No confirmed bearish trend reversal yet")

    # Never promote to strong_bullish when multi-metric exhaustion exists on 15m.
    if regime == "strong_bullish_trend" and multi15:
        regime = "bullish_trend_with_trend_weakness"

    # Deduplicate reasons.
    dedup: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in reason_codes:
        code = item["code"]
        if code in seen:
            continue
        seen.add(code)
        dedup.append(item)

    confidence = "high" if regime == "bullish_trend_with_trend_weakness" and multi15 else primary.get(
        "confidence", "medium"
    )

    return {
        "regime": regime,
        "confidence": confidence,
        "primary_timeframe": primary_tf,
        "reason_codes": dedup,
        "primary_reasons": primary_reasons,
        "by_timeframe": per_tf,
        "notes": {
            "weights": "15m primary structure; 5m timing/rollover; 30m higher-TF confirmation; 1h unsupported",
            "technical_signals_preserved": True,
        },
    }


def build_regime_summary(
    by_timeframe: dict[str, dict[str, Any]],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Public helper used by point_audit JSON / human output."""
    return combine_timeframe_regimes(by_timeframe, config=config)

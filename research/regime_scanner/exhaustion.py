"""Equal-high / retest exhaustion and lower-high momentum weakness.

These structures are intentionally separate from classic higher-high divergence.
They never emit ``confirmed_higher_high_divergence`` / classic bearish HH labels.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config
from .swings import ConfirmedPivot, pivots_by_type

StructureType = Literal[
    "strict_higher_high_divergence",
    "equal_high_exhaustion",
    "lower_high_momentum_weakness",
    "outside_retest_zone",
]

PriceDirection = Literal["slightly_higher", "equal", "slightly_lower"]

INDICATOR_KEYS = ("adx", "atr", "atr_pct", "plus_di", "di_spread")


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _ts_iso(value: object) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def price_distance_pct(first_price: float, second_price: float) -> float | None:
    if first_price == 0 or not np.isfinite(first_price) or not np.isfinite(second_price):
        return None
    return abs(float(second_price) - float(first_price)) / abs(float(first_price)) * 100.0


def price_direction(
    first_price: float,
    second_price: float,
    *,
    epsilon: float,
) -> PriceDirection:
    if second_price > first_price + epsilon:
        return "slightly_higher"
    if second_price < first_price - epsilon:
        return "slightly_lower"
    return "equal"


def classify_high_structure(
    first_price: float,
    second_price: float,
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Classify a second high relative to a reference high into three structure types."""
    cfg = config or default_regime_scanner_config()
    eps = float(cfg.divergence_price_epsilon)
    dist = price_distance_pct(first_price, second_price)
    direction = price_direction(first_price, second_price, epsilon=eps)
    tolerance_match = {
        str(tol): bool(dist is not None and dist <= float(tol))
        for tol in cfg.retest_tolerances_pct
    }
    equal_tol = float(cfg.equal_high_tolerance_pct)
    lower_tol = float(cfg.lower_high_retest_tolerance_pct)
    equal_or_retest = bool(dist is not None and dist <= equal_tol)

    if dist is not None and dist <= equal_tol:
        structure: StructureType = "equal_high_exhaustion"
    elif second_price > first_price + eps:
        structure = "strict_higher_high_divergence"
    elif (
        second_price < first_price - eps
        and dist is not None
        and dist <= lower_tol
    ):
        structure = "lower_high_momentum_weakness"
    else:
        structure = "outside_retest_zone"

    return {
        "structure_type": structure,
        "price_distance_pct": dist,
        "price_direction": direction,
        "equal_or_retest_high": equal_or_retest,
        "tolerance_match": tolerance_match,
        "equal_high_tolerance_pct": equal_tol,
        "lower_high_retest_tolerance_pct": lower_tol,
    }


def _weakening_threshold_pct(cfg: RegimeScannerConfig, metric: str) -> float:
    mapping = {
        "adx": cfg.exhaustion_adx_min_weakening_pct,
        "atr": cfg.exhaustion_atr_min_weakening_pct,
        "atr_pct": cfg.exhaustion_atr_pct_min_weakening_pct,
        "plus_di": cfg.exhaustion_plus_di_min_weakening_pct,
        "di_spread": cfg.exhaustion_di_spread_min_weakening_pct,
    }
    return float(mapping[metric])


def _window_max(
    frame: pd.DataFrame,
    index: int,
    column: str,
    radius: int,
    *,
    last_usable_index: int,
) -> float | None:
    """Max of ``column`` in [index-radius, index+radius] clipped to closed bars only."""
    if column not in frame.columns:
        return None
    lo = max(0, index - radius)
    hi = min(last_usable_index, index + radius)
    if hi < lo:
        return None
    values = pd.to_numeric(frame.iloc[lo : hi + 1][column], errors="coerce")
    finite = values[np.isfinite(values.to_numpy(dtype=float))]
    if finite.empty:
        return None
    return float(finite.max())


def compare_indicator(
    frame: pd.DataFrame,
    *,
    reference_index: int,
    candidate_index: int,
    metric: str,
    config: RegimeScannerConfig,
    last_usable_index: int | None = None,
) -> dict[str, Any]:
    """Compare a metric at exact bars and causal ±1 / ±2 maxima."""
    last_idx = len(frame) - 1 if last_usable_index is None else int(last_usable_index)
    ref = _finite(frame.iloc[reference_index].get(metric)) if 0 <= reference_index < len(frame) else None
    cand = (
        _finite(frame.iloc[candidate_index].get(metric))
        if 0 <= candidate_index <= last_idx
        else None
    )
    abs_change = None if ref is None or cand is None else float(cand - ref)
    pct_change = None
    if ref is not None and cand is not None and abs(ref) > float(config.epsilon):
        pct_change = float((cand - ref) / abs(ref) * 100.0)
    min_weak = _weakening_threshold_pct(config, metric)
    weakening = bool(pct_change is not None and pct_change <= -min_weak)

    windows: dict[str, Any] = {}
    for radius in config.exhaustion_indicator_windows:
        key = "exact" if int(radius) == 0 else f"max_pm{int(radius)}"
        r_val = (
            ref
            if int(radius) == 0
            else _window_max(
                frame, reference_index, metric, int(radius), last_usable_index=last_idx
            )
        )
        c_val = (
            cand
            if int(radius) == 0
            else _window_max(
                frame, candidate_index, metric, int(radius), last_usable_index=last_idx
            )
        )
        w_abs = None if r_val is None or c_val is None else float(c_val - r_val)
        w_pct = None
        if r_val is not None and c_val is not None and abs(r_val) > float(config.epsilon):
            w_pct = float((c_val - r_val) / abs(r_val) * 100.0)
        windows[key] = {
            "reference_value": r_val,
            "candidate_value": c_val,
            "absolute_change": w_abs,
            "percent_change": w_pct,
            "weakening": bool(w_pct is not None and w_pct <= -min_weak),
        }

    return {
        "metric": metric,
        "reference_value": ref,
        "candidate_value": cand,
        "absolute_change": abs_change,
        "percent_change": pct_change,
        "weakening": weakening,
        "min_weakening_pct": min_weak,
        "windows": windows,
    }


def _signal_code(metric: str) -> str:
    mapping = {
        "adx": "ADX_EQUAL_HIGH_EXHAUSTION",
        "atr": "ATR_EQUAL_HIGH_EXHAUSTION",
        "atr_pct": "ATR_PERCENT_EQUAL_HIGH_EXHAUSTION",
        "plus_di": "PLUS_DI_EQUAL_HIGH_EXHAUSTION",
        "di_spread": "DI_SPREAD_EQUAL_HIGH_EXHAUSTION",
    }
    return mapping[metric]


def build_exhaustion_signals(
    comparisons: dict[str, dict[str, Any]],
    *,
    structure_type: StructureType,
) -> list[dict[str, Any]]:
    """Emit per-metric and multi-metric exhaustion codes for equal/lower-high structures."""
    if structure_type not in {
        "equal_high_exhaustion",
        "lower_high_momentum_weakness",
    }:
        return []

    signals: list[dict[str, Any]] = []
    weakened: list[str] = []
    atr_family_weak = False

    for metric in INDICATOR_KEYS:
        item = comparisons.get(metric) or {}
        if not item.get("weakening"):
            continue
        if metric in {"atr", "atr_pct"}:
            atr_family_weak = True
        else:
            weakened.append(metric)
        code = _signal_code(metric)
        if structure_type == "lower_high_momentum_weakness":
            code = code.replace("EQUAL_HIGH", "LOWER_HIGH")
        signals.append(
            {
                "code": code,
                "metric": metric,
                "structure_type": structure_type,
                "percent_change": item.get("percent_change"),
                "note": "Heuristic descriptive signal; not a trading entry.",
            }
        )

    # Multi-metric family: ADX, ATR|ATR%, +DI, DI-spread → need >= 2 families.
    families = 0
    if (comparisons.get("adx") or {}).get("weakening"):
        families += 1
    if atr_family_weak:
        families += 1
    if (comparisons.get("plus_di") or {}).get("weakening"):
        families += 1
    if (comparisons.get("di_spread") or {}).get("weakening"):
        families += 1
    if families >= 2:
        multi_code = (
            "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION"
            if structure_type == "equal_high_exhaustion"
            else "MULTI_METRIC_LOWER_HIGH_MOMENTUM_WEAKNESS"
        )
        signals.append(
            {
                "code": multi_code,
                "metric": "multi",
                "structure_type": structure_type,
                "weakened_families": families,
                "note": (
                    "At least two of {ADX, ATR|ATR%, +DI, DI-spread} clearly weaker "
                    "on the retest (heuristic)."
                ),
            }
        )
    return signals


def select_reference_pivot_high(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> ConfirmedPivot | None:
    """Select the medium/major confirmed high: highest price within lookback."""
    cfg = config or default_regime_scanner_config()
    highs = pivots_by_type(pivots, "high")
    if not highs or frame.empty:
        return None
    last_idx = len(frame) - 1
    lookback = int(cfg.exhaustion_reference_lookback_candles)
    min_idx = max(0, last_idx - lookback)
    recent = [p for p in highs if p.pivot_index >= min_idx]
    if not recent:
        recent = highs[-5:] if len(highs) >= 5 else highs
    # Highest price; ties → most recent.
    return max(recent, key=lambda p: (p.price, p.pivot_index))


def find_retest_high_candidate(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any] | None:
    """Highest closed high after the medium/major confirmed pivot high."""
    cfg = config or default_regime_scanner_config()
    if frame.empty:
        return None
    ref = select_reference_pivot_high(frame, pivots, config=cfg)
    if ref is None:
        return None
    last_idx = len(frame) - 1
    if ref.pivot_index >= last_idx:
        return None

    highs = pd.to_numeric(frame["high"], errors="coerce")
    after = highs.iloc[ref.pivot_index + 1 : last_idx + 1]
    if after.empty or not np.isfinite(after.to_numpy(dtype=float)).any():
        return None
    max_val = float(np.nanmax(after.to_numpy(dtype=float)))
    candidates = [
        i
        for i, v in enumerate(after.to_numpy(dtype=float))
        if np.isfinite(v) and abs(float(v) - max_val) <= float(cfg.epsilon)
    ]
    cand_index = ref.pivot_index + 1 + candidates[-1]
    cand_price = float(highs.iloc[cand_index])

    confirmed_high_idx = {p.pivot_index for p in pivots_by_type(pivots, "high")}
    is_confirmed = cand_index in confirmed_high_idx
    available_right = max(0, last_idx - cand_index)
    required_right = int(cfg.pivot_right)
    earliest = pd.Timestamp(frame.iloc[cand_index]["timestamp"]) + pd.Timedelta(
        minutes=int(cfg.candle_interval_minutes) * required_right
    )
    if earliest.tzinfo is None:
        earliest = earliest.tz_localize("UTC")
    else:
        earliest = earliest.tz_convert("UTC")

    structure = classify_high_structure(ref.price, cand_price, config=cfg)
    comparisons = {
        metric: compare_indicator(
            frame,
            reference_index=ref.pivot_index,
            candidate_index=cand_index,
            metric=metric,
            config=cfg,
            last_usable_index=last_idx,
        )
        for metric in INDICATOR_KEYS
    }
    structure_type = structure["structure_type"]
    if structure_type == "equal_high_exhaustion":
        conf_status = (
            "confirmed_equal_high_exhaustion"
            if is_confirmed
            else "developing_equal_high_exhaustion"
        )
    elif structure_type == "lower_high_momentum_weakness":
        conf_status = (
            "confirmed_lower_high_momentum_weakness"
            if is_confirmed
            else "developing_lower_high_momentum_weakness"
        )
    elif structure_type == "strict_higher_high_divergence":
        conf_status = (
            "confirmed_strict_higher_high_candidate"
            if is_confirmed
            else "developing_strict_higher_high_candidate"
        )
    else:
        conf_status = "outside_retest_zone"

    signals = build_exhaustion_signals(comparisons, structure_type=structure_type)
    return {
        "timeframe_interval_minutes": cfg.candle_interval_minutes,
        "reference_confirmed_pivot": ref.to_dict(),
        "reference_pivot_timestamp": ref.pivot_timestamp,
        "reference_pivot_price": ref.price,
        "candidate_index": cand_index,
        "candidate_timestamp": _ts_iso(frame.iloc[cand_index]["timestamp"]),
        "candidate_price": cand_price,
        "price_distance_pct": structure["price_distance_pct"],
        "tolerance_match": structure["tolerance_match"],
        "is_confirmed_pivot": is_confirmed,
        "available_right_candles": int(available_right),
        "required_right_candles": required_right,
        "earliest_confirmation_time": earliest.isoformat(),
        "confirmation_status": conf_status,
        "structure": structure,
        "indicator_comparisons": comparisons,
        "signals": signals,
        "mark_price_note": cfg.mark_price_deviation_note,
        "note": (
            "Retest high candidate is the highest closed high after the selected "
            "medium/major confirmed pivot high; never labeled as classic "
            "higher-high divergence."
        ),
    }


def evaluate_confirmed_high_pair_structures(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
    max_pairs: int | None = None,
) -> dict[str, Any]:
    """Classify recent confirmed high pairs into the three structure buckets."""
    cfg = config or default_regime_scanner_config()
    series = pivots_by_type(pivots, "high")
    n_pairs = int(cfg.recent_swing_pairs if max_pairs is None else max_pairs)
    last_idx = len(frame) - 1
    buckets: dict[str, list[dict[str, Any]]] = {
        "strict_higher_high_divergence": [],
        "equal_high_exhaustion": [],
        "lower_high_momentum_weakness": [],
    }
    recent: list[dict[str, Any]] = []

    for j in range(len(series) - 1, 0, -1):
        if len(recent) >= n_pairs:
            break
        first = series[j - 1]
        second = series[j]
        structure = classify_high_structure(first.price, second.price, config=cfg)
        comparisons = {
            metric: compare_indicator(
                frame,
                reference_index=first.pivot_index,
                candidate_index=second.pivot_index,
                metric=metric,
                config=cfg,
                last_usable_index=last_idx,
            )
            for metric in INDICATOR_KEYS
        }
        signals = build_exhaustion_signals(
            comparisons, structure_type=structure["structure_type"]
        )
        payload = {
            "first_pivot": first.to_dict(),
            "second_pivot": second.to_dict(),
            "structure": structure,
            "indicator_comparisons": comparisons,
            "signals": signals,
            "confirmation_status": (
                f"confirmed_{structure['structure_type']}"
                if structure["structure_type"] != "outside_retest_zone"
                else "outside_retest_zone"
            ),
        }
        recent.append(payload)
        st = structure["structure_type"]
        if st in buckets and signals:
            buckets[st].append(payload)
        elif st == "strict_higher_high_divergence":
            # Classic HH only if indicators weaken under classic rules elsewhere;
            # still list the structure classification.
            buckets[st].append(payload)

    return {
        "recent_pair_structures": recent,
        "classic_pivot_divergence": buckets["strict_higher_high_divergence"],
        "equal_high_retest_exhaustion": buckets["equal_high_exhaustion"],
        "lower_high_momentum_weakness": buckets["lower_high_momentum_weakness"],
    }


def detect_structural_exhaustion(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    timeframe: str,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Full structural exhaustion pack for one timeframe."""
    cfg = config or default_regime_scanner_config()
    pairs = evaluate_confirmed_high_pair_structures(frame, pivots, config=cfg)
    retest = find_retest_high_candidate(frame, pivots, config=cfg)
    developing = None
    confirmed_equal = [
        p
        for p in pairs["equal_high_retest_exhaustion"]
        if p.get("signals")
    ]
    confirmed_lower = [
        p
        for p in pairs["lower_high_momentum_weakness"]
        if p.get("signals")
    ]
    if retest is not None and not retest.get("is_confirmed_pivot"):
        developing = retest

    return {
        "timeframe": timeframe,
        "classic_pivot_divergence": pairs["classic_pivot_divergence"],
        "equal_high_retest_exhaustion": confirmed_equal
        + (
            [retest]
            if retest is not None
            and retest.get("structure", {}).get("structure_type")
            == "equal_high_exhaustion"
            and retest.get("is_confirmed_pivot")
            and retest.get("signals")
            else []
        ),
        "lower_high_momentum_weakness": confirmed_lower
        + (
            [retest]
            if retest is not None
            and retest.get("structure", {}).get("structure_type")
            == "lower_high_momentum_weakness"
            and retest.get("is_confirmed_pivot")
            and retest.get("signals")
            else []
        ),
        "developing_structural_exhaustion": developing,
        "retest_high_candidate": retest,
        "recent_pair_structures": pairs["recent_pair_structures"],
        "mark_price_note": cfg.mark_price_deviation_note,
    }

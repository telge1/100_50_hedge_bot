"""Confirmed swing divergences (never conflated with live weakening signals)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config
from .swings import ConfirmedPivot, latest_pivots, pivots_by_type

DivergenceStatus = Literal[
    "confirmed_bearish_divergence",
    "confirmed_bullish_divergence",
    "confirmed_bearish_atr_divergence",
    "confirmed_bearish_atr_percent_divergence",
    "confirmed_bullish_atr_divergence",
    "confirmed_bullish_atr_percent_divergence",
    "no_confirmed_divergence",
    "insufficient_confirmed_swings",
]


@dataclass(frozen=True)
class DivergenceResult:
    status: DivergenceStatus
    indicator: str
    first_pivot: dict[str, Any] | None
    second_pivot: dict[str, Any] | None
    first_indicator_value: float | None
    second_indicator_value: float | None
    price_change: float | None
    indicator_change: float | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _indicator_at_pivot(
    frame: pd.DataFrame,
    pivot: ConfirmedPivot,
    column: str,
) -> float | None:
    if column not in frame.columns:
        return None
    if pivot.pivot_index < 0 or pivot.pivot_index >= len(frame):
        return None
    return _finite(frame.iloc[pivot.pivot_index][column])


def _pair_within_limits(
    first: ConfirmedPivot,
    second: ConfirmedPivot,
    *,
    config: RegimeScannerConfig,
    last_index: int,
) -> bool:
    gap = second.pivot_index - first.pivot_index
    if gap < int(config.divergence_min_swing_separation):
        return False
    if gap > int(config.divergence_max_swing_gap):
        return False
    max_age = config.divergence_max_age_candles
    if max_age is not None and (last_index - second.pivot_index) > int(max_age):
        return False
    return True


def _insufficient(indicator: str, note: str) -> DivergenceResult:
    return DivergenceResult(
        status="insufficient_confirmed_swings",
        indicator=indicator,
        first_pivot=None,
        second_pivot=None,
        first_indicator_value=None,
        second_indicator_value=None,
        price_change=None,
        indicator_change=None,
        note=note,
    )


def detect_bearish_divergence(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    indicator: str,
    config: RegimeScannerConfig | None = None,
) -> DivergenceResult:
    """Higher confirmed highs with a weaker indicator at the later high."""
    cfg = config or default_regime_scanner_config()
    price_eps = float(cfg.divergence_price_epsilon)
    ind_eps = float(cfg.divergence_indicator_epsilon)
    last_index = max(len(frame) - 1, 0)
    highs = pivots_by_type(pivots, "high")
    if len(highs) < 2:
        return _insufficient(indicator, "Need at least two confirmed pivot highs.")

    for j in range(len(highs) - 1, 0, -1):
        second = highs[j]
        first = highs[j - 1]
        if not _pair_within_limits(first, second, config=cfg, last_index=last_index):
            continue
        v1 = _indicator_at_pivot(frame, first, indicator)
        v2 = _indicator_at_pivot(frame, second, indicator)
        if v1 is None or v2 is None:
            continue
        price_up = second.price > first.price + price_eps
        ind_weaker = v2 < v1 - ind_eps
        if price_up and ind_weaker:
            return DivergenceResult(
                status="confirmed_bearish_divergence",
                indicator=indicator,
                first_pivot=first.to_dict(),
                second_pivot=second.to_dict(),
                first_indicator_value=v1,
                second_indicator_value=v2,
                price_change=float(second.price - first.price),
                indicator_change=float(v2 - v1),
                note="Higher confirmed pivot high with lower indicator value.",
            )
        return DivergenceResult(
            status="no_confirmed_divergence",
            indicator=indicator,
            first_pivot=first.to_dict(),
            second_pivot=second.to_dict(),
            first_indicator_value=v1,
            second_indicator_value=v2,
            price_change=float(second.price - first.price),
            indicator_change=float(v2 - v1),
            note="Last eligible confirmed high pair does not meet bearish divergence rules.",
        )
    return _insufficient(
        indicator,
        "No eligible confirmed high pair within separation/age/gap limits.",
    )


def detect_bullish_divergence(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    indicator: str,
    config: RegimeScannerConfig | None = None,
) -> DivergenceResult:
    """Lower confirmed lows with a weaker indicator at the later low."""
    cfg = config or default_regime_scanner_config()
    price_eps = float(cfg.divergence_price_epsilon)
    ind_eps = float(cfg.divergence_indicator_epsilon)
    last_index = max(len(frame) - 1, 0)
    lows = pivots_by_type(pivots, "low")
    if len(lows) < 2:
        return _insufficient(indicator, "Need at least two confirmed pivot lows.")

    for j in range(len(lows) - 1, 0, -1):
        second = lows[j]
        first = lows[j - 1]
        if not _pair_within_limits(first, second, config=cfg, last_index=last_index):
            continue
        v1 = _indicator_at_pivot(frame, first, indicator)
        v2 = _indicator_at_pivot(frame, second, indicator)
        if v1 is None or v2 is None:
            continue
        price_down = second.price < first.price - price_eps
        if indicator == "di_spread":
            ind_weaker = abs(v2) < abs(v1) - ind_eps
        else:
            ind_weaker = v2 < v1 - ind_eps
        if price_down and ind_weaker:
            return DivergenceResult(
                status="confirmed_bullish_divergence",
                indicator=indicator,
                first_pivot=first.to_dict(),
                second_pivot=second.to_dict(),
                first_indicator_value=v1,
                second_indicator_value=v2,
                price_change=float(second.price - first.price),
                indicator_change=float(v2 - v1),
                note="Lower confirmed pivot low with weaker indicator value.",
            )
        return DivergenceResult(
            status="no_confirmed_divergence",
            indicator=indicator,
            first_pivot=first.to_dict(),
            second_pivot=second.to_dict(),
            first_indicator_value=v1,
            second_indicator_value=v2,
            price_change=float(second.price - first.price),
            indicator_change=float(v2 - v1),
            note="Last eligible confirmed low pair does not meet bullish divergence rules.",
        )
    return _insufficient(
        indicator,
        "No eligible confirmed low pair within separation/age/gap limits.",
    )


def detect_confirmed_divergence_for_indicator(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    indicator: str,
    config: RegimeScannerConfig | None = None,
) -> DivergenceResult:
    if indicator == "plus_di":
        return detect_bearish_divergence(frame, pivots, indicator=indicator, config=config)
    if indicator == "minus_di":
        return detect_bullish_divergence(frame, pivots, indicator=indicator, config=config)
    if indicator in {"adx", "di_spread"}:
        bearish = detect_bearish_divergence(frame, pivots, indicator=indicator, config=config)
        if bearish.status == "confirmed_bearish_divergence":
            return bearish
        bullish = detect_bullish_divergence(frame, pivots, indicator=indicator, config=config)
        if bullish.status == "confirmed_bullish_divergence":
            return bullish
        if bearish.status != "insufficient_confirmed_swings":
            return bearish
        return bullish
    return DivergenceResult(
        status="no_confirmed_divergence",
        indicator=indicator,
        first_pivot=None,
        second_pivot=None,
        first_indicator_value=None,
        second_indicator_value=None,
        price_change=None,
        indicator_change=None,
        note=f"Unsupported divergence indicator: {indicator}",
    )


def detect_confirmed_divergences(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> list[dict[str, Any]]:
    cfg = config or default_regime_scanner_config()
    results: list[DivergenceResult] = [
        detect_bearish_divergence(frame, pivots, indicator="adx", config=cfg),
        detect_bullish_divergence(frame, pivots, indicator="adx", config=cfg),
    ]
    if "plus_di" in cfg.divergence_indicators:
        results.append(detect_bearish_divergence(frame, pivots, indicator="plus_di", config=cfg))
    if "minus_di" in cfg.divergence_indicators:
        results.append(detect_bullish_divergence(frame, pivots, indicator="minus_di", config=cfg))
    if "di_spread" in cfg.divergence_indicators:
        results.append(detect_bearish_divergence(frame, pivots, indicator="di_spread", config=cfg))
        results.append(detect_bullish_divergence(frame, pivots, indicator="di_spread", config=cfg))

    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in results:
        payload = item.to_dict()
        key = (
            payload.get("indicator"),
            payload.get("status"),
            (payload.get("first_pivot") or {}).get("pivot_index"),
            (payload.get("second_pivot") or {}).get("pivot_index"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)

    atr_pack = detect_price_atr_divergences(frame, pivots, config=cfg)
    for hit in atr_pack["recent_confirmed_divergences"]:
        key = (
            hit.get("indicator"),
            hit.get("status"),
            (hit.get("first_pivot") or {}).get("pivot_index"),
            (hit.get("second_pivot") or {}).get("pivot_index"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(
            {
                "status": hit.get("status"),
                "indicator": hit.get("indicator"),
                "first_pivot": hit.get("first_pivot"),
                "second_pivot": hit.get("second_pivot"),
                "first_indicator_value": hit.get("first_indicator_value"),
                "second_indicator_value": hit.get("second_indicator_value"),
                "price_change": hit.get("price_change"),
                "indicator_change": hit.get("indicator_change"),
                "note": hit.get("note"),
                "age_candles": hit.get("age_candles"),
                "age_minutes": hit.get("age_minutes"),
            }
        )
    return unique


def detect_confirmed_price_adx_divergences(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    cfg = config or default_regime_scanner_config()
    return {
        "bearish_adx": detect_bearish_divergence(frame, pivots, indicator="adx", config=cfg).to_dict(),
        "bullish_adx": detect_bullish_divergence(frame, pivots, indicator="adx", config=cfg).to_dict(),
        "last_two_pivot_highs": [p.to_dict() for p in latest_pivots(pivots, "high", count=2)],
        "last_two_pivot_lows": [p.to_dict() for p in latest_pivots(pivots, "low", count=2)],
    }


def _pair_result_payload(
    *,
    side: str,
    indicator: str,
    first: ConfirmedPivot,
    second: ConfirmedPivot,
    v1: float | None,
    v2: float | None,
    status: str,
    note: str,
    last_index: int,
    candle_minutes: int,
) -> dict[str, Any]:
    age_candles = last_index - second.pivot_index
    return {
        "side": side,
        "indicator": indicator,
        "status": status,
        "first_pivot_timestamp": first.pivot_timestamp,
        "second_pivot_timestamp": second.pivot_timestamp,
        "first_price": first.price,
        "second_price": second.price,
        "first_indicator_value": v1,
        "second_indicator_value": v2,
        "price_change": float(second.price - first.price),
        "indicator_change": None if v1 is None or v2 is None else float(v2 - v1),
        "age_candles": int(age_candles),
        "age_minutes": int(age_candles * candle_minutes),
        "note": note,
        "first_pivot": first.to_dict(),
        "second_pivot": second.to_dict(),
    }


def evaluate_recent_swing_pairs(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    side: Literal["high", "low"],
    indicator: str,
    config: RegimeScannerConfig | None = None,
    max_pairs: int | None = None,
) -> dict[str, Any]:
    """Evaluate the latest N eligible consecutive swing pairs for one indicator."""
    cfg = config or default_regime_scanner_config()
    price_eps = float(cfg.divergence_price_epsilon)
    if indicator in {"atr", "atr_pct"}:
        ind_eps = float(cfg.atr_divergence_indicator_epsilon)
    else:
        ind_eps = float(cfg.divergence_indicator_epsilon)
    last_index = max(len(frame) - 1, 0)
    n_pairs = int(cfg.recent_swing_pairs if max_pairs is None else max_pairs)
    series = pivots_by_type(pivots, side)
    pair_results: list[dict[str, Any]] = []
    confirmed: list[dict[str, Any]] = []

    if len(series) < 2:
        return {
            "side": side,
            "indicator": indicator,
            "latest_pair_result": {
                "status": "insufficient_confirmed_swings",
                "note": f"Need at least two confirmed pivot {side}s.",
                "indicator": indicator,
            },
            "recent_pair_results": [],
            "recent_confirmed_divergences": [],
        }

    for j in range(len(series) - 1, 0, -1):
        if len(pair_results) >= n_pairs:
            break
        second = series[j]
        first = series[j - 1]
        if not _pair_within_limits(first, second, config=cfg, last_index=last_index):
            continue
        v1 = _indicator_at_pivot(frame, first, indicator)
        v2 = _indicator_at_pivot(frame, second, indicator)
        if v1 is None or v2 is None:
            pair_results.append(
                _pair_result_payload(
                    side=side,
                    indicator=indicator,
                    first=first,
                    second=second,
                    v1=v1,
                    v2=v2,
                    status="no_confirmed_divergence",
                    note="Missing indicator value at one of the pivots.",
                    last_index=last_index,
                    candle_minutes=cfg.candle_interval_minutes,
                )
            )
            continue

        if side == "high":
            price_ok = second.price > first.price + price_eps
            ind_weaker = v2 < v1 - ind_eps
            if indicator == "atr":
                confirmed_status = "confirmed_bearish_atr_divergence"
            elif indicator == "atr_pct":
                confirmed_status = "confirmed_bearish_atr_percent_divergence"
            else:
                confirmed_status = "confirmed_bearish_divergence"
            if price_ok and ind_weaker:
                result_status = confirmed_status
                note = "Higher confirmed pivot high with lower indicator value."
            else:
                result_status = "no_confirmed_divergence"
                if not price_ok:
                    note = "Second high is not higher than first (no bearish price condition)."
                else:
                    note = "Indicator is not lower at the second high."
        else:
            price_ok = second.price < first.price - price_eps
            ind_weaker = v2 < v1 - ind_eps
            if indicator == "atr":
                confirmed_status = "confirmed_bullish_atr_divergence"
            elif indicator == "atr_pct":
                confirmed_status = "confirmed_bullish_atr_percent_divergence"
            else:
                confirmed_status = "confirmed_bullish_divergence"
            if price_ok and ind_weaker:
                result_status = confirmed_status
                note = "Lower confirmed pivot low with lower indicator value."
            else:
                result_status = "no_confirmed_divergence"
                if not price_ok:
                    note = "Second low is not lower than first (no bullish price condition)."
                else:
                    note = "Indicator is not lower at the second low."

        payload = _pair_result_payload(
            side=side,
            indicator=indicator,
            first=first,
            second=second,
            v1=v1,
            v2=v2,
            status=result_status,
            note=note,
            last_index=last_index,
            candle_minutes=cfg.candle_interval_minutes,
        )
        pair_results.append(payload)
        if str(result_status).startswith("confirmed_"):
            confirmed.append(payload)

    latest = pair_results[0] if pair_results else {
        "status": "insufficient_confirmed_swings",
        "note": "No eligible consecutive swing pairs within limits.",
        "indicator": indicator,
    }
    return {
        "side": side,
        "indicator": indicator,
        "latest_pair_result": latest,
        "recent_pair_results": pair_results,
        "recent_confirmed_divergences": confirmed,
    }


def detect_price_atr_divergences(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Price/ATR and Price/ATR% divergences over recent eligible swing pairs."""
    cfg = config or default_regime_scanner_config()
    bearish_atr = evaluate_recent_swing_pairs(frame, pivots, side="high", indicator="atr", config=cfg)
    bearish_atr_pct = evaluate_recent_swing_pairs(frame, pivots, side="high", indicator="atr_pct", config=cfg)
    bullish_atr = evaluate_recent_swing_pairs(frame, pivots, side="low", indicator="atr", config=cfg)
    bullish_atr_pct = evaluate_recent_swing_pairs(frame, pivots, side="low", indicator="atr_pct", config=cfg)
    recent_confirmed = (
        bearish_atr["recent_confirmed_divergences"]
        + bearish_atr_pct["recent_confirmed_divergences"]
        + bullish_atr["recent_confirmed_divergences"]
        + bullish_atr_pct["recent_confirmed_divergences"]
    )
    return {
        "bearish_atr": bearish_atr,
        "bearish_atr_percent": bearish_atr_pct,
        "bullish_atr": bullish_atr,
        "bullish_atr_percent": bullish_atr_pct,
        "recent_confirmed_divergences": recent_confirmed,
        "latest_pair_result": {
            "bearish_atr": bearish_atr["latest_pair_result"],
            "bearish_atr_percent": bearish_atr_pct["latest_pair_result"],
            "bullish_atr": bullish_atr["latest_pair_result"],
            "bullish_atr_percent": bullish_atr_pct["latest_pair_result"],
        },
    }


def detect_recent_adx_di_pair_scans(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Scan last N eligible pairs for ADX/DI (not only the newest pair)."""
    cfg = config or default_regime_scanner_config()
    return {
        "pivot_high_pairs_adx": evaluate_recent_swing_pairs(
            frame, pivots, side="high", indicator="adx", config=cfg
        ),
        "pivot_low_pairs_adx": evaluate_recent_swing_pairs(
            frame, pivots, side="low", indicator="adx", config=cfg
        ),
        "pivot_high_pairs_plus_di": evaluate_recent_swing_pairs(
            frame, pivots, side="high", indicator="plus_di", config=cfg
        ),
        "pivot_low_pairs_minus_di": evaluate_recent_swing_pairs(
            frame, pivots, side="low", indicator="minus_di", config=cfg
        ),
    }


_MULTI_METRIC_INDICATORS = ("adx", "atr", "atr_pct", "plus_di", "di_spread")


def _indicator_bundle_at_pivot(
    frame: pd.DataFrame,
    pivot: ConfirmedPivot,
) -> dict[str, float | None]:
    return {
        "adx": _indicator_at_pivot(frame, pivot, "adx"),
        "atr": _indicator_at_pivot(frame, pivot, "atr"),
        "atr_pct": _indicator_at_pivot(frame, pivot, "atr_pct"),
        "plus_di": _indicator_at_pivot(frame, pivot, "plus_di"),
        "minus_di": _indicator_at_pivot(frame, pivot, "minus_di"),
        "di_spread": _indicator_at_pivot(frame, pivot, "di_spread"),
    }


def evaluate_multi_metric_swing_pairs(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    side: Literal["high", "low"],
    config: RegimeScannerConfig | None = None,
    max_pairs: int | None = None,
) -> dict[str, Any]:
    """Evaluate last N swing pairs against the full multi-metric divergence rules."""
    cfg = config or default_regime_scanner_config()
    price_eps = float(cfg.divergence_price_epsilon)
    adx_eps = float(cfg.divergence_indicator_epsilon)
    atr_eps = float(cfg.atr_divergence_indicator_epsilon)
    last_index = max(len(frame) - 1, 0)
    n_pairs = int(cfg.recent_swing_pairs if max_pairs is None else max_pairs)
    series = pivots_by_type(pivots, side)
    pair_results: list[dict[str, Any]] = []
    confirmed: list[dict[str, Any]] = []

    if len(series) < 2:
        return {
            "side": side,
            "recent_pair_results": [],
            "confirmed_divergences": [],
            "latest_pair_result": {
                "status": "insufficient_confirmed_swings",
                "note": f"Need at least two confirmed pivot {side}s.",
            },
        }

    for j in range(len(series) - 1, 0, -1):
        if len(pair_results) >= n_pairs:
            break
        second = series[j]
        first = series[j - 1]
        if not _pair_within_limits(first, second, config=cfg, last_index=last_index):
            continue
        first_vals = _indicator_bundle_at_pivot(frame, first)
        second_vals = _indicator_bundle_at_pivot(frame, second)
        changes = {
            "price_change": float(second.price - first.price),
            "adx_change": None
            if first_vals["adx"] is None or second_vals["adx"] is None
            else float(second_vals["adx"] - first_vals["adx"]),
            "atr_change": None
            if first_vals["atr"] is None or second_vals["atr"] is None
            else float(second_vals["atr"] - first_vals["atr"]),
            "atr_pct_change": None
            if first_vals["atr_pct"] is None or second_vals["atr_pct"] is None
            else float(second_vals["atr_pct"] - first_vals["atr_pct"]),
            "plus_di_change": None
            if first_vals["plus_di"] is None or second_vals["plus_di"] is None
            else float(second_vals["plus_di"] - first_vals["plus_di"]),
            "di_spread_change": None
            if first_vals["di_spread"] is None or second_vals["di_spread"] is None
            else float(second_vals["di_spread"] - first_vals["di_spread"]),
        }

        reasons: list[str] = []
        if side == "high":
            price_ok = second.price > first.price + price_eps
            if not price_ok:
                reasons.append("second high is not higher than first")
            checks = {
                "adx": (first_vals["adx"], second_vals["adx"], adx_eps),
                "atr": (first_vals["atr"], second_vals["atr"], atr_eps),
                "atr_pct": (first_vals["atr_pct"], second_vals["atr_pct"], atr_eps),
                "plus_di": (first_vals["plus_di"], second_vals["plus_di"], adx_eps),
                "di_spread": (first_vals["di_spread"], second_vals["di_spread"], adx_eps),
            }
            for name, (v1, v2, eps) in checks.items():
                if v1 is None or v2 is None:
                    reasons.append(f"missing {name}")
                elif not (v2 < v1 - eps):
                    reasons.append(f"{name} not lower at second high")
            status = (
                "confirmed_bearish_divergence"
                if not reasons
                else "no_confirmed_divergence"
            )
            note = (
                "Higher confirmed pivot high with weaker ADX/ATR/ATR%/+DI/DI-spread."
                if not reasons
                else "; ".join(reasons)
            )
        else:
            price_ok = second.price < first.price - price_eps
            if not price_ok:
                reasons.append("second low is not lower than first")
            checks = {
                "adx": (first_vals["adx"], second_vals["adx"], adx_eps),
                "atr": (first_vals["atr"], second_vals["atr"], atr_eps),
                "atr_pct": (first_vals["atr_pct"], second_vals["atr_pct"], atr_eps),
                "plus_di": (first_vals["plus_di"], second_vals["plus_di"], adx_eps),
                "di_spread": (first_vals["di_spread"], second_vals["di_spread"], adx_eps),
            }
            for name, (v1, v2, eps) in checks.items():
                if v1 is None or v2 is None:
                    reasons.append(f"missing {name}")
                elif not (v2 < v1 - eps):
                    reasons.append(f"{name} not lower at second low")
            status = (
                "confirmed_bullish_divergence"
                if not reasons
                else "no_confirmed_divergence"
            )
            note = (
                "Lower confirmed pivot low with weaker ADX/ATR/ATR%/+DI/DI-spread."
                if not reasons
                else "; ".join(reasons)
            )

        payload = {
            "side": side,
            "status": status,
            "confirmation_status": status,
            "first_pivot": first.to_dict(),
            "second_pivot": second.to_dict(),
            "first_indicator_values": first_vals,
            "second_indicator_values": second_vals,
            **changes,
            "note": note,
            "reason": None if not reasons else "; ".join(reasons),
        }
        pair_results.append(payload)
        if str(status).startswith("confirmed_"):
            confirmed.append(payload)

    latest = pair_results[0] if pair_results else {
        "status": "insufficient_confirmed_swings",
        "note": "No eligible consecutive swing pairs within limits.",
    }
    return {
        "side": side,
        "recent_pair_results": pair_results,
        "confirmed_divergences": confirmed,
        "latest_pair_result": latest,
    }


def detect_confirmed_multi_metric_divergences(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    cfg = config or default_regime_scanner_config()
    highs = evaluate_multi_metric_swing_pairs(frame, pivots, side="high", config=cfg)
    lows = evaluate_multi_metric_swing_pairs(frame, pivots, side="low", config=cfg)
    return {
        "pivot_high_pairs": highs,
        "pivot_low_pairs": lows,
        "confirmed_bearish": highs["confirmed_divergences"],
        "confirmed_bullish": lows["confirmed_divergences"],
    }


def detect_developing_divergences(
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    timeframe: str,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Detect unconfirmed developing divergences (never labeled confirmed)."""
    from .swings import find_developing_swing_candidates, latest_pivots

    cfg = config or default_regime_scanner_config()
    price_eps = float(cfg.divergence_price_epsilon)
    adx_eps = float(cfg.divergence_indicator_epsilon)
    atr_eps = float(cfg.atr_divergence_indicator_epsilon)

    def _build(side: Literal["high", "low"]) -> dict[str, Any] | None:
        confirmed = latest_pivots(pivots, side, count=1)
        if not confirmed:
            return None
        ref = confirmed[0]
        candidates = find_developing_swing_candidates(
            frame,
            pivot_left=cfg.pivot_left,
            pivot_right=cfg.pivot_right,
            candle_interval_minutes=cfg.candle_interval_minutes,
            pivot_type=side,
        )
        # Prefer candidates after the reference pivot with the most extreme price.
        after = [c for c in candidates if c.candidate_index > ref.pivot_index]
        if not after:
            return None
        if side == "high":
            after = [c for c in after if c.price > ref.price + price_eps]
            if not after:
                return None
            candidate = max(after, key=lambda c: (c.price, c.candidate_index))
        else:
            after = [c for c in after if c.price < ref.price - price_eps]
            if not after:
                return None
            candidate = min(after, key=lambda c: (c.price, -c.candidate_index))

        ref_vals = _indicator_bundle_at_pivot(frame, ref)
        cand_vals = {
            "adx": _finite(frame.iloc[candidate.candidate_index].get("adx")),
            "atr": _finite(frame.iloc[candidate.candidate_index].get("atr")),
            "atr_pct": _finite(frame.iloc[candidate.candidate_index].get("atr_pct")),
            "plus_di": _finite(frame.iloc[candidate.candidate_index].get("plus_di")),
            "minus_di": _finite(frame.iloc[candidate.candidate_index].get("minus_di")),
            "di_spread": _finite(frame.iloc[candidate.candidate_index].get("di_spread")),
        }

        comparisons = {
            "price": {
                "reference": ref.price,
                "candidate": candidate.price,
                "delta": float(candidate.price - ref.price),
                "condition_met": (
                    candidate.price > ref.price + price_eps
                    if side == "high"
                    else candidate.price < ref.price - price_eps
                ),
            }
        }
        for name, eps in (
            ("adx", adx_eps),
            ("atr", atr_eps),
            ("atr_pct", atr_eps),
            ("plus_di", adx_eps),
            ("di_spread", adx_eps),
        ):
            v1 = ref_vals.get(name)
            v2 = cand_vals.get(name)
            comparisons[name] = {
                "reference": v1,
                "candidate": v2,
                "delta": None if v1 is None or v2 is None else float(v2 - v1),
                "condition_met": (
                    False if v1 is None or v2 is None else bool(v2 < v1 - eps)
                ),
            }

        adx_ok = comparisons["adx"]["condition_met"]
        atr_ok = comparisons["atr"]["condition_met"] or comparisons["atr_pct"]["condition_met"]
        di_ok = (
            comparisons["plus_di"]["condition_met"]
            or comparisons["di_spread"]["condition_met"]
        )
        if not (comparisons["price"]["condition_met"] and adx_ok and atr_ok and di_ok):
            return None

        label = (
            "developing_bearish_divergence"
            if side == "high"
            else "developing_bullish_divergence"
        )
        return {
            "status": label,
            "timeframe": timeframe,
            "reference_confirmed_pivot": {
                **ref.to_dict(),
                "indicators": ref_vals,
            },
            "developing_candidate_timestamp": candidate.candidate_timestamp,
            "candidate_index": candidate.candidate_index,
            "candidate_price": candidate.price,
            "candidate_indicators": cand_vals,
            "indicator_comparisons": comparisons,
            "missing_confirmation_candles": candidate.missing_confirmation_candles,
            "earliest_confirmation_time": candidate.earliest_confirmation_time,
            "note": (
                "Unconfirmed local extreme vs last confirmed pivot; "
                "never labeled as confirmed."
            ),
        }

    return {
        "developing_bearish_divergence": _build("high"),
        "developing_bullish_divergence": _build("low"),
    }

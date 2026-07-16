"""Phase C3 / C3.1 trend / pullback / range / transition regime classifier.

Consumes precomputed shared structure features per bar. Does not rebuild
market structure or reload candles. Policy-only replay on PreparedBar inputs.

C3.1 adds multi-feature range_score, range bounds, and entry/exit hysteresis.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.regime_scanner.trend_audit_shared_replay import PreparedBar
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    has_hh_hl,
    has_lh_ll,
)

RegimeState = Literal[
    "confirmed_uptrend",
    "confirmed_downtrend",
    "range_sideways",
    "bullish_pullback",
    "bearish_pullback",
    "transition_up",
    "transition_down",
    "unclear",
]
ParentTrend = Literal["up", "down", "none"]

REGIME_STATES: tuple[str, ...] = (
    "confirmed_uptrend",
    "confirmed_downtrend",
    "range_sideways",
    "bullish_pullback",
    "bearish_pullback",
    "transition_up",
    "transition_down",
    "unclear",
)

C2_TREND_UP = frozenset({"strong_bullish", "early_bullish", "bullish_weakening"})
C2_TREND_DOWN = frozenset({"strong_bearish", "early_bearish", "bearish_weakening"})
C2_TRANSITION_UP = frozenset({"bottoming", "bullish_warning"})
C2_TRANSITION_DOWN = frozenset({"topping", "bearish_warning"})


@dataclass(frozen=True)
class RegimeClassifierConfig:
    variant_id: str
    efficiency_window: int = 24
    net_move_window: int = 24
    overlap_window: int = 12
    range_width_window: int = 24
    range_lookback: int = 96
    failed_breakout_window: int = 24
    alternating_window: int = 16

    confirm_trend_net_move_atr: float = 0.9
    confirm_trend_de_min: float = 0.30
    confirm_trend_min_bars: int = 3
    require_bos_for_confirm: bool = True
    require_structure_for_confirm: bool = True
    require_htf_alignment: bool = False

    # C3.1 range score thresholds (lookback-based)
    range_score_enter_min: float = 0.58
    range_score_exit_max: float = 0.42
    range_enter_net_move_atr_max: float = 3.2
    range_enter_de_max: float = 0.12
    range_exit_net_move_atr_min: float = 4.0
    range_exit_de_min: float = 0.18
    range_width_atr_min: float = 2.0
    range_width_atr_max: float = 12.0
    range_min_bars: int = 12
    range_exit_confirm_bars: int = 3
    range_exit_atr_distance: float = 0.35
    range_bound_update_atr_tol: float = 0.25
    overlap_ratio_min: float = 0.38
    box_efficiency_min: float = 0.58
    bound_drift_atr_max: float = 3.0
    pullback_to_range_min_bars: int = 8

    pullback_max_depth_atr: float = 1.25
    pullback_invalidate_net_atr: float = 0.85

    transition_max_bars: int = 24
    transition_confirm_bars: int = 3
    transition_follow_de_min: float = 0.22
    transition_follow_net_atr: float = 0.45

    trend_exit_net_move_atr: float = 0.35
    trend_exit_de_min: float = 0.18
    trend_exit_confirm_bars: int = 2

    single_event_never_confirms: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def config_c3(variant: str) -> RegimeClassifierConfig:
    key = variant.strip().lower()
    if key in {"conservative", "c3_a_conservative", "c3_a"}:
        return RegimeClassifierConfig(
            variant_id="C3_A_conservative",
            confirm_trend_net_move_atr=1.15,
            confirm_trend_de_min=0.34,
            confirm_trend_min_bars=4,
            require_bos_for_confirm=True,
            range_lookback=96,
            range_score_enter_min=0.62,
            range_score_exit_max=0.45,
            range_enter_net_move_atr_max=2.8,
            range_enter_de_max=0.10,
            range_min_bars=16,
            range_exit_confirm_bars=3,
            range_exit_net_move_atr_min=3.5,
            range_exit_de_min=0.16,
            range_exit_atr_distance=0.40,
            range_width_atr_max=11.0,
            box_efficiency_min=0.62,
            bound_drift_atr_max=2.6,
            overlap_ratio_min=0.40,
            pullback_to_range_min_bars=10,
            pullback_max_depth_atr=1.0,
            transition_max_bars=18,
            transition_confirm_bars=4,
            trend_exit_net_move_atr=0.45,
            trend_exit_de_min=0.20,
        )
    if key in {"balanced", "c3_b_balanced", "c3_b"}:
        return RegimeClassifierConfig(
            variant_id="C3_B_balanced",
            confirm_trend_net_move_atr=0.85,
            confirm_trend_de_min=0.28,
            confirm_trend_min_bars=3,
            require_bos_for_confirm=True,
            range_lookback=96,
            range_score_enter_min=0.56,
            range_score_exit_max=0.40,
            range_enter_net_move_atr_max=3.2,
            range_enter_de_max=0.12,
            range_min_bars=12,
            range_exit_confirm_bars=3,
            range_exit_net_move_atr_min=3.8,
            range_exit_de_min=0.15,
            range_exit_atr_distance=0.32,
            range_width_atr_max=12.5,
            box_efficiency_min=0.58,
            bound_drift_atr_max=3.0,
            overlap_ratio_min=0.36,
            pullback_to_range_min_bars=8,
            transition_max_bars=24,
            transition_confirm_bars=3,
        )
    if key in {"responsive", "c3_c_responsive", "c3_c"}:
        return RegimeClassifierConfig(
            variant_id="C3_C_responsive",
            confirm_trend_net_move_atr=0.65,
            confirm_trend_de_min=0.24,
            confirm_trend_min_bars=2,
            require_bos_for_confirm=False,
            range_lookback=72,
            range_score_enter_min=0.50,
            range_score_exit_max=0.36,
            range_enter_net_move_atr_max=3.6,
            range_enter_de_max=0.14,
            range_min_bars=8,
            range_exit_confirm_bars=2,
            range_width_atr_max=14.0,
            box_efficiency_min=0.52,
            bound_drift_atr_max=3.5,
            pullback_to_range_min_bars=6,
            transition_max_bars=30,
            transition_confirm_bars=2,
            trend_exit_net_move_atr=0.28,
            trend_exit_de_min=0.15,
        )
    raise ValueError(f"unknown C3 variant: {variant!r}")


VARIANT_ALIASES: dict[str, str] = {
    "conservative": "C3_A_conservative",
    "balanced": "C3_B_balanced",
    "responsive": "C3_C_responsive",
}


@dataclass
class RegimeRuntime:
    state: str = "unclear"
    parent_trend: ParentTrend = "none"
    in_range: bool = False
    range_bars: int = 0
    range_candidate_bars: int = 0
    range_exit_streak: int = 0
    range_high: float | None = None
    range_low: float | None = None
    range_mid: float | None = None
    range_width_atr: float = 0.0
    pullback_range_streak: int = 0
    last_range_score: float = 0.0
    last_failed_breakout: bool = False
    last_breakout_dir: str | None = None
    state_age: int = 0
    transition_bars: int = 0
    trend_confirm_streak: int = 0
    trend_confirm_streak_down: int = 0
    transition_confirm_streak: int = 0
    pending_transition: str | None = None
    last_confirmed_up: bool = False
    last_confirmed_down: bool = False
    sustained_bos_up: int = 0
    sustained_bos_down: int = 0
    active_reasons: list[str] = field(default_factory=list)

    def parent_trend_label(self) -> str | None:
        if self.parent_trend == "up":
            return "up"
        if self.parent_trend == "down":
            return "down"
        return None


@dataclass(frozen=True)
class RegimeBarFeatures:
    bar_index: int
    net_move_atr: float
    directional_efficiency: float
    overlap_ratio: float
    range_width_atr: float
    range_de: float
    range_net_move_atr: float
    box_efficiency: float
    bound_drift_atr: float
    failed_breakout_count: float
    alternating_score: float
    hh_hl: bool
    lh_ll: bool
    bull_bos: bool
    bear_bos: bool
    bull_choch: bool
    bear_choch: bool
    htf_bias: str
    close: float
    high: float
    low: float
    atr: float
    rolling_high: float
    rolling_low: float


def _finite(v: object, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def directional_efficiency(closes: np.ndarray, end_idx: int, window: int) -> float:
    """|net| / sum(|bar changes|) over causal window ending at end_idx."""
    start = max(0, end_idx - window + 1)
    seg = closes[start : end_idx + 1]
    if len(seg) < 2:
        return 0.0
    net = float(seg[-1] - seg[0])
    path = float(np.sum(np.abs(np.diff(seg))))
    if path <= 0.0:
        return 0.0
    return abs(net) / path


def overlap_ratio(high: np.ndarray, low: np.ndarray, end_idx: int, window: int) -> float:
    start = max(0, end_idx - window + 1)
    if end_idx - start < 1:
        return 0.0
    overlaps: list[float] = []
    prev_hi = prev_lo = None
    for i in range(start, end_idx + 1):
        hi, lo = float(high[i]), float(low[i])
        if prev_hi is not None and prev_lo is not None:
            inter = max(0.0, min(hi, prev_hi) - max(lo, prev_lo))
            union = max(hi, prev_hi) - min(lo, prev_lo)
            overlaps.append(inter / union if union > 0 else 0.0)
        prev_hi, prev_lo = hi, lo
    return float(np.mean(overlaps)) if overlaps else 0.0


def _alternating_score(closes: np.ndarray, end_idx: int, window: int) -> float:
    """Fraction of sign flips in bar-to-bar returns (high = chop)."""
    start = max(0, end_idx - window + 1)
    seg = closes[start : end_idx + 1]
    if len(seg) < 3:
        return 0.0
    diffs = np.diff(seg)
    signs = np.sign(diffs)
    signs = signs[signs != 0]
    if len(signs) < 2:
        return 0.0
    flips = float(np.sum(signs[1:] != signs[:-1]))
    return flips / float(len(signs) - 1)


def _failed_breakout_count(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    end_idx: int,
    window: int,
) -> float:
    """Count temporary pierces of prior window bounds that reverse within the window."""
    if end_idx < 4:
        return 0.0
    start = max(0, end_idx - window + 1)
    mid = max(start, end_idx - max(4, window // 2))
    base_hi = float(np.max(high[start:mid])) if mid > start else float(high[end_idx])
    base_lo = float(np.min(low[start:mid])) if mid > start else float(low[end_idx])
    count = 0.0
    for i in range(mid, end_idx + 1):
        pierced_hi = float(high[i]) > base_hi
        pierced_lo = float(low[i]) < base_lo
        if pierced_hi and float(close[i]) <= base_hi:
            count += 1.0
        if pierced_lo and float(close[i]) >= base_lo:
            count += 1.0
    return count


def precompute_regime_arrays(
    frame: pd.DataFrame,
    *,
    efficiency_window: int = 24,
    net_move_window: int = 24,
    overlap_window: int = 12,
    range_width_window: int = 24,
    range_lookback: int = 96,
    failed_breakout_window: int = 24,
    alternating_window: int = 16,
) -> dict[str, Any]:
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    atr = (
        frame["atr"].to_numpy(dtype=float)
        if "atr" in frame.columns
        else np.maximum(high - low, 1e-9)
    )
    n = len(close)
    net_move_atr = np.zeros(n, dtype=float)
    de = np.zeros(n, dtype=float)
    overlap = np.zeros(n, dtype=float)
    range_width_atr = np.zeros(n, dtype=float)
    range_de = np.zeros(n, dtype=float)
    range_net = np.zeros(n, dtype=float)
    box_eff = np.zeros(n, dtype=float)
    bound_drift = np.zeros(n, dtype=float)
    failed_bo = np.zeros(n, dtype=float)
    alternating = np.zeros(n, dtype=float)
    rolling_high = np.zeros(n, dtype=float)
    rolling_low = np.zeros(n, dtype=float)
    for i in range(n):
        de[i] = directional_efficiency(close, i, efficiency_window)
        start = max(0, i - net_move_window + 1)
        atr_i = float(atr[i]) if atr[i] > 0 else 1.0
        net_move_atr[i] = float(close[i] - close[start]) / atr_i
        overlap[i] = overlap_ratio(high, low, i, overlap_window)
        w_start = max(0, i - range_width_window + 1)
        rh = float(np.max(high[w_start : i + 1]))
        rl = float(np.min(low[w_start : i + 1]))
        rolling_high[i] = rh
        rolling_low[i] = rl
        range_width_atr[i] = (rh - rl) / atr_i

        rs = max(0, i - range_lookback + 1)
        r_hi = float(np.max(high[rs : i + 1]))
        r_lo = float(np.min(low[rs : i + 1]))
        span = max(r_hi - r_lo, 1e-9)
        r_net = float(close[i] - close[rs])
        range_de[i] = directional_efficiency(close, i, range_lookback)
        range_net[i] = abs(r_net) / atr_i
        box_eff[i] = 1.0 - abs(r_net) / span
        lag = max(8, range_lookback // 2)
        prev_end = max(0, i - lag)
        prev_start = max(0, prev_end - range_lookback + 1)
        if prev_end > prev_start:
            p_hi = float(np.max(high[prev_start : prev_end + 1]))
            p_lo = float(np.min(low[prev_start : prev_end + 1]))
            bound_drift[i] = (abs(r_hi - p_hi) + abs(r_lo - p_lo)) / atr_i
        else:
            bound_drift[i] = 0.0

        failed_bo[i] = _failed_breakout_count(high, low, close, i, failed_breakout_window)
        alternating[i] = _alternating_score(close, i, alternating_window)
    return {
        "close": close,
        "high": high,
        "low": low,
        "atr": atr,
        "net_move_atr": net_move_atr,
        "directional_efficiency": de,
        "overlap_ratio": overlap,
        "range_width_atr": range_width_atr,
        "range_de": range_de,
        "range_net_move_atr": range_net,
        "box_efficiency": box_eff,
        "bound_drift_atr": bound_drift,
        "failed_breakout_count": failed_bo,
        "alternating_score": alternating,
        "rolling_high": rolling_high,
        "rolling_low": rolling_low,
        "n_bars": n,
    }


def _event_types(events: list) -> set[str]:
    return {
        str(getattr(e, "event_type", e.get("event_type") if isinstance(e, dict) else ""))
        for e in events
    }


def build_bar_features(
    prepared: PreparedBar,
    arrays: dict[str, Any],
    *,
    net_move_window: int,
    efficiency_window: int,
    overlap_window: int,
) -> RegimeBarFeatures:
    i = prepared.bar_index
    structure: MarketStructureState = prepared.structure_5m
    events = _event_types(prepared.events_5m)
    row = prepared.row
    atr = _finite(
        row.get("atr"),
        default=max(_finite(row.get("high")) - _finite(row.get("low")), 1e-9),
    )
    return RegimeBarFeatures(
        bar_index=i,
        net_move_atr=float(arrays["net_move_atr"][i]),
        directional_efficiency=float(arrays["directional_efficiency"][i]),
        overlap_ratio=float(arrays["overlap_ratio"][i]),
        range_width_atr=float(arrays["range_width_atr"][i]),
        range_de=float(arrays["range_de"][i]),
        range_net_move_atr=float(arrays["range_net_move_atr"][i]),
        box_efficiency=float(arrays["box_efficiency"][i]),
        bound_drift_atr=float(arrays["bound_drift_atr"][i]),
        failed_breakout_count=float(arrays["failed_breakout_count"][i]),
        alternating_score=float(arrays["alternating_score"][i]),
        hh_hl=bool(has_hh_hl(structure)),
        lh_ll=bool(has_lh_ll(structure)),
        bull_bos="bullish_bos" in events,
        bear_bos="bearish_bos" in events,
        bull_choch="bullish_choch" in events,
        bear_choch="bearish_choch" in events,
        htf_bias=str(prepared.structure_15m.current_structure_bias or "neutral"),
        close=_finite(row.get("close")),
        high=_finite(row.get("high"), default=_finite(row.get("close"))),
        low=_finite(row.get("low"), default=_finite(row.get("close"))),
        atr=atr,
        rolling_high=float(arrays["rolling_high"][i]),
        rolling_low=float(arrays["rolling_low"][i]),
    )


def _htf_supports(direction: str, bias: str) -> bool:
    if direction == "up":
        return bias in {"bullish", "neutral"}
    if direction == "down":
        return bias in {"bearish", "neutral"}
    return True


def _count_up_confirm(feat: RegimeBarFeatures, cfg: RegimeClassifierConfig) -> int:
    score = 0
    if feat.hh_hl or not cfg.require_structure_for_confirm:
        score += 1
    if feat.bull_bos or not cfg.require_bos_for_confirm:
        score += 1
    if feat.net_move_atr >= cfg.confirm_trend_net_move_atr:
        score += 1
    if feat.directional_efficiency >= cfg.confirm_trend_de_min:
        score += 1
    if not cfg.require_htf_alignment or _htf_supports("up", feat.htf_bias):
        score += 1
    return score


def _count_down_confirm(feat: RegimeBarFeatures, cfg: RegimeClassifierConfig) -> int:
    score = 0
    if feat.lh_ll or not cfg.require_structure_for_confirm:
        score += 1
    if feat.bear_bos or not cfg.require_bos_for_confirm:
        score += 1
    if feat.net_move_atr <= -cfg.confirm_trend_net_move_atr:
        score += 1
    if feat.directional_efficiency >= cfg.confirm_trend_de_min:
        score += 1
    if not cfg.require_htf_alignment or _htf_supports("down", feat.htf_bias):
        score += 1
    return score


def _min_confirm_score(cfg: RegimeClassifierConfig) -> int:
    base = 3 if cfg.require_bos_for_confirm and cfg.require_structure_for_confirm else 2
    return max(base, cfg.confirm_trend_min_bars)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def compute_range_score(
    feat: RegimeBarFeatures,
    *,
    cfg: RegimeClassifierConfig,
    sustained_bos_up: int,
    sustained_bos_down: int,
) -> dict[str, float]:
    """Transparent multi-feature range score in [0, 1] using long lookback."""
    de_part = _clip01(1.0 - feat.range_de / max(cfg.range_enter_de_max, 1e-6))
    net_part = _clip01(
        1.0 - feat.range_net_move_atr / max(cfg.range_enter_net_move_atr_max, 1e-6)
    )
    box_part = _clip01(feat.box_efficiency / max(cfg.box_efficiency_min, 1e-6))
    drift_part = _clip01(
        1.0 - feat.bound_drift_atr / max(cfg.bound_drift_atr_max, 1e-6)
    )
    overlap_part = _clip01(feat.overlap_ratio / max(cfg.overlap_ratio_min, 1e-6))
    if cfg.range_width_atr_min <= feat.range_width_atr <= cfg.range_width_atr_max:
        width_part = 1.0
    elif feat.range_width_atr < cfg.range_width_atr_min:
        width_part = _clip01(feat.range_width_atr / max(cfg.range_width_atr_min, 1e-6))
    else:
        over = feat.range_width_atr - cfg.range_width_atr_max
        width_part = _clip01(1.0 - over / max(0.5 * cfg.range_width_atr_max, 1e-6))
    # Structure may flicker in chop; soft penalty only.
    structure_part = 0.35 if (feat.hh_hl or feat.lh_ll) else 1.0
    failed_part = _clip01(feat.failed_breakout_count / 2.0)
    alt_part = _clip01(feat.alternating_score)
    bos_part = 1.0 if sustained_bos_up < 2 and sustained_bos_down < 2 else 0.2

    parts = {
        "de": de_part,
        "net": net_part,
        "box": box_part,
        "drift": drift_part,
        "overlap": overlap_part,
        "width": width_part,
        "structure": structure_part,
        "failed_breakout": failed_part,
        "alternating": alt_part,
        "no_sustained_bos": bos_part,
    }
    # Emphasize box + drift + long DE for C3.1
    weights = {
        "de": 1.4,
        "net": 1.0,
        "box": 1.6,
        "drift": 1.5,
        "overlap": 0.8,
        "width": 0.7,
        "structure": 0.6,
        "failed_breakout": 0.8,
        "alternating": 0.7,
        "no_sustained_bos": 0.7,
    }
    score = float(sum(parts[k] * weights[k] for k in parts) / sum(weights.values()))
    return {"range_score": score, **{f"part_{k}": v for k, v in parts.items()}}


def _range_hard_gates(feat: RegimeBarFeatures, cfg: RegimeClassifierConfig) -> bool:
    return (
        feat.range_de <= cfg.range_enter_de_max
        and feat.range_net_move_atr <= cfg.range_enter_net_move_atr_max
        and feat.box_efficiency >= cfg.box_efficiency_min
        and feat.bound_drift_atr <= cfg.bound_drift_atr_max
        and feat.range_width_atr <= cfg.range_width_atr_max * 1.1
    )


def _init_range_bounds(rt: RegimeRuntime, feat: RegimeBarFeatures) -> None:
    rt.range_high = float(feat.rolling_high)
    rt.range_low = float(feat.rolling_low)
    if rt.range_high is not None and rt.range_low is not None:
        rt.range_mid = 0.5 * (rt.range_high + rt.range_low)
        atr = feat.atr if feat.atr > 0 else 1.0
        rt.range_width_atr = (rt.range_high - rt.range_low) / atr


def _update_range_bounds(
    rt: RegimeRuntime, feat: RegimeBarFeatures, *, cfg: RegimeClassifierConfig
) -> None:
    if rt.range_high is None or rt.range_low is None:
        _init_range_bounds(rt, feat)
        return
    tol = cfg.range_bound_update_atr_tol * feat.atr
    # Expand only modestly; do not chase every wick.
    if feat.high > rt.range_high + tol:
        # Failed breakout candidate: do not expand unless close holds above.
        if feat.close > rt.range_high + 0.5 * tol:
            rt.range_high = min(feat.high, rt.range_high + tol)
    elif feat.high > rt.range_high:
        rt.range_high = 0.8 * rt.range_high + 0.2 * feat.high

    if feat.low < rt.range_low - tol:
        if feat.close < rt.range_low - 0.5 * tol:
            rt.range_low = max(feat.low, rt.range_low - tol)
    elif feat.low < rt.range_low:
        rt.range_low = 0.8 * rt.range_low + 0.2 * feat.low

    rt.range_mid = 0.5 * (rt.range_high + rt.range_low)
    atr = feat.atr if feat.atr > 0 else 1.0
    rt.range_width_atr = (rt.range_high - rt.range_low) / atr


def _outside_range(
    feat: RegimeBarFeatures, rt: RegimeRuntime, *, cfg: RegimeClassifierConfig
) -> str | None:
    if rt.range_high is None or rt.range_low is None:
        return None
    dist = cfg.range_exit_atr_distance * feat.atr
    if feat.close > rt.range_high + dist:
        return "up"
    if feat.close < rt.range_low - dist:
        return "down"
    return None


def _strong_confirmed_trend_active(
    feat: RegimeBarFeatures, cfg: RegimeClassifierConfig, prev_state: str
) -> bool:
    if prev_state not in {"confirmed_uptrend", "confirmed_downtrend"}:
        return False
    if prev_state == "confirmed_uptrend":
        return (
            feat.net_move_atr >= cfg.confirm_trend_net_move_atr * 0.7
            and feat.directional_efficiency >= cfg.confirm_trend_de_min * 0.85
        )
    return (
        feat.net_move_atr <= -cfg.confirm_trend_net_move_atr * 0.7
        and feat.directional_efficiency >= cfg.confirm_trend_de_min * 0.85
    )


def step_regime_classifier(
    rt: RegimeRuntime,
    feat: RegimeBarFeatures,
    *,
    cfg: RegimeClassifierConfig,
) -> RegimeRuntime:
    reasons: list[str] = []
    prev_state = rt.state

    if feat.bull_bos:
        rt.sustained_bos_up += 1
        rt.sustained_bos_down = max(0, rt.sustained_bos_down - 1)
    else:
        rt.sustained_bos_up = max(0, rt.sustained_bos_up - 1)
    if feat.bear_bos:
        rt.sustained_bos_down += 1
        rt.sustained_bos_up = max(0, rt.sustained_bos_up - 1)
    else:
        rt.sustained_bos_down = max(0, rt.sustained_bos_down - 1)

    score_parts = compute_range_score(
        feat,
        cfg=cfg,
        sustained_bos_up=rt.sustained_bos_up,
        sustained_bos_down=rt.sustained_bos_down,
    )
    range_score = float(score_parts["range_score"])
    rt.last_range_score = range_score
    range_candidate = (
        range_score >= cfg.range_score_enter_min
        and _range_hard_gates(feat, cfg)
        and not _strong_confirmed_trend_active(feat, cfg, prev_state)
    )

    if range_candidate:
        rt.range_candidate_bars += 1
    else:
        rt.range_candidate_bars = max(0, rt.range_candidate_bars - 1)

    # --- Range entry / exit hysteresis ---
    if not rt.in_range:
        if range_candidate and rt.range_candidate_bars >= cfg.range_min_bars:
            rt.in_range = True
            rt.range_bars = 1
            rt.range_exit_streak = 0
            _init_range_bounds(rt, feat)
            reasons.append("range_enter")
    else:
        rt.range_bars += 1
        breakout_dir = _outside_range(feat, rt, cfg=cfg)
        if breakout_dir is None:
            _update_range_bounds(rt, feat, cfg=cfg)
        exit_pressure = False
        if breakout_dir is not None:
            exit_pressure = True
            rt.last_breakout_dir = breakout_dir
        # Soft exit: score/structure/efficiency without needing price break
        soft_exit = (
            range_score < cfg.range_score_exit_max
            or feat.range_de >= cfg.range_exit_de_min
            or feat.range_net_move_atr >= cfg.range_exit_net_move_atr_min
            or feat.box_efficiency < cfg.box_efficiency_min * 0.85
            or feat.bound_drift_atr > cfg.bound_drift_atr_max * 1.25
            or (rt.sustained_bos_up >= 2 or rt.sustained_bos_down >= 2)
        )
        if soft_exit:
            exit_pressure = True
            if rt.last_breakout_dir is None:
                rt.last_breakout_dir = "up" if feat.net_move_atr >= 0 else "down"

        failed = False
        if rt.range_high is not None and rt.range_low is not None and breakout_dir is None:
            pierce_hi = feat.high > rt.range_high and feat.close <= rt.range_high
            pierce_lo = feat.low < rt.range_low and feat.close >= rt.range_low
            failed = pierce_hi or pierce_lo
        rt.last_failed_breakout = failed
        if failed and not soft_exit:
            reasons.append("failed_breakout")
            rt.range_exit_streak = 0
        elif exit_pressure:
            rt.range_exit_streak += 1
        else:
            rt.range_exit_streak = 0

        if rt.range_exit_streak >= cfg.range_exit_confirm_bars:
            rt.in_range = False
            rt.range_candidate_bars = 0
            reasons.append("range_exit")
            direction = rt.last_breakout_dir
            if direction is None:
                direction = "up" if feat.net_move_atr > 0 else "down"
            rt.pending_transition = (
                "transition_up" if direction == "up" else "transition_down"
            )
            rt.transition_bars = 0
            rt.transition_confirm_streak = 0

    proposed = prev_state
    up_score = _count_up_confirm(feat, cfg)
    down_score = _count_down_confirm(feat, cfg)
    min_score = _min_confirm_score(cfg)

    trans_up = feat.bull_choch or (feat.bull_bos and not feat.hh_hl)
    trans_down = feat.bear_choch or (feat.bear_bos and not feat.lh_ll)
    if cfg.single_event_never_confirms:
        if trans_up and not (feat.bull_bos and feat.hh_hl):
            trans_up = bool(feat.bull_choch or (feat.bull_bos and up_score >= 2))
        if trans_down and not (feat.bear_bos and feat.lh_ll):
            trans_down = bool(feat.bear_choch or (feat.bear_bos and down_score >= 2))

    if up_score >= min_score and feat.net_move_atr > 0:
        rt.trend_confirm_streak += 1
    else:
        rt.trend_confirm_streak = max(0, rt.trend_confirm_streak - 1)
    if down_score >= min_score and feat.net_move_atr < 0:
        rt.trend_confirm_streak_down += 1
    else:
        rt.trend_confirm_streak_down = max(0, rt.trend_confirm_streak_down - 1)

    # Fresh range exit → transition first (not confirmed trend)
    if "range_exit" in reasons and rt.pending_transition:
        proposed = rt.pending_transition
        rt.transition_bars = 1
        reasons.append(f"range_exit_to_{rt.pending_transition}")
        rt.range_bars = 0
        rt.range_exit_streak = 0
    elif rt.in_range:
        proposed = "range_sideways"
        reasons.append("range_active")
    elif (
        rt.trend_confirm_streak >= cfg.confirm_trend_min_bars
        and up_score >= min_score
        and not (prev_state == "confirmed_downtrend" and feat.net_move_atr < cfg.trend_exit_net_move_atr)
        and not rt.in_range
    ):
        proposed = "confirmed_uptrend"
        rt.last_confirmed_up = True
        rt.last_confirmed_down = False
        rt.parent_trend = "up"
        rt.pending_transition = None
        rt.transition_bars = 0
        rt.pullback_range_streak = 0
        reasons.append("confirmed_up")
    elif (
        rt.trend_confirm_streak_down >= cfg.confirm_trend_min_bars
        and down_score >= min_score
        and not rt.in_range
    ):
        proposed = "confirmed_downtrend"
        rt.last_confirmed_down = True
        rt.last_confirmed_up = False
        rt.parent_trend = "down"
        rt.pending_transition = None
        rt.transition_bars = 0
        rt.pullback_range_streak = 0
        reasons.append("confirmed_down")
    elif prev_state == "confirmed_uptrend":
        if (
            feat.net_move_atr < -cfg.pullback_invalidate_net_atr
            and feat.bear_bos
            and feat.directional_efficiency >= cfg.transition_follow_de_min
        ):
            rt.pending_transition = "transition_down"
            rt.transition_bars = 1
            proposed = "transition_down"
            reasons.append("uptrend_invalidation")
        elif feat.net_move_atr < 0 and abs(feat.net_move_atr) <= cfg.pullback_max_depth_atr:
            proposed = "bullish_pullback"
            rt.parent_trend = "up"
            reasons.append("bullish_pullback")
        elif (
            feat.net_move_atr <= cfg.trend_exit_net_move_atr
            and feat.directional_efficiency <= cfg.trend_exit_de_min
        ):
            proposed = "unclear"
            reasons.append("uptrend_weaken")
        else:
            proposed = "confirmed_uptrend"
            rt.parent_trend = "up"
    elif prev_state == "confirmed_downtrend":
        if (
            feat.net_move_atr > cfg.pullback_invalidate_net_atr
            and feat.bull_bos
            and feat.directional_efficiency >= cfg.transition_follow_de_min
        ):
            rt.pending_transition = "transition_up"
            rt.transition_bars = 1
            proposed = "transition_up"
            reasons.append("downtrend_invalidation")
        elif feat.net_move_atr > 0 and feat.net_move_atr <= cfg.pullback_max_depth_atr:
            proposed = "bearish_pullback"
            rt.parent_trend = "down"
            reasons.append("bearish_pullback")
        elif (
            feat.net_move_atr >= -cfg.trend_exit_net_move_atr
            and feat.directional_efficiency <= cfg.trend_exit_de_min
        ):
            proposed = "unclear"
            reasons.append("downtrend_weaken")
        else:
            proposed = "confirmed_downtrend"
            rt.parent_trend = "down"
    elif prev_state in {"transition_up", "transition_down"}:
        rt.transition_bars += 1
        if rt.transition_bars > cfg.transition_max_bars:
            if range_score >= cfg.range_score_enter_min:
                proposed = "range_sideways"
                rt.in_range = True
                _init_range_bounds(rt, feat)
                reasons.append("transition_expired_to_range")
            elif rt.parent_trend == "up":
                proposed = "bullish_pullback"
                reasons.append("transition_expired")
            elif rt.parent_trend == "down":
                proposed = "bearish_pullback"
                reasons.append("transition_expired")
            else:
                proposed = "unclear"
                reasons.append("transition_expired")
        elif prev_state == "transition_up":
            if up_score >= min_score and feat.net_move_atr >= cfg.transition_follow_net_atr:
                rt.transition_confirm_streak += 1
            else:
                rt.transition_confirm_streak = 0
            if rt.transition_confirm_streak >= cfg.transition_confirm_bars:
                proposed = "confirmed_uptrend"
                rt.parent_trend = "up"
                rt.in_range = False
                reasons.append("transition_up_confirmed")
            elif range_score >= cfg.range_score_enter_min:
                proposed = "range_sideways"
                rt.in_range = True
                _init_range_bounds(rt, feat)
                reasons.append("transition_up_to_range")
            elif rt.parent_trend == "down":
                proposed = "bearish_pullback"
                reasons.append("transition_up_failed")
            else:
                proposed = "transition_up"
        else:
            if down_score >= min_score and feat.net_move_atr <= -cfg.transition_follow_net_atr:
                rt.transition_confirm_streak += 1
            else:
                rt.transition_confirm_streak = 0
            if rt.transition_confirm_streak >= cfg.transition_confirm_bars:
                proposed = "confirmed_downtrend"
                rt.parent_trend = "down"
                rt.in_range = False
                reasons.append("transition_down_confirmed")
            elif range_score >= cfg.range_score_enter_min:
                proposed = "range_sideways"
                rt.in_range = True
                _init_range_bounds(rt, feat)
                reasons.append("transition_down_to_range")
            elif rt.parent_trend == "up":
                proposed = "bullish_pullback"
                reasons.append("transition_down_failed")
            else:
                proposed = "transition_down"
    elif prev_state in {"bullish_pullback", "bearish_pullback"}:
        # Pullback → range when chop persists without trend resume
        if range_score >= cfg.range_score_enter_min and _range_hard_gates(feat, cfg):
            rt.pullback_range_streak += 1
        else:
            rt.pullback_range_streak = 0
        if rt.pullback_range_streak >= cfg.pullback_to_range_min_bars:
            proposed = "range_sideways"
            rt.in_range = True
            _init_range_bounds(rt, feat)
            reasons.append("pullback_to_range")
        elif prev_state == "bullish_pullback" and up_score >= min_score - 1 and feat.net_move_atr > 0:
            proposed = "confirmed_uptrend"
            rt.pullback_range_streak = 0
            reasons.append("pullback_resume_up")
        elif prev_state == "bearish_pullback" and down_score >= min_score - 1 and feat.net_move_atr < 0:
            proposed = "confirmed_downtrend"
            rt.pullback_range_streak = 0
            reasons.append("pullback_resume_down")
        elif trans_up and rt.parent_trend != "up":
            proposed = "transition_up"
            rt.transition_bars = 1
            reasons.append("pullback_to_transition_up")
        elif trans_down and rt.parent_trend != "down":
            proposed = "transition_down"
            rt.transition_bars = 1
            reasons.append("pullback_to_transition_down")
        else:
            proposed = prev_state
    else:
        if trans_up and not trans_down:
            proposed = "transition_up"
            rt.transition_bars = 1
            rt.parent_trend = "up" if rt.last_confirmed_up else rt.parent_trend
            reasons.append("initial_transition_up")
        elif trans_down and not trans_up:
            proposed = "transition_down"
            rt.transition_bars = 1
            rt.parent_trend = "down" if rt.last_confirmed_down else rt.parent_trend
            reasons.append("initial_transition_down")
        elif up_score >= min_score and feat.net_move_atr > 0:
            proposed = "confirmed_uptrend"
            reasons.append("direct_up_confirm")
        elif down_score >= min_score and feat.net_move_atr < 0:
            proposed = "confirmed_downtrend"
            reasons.append("direct_down_confirm")
        else:
            proposed = "unclear"

    if proposed == prev_state:
        rt.state_age += 1
    else:
        rt.state_age = 0
    rt.state = proposed
    rt.active_reasons = reasons
    return rt


def c2_direction(state: str) -> str:
    if state in C2_TREND_UP:
        return "up"
    if state in C2_TREND_DOWN:
        return "down"
    if state in C2_TRANSITION_UP:
        return "transition_up"
    if state in C2_TRANSITION_DOWN:
        return "transition_down"
    if state in {"neutral"}:
        return "range"
    return "unclear"


def c3_direction(state: str) -> str:
    if state == "confirmed_uptrend":
        return "up"
    if state == "confirmed_downtrend":
        return "down"
    if state == "range_sideways":
        return "range"
    if state == "bullish_pullback":
        return "pullback_up"
    if state == "bearish_pullback":
        return "pullback_down"
    if state == "transition_up":
        return "transition_up"
    if state == "transition_down":
        return "transition_down"
    return "unclear"


def replay_regime_variant(
    prepared_bars: list[PreparedBar],
    *,
    arrays: dict[str, Any],
    cfg: RegimeClassifierConfig,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
) -> dict[str, Any]:
    rt = RegimeRuntime()
    timeline: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []

    for prep in prepared_bars:
        ts = prep.decision_time
        if ts < analyze_start or ts > analyze_end:
            continue
        prev = rt.state
        feat = build_bar_features(
            prep,
            arrays,
            net_move_window=cfg.net_move_window,
            efficiency_window=cfg.efficiency_window,
            overlap_window=cfg.overlap_window,
        )
        rt = step_regime_classifier(rt, feat, cfg=cfg)
        score_parts = compute_range_score(
            feat,
            cfg=cfg,
            sustained_bos_up=rt.sustained_bos_up,
            sustained_bos_down=rt.sustained_bos_down,
        )
        row = {
            "decision_time": ts.isoformat(),
            "bar_index": int(prep.bar_index),
            "state": rt.state,
            "previous_state": prev,
            "parent_trend": rt.parent_trend_label(),
            "in_range": bool(rt.in_range),
            "close": float(prep.row.get("close", 0.0)),
            "net_move_atr": feat.net_move_atr,
            "normalized_net_move": feat.net_move_atr,
            "directional_efficiency": feat.directional_efficiency,
            "overlap_ratio": feat.overlap_ratio,
            "normalized_range_width": feat.range_width_atr,
            "range_de": feat.range_de,
            "range_net_move_atr": feat.range_net_move_atr,
            "box_efficiency": feat.box_efficiency,
            "bound_drift_atr": feat.bound_drift_atr,
            "failed_breakout_count": feat.failed_breakout_count,
            "alternating_score": feat.alternating_score,
            "range_score": score_parts["range_score"],
            "range_candidate": bool(
                score_parts["range_score"] >= cfg.range_score_enter_min
            ),
            "range_confirmed": bool(rt.in_range),
            "sustained_bos_up": int(rt.sustained_bos_up),
            "sustained_bos_down": int(rt.sustained_bos_down),
            "range_high": rt.range_high,
            "range_low": rt.range_low,
            "range_mid": rt.range_mid,
            "range_width_atr": rt.range_width_atr,
            "bars_in_range": int(rt.range_bars) if rt.in_range else 0,
            "failed_breakout_event": bool(rt.last_failed_breakout),
            "reasons": "|".join(rt.active_reasons),
            "transition": rt.state != prev,
        }
        timeline.append(row)
        if rt.state != prev:
            transitions.append(
                {
                    "decision_time": ts.isoformat(),
                    "bar_index": int(prep.bar_index),
                    "previous_state": prev,
                    "new_state": rt.state,
                    "parent_trend": rt.parent_trend_label(),
                    "in_range": bool(rt.in_range),
                    "range_score": score_parts["range_score"],
                    "reasons": "|".join(rt.active_reasons),
                }
            )

    return {
        "variant": cfg.variant_id,
        "config": cfg.to_dict(),
        "timeline": timeline,
        "transitions": transitions,
    }

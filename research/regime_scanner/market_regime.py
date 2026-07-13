"""Read-only four-class market regime (K2 + H4) — research V1.

Separates coarse market dynamics (EMA geometry + price progress) from the
structure lifecycle in ``trend_state_machine``.

This module must not change allow_long / allow_short / entries / HTF gates.
Policy (``trend_state_policy``) must not import or call it for decisions.

Audit provenance: ``market_regime_four_class_audit`` best candidate **K2_H4**.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, MutableMapping

import numpy as np

MarketRegimeName = Literal[
    "strong_bullish_trend",
    "strong_bearish_trend",
    "accumulation_range",
    "transition_unclear",
]
MarketDirection = Literal["bullish", "bearish", "neutral"]

TREND_REGIMES = frozenset({"strong_bullish_trend", "strong_bearish_trend"})


@dataclass(frozen=True)
class MarketRegimeConfig:
    """Thresholds frozen from the K2_H4 four-class audit (30m, window=12)."""

    variant_id: str = "K2_H4"
    window_bars: int = 12

    # --- K2 strong trend (EMA + progress) ---
    strong_net_move_atr: float = 1.0
    strong_directional_efficiency_min: float = 0.32
    strong_progress_vs_range_min: float = 0.45
    strong_share_aligned_min: float = 0.65
    strong_ema20_slope_atr_min: float = 0.015
    strong_ema9_slope_atr_min: float = 0.01

    # --- K2 accumulation_range ---
    range_net_move_atr_max: float = 0.35
    range_directional_efficiency_max: float = 0.22
    range_progress_vs_range_max: float = 0.35

    # --- nested K1 geometry (used only inside K2 residual paths) ---
    k1_ema9_slope_atr_min: float = 0.035
    k1_ema20_slope_atr_min: float = 0.03
    k1_sep_atr_min: float = 0.05
    k1_share_aligned_min: float = 0.75
    k1_max_crosses: int = 1
    k1_flat_slope_atr_max: float = 0.05
    k1_entangle_crosses_min: int = 2
    k1_entangle_ema20_abs_max: float = 0.03
    k1_entangle_ema9_abs_max: float = 0.04
    k1_hint_share_min: float = 0.55
    ema_strong_insufficient_de_max: float = 0.28
    residual_net_hint_atr: float = 0.3

    # --- H4 hysteresis (exact audit semantics) ---
    # Trend entry needs 2 consecutive raw bars; range needs 3; other (incl.
    # transition_unclear) needs 2. First bar adopts raw immediately.
    # Opposite trends may switch directly (no forced transition — that is H3).
    trend_confirm_bars: int = 2
    range_confirm_bars: int = 3
    transition_confirm_bars: int = 2


def default_market_regime_config() -> MarketRegimeConfig:
    return MarketRegimeConfig()


@dataclass(frozen=True)
class MarketRegimeFeatures:
    """Causal feature snapshot for one closed bar (typically 30m)."""

    ema9: float
    ema20: float
    ema9_slope_atr: float
    ema20_slope_atr: float
    ema_sep_atr: float
    ema_sep_change_atr: float
    share_above_both: float
    share_below_both: float
    ema_crosses: int
    ema_flat: bool
    net_move_atr: float
    directional_efficiency: float
    progress_vs_range: float
    up_close_share: float
    down_close_share: float
    maximum_counter_move_atr: float
    close: float
    atr: float

    def as_mapping(self) -> dict[str, float | int | bool | None]:
        return {
            "ema9": self.ema9,
            "ema20": self.ema20,
            "ema9_slope_atr": self.ema9_slope_atr,
            "ema20_slope_atr": self.ema20_slope_atr,
            "ema_sep_atr": self.ema_sep_atr,
            "ema_sep_change_atr": self.ema_sep_change_atr,
            "share_above_both": self.share_above_both,
            "share_below_both": self.share_below_both,
            "ema_crosses": int(self.ema_crosses),
            "ema_flat": bool(self.ema_flat),
            "net_move_atr": self.net_move_atr,
            "directional_efficiency": self.directional_efficiency,
            "progress_vs_range": self.progress_vs_range,
            "up_close_share": self.up_close_share,
            "down_close_share": self.down_close_share,
            "maximum_counter_move_atr": self.maximum_counter_move_atr,
            "close": self.close,
            "atr": self.atr,
        }


@dataclass(frozen=True)
class MarketRegimeContext:
    regime: MarketRegimeName
    direction: MarketDirection
    confidence: float | None
    reason_codes: tuple[str, ...]
    effective_at: datetime
    feature_snapshot: Mapping[str, float | int | bool | None]
    candidate_regime: MarketRegimeName | None = None
    candidate_streak: int = 0
    confirm_bars_required: int = 0
    raw_regime: MarketRegimeName | None = None
    variant_id: str = "K2_H4"
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_regime": self.regime,
            "market_regime_direction": self.direction,
            "market_regime_confidence": self.confidence,
            "market_regime_reason_codes": list(self.reason_codes),
            "market_regime_effective_at": self.effective_at.isoformat(),
            "market_regime_candidate": self.candidate_regime,
            "market_regime_candidate_streak": self.candidate_streak,
            "market_regime_confirm_bars_required": self.confirm_bars_required,
            "market_regime_raw": self.raw_regime,
            "market_regime_variant_id": self.variant_id,
            "market_regime_read_only": True,
            "market_regime_features": dict(self.feature_snapshot),
        }


def _finite(x: float, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return v


def _slope(arr: np.ndarray, n: int) -> float:
    if len(arr) < n + 1 or n <= 0:
        return float("nan")
    return float(arr[-1] - arr[-(n + 1)]) / n


def compute_market_regime_features(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    ema9: np.ndarray,
    ema20: np.ndarray,
    atr: np.ndarray,
    *,
    window: int | None = None,
    cfg: MarketRegimeConfig | None = None,
) -> MarketRegimeFeatures | None:
    """Build features from causal closed-bar arrays (prefix ending at current)."""
    config = cfg or default_market_regime_config()
    n = int(window if window is not None else config.window_bars)
    if len(close) < n + 1:
        return None
    c = np.asarray(close[-(n + 1) :], dtype=float)
    h = np.asarray(high[-(n + 1) :], dtype=float)
    l = np.asarray(low[-(n + 1) :], dtype=float)
    e9 = np.asarray(ema9[-(n + 1) :], dtype=float)
    e20 = np.asarray(ema20[-(n + 1) :], dtype=float)
    a = float(atr[-1]) if atr[-1] == atr[-1] and atr[-1] > 0 else float("nan")
    if not (a == a and a > 0):
        return None

    rets = np.diff(c)
    net = float(c[-1] - c[0])
    path = float(np.sum(np.abs(rets)))
    de = abs(net) / path if path > 1e-12 else 0.0
    rng = float(np.max(h) - np.min(l))
    progress = abs(net) / rng if rng > 1e-12 else 0.0
    up = float(np.mean(rets > 0))
    dn = float(np.mean(rets < 0))
    if net >= 0:
        mae = float(c[0] - np.min(l))
    else:
        mae = float(np.max(h) - c[0])

    s9 = _slope(e9, n)
    s20 = _slope(e20, n)
    s9_atr = s9 / a
    s20_atr = s20 / a
    sep = float(e9[-1] - e20[-1])
    sep_atr = sep / a
    sep_chg = float((e9[-1] - e20[-1]) - (e9[0] - e20[0]))
    sep_chg_atr = sep_chg / a
    above = float(np.mean((c[1:] > e9[1:]) & (c[1:] > e20[1:])))
    below = float(np.mean((c[1:] < e9[1:]) & (c[1:] < e20[1:])))
    crosses = int(np.sum(np.diff(np.sign(e9 - e20)) != 0))
    flat9 = abs(s9_atr) < config.k1_flat_slope_atr_max
    flat20 = abs(s20_atr) < config.k1_flat_slope_atr_max

    return MarketRegimeFeatures(
        ema9=float(e9[-1]),
        ema20=float(e20[-1]),
        ema9_slope_atr=_finite(s9_atr),
        ema20_slope_atr=_finite(s20_atr),
        ema_sep_atr=_finite(sep_atr),
        ema_sep_change_atr=_finite(sep_chg_atr),
        share_above_both=_finite(above),
        share_below_both=_finite(below),
        ema_crosses=crosses,
        ema_flat=bool(flat9 and flat20),
        net_move_atr=_finite(net / a),
        directional_efficiency=_finite(de),
        progress_vs_range=_finite(progress),
        up_close_share=_finite(up),
        down_close_share=_finite(dn),
        maximum_counter_move_atr=_finite(mae / a),
        close=float(c[-1]),
        atr=a,
    )


def _direction_for(regime: MarketRegimeName) -> MarketDirection:
    if regime == "strong_bullish_trend":
        return "bullish"
    if regime == "strong_bearish_trend":
        return "bearish"
    return "neutral"


def _confidence_for(regime: MarketRegimeName, feat: MarketRegimeFeatures, cfg: MarketRegimeConfig) -> float:
    de = feat.directional_efficiency
    net = abs(feat.net_move_atr)
    if regime in TREND_REGIMES:
        # soft 0..1 vs audit thresholds
        return float(
            min(
                1.0,
                0.35 * min(1.0, de / max(cfg.strong_directional_efficiency_min, 1e-9))
                + 0.35 * min(1.0, net / max(cfg.strong_net_move_atr, 1e-9))
                + 0.30 * min(1.0, feat.progress_vs_range / max(cfg.strong_progress_vs_range_min, 1e-9)),
            )
        )
    if regime == "accumulation_range":
        return float(min(1.0, 0.5 + 0.5 * (1.0 - min(1.0, de / max(cfg.range_directional_efficiency_max, 1e-9)))))
    return 0.35


@dataclass
class _RawClass:
    regime: MarketRegimeName
    reasons: list[str]


def classify_k2_raw(feat: MarketRegimeFeatures, cfg: MarketRegimeConfig | None = None) -> _RawClass:
    """Exact K2 raw label from the four-class audit (no hysteresis)."""
    config = cfg or default_market_regime_config()
    s9 = feat.ema9_slope_atr
    s20 = feat.ema20_slope_atr
    sep = feat.ema_sep_atr
    below = feat.share_below_both
    above = feat.share_above_both
    crosses = feat.ema_crosses
    flat = feat.ema_flat
    de = feat.directional_efficiency
    net_atr = feat.net_move_atr
    prog = feat.progress_vs_range

    # nested K1 for residual paths
    bearish_hint = s9 < 0 and s20 < 0 and below >= config.k1_hint_share_min
    bullish_hint = s9 > 0 and s20 > 0 and above >= config.k1_hint_share_min
    k1_regime: MarketRegimeName
    k1_reasons: list[str]
    if (
        s9 < -config.k1_ema9_slope_atr_min
        and s20 < -config.k1_ema20_slope_atr_min
        and sep < -config.k1_sep_atr_min
        and below >= config.k1_share_aligned_min
        and crosses <= config.k1_max_crosses
    ):
        k1_regime = "strong_bearish_trend"
        k1_reasons = ["ema_slopes_down", "ema9_lt_ema20", f"below_both={below:.2f}", "few_crosses"]
    elif (
        s9 > config.k1_ema9_slope_atr_min
        and s20 > config.k1_ema20_slope_atr_min
        and sep > config.k1_sep_atr_min
        and above >= config.k1_share_aligned_min
        and crosses <= config.k1_max_crosses
    ):
        k1_regime = "strong_bullish_trend"
        k1_reasons = ["ema_slopes_up", "ema9_gt_ema20", f"above_both={above:.2f}", "few_crosses"]
    elif flat or (
        crosses >= config.k1_entangle_crosses_min
        and abs(s20) < config.k1_entangle_ema20_abs_max
        and abs(s9) < config.k1_entangle_ema9_abs_max
    ):
        k1_regime = "accumulation_range"
        k1_reasons = [f"ema_crosses={int(crosses)}", "flat_or_entangled_emas"]
    else:
        k1_regime = "transition_unclear"
        k1_reasons = ["mixed_ema_geometry"]

    # K2 strong requires progress AND EMA alignment
    if (
        net_atr <= -config.strong_net_move_atr
        and de >= config.strong_directional_efficiency_min
        and prog >= config.strong_progress_vs_range_min
        and below >= config.strong_share_aligned_min
        and s20 < -config.strong_ema20_slope_atr_min
        and s9 < -config.strong_ema9_slope_atr_min
        and sep <= 0
    ):
        return _RawClass(
            "strong_bearish_trend",
            ["neg_net_atr", f"de={de:.2f}", f"prog={prog:.2f}", "ema_down", f"below={below:.2f}"],
        )
    if (
        net_atr >= config.strong_net_move_atr
        and de >= config.strong_directional_efficiency_min
        and prog >= config.strong_progress_vs_range_min
        and above >= config.strong_share_aligned_min
        and s20 > config.strong_ema20_slope_atr_min
        and s9 > config.strong_ema9_slope_atr_min
        and sep >= 0
    ):
        return _RawClass(
            "strong_bullish_trend",
            ["pos_net_atr", f"de={de:.2f}", f"prog={prog:.2f}", "ema_up", f"above={above:.2f}"],
        )
    if (
        abs(net_atr) < config.range_net_move_atr_max
        and de < config.range_directional_efficiency_max
        and prog < config.range_progress_vs_range_max
    ):
        return _RawClass(
            "accumulation_range",
            [f"low_de={de:.2f}", f"small_net_atr={net_atr:.2f}", f"low_prog={prog:.2f}"],
        )
    if k1_regime in TREND_REGIMES and de < config.ema_strong_insufficient_de_max:
        return _RawClass(
            "transition_unclear",
            ["ema_trendish_but_insufficient_progress", *k1_reasons],
        )
    if k1_regime == "accumulation_range":
        return _RawClass(k1_regime, k1_reasons)
    # residual — hints only affect reason trail, not class
    reasons = ["k2_residual", f"de={de:.2f}", f"net_atr={net_atr:.2f}", f"sep={sep:.2f}"]
    if bearish_hint or (net_atr < -config.residual_net_hint_atr and s20 < 0):
        reasons.append("bearish_lean")
    if bullish_hint or (net_atr > config.residual_net_hint_atr and s20 > 0):
        reasons.append("bullish_lean")
    return _RawClass("transition_unclear", reasons)


def h4_confirm_bars(dst: MarketRegimeName, cfg: MarketRegimeConfig) -> int:
    """H4 confirmation lengths from the audit."""
    if dst in TREND_REGIMES:
        return int(cfg.trend_confirm_bars)
    if dst == "accumulation_range":
        return int(cfg.range_confirm_bars)
    return int(cfg.transition_confirm_bars)


def _as_utc(dt: datetime | Any) -> datetime:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    # pandas Timestamp / string
    import pandas as pd

    t = pd.Timestamp(dt)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


class MarketRegimeClassifier:
    """Stateful K2 classifier with H4 hysteresis. Deterministic, causal, no rewrite."""

    def __init__(self, cfg: MarketRegimeConfig | None = None) -> None:
        self.cfg = cfg or default_market_regime_config()
        self._current: MarketRegimeName | None = None
        self._pending: MarketRegimeName | None = None
        self._pend_count: int = 0
        self._last: MarketRegimeContext | None = None

    def reset(self) -> None:
        self._current = None
        self._pending = None
        self._pend_count = 0
        self._last = None

    @property
    def current_regime(self) -> MarketRegimeName | None:
        return self._current

    def update(
        self,
        *,
        decision_time: datetime | Any,
        features: MarketRegimeFeatures,
        candle: Mapping[str, Any] | None = None,  # accepted for API clarity; unused
    ) -> MarketRegimeContext:
        del candle  # closed-candle features are the sole causal input
        raw = classify_k2_raw(features, self.cfg)
        effective = _as_utc(decision_time)
        snap = features.as_mapping()

        # First observation: adopt immediately (audit H4 / H0 first-bar behaviour).
        if self._current is None:
            self._current = raw.regime
            self._pending = None
            self._pend_count = 0
            ctx = MarketRegimeContext(
                regime=raw.regime,
                direction=_direction_for(raw.regime),
                confidence=_confidence_for(raw.regime, features, self.cfg),
                reason_codes=tuple(raw.reasons),
                effective_at=effective,
                feature_snapshot=snap,
                candidate_regime=None,
                candidate_streak=0,
                confirm_bars_required=0,
                raw_regime=raw.regime,
                variant_id=self.cfg.variant_id,
            )
            self._last = ctx
            return ctx

        assert self._current is not None
        cur = self._current
        dst = raw.regime
        if dst == cur:
            self._pending = None
            self._pend_count = 0
            ctx = MarketRegimeContext(
                regime=cur,
                direction=_direction_for(cur),
                confidence=_confidence_for(cur, features, self.cfg),
                reason_codes=tuple(raw.reasons),
                effective_at=effective,
                feature_snapshot=snap,
                candidate_regime=None,
                candidate_streak=0,
                confirm_bars_required=0,
                raw_regime=raw.regime,
                variant_id=self.cfg.variant_id,
            )
            self._last = ctx
            return ctx

        if self._pending != dst:
            self._pending = dst
            self._pend_count = 1
        else:
            self._pend_count += 1
        req = h4_confirm_bars(dst, self.cfg)
        if self._pend_count >= req:
            self._current = dst
            self._pending = None
            self._pend_count = 0
            ctx = MarketRegimeContext(
                regime=dst,
                direction=_direction_for(dst),
                confidence=_confidence_for(dst, features, self.cfg),
                reason_codes=tuple(raw.reasons),
                effective_at=effective,
                feature_snapshot=snap,
                candidate_regime=None,
                candidate_streak=0,
                confirm_bars_required=0,
                raw_regime=raw.regime,
                variant_id=self.cfg.variant_id,
            )
            self._last = ctx
            return ctx

        # Hold previous class (bounce / incomplete confirmation).
        hold_reasons = (f"hyst_hold_H4:{self._pend_count}/{req}", *raw.reasons)
        ctx = MarketRegimeContext(
            regime=cur,
            direction=_direction_for(cur),
            confidence=_confidence_for(cur, features, self.cfg),
            reason_codes=hold_reasons,
            effective_at=effective,
            feature_snapshot=snap,
            candidate_regime=dst,
            candidate_streak=self._pend_count,
            confirm_bars_required=req,
            raw_regime=raw.regime,
            variant_id=self.cfg.variant_id,
        )
        self._last = ctx
        return ctx


def attach_readonly_market_regime(
    snapshot: MutableMapping[str, Any] | Mapping[str, Any],
    ctx: MarketRegimeContext | None,
) -> dict[str, Any]:
    """Merge read-only market-regime fields into a pipeline / trend snapshot dict.

    Never overwrites allow_long / allow_short / combined_regime / trend_state.
    """
    out = dict(snapshot)
    if ctx is None:
        out["market_regime"] = None
        out["market_regime_read_only"] = True
        out["market_regime_variant_id"] = default_market_regime_config().variant_id
        return out
    payload = ctx.to_dict()
    # Flatten primary keys; keep features nested.
    for key in (
        "market_regime",
        "market_regime_direction",
        "market_regime_confidence",
        "market_regime_reason_codes",
        "market_regime_effective_at",
        "market_regime_candidate",
        "market_regime_candidate_streak",
        "market_regime_confirm_bars_required",
        "market_regime_raw",
        "market_regime_variant_id",
        "market_regime_read_only",
        "market_regime_features",
    ):
        out[key] = payload[key]
    return out


def market_regime_hysteresis_docs() -> dict[str, Any]:
    """Document H4 behaviour exactly as audited (no new rules)."""
    cfg = default_market_regime_config()
    return {
        "variant": cfg.variant_id,
        "first_bar": "raw label adopted immediately (no confirmation)",
        "trend_confirm_bars": cfg.trend_confirm_bars,
        "range_confirm_bars": cfg.range_confirm_bars,
        "transition_confirm_bars": cfg.transition_confirm_bars,
        "opposite_trend_direct_switch": True,
        "opposite_trend_note": (
            "H4 allows strong_bullish ↔ strong_bearish after trend_confirm_bars "
            "consecutive raw opposite labels. Forced via transition_unclear is H3 only."
        ),
        "short_bounce": (
            "A 1-bar raw leave of a trend holds the prior trend (hyst_hold_H4:1/2). "
            "Two consecutive non-trend raw bars (e.g. transition_unclear) switch after "
            "transition_confirm_bars. Three consecutive range raw bars needed for "
            "accumulation_range."
        ),
        "no_rewrite": "Past labels are never rewritten; only forward state updates.",
    }


__all__ = [
    "MarketRegimeConfig",
    "MarketRegimeContext",
    "MarketRegimeFeatures",
    "MarketRegimeClassifier",
    "MarketRegimeName",
    "MarketDirection",
    "attach_readonly_market_regime",
    "classify_k2_raw",
    "compute_market_regime_features",
    "default_market_regime_config",
    "h4_confirm_bars",
    "market_regime_hysteresis_docs",
]

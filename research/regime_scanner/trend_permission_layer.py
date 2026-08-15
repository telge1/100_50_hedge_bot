"""Additive multi-timeframe trend permission layer (research-only).

C3.4B Protected Structure remains the sole structural ``major_direction`` truth.
This module never mutates C3.4B columns or ``step_protected_structure_state``.

EMAs are recomputed from closed-bar OHLC when missing (same ``indicators.ema``),
and never flip ``major_direction``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from research.regime_scanner.indicators import atr_wilder, ema

# ---------------------------------------------------------------------------
# Enumerations (string contracts)
# ---------------------------------------------------------------------------

EMA_REGIMES: tuple[str, ...] = (
    "STRONG_BULLISH",
    "BULLISH",
    "BULLISH_PULLBACK",
    "NEUTRAL",
    "BEARISH_PULLBACK",
    "BEARISH",
    "STRONG_BEARISH",
    "UNKNOWN",
)

STRUCTURE_DIRECTIONS: tuple[str, ...] = ("BULLISH", "BEARISH", "UNKNOWN")

TRANSITION_STATES: tuple[str, ...] = (
    "NONE",
    "BULLISH_CHOCH_PENDING",
    "BEARISH_CHOCH_PENDING",
    "BULLISH_BREAK_FAILED",
    "BEARISH_BREAK_FAILED",
    "TRANSITION_BLOCKED",
)

STRUCTURE_EMA_ALIGNMENTS: tuple[str, ...] = (
    "ALIGNED_BULLISH",
    "ALIGNED_BEARISH",
    "STRUCTURE_BULLISH_EMA_CONFLICT",
    "STRUCTURE_BEARISH_EMA_CONFLICT",
    "TRANSITION",
    "UNKNOWN",
)

PRIMARY_DIRECTIONS: tuple[str, ...] = ("LONG", "SHORT", "NEUTRAL", "CONFLICT")

MTF_STATES: tuple[str, ...] = (
    "FULL_BULLISH_ALIGNMENT",
    "FULL_BEARISH_ALIGNMENT",
    "BULLISH_HTF_PULLBACK",
    "BEARISH_HTF_PULLBACK",
    "BULLISH_TRANSITION",
    "BEARISH_TRANSITION",
    "MIXED_TIMEFRAMES",
    "RANGE_OR_UNKNOWN",
)

TRADE_PERMISSIONS: tuple[str, ...] = (
    "LONG_ALLOWED",
    "SHORT_ALLOWED",
    "WAIT_FOR_LONG_TRIGGER",
    "WAIT_FOR_SHORT_TRIGGER",
    "BLOCK_BOTH",
)

DEFAULT_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "1h", "4h")

_BULLISH_EMA = frozenset({"STRONG_BULLISH", "BULLISH", "BULLISH_PULLBACK"})
_BEARISH_EMA = frozenset({"STRONG_BEARISH", "BEARISH", "BEARISH_PULLBACK"})
_STRONG_BEARISH_EMA = frozenset({"STRONG_BEARISH", "BEARISH"})
_STRONG_BULLISH_EMA = frozenset({"STRONG_BULLISH", "BULLISH"})


@dataclass(frozen=True)
class TrendPermissionConfig:
    """Central config for additive MTF permission (no magic numbers in logic)."""

    enabled_timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    required_timeframes: tuple[str, ...] = ("5m", "15m", "1h", "4h")
    structure_is_primary: bool = True
    require_4h_direction: bool = True
    require_1h_confirmation: bool = True
    use_15m_setup: bool = True
    use_5m_trigger: bool = True
    ema_gate_enabled: bool = True
    ema_slope_lookback: int = 3
    ema_flat_threshold: float = 0.05  # |slope|/price pct threshold for "flat"
    ema_spread_flat_pct: float = 0.15  # |ema9-ema20|/close * 100
    ema_conflict_policy: str = "block"  # block | allow_wait | ignore
    block_on_htf_choch_pending: bool = True
    block_on_missing_warmup: bool = True
    allow_neutral_5m_for_wait_state: bool = True
    require_trigger_for_allowed: bool = True
    ema_fast: int = 9
    ema_mid_fast: int = 20
    ema_medium: int = 59
    ema_long: int = 200
    ema_warmup_bars: int = 200
    confidence_full_align: float = 0.90
    confidence_htf_pullback: float = 0.70
    confidence_wait: float = 0.55
    confidence_block: float = 0.40
    confidence_allowed: float = 0.85
    trigger_need_bos_or_choch_confirm: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_trend_permission_config() -> TrendPermissionConfig:
    return TrendPermissionConfig()


def _finite(v: Any, default: float = float("nan")) -> float:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def structure_direction_from_major(major: Any) -> str:
    """Map sticky major_direction only — never CHOCH state labels."""
    try:
        if major is None or (isinstance(major, float) and np.isnan(major)):
            m = 0
        else:
            m = int(major)
    except (TypeError, ValueError):
        m = 0
    if m > 0:
        return "BULLISH"
    if m < 0:
        return "BEARISH"
    return "UNKNOWN"


def transition_state_from_structure(
    *,
    major_direction: Any,
    protected_structure_state: Any,
) -> str:
    """CHOCH pending keeps major sticky; expose transition separately."""
    del major_direction  # documented: major stays sticky; state drives transition
    state = str(protected_structure_state or "").strip().lower()
    if state == "transition_blocked":
        return "TRANSITION_BLOCKED"
    if state == "bullish_break_failed":
        return "BULLISH_BREAK_FAILED"
    if state == "bearish_break_failed":
        return "BEARISH_BREAK_FAILED"
    if state in {"bullish_choch", "bullish_structure_candidate", "bullish_retest_pending"}:
        return "BULLISH_CHOCH_PENDING"
    if state in {"bearish_choch", "bearish_structure_candidate", "bearish_retest_pending"}:
        return "BEARISH_CHOCH_PENDING"
    return "NONE"


def ensure_ema_columns(
    frame: pd.DataFrame,
    *,
    cfg: TrendPermissionConfig,
) -> pd.DataFrame:
    """Ensure EMA / ATR columns exist using closed-bar close (causal ewm)."""
    out = frame.copy()
    if "close" not in out.columns:
        raise ValueError("frame requires close")
    close = pd.to_numeric(out["close"], errors="coerce").astype("float64")
    high = pd.to_numeric(out["high"], errors="coerce").astype("float64") if "high" in out.columns else close
    low = pd.to_numeric(out["low"], errors="coerce").astype("float64") if "low" in out.columns else close
    periods = sorted({cfg.ema_fast, cfg.ema_mid_fast, cfg.ema_medium, cfg.ema_long})
    for p in periods:
        col = f"ema_{p}"
        if col not in out.columns:
            out[col] = ema(close, p)
    if "atr_14" not in out.columns:
        out["atr_14"] = atr_wilder(high, low, close, 14)
    lb = int(cfg.ema_slope_lookback)
    for p in (cfg.ema_fast, cfg.ema_mid_fast):
        col = f"ema_{p}"
        slope_col = f"ema_{p}_slope"
        if slope_col not in out.columns:
            out[slope_col] = out[col] - out[col].shift(lb)
    return out


def classify_ema_regime_row(
    row: Mapping[str, Any],
    *,
    cfg: TrendPermissionConfig,
    bar_index: int,
    structure_direction: str,
) -> dict[str, Any]:
    """Classify one closed bar EMA regime. Missing warmup → UNKNOWN."""
    if bar_index < int(cfg.ema_warmup_bars):
        return {
            "ema_regime": "UNKNOWN",
            "ema_confidence": 0.0,
            "price_position": "UNKNOWN",
            "ema_stack": "not_ready",
            "ema_slope_state": "not_ready",
            "close_above_ema9": None,
            "close_above_ema20": None,
            "close_above_medium_ema": None,
            "close_above_long_ema": None,
            "ema9_above_ema20": None,
            "medium_above_long": None,
            "ema9_slope": None,
            "ema20_slope": None,
            "ema_spread_pct": None,
            "ema_regime_reason": "insufficient_ema_warmup",
        }

    close = _finite(row.get("close"))
    e9 = _finite(row.get(f"ema_{cfg.ema_fast}"))
    e20 = _finite(row.get(f"ema_{cfg.ema_mid_fast}"))
    e_med = _finite(row.get(f"ema_{cfg.ema_medium}"))
    e_long = _finite(row.get(f"ema_{cfg.ema_long}"))
    s9 = _finite(row.get(f"ema_{cfg.ema_fast}_slope"), 0.0)
    s20 = _finite(row.get(f"ema_{cfg.ema_mid_fast}_slope"), 0.0)

    if not all(np.isfinite(x) for x in (close, e9, e20, e_med, e_long)) or close <= 0:
        return {
            "ema_regime": "UNKNOWN",
            "ema_confidence": 0.0,
            "price_position": "UNKNOWN",
            "ema_stack": "not_ready",
            "ema_slope_state": "not_ready",
            "close_above_ema9": None,
            "close_above_ema20": None,
            "close_above_medium_ema": None,
            "close_above_long_ema": None,
            "ema9_above_ema20": None,
            "medium_above_long": None,
            "ema9_slope": None,
            "ema20_slope": None,
            "ema_spread_pct": None,
            "ema_regime_reason": "missing_ema_values",
        }

    flat_abs = abs(close) * (cfg.ema_flat_threshold / 100.0)
    spread_pct = abs(e9 - e20) / close * 100.0
    close_above_9 = close > e9
    close_above_20 = close > e20
    close_above_med = close > e_med
    close_above_long = close > e_long
    e9_above_e20 = e9 > e20
    med_above_long = e_med > e_long
    slope9_up = s9 > flat_abs
    slope9_dn = s9 < -flat_abs
    slope20_up = s20 > flat_abs
    slope20_dn = s20 < -flat_abs
    slope_flat = abs(s9) <= flat_abs and abs(s20) <= flat_abs

    if close_above_9 and e9_above_e20 and e20 > e_med and med_above_long:
        stack = "bullish_full"
    elif (not close_above_9) and (not e9_above_e20) and e20 < e_med and (not med_above_long):
        stack = "bearish_full"
    elif close_above_20 and e9_above_e20:
        stack = "bullish_partial"
    elif (not close_above_20) and (not e9_above_e20):
        stack = "bearish_partial"
    else:
        stack = "mixed"

    if slope9_up and slope20_up:
        slope_state = "bullish_aligned"
    elif slope9_dn and slope20_dn:
        slope_state = "bearish_aligned"
    elif slope_flat:
        slope_state = "flat"
    else:
        slope_state = "mixed"

    if close_above_long and close_above_med and close_above_20:
        price_position = "ABOVE_STACK"
    elif (not close_above_long) and (not close_above_med) and (not close_above_20):
        price_position = "BELOW_STACK"
    else:
        price_position = "MIXED"

    strong_bull = close_above_9 and e9_above_e20 and e20 > e_med and slope9_up and slope20_up
    strong_bear = (not close_above_9) and (not e9_above_e20) and e20 < e_med and slope9_dn and slope20_dn
    bull = close_above_20 and (e9_above_e20 or slope9_up) and not (
        e20 < e_med and not med_above_long and slope20_dn
    )
    bear = (not close_above_20) and ((not e9_above_e20) or slope9_dn) and not (
        e20 > e_med and med_above_long and slope20_up
    )

    if strong_bull:
        regime, conf, reason_parts = "STRONG_BULLISH", 0.9, ["strong_bull_stack_slopes"]
    elif strong_bear:
        regime, conf, reason_parts = "STRONG_BEARISH", 0.9, ["strong_bear_stack_slopes"]
    elif (
        structure_direction == "BULLISH"
        and bull
        and (not close_above_9 or not close_above_20)
        and close_above_med
    ):
        regime, conf, reason_parts = "BULLISH_PULLBACK", 0.75, ["struct_bull_pullback_into_fast_ema"]
    elif (
        structure_direction == "BEARISH"
        and bear
        and (close_above_9 or close_above_20)
        and (not close_above_med)
    ):
        regime, conf, reason_parts = "BEARISH_PULLBACK", 0.75, ["struct_bear_pullback_into_fast_ema"]
    elif bull and not strong_bear:
        regime, conf, reason_parts = "BULLISH", 0.7, ["bullish_partial"]
    elif bear and not strong_bull:
        regime, conf, reason_parts = "BEARISH", 0.7, ["bearish_partial"]
    elif spread_pct <= cfg.ema_spread_flat_pct or slope_flat:
        regime, conf, reason_parts = "NEUTRAL", 0.55, ["flat_or_tight_spread"]
    else:
        regime, conf, reason_parts = "NEUTRAL", 0.5, ["conflicting_ema_signals"]

    return {
        "ema_regime": regime,
        "ema_confidence": float(conf),
        "price_position": price_position,
        "ema_stack": stack,
        "ema_slope_state": slope_state,
        "close_above_ema9": bool(close_above_9),
        "close_above_ema20": bool(close_above_20),
        "close_above_medium_ema": bool(close_above_med),
        "close_above_long_ema": bool(close_above_long),
        "ema9_above_ema20": bool(e9_above_e20),
        "medium_above_long": bool(med_above_long),
        "ema9_slope": float(s9),
        "ema20_slope": float(s20),
        "ema_spread_pct": float(spread_pct),
        "ema_regime_reason": "|".join(reason_parts),
    }


def structure_ema_alignment(
    structure_direction: str,
    transition_state: str,
    ema_regime: str,
) -> str:
    if transition_state != "NONE":
        return "TRANSITION"
    if structure_direction == "UNKNOWN" or ema_regime == "UNKNOWN":
        return "UNKNOWN"
    if structure_direction == "BULLISH" and ema_regime in _BULLISH_EMA:
        return "ALIGNED_BULLISH"
    if structure_direction == "BEARISH" and ema_regime in _BEARISH_EMA:
        return "ALIGNED_BEARISH"
    if structure_direction == "BULLISH" and ema_regime in _STRONG_BEARISH_EMA:
        return "STRUCTURE_BULLISH_EMA_CONFLICT"
    if structure_direction == "BEARISH" and ema_regime in _STRONG_BULLISH_EMA:
        return "STRUCTURE_BEARISH_EMA_CONFLICT"
    if structure_direction == "BULLISH":
        return "ALIGNED_BULLISH" if ema_regime == "NEUTRAL" else "STRUCTURE_BULLISH_EMA_CONFLICT"
    if structure_direction == "BEARISH":
        return "ALIGNED_BEARISH" if ema_regime == "NEUTRAL" else "STRUCTURE_BEARISH_EMA_CONFLICT"
    return "UNKNOWN"


def enrich_tf_permission_context(
    structure: pd.DataFrame,
    *,
    cfg: TrendPermissionConfig | None = None,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """Add per-TF structure_direction / transition / ema_regime columns."""
    cfg = cfg or default_trend_permission_config()
    if structure.empty:
        return structure.copy()
    out = ensure_ema_columns(structure, cfg=cfg)
    maj = out["major_direction"] if "major_direction" in out.columns else pd.Series(0, index=out.index)
    states = (
        out["protected_structure_state"]
        if "protected_structure_state" in out.columns
        else out.get("trend_state", pd.Series("", index=out.index))
    )
    rows: list[dict[str, Any]] = []
    for i in range(len(out)):
        row = out.iloc[i]
        sd = structure_direction_from_major(row.get("major_direction", maj.iloc[i]))
        ts = transition_state_from_structure(
            major_direction=row.get("major_direction", maj.iloc[i]),
            protected_structure_state=row.get("protected_structure_state", states.iloc[i]),
        )
        ema_info = classify_ema_regime_row(
            row,
            cfg=cfg,
            bar_index=i,
            structure_direction=sd,
        )
        align = structure_ema_alignment(sd, ts, str(ema_info["ema_regime"]))
        rows.append(
            {
                "structure_direction": sd,
                "transition_state": ts,
                "structure_ema_alignment": align,
                **ema_info,
            }
        )
    extra = pd.DataFrame(rows)
    for c in extra.columns:
        out[c] = extra[c].values
    if timeframe is not None:
        out["timeframe"] = str(timeframe).strip().lower()
    return out


def _tf_get(row: Mapping[str, Any], field: str, tf: str) -> Any:
    if tf == "5m":
        if field in row and row.get(field) is not None:
            return row.get(field)
        return row.get(f"{field}_5m")
    return row.get(f"{field}_{tf}")


def _sd(row: Mapping[str, Any], tf: str) -> str:
    v = _tf_get(row, "structure_direction", tf)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return structure_direction_from_major(_tf_get(row, "major_direction", tf))
    return str(v)


def _tr(row: Mapping[str, Any], tf: str) -> str:
    v = _tf_get(row, "transition_state", tf)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return transition_state_from_structure(
            major_direction=_tf_get(row, "major_direction", tf),
            protected_structure_state=_tf_get(row, "protected_structure_state", tf)
            or _tf_get(row, "trend_state", tf),
        )
    return str(v)


def _er(row: Mapping[str, Any], tf: str) -> str:
    v = _tf_get(row, "ema_regime", tf)
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "UNKNOWN"
    return str(v)


def _is_bull(sd: str) -> bool:
    return sd == "BULLISH"


def _is_bear(sd: str) -> bool:
    return sd == "BEARISH"


def _htf_choch_pending(row: Mapping[str, Any]) -> bool:
    for tf in ("4h", "1h"):
        tr = _tr(row, tf)
        if tr in {"BULLISH_CHOCH_PENDING", "BEARISH_CHOCH_PENDING"}:
            return True
    return False


def _missing_required(row: Mapping[str, Any], cfg: TrendPermissionConfig) -> bool:
    for tf in cfg.required_timeframes:
        avail = _tf_get(row, "available_at", tf)
        if avail is None or (isinstance(avail, float) and np.isnan(avail)):
            return True
        er = _er(row, tf)
        if cfg.block_on_missing_warmup and er == "UNKNOWN" and tf in ("4h", "1h"):
            return True
    return False


def _5m_bull_trigger(row: Mapping[str, Any], cfg: TrendPermissionConfig) -> bool:
    if not cfg.require_trigger_for_allowed:
        return True
    sd = _sd(row, "5m")
    tr = _tr(row, "5m")
    if tr == "BULLISH_CHOCH_PENDING":
        return False
    bos = bool(_tf_get(row, "external_bos_up", "5m") or _tf_get(row, "bullish_bos", "5m"))
    if sd == "BULLISH" and tr == "NONE":
        return True
    if bos and sd == "BULLISH":
        return True
    return False


def _5m_bear_trigger(row: Mapping[str, Any], cfg: TrendPermissionConfig) -> bool:
    if not cfg.require_trigger_for_allowed:
        return True
    sd = _sd(row, "5m")
    tr = _tr(row, "5m")
    if tr == "BEARISH_CHOCH_PENDING":
        return False
    bos = bool(_tf_get(row, "external_bos_down", "5m") or _tf_get(row, "bearish_bos", "5m"))
    if sd == "BEARISH" and tr == "NONE":
        return True
    if bos and sd == "BEARISH":
        return True
    return False


def _iso_safe(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        t = pd.Timestamp(v)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        return t.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return str(v)


def _stamp_sources(row: Mapping[str, Any], decision_available_at: str | None) -> dict[str, Any]:
    return {
        "decision_available_at": decision_available_at,
        "source_5m_available_at": _iso_safe(_tf_get(row, "available_at", "5m")),
        "source_15m_available_at": _iso_safe(_tf_get(row, "available_at", "15m")),
        "source_1h_available_at": _iso_safe(_tf_get(row, "available_at", "1h")),
        "source_4h_available_at": _iso_safe(_tf_get(row, "available_at", "4h")),
    }


def decide_mtf_permission(
    row: Mapping[str, Any],
    *,
    cfg: TrendPermissionConfig | None = None,
) -> dict[str, Any]:
    """Decide primary_direction / mtf_state / trade_permission for one causal snapshot."""
    cfg = cfg or default_trend_permission_config()

    d4 = _sd(row, "4h")
    d1 = _sd(row, "1h")
    d15 = _sd(row, "15m") if cfg.use_15m_setup else "UNKNOWN"
    d5 = _sd(row, "5m")
    t4, t1 = _tr(row, "4h"), _tr(row, "1h")
    e4, e1, e15, e5 = _er(row, "4h"), _er(row, "1h"), _er(row, "15m"), _er(row, "5m")

    avails = []
    for tf in cfg.required_timeframes:
        a = _tf_get(row, "available_at", tf)
        if a is not None and not (isinstance(a, float) and np.isnan(a)):
            try:
                avails.append(pd.Timestamp(a))
            except (TypeError, ValueError):
                pass
    decision_available_at = max(avails).isoformat().replace("+00:00", "Z") if avails else None
    stamps = _stamp_sources(row, decision_available_at)

    def _block(reason: str, mtf_state: str = "RANGE_OR_UNKNOWN", conf: float | None = None) -> dict[str, Any]:
        primary = "NEUTRAL" if mtf_state == "RANGE_OR_UNKNOWN" else "CONFLICT"
        return {
            "primary_direction": primary,
            "htf_direction": d4,
            "setup_direction": d15,
            "trigger_direction": d5,
            "mtf_state": mtf_state,
            "mtf_confidence": float(conf if conf is not None else cfg.confidence_block),
            "conflict_reason": reason,
            "trade_permission": "BLOCK_BOTH",
            "long_allowed": False,
            "short_allowed": False,
            "wait_for_trigger": False,
            "blocked_side": "BOTH",
            "permission_confidence": float(conf if conf is not None else cfg.confidence_block),
            "permission_reason": reason,
            "invalidation_reason": reason,
            **stamps,
        }

    if _missing_required(row, cfg):
        return _block("missing_required_tf_or_warmup")

    if cfg.require_4h_direction and d4 == "UNKNOWN":
        return _block("htf_4h_unknown")

    if cfg.block_on_htf_choch_pending and _htf_choch_pending(row):
        mtf = (
            "BULLISH_TRANSITION"
            if t4 == "BULLISH_CHOCH_PENDING" or t1 == "BULLISH_CHOCH_PENDING"
            else "BEARISH_TRANSITION"
        )
        return _block("htf_choch_pending", mtf_state=mtf)

    if _is_bull(d4) and _is_bear(d1):
        return _block("4h_bull_1h_bear_conflict", mtf_state="MIXED_TIMEFRAMES")
    if _is_bear(d4) and _is_bull(d1):
        return _block("4h_bear_1h_bull_conflict", mtf_state="MIXED_TIMEFRAMES")

    if cfg.ema_gate_enabled and cfg.ema_conflict_policy == "block":
        if _is_bull(d4) and e4 in _STRONG_BEARISH_EMA:
            return _block("4h_structure_bull_ema_strong_bear", mtf_state="MIXED_TIMEFRAMES")
        if _is_bear(d4) and e4 in _STRONG_BULLISH_EMA:
            return _block("4h_structure_bear_ema_strong_bull", mtf_state="MIXED_TIMEFRAMES")

    # Bullish HTF
    if _is_bull(d4) and (d1 in {"BULLISH", "UNKNOWN"} or (not cfg.require_1h_confirmation)):
        if cfg.require_1h_confirmation and d1 == "UNKNOWN":
            return _block("1h_unknown_no_confirmation")
        ema_ok = (not cfg.ema_gate_enabled) or (
            e4 not in _STRONG_BEARISH_EMA and e1 not in _STRONG_BEARISH_EMA
        )
        if not ema_ok:
            return _block("htf_ema_against_bull_structure", mtf_state="MIXED_TIMEFRAMES")

        ltf_bear = _is_bear(d15) or _is_bear(d5)
        if ltf_bear and d1 != "BEARISH":
            if not _5m_bull_trigger(row, cfg) or _is_bear(d15):
                return {
                    "primary_direction": "LONG",
                    "htf_direction": d4,
                    "setup_direction": d15,
                    "trigger_direction": d5,
                    "mtf_state": "BULLISH_HTF_PULLBACK",
                    "mtf_confidence": cfg.confidence_htf_pullback,
                    "conflict_reason": "",
                    "trade_permission": "WAIT_FOR_LONG_TRIGGER",
                    "long_allowed": False,
                    "short_allowed": False,
                    "wait_for_trigger": True,
                    "blocked_side": "SHORT",
                    "permission_confidence": cfg.confidence_wait,
                    "permission_reason": "bullish_htf_ltf_pullback_wait_trigger",
                    "invalidation_reason": "htf_pl_break_or_1h_bear_confirm",
                    **stamps,
                }

        align_15 = (not cfg.use_15m_setup) or d15 in {"BULLISH", "UNKNOWN"} or e15 in {
            "BULLISH_PULLBACK",
            "BULLISH",
            "STRONG_BULLISH",
            "NEUTRAL",
            "UNKNOWN",
        }
        if _is_bull(d4) and _is_bull(d1) and align_15 and ema_ok:
            if _5m_bull_trigger(row, cfg) and not _is_bear(d5):
                return {
                    "primary_direction": "LONG",
                    "htf_direction": d4,
                    "setup_direction": d15,
                    "trigger_direction": d5,
                    "mtf_state": "FULL_BULLISH_ALIGNMENT",
                    "mtf_confidence": cfg.confidence_full_align,
                    "conflict_reason": "",
                    "trade_permission": "LONG_ALLOWED",
                    "long_allowed": True,
                    "short_allowed": False,
                    "wait_for_trigger": False,
                    "blocked_side": "SHORT",
                    "permission_confidence": cfg.confidence_allowed,
                    "permission_reason": "full_bullish_alignment_with_trigger",
                    "invalidation_reason": "htf_choch_pending_or_pl_break",
                    **stamps,
                }
            return {
                "primary_direction": "LONG",
                "htf_direction": d4,
                "setup_direction": d15,
                "trigger_direction": d5,
                "mtf_state": "FULL_BULLISH_ALIGNMENT",
                "mtf_confidence": cfg.confidence_wait,
                "conflict_reason": "",
                "trade_permission": "WAIT_FOR_LONG_TRIGGER",
                "long_allowed": False,
                "short_allowed": False,
                "wait_for_trigger": True,
                "blocked_side": "SHORT",
                "permission_confidence": cfg.confidence_wait,
                "permission_reason": "bullish_context_await_5m_trigger",
                "invalidation_reason": "htf_flip",
                **stamps,
            }

    # Bearish HTF (mirror)
    if _is_bear(d4) and (d1 in {"BEARISH", "UNKNOWN"} or (not cfg.require_1h_confirmation)):
        if cfg.require_1h_confirmation and d1 == "UNKNOWN":
            return _block("1h_unknown_no_confirmation")
        ema_ok = (not cfg.ema_gate_enabled) or (
            e4 not in _STRONG_BULLISH_EMA and e1 not in _STRONG_BULLISH_EMA
        )
        if not ema_ok:
            return _block("htf_ema_against_bear_structure", mtf_state="MIXED_TIMEFRAMES")

        ltf_bull = _is_bull(d15) or _is_bull(d5)
        if ltf_bull and d1 != "BULLISH":
            if not _5m_bear_trigger(row, cfg) or _is_bull(d15):
                return {
                    "primary_direction": "SHORT",
                    "htf_direction": d4,
                    "setup_direction": d15,
                    "trigger_direction": d5,
                    "mtf_state": "BEARISH_HTF_PULLBACK",
                    "mtf_confidence": cfg.confidence_htf_pullback,
                    "conflict_reason": "",
                    "trade_permission": "WAIT_FOR_SHORT_TRIGGER",
                    "long_allowed": False,
                    "short_allowed": False,
                    "wait_for_trigger": True,
                    "blocked_side": "LONG",
                    "permission_confidence": cfg.confidence_wait,
                    "permission_reason": "bearish_htf_ltf_pullback_wait_trigger",
                    "invalidation_reason": "htf_ph_break_or_1h_bull_confirm",
                    **stamps,
                }

        align_15 = (not cfg.use_15m_setup) or d15 in {"BEARISH", "UNKNOWN"} or e15 in {
            "BEARISH_PULLBACK",
            "BEARISH",
            "STRONG_BEARISH",
            "NEUTRAL",
            "UNKNOWN",
        }
        if _is_bear(d4) and _is_bear(d1) and align_15 and ema_ok:
            if _5m_bear_trigger(row, cfg) and not _is_bull(d5):
                return {
                    "primary_direction": "SHORT",
                    "htf_direction": d4,
                    "setup_direction": d15,
                    "trigger_direction": d5,
                    "mtf_state": "FULL_BEARISH_ALIGNMENT",
                    "mtf_confidence": cfg.confidence_full_align,
                    "conflict_reason": "",
                    "trade_permission": "SHORT_ALLOWED",
                    "long_allowed": False,
                    "short_allowed": True,
                    "wait_for_trigger": False,
                    "blocked_side": "LONG",
                    "permission_confidence": cfg.confidence_allowed,
                    "permission_reason": "full_bearish_alignment_with_trigger",
                    "invalidation_reason": "htf_choch_pending_or_ph_break",
                    **stamps,
                }
            return {
                "primary_direction": "SHORT",
                "htf_direction": d4,
                "setup_direction": d15,
                "trigger_direction": d5,
                "mtf_state": "FULL_BEARISH_ALIGNMENT",
                "mtf_confidence": cfg.confidence_wait,
                "conflict_reason": "",
                "trade_permission": "WAIT_FOR_SHORT_TRIGGER",
                "long_allowed": False,
                "short_allowed": False,
                "wait_for_trigger": True,
                "blocked_side": "LONG",
                "permission_confidence": cfg.confidence_wait,
                "permission_reason": "bearish_context_await_5m_trigger",
                "invalidation_reason": "htf_flip",
                **stamps,
            }

    if (_is_bull(d5) and _is_bear(d4)) or (_is_bear(d5) and _is_bull(d4)):
        return _block("ltf_against_htf_no_override", mtf_state="MIXED_TIMEFRAMES")

    return _block("no_clear_mtf_permission_path", mtf_state="RANGE_OR_UNKNOWN")


def apply_mtf_permission_layer(
    mtf: pd.DataFrame,
    *,
    cfg: TrendPermissionConfig | None = None,
) -> pd.DataFrame:
    """Apply permission decision to each asof-joined 5m row."""
    cfg = cfg or default_trend_permission_config()
    if mtf.empty:
        return mtf.copy()
    out = mtf.copy()
    decisions = [decide_mtf_permission(out.iloc[i].to_dict(), cfg=cfg) for i in range(len(out))]
    dec_df = pd.DataFrame(decisions)
    for c in dec_df.columns:
        out[c] = dec_df[c].values
    both = out["long_allowed"].astype(bool) & out["short_allowed"].astype(bool)
    if bool(both.any()):
        raise RuntimeError("invariant violated: long_allowed and short_allowed both true")
    return out


def permission_invariants_ok(row: Mapping[str, Any]) -> bool:
    long_a = bool(row.get("long_allowed"))
    short_a = bool(row.get("short_allowed"))
    perm = str(row.get("trade_permission") or "")
    if long_a and short_a:
        return False
    if perm == "BLOCK_BOTH" and (long_a or short_a):
        return False
    if perm.startswith("WAIT_") and (long_a or short_a):
        return False
    if perm == "LONG_ALLOWED" and not long_a:
        return False
    if perm == "SHORT_ALLOWED" and not short_a:
        return False
    return True

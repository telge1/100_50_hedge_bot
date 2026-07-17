"""Phase C3.5 — causal pullback entry state machine (research-only).

Flow (short; long mirrored):
  5m structure edge → ARM → EMA-zone pullback → LH/rejection → READY
  → breakout of pullback/micro low → ENTRY (next-bar open by default)

Does not modify C3.4B. No live-bot integration. Closed bars only.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicators import atr_wilder, compute_indicator_frame, ema
from research.regime_scanner.market_structure_c3_4b import (
    ProtectedStructureConfig,
    RESEARCH_MATRIX as C34B_MATRIX,
    apply_protected_structure,
)
from research.regime_scanner.point_audit import json_safe

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

ENTRY_STATES: tuple[str, ...] = (
    "IDLE",
    "SHORT_ARMED",
    "SHORT_PULLBACK",
    "SHORT_READY",
    "SHORT_ENTERED",
    "LONG_ARMED",
    "LONG_PULLBACK",
    "LONG_READY",
    "LONG_ENTERED",
)

SHORT_FAMILY = {"SHORT_ARMED", "SHORT_PULLBACK", "SHORT_READY", "SHORT_ENTERED"}
LONG_FAMILY = {"LONG_ARMED", "LONG_PULLBACK", "LONG_READY", "LONG_ENTERED"}

ARMING_TYPES: tuple[str, ...] = (
    "external_bos",
    "internal_bos",
    "choch",
    "major_dir_change",
    "structure_plus_protected",
)

EMA_ZONE_MODES: tuple[str, ...] = ("band_9_20", "ema20_alone", "band_20_50")
TOUCH_MODES: tuple[str, ...] = ("touch_high_low", "close_in_band", "wick_in_band", "atr_distance")
REJECTION_MODES: tuple[str, ...] = ("lower_high", "ema_rejection", "wick_rejection", "combined")
BREAKOUT_MODES: tuple[str, ...] = (
    "break_pullback_extreme",
    "break_micro",
    "two_closes",
    "break_plus_body",
)

FORWARD_HORIZONS: tuple[int, ...] = (3, 5, 10, 20, 40, 80)
TARGET_ATRS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)
STOP_ATRS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5)
MAX_AGE_BARS: tuple[int, ...] = (12, 24, 48)

FEE_BPS = 6.0  # research fee+slippage assumption per side (round-trip later)


@dataclass(frozen=True)
class PullbackEntryConfig:
    """One researched variant of the pullback entry SM."""

    name: str = "A1"
    label: str = "arm_pullback_breakout"
    side_mode: str = "both"  # short | long | both
    arming_type: str = "external_bos"
    ema_zone_mode: str = "band_9_20"
    touch_mode: str = "touch_high_low"
    touch_atr_max: float = 0.5
    rejection_mode: str = "combined"
    breakout_mode: str = "break_pullback_extreme"
    entry_price_mode: str = "next_open"  # next_open | signal_close
    max_age_bars: int = 24
    require_lower_high: bool = False
    require_ema_direction: bool = False
    require_ema_slope: bool = False
    require_adx_di: bool = False
    adx_min: float = 15.0
    adx_rising_bars: int = 2
    require_atr_anti_chase: bool = False
    max_entry_dist_ema_atr: float = 1.5
    max_move_since_arm_atr: float = 2.0
    max_breakout_candle_atr: float = 2.0
    mtf_mode: str = "none"  # none | veto_15m | setup_15m | veto_30m | setup15_veto30
    require_candle_rejection: bool = False
    direct_entry: bool = False  # A0 reference
    fee_bps_per_side: float = FEE_BPS
    # Research diagnostics (O0/R0 defaults preserve baseline):
    max_ready_age_bars: int | None = None  # None = unlimited (R0)
    opposite_veto_mode: str = "none"  # none|trigger_bar|since_ready|lookback_1|lookback_2|lookback_3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Controlled research matrix (no combinatorial sweep).
RESEARCH_VARIANTS: tuple[PullbackEntryConfig, ...] = (
    PullbackEntryConfig(name="A0", label="direct_structure_entry", direct_entry=True, arming_type="external_bos"),
    PullbackEntryConfig(name="A1", label="arm_pullback_breakout"),
    PullbackEntryConfig(name="A2", label="A1_plus_lower_high", require_lower_high=True, rejection_mode="lower_high"),
    PullbackEntryConfig(
        name="A3",
        label="A2_plus_ema_direction",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
    ),
    PullbackEntryConfig(
        name="A4",
        label="A3_plus_ema_slope",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
    ),
    PullbackEntryConfig(
        name="A5",
        label="A4_plus_adx_di",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
        require_adx_di=True,
    ),
    PullbackEntryConfig(
        name="A6",
        label="A5_plus_atr_anti_chase",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
        require_adx_di=True,
        require_atr_anti_chase=True,
    ),
    PullbackEntryConfig(
        name="A7",
        label="A6_plus_15m_veto",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
        require_adx_di=True,
        require_atr_anti_chase=True,
        mtf_mode="veto_15m",
    ),
    PullbackEntryConfig(
        name="A8",
        label="A6_plus_15m_setup",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
        require_adx_di=True,
        require_atr_anti_chase=True,
        mtf_mode="setup_15m",
    ),
    PullbackEntryConfig(
        name="A9",
        label="A8_plus_30m_veto",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
        require_adx_di=True,
        require_atr_anti_chase=True,
        mtf_mode="setup15_veto30",
    ),
    PullbackEntryConfig(
        name="A10",
        label="A9_plus_candle_rejection",
        require_lower_high=True,
        rejection_mode="combined",
        require_ema_direction=True,
        require_ema_slope=True,
        require_adx_di=True,
        require_atr_anti_chase=True,
        mtf_mode="setup15_veto30",
        require_candle_rejection=True,
    ),
)

ABLATION_BASE = "A6"


TERMINAL_OUTCOMES: tuple[str, ...] = (
    "entered",
    "invalidated",
    "timed_out",
    "rejected",
    "superseded_by_opposite",
    "ready_expired",
    "never_reached_pullback",
    "never_reached_ready",
    "no_breakout",
    "filtered",
)

OPPOSITE_VETO_MODES: tuple[str, ...] = (
    "none",
    "trigger_bar",
    "since_ready",
    "lookback_1",
    "lookback_2",
    "lookback_3",
)


@dataclass
class SetupRuntime:
    state: str = "IDLE"
    side: int = 0  # -1 short, +1 long, 0 none
    setup_id: int | None = None
    start_bar: int | None = None
    start_timestamp: Any = None
    armed_price: float | None = None
    pullback_start_bar: int | None = None
    pullback_start_timestamp: Any = None
    pullback_high: float | None = None
    pullback_low: float | None = None
    prior_swing_high: float | None = None
    prior_swing_low: float | None = None
    rejection_bar: int | None = None
    rejection_timestamp: Any = None
    breakout_level: float | None = None
    setup_age: int = 0
    ready_age: int = 0
    invalidation_reason: str | None = None
    entry_reason: str | None = None
    entry_bar: int | None = None
    entry_timestamp: Any = None
    entry_price: float | None = None
    closes_beyond: int = 0
    arming_type: str | None = None
    last_event: str | None = None
    opposite_arm_seen: bool = False
    opposite_arm_bar: int | None = None
    opposite_arm_type: str | None = None
    last_reject_reason: str | None = None
    terminal_outcome: str | None = None
    terminal_reason: str | None = None
    terminal_state: str | None = None
    terminal_bar: int | None = None
    terminal_setup_id: int | None = None
    terminal_setup_age: int | None = None
    terminal_ready_age: int | None = None
    terminal_opposite_arm_seen: bool = False
    terminal_opposite_arm_bar: int | None = None
    terminal_opposite_arm_type: str | None = None
    terminal_direction: str | None = None


def config_hash(cfg: PullbackEntryConfig) -> str:
    blob = json_safe(cfg.to_dict())
    import json

    raw = json.dumps(blob, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _rising_edge(curr: bool, prev: bool) -> bool:
    return bool(curr) and not bool(prev)


# ---------------------------------------------------------------------------
# Feature preparation (5m + optional HTF asof)
# ---------------------------------------------------------------------------


def enrich_indicators(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Causal EMA/ADX/ATR features on closed bars. Adds ema_50 explicitly."""
    frame = compute_indicator_frame(ohlcv)
    close = pd.to_numeric(frame["close"], errors="coerce").astype("float64")
    high = pd.to_numeric(frame["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(frame["low"], errors="coerce").astype("float64")
    frame["ema_50"] = ema(close, 50)
    if "atr" in frame.columns:
        frame["atr_14"] = frame["atr"]
    else:
        frame["atr_14"] = atr_wilder(high, low, close, 14)
    # Slopes in price units (causal).
    for p in (9, 20, 50):
        col = f"ema_{p}"
        if col in frame.columns:
            frame[f"ema_{p}_slope_1"] = frame[col] - frame[col].shift(1)
            frame[f"ema_{p}_slope_2"] = frame[col] - frame[col].shift(2)
            frame[f"ema_{p}_slope_3"] = frame[col] - frame[col].shift(3)
    frame["adx_slope_1"] = frame["adx"] - frame["adx"].shift(1)
    frame["adx_slope_2"] = frame["adx"] - frame["adx"].shift(2)
    frame["adx_slope_3"] = frame["adx"] - frame["adx"].shift(3)
    frame["adx_rising_2"] = (frame["adx_slope_1"] > 0) & (frame["adx"].shift(1) > frame["adx"].shift(2))
    frame["adx_rising_3"] = frame["adx_rising_2"] & (frame["adx"].shift(2) > frame["adx"].shift(3))
    # EMA cross age (bars since ema9 crossed below/above ema20).
    spread = frame["ema_9"] - frame["ema_20"]
    prev = spread.shift(1)
    bear_cross = (spread < 0) & (prev >= 0)
    bull_cross = (spread > 0) & (prev <= 0)
    age = np.zeros(len(frame), dtype=int)
    last_bear = -10_000
    last_bull = -10_000
    for i in range(len(frame)):
        if bool(bear_cross.iloc[i]):
            last_bear = i
        if bool(bull_cross.iloc[i]):
            last_bull = i
        age[i] = i - last_bear if spread.iloc[i] < 0 else i - last_bull
    frame["ema_cross_age"] = age
    frame["ema9_below_ema20"] = frame["ema_9"] < frame["ema_20"]
    frame["ema20_below_ema50"] = frame["ema_20"] < frame["ema_50"]
    frame["ema9_above_ema20"] = frame["ema_9"] > frame["ema_20"]
    frame["ema20_above_ema50"] = frame["ema_20"] > frame["ema_50"]
    return frame


def attach_structure_edges(ohlcv_features: pd.DataFrame) -> pd.DataFrame:
    """Run C3.4B as black box; attach edge columns. Does not modify C3.4B."""
    cfg = ProtectedStructureConfig.from_matrix_entry(C34B_MATRIX[0])
    struct = apply_protected_structure(ohlcv_features, cfg)
    keep = [
        c
        for c in [
            "protected_structure_state",
            "previous_protected_structure_state",
            "protected_structure_changed",
            "major_direction",
            "external_bos_up",
            "external_bos_down",
            "internal_bos_up",
            "internal_bos_down",
            "choch_side",
            "protected_high",
            "protected_low",
            "active_external_break_level",
            "new_micro_high",
            "new_micro_low",
            "micro_swing_high",
            "micro_swing_low",
            "transition_reason",
        ]
        if c in struct.columns
    ]
    out = ohlcv_features.copy()
    for c in keep:
        out[c] = struct[c].values
    # Rising-edge helpers (causal vs prior bar).
    maj = out["major_direction"].fillna(0).astype(int)
    out["major_dir_changed"] = maj != maj.shift(1).fillna(0).astype(int)

    def _edge(col: str) -> pd.Series:
        cur = out[col].fillna(False).astype(bool)
        prev = cur.shift(1).fillna(False).astype(bool)
        return cur & ~prev

    out["arm_edge_external_bear"] = _edge("external_bos_down")
    out["arm_edge_external_bull"] = _edge("external_bos_up")
    out["arm_edge_internal_bear"] = _edge("internal_bos_down")
    out["arm_edge_internal_bull"] = _edge("internal_bos_up")
    choch = out["choch_side"].astype(str)
    prev_choch = choch.shift(1).fillna("")
    out["arm_edge_choch_bear"] = (choch == "down") & (prev_choch != "down")
    out["arm_edge_choch_bull"] = (choch == "up") & (prev_choch != "up")
    out["arm_edge_major_bear"] = (maj < 0) & (maj.shift(1).fillna(0).astype(int) >= 0)
    out["arm_edge_major_bull"] = (maj > 0) & (maj.shift(1).fillna(0).astype(int) <= 0)
    # Structure + intact protected level.
    out["arm_edge_struct_prot_bear"] = out["arm_edge_external_bear"] & out["protected_high"].notna()
    out["arm_edge_struct_prot_bull"] = out["arm_edge_external_bull"] & out["protected_low"].notna()
    return out


def _htf_close_decision(ts: pd.Series, minutes: int) -> pd.Series:
    """Decision time = HTF bar close (= timestamp + tf)."""
    return pd.to_datetime(ts, utc=True) + pd.Timedelta(minutes=minutes)


def asof_htf_context(base_5m: pd.DataFrame, htf: pd.DataFrame, *, tf_minutes: int, prefix: str) -> pd.DataFrame:
    """Causal merge_asof: only fully closed HTF bars (close_decision <= 5m decision)."""
    left = base_5m.copy()
    left["decision_time"] = pd.to_datetime(left["timestamp"], utc=True) + pd.Timedelta(minutes=5)
    right = htf.copy()
    right["htf_close_decision"] = _htf_close_decision(right["timestamp"], tf_minutes)
    cols = [
        c
        for c in [
            "htf_close_decision",
            "major_direction",
            "protected_structure_state",
            "ema9_below_ema20",
            "ema9_above_ema20",
            "adx",
            "plus_di",
            "minus_di",
        ]
        if c in right.columns or c == "htf_close_decision"
    ]
    right = right[cols].sort_values("htf_close_decision")
    rename = {c: f"{prefix}_{c}" for c in cols if c != "htf_close_decision"}
    right = right.rename(columns=rename)
    merged = pd.merge_asof(
        left.sort_values("decision_time"),
        right,
        left_on="decision_time",
        right_on="htf_close_decision",
        direction="backward",
    )
    return merged.reset_index(drop=True)


def prepare_research_frame(
    ohlcv_5m: pd.DataFrame,
    *,
    ohlcv_15m: pd.DataFrame | None = None,
    ohlcv_30m: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build causal 5m research frame with structure + optional HTF context."""
    feat = enrich_indicators(ohlcv_5m)
    feat = attach_structure_edges(feat)
    if ohlcv_15m is not None and not ohlcv_15m.empty:
        f15 = enrich_indicators(ohlcv_15m)
        f15 = attach_structure_edges(f15)
        feat = asof_htf_context(feat, f15, tf_minutes=15, prefix="m15")
    if ohlcv_30m is not None and not ohlcv_30m.empty:
        f30 = enrich_indicators(ohlcv_30m)
        f30 = attach_structure_edges(f30)
        feat = asof_htf_context(feat, f30, tf_minutes=30, prefix="m30")
    return feat.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Zone / rejection / breakout helpers
# ---------------------------------------------------------------------------


def _ema_band(row: Mapping[str, Any], mode: str) -> tuple[float | None, float | None]:
    if mode == "ema20_alone":
        e = _finite(row.get("ema_20"))
        return (e, e) if math.isfinite(e) else (None, None)
    if mode == "band_20_50":
        a, b = _finite(row.get("ema_20")), _finite(row.get("ema_50"))
        if not (math.isfinite(a) and math.isfinite(b)):
            return None, None
        return (min(a, b), max(a, b))
    # default band_9_20
    a, b = _finite(row.get("ema_9")), _finite(row.get("ema_20"))
    if not (math.isfinite(a) and math.isfinite(b)):
        return None, None
    return (min(a, b), max(a, b))


def _zone_reached_short(row: Mapping[str, Any], cfg: PullbackEntryConfig) -> bool:
    lo, hi = _ema_band(row, cfg.ema_zone_mode)
    if lo is None or hi is None:
        return False
    high = _finite(row.get("high"))
    close = _finite(row.get("close"))
    low = _finite(row.get("low"))
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    if cfg.touch_mode == "close_in_band":
        return lo <= close <= hi
    if cfg.touch_mode == "wick_in_band":
        return high >= lo and low <= hi
    if cfg.touch_mode == "atr_distance":
        # Distance from high to band mid / ATR
        mid = 0.5 * (lo + hi)
        return (mid - high) / atr <= cfg.touch_atr_max and high >= lo - cfg.touch_atr_max * atr
    # touch: high reaches band
    return high >= lo


def _zone_reached_long(row: Mapping[str, Any], cfg: PullbackEntryConfig) -> bool:
    lo, hi = _ema_band(row, cfg.ema_zone_mode)
    if lo is None or hi is None:
        return False
    high = _finite(row.get("high"))
    close = _finite(row.get("close"))
    low = _finite(row.get("low"))
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    if cfg.touch_mode == "close_in_band":
        return lo <= close <= hi
    if cfg.touch_mode == "wick_in_band":
        return high >= lo and low <= hi
    if cfg.touch_mode == "atr_distance":
        mid = 0.5 * (lo + hi)
        return (low - mid) / atr <= cfg.touch_atr_max and low <= hi + cfg.touch_atr_max * atr
    return low <= hi


def _bearish_candle(row: Mapping[str, Any]) -> bool:
    o, c = _finite(row.get("open")), _finite(row.get("close"))
    return c < o


def _bullish_candle(row: Mapping[str, Any]) -> bool:
    o, c = _finite(row.get("open")), _finite(row.get("close"))
    return c > o


def _close_in_lower_third(row: Mapping[str, Any]) -> bool:
    h, l, c = _finite(row.get("high")), _finite(row.get("low")), _finite(row.get("close"))
    rng = max(h - l, 1e-12)
    return (c - l) / rng <= 1.0 / 3.0


def _close_in_upper_third(row: Mapping[str, Any]) -> bool:
    h, l, c = _finite(row.get("high")), _finite(row.get("low")), _finite(row.get("close"))
    rng = max(h - l, 1e-12)
    return (h - c) / rng <= 1.0 / 3.0


def _upper_wick_large(row: Mapping[str, Any], *, min_frac: float = 0.4) -> bool:
    h, l, o, c = (
        _finite(row.get("high")),
        _finite(row.get("low")),
        _finite(row.get("open")),
        _finite(row.get("close")),
    )
    rng = max(h - l, 1e-12)
    upper = h - max(o, c)
    return upper / rng >= min_frac


def _lower_wick_large(row: Mapping[str, Any], *, min_frac: float = 0.4) -> bool:
    h, l, o, c = (
        _finite(row.get("high")),
        _finite(row.get("low")),
        _finite(row.get("open")),
        _finite(row.get("close")),
    )
    rng = max(h - l, 1e-12)
    lower = min(o, c) - l
    return lower / rng >= min_frac


def _short_rejection_ok(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> bool:
    lo, hi = _ema_band(row, cfg.ema_zone_mode)
    close = _finite(row.get("close"))
    high = _finite(row.get("high"))
    lh = True
    if rt.prior_swing_high is not None and rt.pullback_high is not None:
        lh = rt.pullback_high < rt.prior_swing_high - 1e-12
    elif rt.prior_swing_high is not None:
        lh = high < rt.prior_swing_high - 1e-12
    ema_rej = False
    if lo is not None and hi is not None:
        ema_rej = high >= lo and close < lo and _bearish_candle(row) and _close_in_lower_third(row)
    wick_rej = False
    if lo is not None:
        wick_rej = _upper_wick_large(row) and close < lo
    mode = cfg.rejection_mode
    if mode == "lower_high":
        return lh and bool(row.get("new_micro_high"))
    if mode == "ema_rejection":
        return ema_rej
    if mode == "wick_rejection":
        return wick_rej
    # combined
    base = lh and (ema_rej or wick_rej or bool(row.get("new_micro_high")))
    if cfg.require_candle_rejection:
        return base and (_bearish_candle(row) or wick_rej)
    if cfg.require_lower_high:
        return lh and (ema_rej or wick_rej or bool(row.get("new_micro_high")))
    return base


def _long_rejection_ok(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> bool:
    lo, hi = _ema_band(row, cfg.ema_zone_mode)
    close = _finite(row.get("close"))
    low = _finite(row.get("low"))
    hl = True
    if rt.prior_swing_low is not None and rt.pullback_low is not None:
        hl = rt.pullback_low > rt.prior_swing_low + 1e-12
    elif rt.prior_swing_low is not None:
        hl = low > rt.prior_swing_low + 1e-12
    ema_rej = False
    if lo is not None and hi is not None:
        ema_rej = low <= hi and close > hi and _bullish_candle(row) and _close_in_upper_third(row)
    wick_rej = False
    if hi is not None:
        wick_rej = _lower_wick_large(row) and close > hi
    mode = cfg.rejection_mode
    if mode == "lower_high":  # mirrored name used as higher_low mode in long path
        return hl and bool(row.get("new_micro_low"))
    if mode == "ema_rejection":
        return ema_rej
    if mode == "wick_rejection":
        return wick_rej
    base = hl and (ema_rej or wick_rej or bool(row.get("new_micro_low")))
    if cfg.require_candle_rejection:
        return base and (_bullish_candle(row) or wick_rej)
    if cfg.require_lower_high:
        return hl and (ema_rej or wick_rej or bool(row.get("new_micro_low")))
    return base


def _arm_signal(row: Mapping[str, Any], *, side: int, arming_type: str) -> bool:
    if side < 0:
        mapping = {
            "external_bos": "arm_edge_external_bear",
            "internal_bos": "arm_edge_internal_bear",
            "choch": "arm_edge_choch_bear",
            "major_dir_change": "arm_edge_major_bear",
            "structure_plus_protected": "arm_edge_struct_prot_bear",
        }
    else:
        mapping = {
            "external_bos": "arm_edge_external_bull",
            "internal_bos": "arm_edge_internal_bull",
            "choch": "arm_edge_choch_bull",
            "major_dir_change": "arm_edge_major_bull",
            "structure_plus_protected": "arm_edge_struct_prot_bull",
        }
    return bool(row.get(mapping[arming_type]))


def _ema_filters_ok(row: Mapping[str, Any], cfg: PullbackEntryConfig, *, side: int) -> bool:
    if cfg.require_ema_direction:
        if side < 0:
            if not bool(row.get("ema9_below_ema20")):
                return False
            if not (_finite(row.get("close")) < _finite(row.get("ema_20"))):
                return False
        else:
            if not bool(row.get("ema9_above_ema20")):
                return False
            if not (_finite(row.get("close")) > _finite(row.get("ema_20"))):
                return False
    if cfg.require_ema_slope:
        if side < 0:
            if not (_finite(row.get("ema_9_slope_3")) < 0 and _finite(row.get("ema_20_slope_3")) < 0):
                return False
        else:
            if not (_finite(row.get("ema_9_slope_3")) > 0 and _finite(row.get("ema_20_slope_3")) > 0):
                return False
    return True


def _adx_filters_ok(row: Mapping[str, Any], cfg: PullbackEntryConfig, *, side: int) -> bool:
    if not cfg.require_adx_di:
        return True
    adx = _finite(row.get("adx"))
    if not (adx >= cfg.adx_min):
        return False
    if side < 0:
        if not (_finite(row.get("minus_di")) > _finite(row.get("plus_di"))):
            return False
    else:
        if not (_finite(row.get("plus_di")) > _finite(row.get("minus_di"))):
            return False
    if cfg.adx_rising_bars >= 2 and not bool(row.get("adx_rising_2")):
        return False
    if cfg.adx_rising_bars >= 3 and not bool(row.get("adx_rising_3")):
        return False
    return True


def _atr_anti_chase_ok(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig, *, side: int) -> tuple[bool, str | None]:
    if not cfg.require_atr_anti_chase:
        return True, None
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    close = _finite(row.get("close"))
    lo, hi = _ema_band(row, cfg.ema_zone_mode)
    if lo is not None and hi is not None:
        mid = 0.5 * (lo + hi)
        dist = abs(close - mid) / atr
        if dist > cfg.max_entry_dist_ema_atr:
            return False, "entry_too_far_from_ema"
    if rt.armed_price is not None:
        move = abs(close - rt.armed_price) / atr
        if move > cfg.max_move_since_arm_atr:
            return False, "move_since_arm_too_large"
    candle_range = (_finite(row.get("high")) - _finite(row.get("low"))) / atr
    if candle_range > cfg.max_breakout_candle_atr:
        return False, "breakout_candle_too_large"
    return True, None


def _mtf_ok(row: Mapping[str, Any], cfg: PullbackEntryConfig, *, side: int) -> tuple[bool, str | None]:
    mode = cfg.mtf_mode
    if mode in {"none", ""}:
        return True, None
    m15_maj = int(row.get("m15_major_direction") or 0) if "m15_major_direction" in row else 0
    m30_maj = int(row.get("m30_major_direction") or 0) if "m30_major_direction" in row else 0
    m15_state = str(row.get("m15_protected_structure_state") or "")
    if mode == "veto_15m":
        if side < 0 and m15_maj > 0:
            return False, "15m_bullish_veto"
        if side > 0 and m15_maj < 0:
            return False, "15m_bearish_veto"
        return True, None
    if mode == "setup_15m":
        if side < 0 and m15_maj >= 0 and "bearish" not in m15_state:
            return False, "15m_not_bearish_setup"
        if side > 0 and m15_maj <= 0 and "bullish" not in m15_state:
            return False, "15m_not_bullish_setup"
        return True, None
    if mode == "veto_30m":
        if side < 0 and m30_maj > 0:
            return False, "30m_strong_bullish_veto"
        if side > 0 and m30_maj < 0:
            return False, "30m_strong_bearish_veto"
        return True, None
    if mode == "setup15_veto30":
        ok, reason = _mtf_ok(row, PullbackEntryConfig(mtf_mode="setup_15m"), side=side)
        if not ok:
            return ok, reason
        return _mtf_ok(row, PullbackEntryConfig(mtf_mode="veto_30m"), side=side)
    return True, None


def _breakout_short(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> tuple[bool, float | None, str | None]:
    close = _finite(row.get("close"))
    level = rt.breakout_level if rt.breakout_level is not None else rt.pullback_low
    micro = _finite(row.get("micro_swing_low")) if row.get("micro_swing_low") is not None else float("nan")
    body = abs(close - _finite(row.get("open")))
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    if cfg.breakout_mode == "break_micro":
        if math.isfinite(micro) and close < micro:
            return True, micro, "break_micro_low"
        return False, level, None
    if cfg.breakout_mode == "two_closes":
        trig = level if level is not None else (micro if math.isfinite(micro) else None)
        if trig is None:
            return False, None, None
        if close < trig:
            rt.closes_beyond += 1
        else:
            rt.closes_beyond = 0
        if rt.closes_beyond >= 2:
            return True, trig, "two_closes_below"
        return False, trig, None
    if cfg.breakout_mode == "break_plus_body":
        trig = level if level is not None else None
        if trig is not None and close < trig and body >= 0.25 * atr:
            return True, trig, "break_pullback_low_body"
        return False, trig, None
    # default break pullback extreme
    if level is not None and close < level:
        return True, level, "break_pullback_low"
    if bool(row.get("arm_edge_external_bear")) or bool(row.get("arm_edge_internal_bear")):
        return True, level, "bearish_bos_after_rejection"
    return False, level, None


def _breakout_long(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> tuple[bool, float | None, str | None]:
    close = _finite(row.get("close"))
    level = rt.breakout_level if rt.breakout_level is not None else rt.pullback_high
    micro = _finite(row.get("micro_swing_high")) if row.get("micro_swing_high") is not None else float("nan")
    body = abs(close - _finite(row.get("open")))
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    if cfg.breakout_mode == "break_micro":
        if math.isfinite(micro) and close > micro:
            return True, micro, "break_micro_high"
        return False, level, None
    if cfg.breakout_mode == "two_closes":
        trig = level if level is not None else (micro if math.isfinite(micro) else None)
        if trig is None:
            return False, None, None
        if close > trig:
            rt.closes_beyond += 1
        else:
            rt.closes_beyond = 0
        if rt.closes_beyond >= 2:
            return True, trig, "two_closes_above"
        return False, trig, None
    if cfg.breakout_mode == "break_plus_body":
        trig = level if level is not None else None
        if trig is not None and close > trig and body >= 0.25 * atr:
            return True, trig, "break_pullback_high_body"
        return False, trig, None
    if level is not None and close > level:
        return True, level, "break_pullback_high"
    if bool(row.get("arm_edge_external_bull")) or bool(row.get("arm_edge_internal_bull")):
        return True, level, "bullish_bos_after_rejection"
    return False, level, None


def _invalidate_short(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> str | None:
    if rt.setup_age > cfg.max_age_bars:
        return "max_age"
    if bool(row.get("arm_edge_external_bull")) or bool(row.get("arm_edge_major_bull")):
        return "structure_flipped_bullish"
    if bool(row.get("ema9_above_ema20")) and _finite(row.get("close")) > _finite(row.get("ema_20")):
        if int(row.get("ema_cross_age") or 99) <= 3:
            return "ema_bullish_reclaim"
    if rt.prior_swing_high is not None and _finite(row.get("close")) > rt.prior_swing_high:
        return "prior_swing_high_broken"
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    if rt.armed_price is not None and (_finite(row.get("high")) - rt.armed_price) / atr > cfg.max_move_since_arm_atr + 0.5:
        return "pullback_ran_too_far"
    m15 = int(row.get("m15_major_direction") or 0) if "m15_major_direction" in row else 0
    if cfg.mtf_mode != "none" and m15 > 0:
        return "15m_turned_bullish"
    m30 = int(row.get("m30_major_direction") or 0) if "m30_major_direction" in row else 0
    if cfg.mtf_mode in {"veto_30m", "setup15_veto30"} and m30 > 0:
        st = str(row.get("m30_protected_structure_state") or "")
        if "choch" not in st and "candidate" not in st:
            return "30m_strong_bullish"
    return None


def _invalidate_long(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig) -> str | None:
    if rt.setup_age > cfg.max_age_bars:
        return "max_age"
    if bool(row.get("arm_edge_external_bear")) or bool(row.get("arm_edge_major_bear")):
        return "structure_flipped_bearish"
    if bool(row.get("ema9_below_ema20")) and _finite(row.get("close")) < _finite(row.get("ema_20")):
        if int(row.get("ema_cross_age") or 99) <= 3:
            return "ema_bearish_reclaim"
    if rt.prior_swing_low is not None and _finite(row.get("close")) < rt.prior_swing_low:
        return "prior_swing_low_broken"
    atr = max(_finite(row.get("atr_14"), 1e-12), 1e-12)
    if rt.armed_price is not None and (rt.armed_price - _finite(row.get("low"))) / atr > cfg.max_move_since_arm_atr + 0.5:
        return "pullback_ran_too_far"
    m15 = int(row.get("m15_major_direction") or 0) if "m15_major_direction" in row else 0
    if cfg.mtf_mode != "none" and m15 < 0:
        return "15m_turned_bearish"
    m30 = int(row.get("m30_major_direction") or 0) if "m30_major_direction" in row else 0
    if cfg.mtf_mode in {"veto_30m", "setup15_veto30"} and m30 < 0:
        st = str(row.get("m30_protected_structure_state") or "")
        if "choch" not in st and "candidate" not in st:
            return "30m_strong_bearish"
    return None


def _reset(rt: SetupRuntime) -> None:
    rt.state = "IDLE"
    rt.side = 0
    rt.setup_id = None
    rt.start_bar = None
    rt.start_timestamp = None
    rt.armed_price = None
    rt.pullback_start_bar = None
    rt.pullback_start_timestamp = None
    rt.pullback_high = None
    rt.pullback_low = None
    rt.prior_swing_high = None
    rt.prior_swing_low = None
    rt.rejection_bar = None
    rt.rejection_timestamp = None
    rt.breakout_level = None
    rt.setup_age = 0
    rt.ready_age = 0
    rt.invalidation_reason = None
    rt.entry_reason = None
    rt.entry_bar = None
    rt.entry_timestamp = None
    rt.entry_price = None
    rt.closes_beyond = 0
    rt.arming_type = None
    rt.last_event = None
    rt.opposite_arm_seen = False
    rt.opposite_arm_bar = None
    rt.opposite_arm_type = None
    rt.last_reject_reason = None
    # terminal_* intentionally retained until next arm overwrites / cleared on arm


def _clear_terminal(rt: SetupRuntime) -> None:
    rt.terminal_outcome = None
    rt.terminal_reason = None
    rt.terminal_state = None
    rt.terminal_bar = None
    rt.terminal_setup_id = None
    rt.terminal_setup_age = None
    rt.terminal_ready_age = None
    rt.terminal_opposite_arm_seen = False
    rt.terminal_opposite_arm_bar = None
    rt.terminal_opposite_arm_type = None
    rt.terminal_direction = None


def classify_terminal_outcome(state_before: str, reason: str | None, *, entered: bool = False) -> str:
    """Map end-of-setup reason to a stable terminal_outcome."""
    if entered:
        return "entered"
    r = str(reason or "unknown")
    if r == "ready_expired":
        return "ready_expired"
    if r.startswith("opposite_veto") or r == "superseded_by_opposite":
        return "superseded_by_opposite"
    if r.startswith("direct_reject") or r.endswith("filter_reject") or r == "direct_entry_filter_reject":
        return "filtered"
    if r.startswith("break_rejected") or r in {
        "entry_too_far_from_ema",
        "move_since_arm_too_large",
        "breakout_candle_too_large",
        "ema_filter",
        "adx_filter",
    }:
        return "rejected"
    if r == "max_age":
        if state_before in {"SHORT_ARMED", "LONG_ARMED"}:
            return "never_reached_pullback"
        if state_before in {"SHORT_PULLBACK", "LONG_PULLBACK"}:
            return "never_reached_ready"
        if state_before in {"SHORT_READY", "LONG_READY"}:
            return "no_breakout"
        return "timed_out"
    if r in {"end_of_data", "forced_close"}:
        if state_before in {"SHORT_ARMED", "LONG_ARMED"}:
            return "never_reached_pullback"
        if state_before in {"SHORT_PULLBACK", "LONG_PULLBACK"}:
            return "never_reached_ready"
        if state_before in {"SHORT_READY", "LONG_READY"}:
            return "no_breakout"
        return "timed_out"
    return "invalidated"


def _note_opposite_arm(rt: SetupRuntime, row: Mapping[str, Any], cfg: PullbackEntryConfig, *, bar_i: int) -> None:
    """Record first opposite-direction structure arm while setup is live.

    Uses *all* ARMING_TYPES (not only cfg.arming_type) so soft opposite signals
    that do not themselves invalidate can still be audited / vetoed.
    """
    if rt.state == "IDLE" or rt.side == 0:
        return
    opp = -1 if rt.side > 0 else 1
    for atype in ARMING_TYPES:
        if _arm_signal(row, side=opp, arming_type=atype):
            if not rt.opposite_arm_seen:
                rt.opposite_arm_seen = True
                rt.opposite_arm_bar = bar_i
                rt.opposite_arm_type = atype
            break


def _opposite_veto_blocks(rt: SetupRuntime, *, bar_i: int, cfg: PullbackEntryConfig) -> bool:
    mode = cfg.opposite_veto_mode or "none"
    if mode in {"none", ""} or not rt.opposite_arm_seen or rt.opposite_arm_bar is None:
        return False
    opp = int(rt.opposite_arm_bar)
    if mode == "trigger_bar":
        return opp == bar_i
    if mode == "since_ready":
        ready_bar = rt.rejection_bar
        if ready_bar is None:
            return False
        return ready_bar <= opp <= bar_i
    if mode == "lookback_1":
        return bar_i - opp <= 1
    if mode == "lookback_2":
        return bar_i - opp <= 2
    if mode == "lookback_3":
        return bar_i - opp <= 3
    return False



def _mark_entered(
    rt: SetupRuntime,
    *,
    bar_i: int,
    reason: str,
    events: list[str] | None = None,
) -> None:
    """Record entered terminal metadata but keep *_ENTERED until next bar reset."""
    rt.terminal_outcome = "entered"
    rt.terminal_reason = reason
    rt.terminal_state = rt.state
    rt.terminal_bar = bar_i
    rt.terminal_setup_id = rt.setup_id
    rt.terminal_setup_age = rt.setup_age
    rt.terminal_ready_age = rt.ready_age
    rt.terminal_opposite_arm_seen = rt.opposite_arm_seen
    rt.terminal_opposite_arm_bar = rt.opposite_arm_bar
    rt.terminal_opposite_arm_type = rt.opposite_arm_type
    rt.terminal_direction = "short" if rt.side < 0 else ("long" if rt.side > 0 else None)
    if events is not None:
        events.append(f"terminal:entered:{reason}")


def _terminate(
    rt: SetupRuntime,
    *,
    bar_i: int,
    reason: str,
    entered: bool = False,
    events: list[str] | None = None,
) -> None:
    state_before = rt.state
    outcome = classify_terminal_outcome(state_before, reason, entered=entered)
    rt.terminal_outcome = outcome
    rt.terminal_reason = reason
    rt.terminal_state = state_before
    rt.terminal_bar = bar_i
    rt.terminal_setup_id = rt.setup_id
    rt.terminal_setup_age = rt.setup_age
    rt.terminal_ready_age = rt.ready_age
    rt.terminal_opposite_arm_seen = rt.opposite_arm_seen
    rt.terminal_opposite_arm_bar = rt.opposite_arm_bar
    rt.terminal_opposite_arm_type = rt.opposite_arm_type
    rt.terminal_direction = "short" if rt.side < 0 else ("long" if rt.side > 0 else None)
    if events is not None:
        events.append(f"terminal:{outcome}:{reason}")
    if not entered:
        rt.invalidation_reason = reason
    _reset(rt)


# ---------------------------------------------------------------------------
# State machine step
# ---------------------------------------------------------------------------


def step_pullback_entry(
    rt: SetupRuntime,
    row: Mapping[str, Any],
    *,
    cfg: PullbackEntryConfig,
    next_open: float | None = None,
    setup_id_factory: Any | None = None,
) -> tuple[SetupRuntime, dict[str, Any]]:
    """One closed-bar step. Entry fills at next_open when entry_price_mode=next_open."""
    bar_i = int(row.get("bar_index", 0))
    ts = row.get("timestamp")
    events: list[str] = []
    entry_now = False
    allow_short = cfg.side_mode in {"both", "short"}
    allow_long = cfg.side_mode in {"both", "long"}

    def _alloc_id() -> int:
        if setup_id_factory is None:
            return int(bar_i) + 1
        return int(setup_id_factory())

    def _fill_price() -> float:
        if cfg.entry_price_mode == "next_open" and next_open is not None:
            return _finite(next_open)
        return _finite(row.get("close"))

    if rt.state != "IDLE":
        rt.setup_age += 1
    if rt.state in {"SHORT_READY", "LONG_READY"}:
        rt.ready_age += 1

    _note_opposite_arm(rt, row, cfg, bar_i=bar_i)

    # Ready-age expiry (research R1–R5); R0 keeps max_ready_age_bars=None.
    if (
        rt.state in {"SHORT_READY", "LONG_READY"}
        and cfg.max_ready_age_bars is not None
        and rt.ready_age > int(cfg.max_ready_age_bars)
    ):
        _terminate(rt, bar_i=bar_i, reason="ready_expired", events=events)

    # --- IDLE: arm ---
    if rt.state == "IDLE":
        if allow_short and _arm_signal(row, side=-1, arming_type=cfg.arming_type):
            _clear_terminal(rt)
            rt.state = "SHORT_ARMED"
            rt.side = -1
            rt.setup_id = _alloc_id()
            rt.start_bar = bar_i
            rt.start_timestamp = ts
            rt.armed_price = _finite(row.get("close"))
            rt.setup_age = 0
            rt.ready_age = 0
            rt.arming_type = cfg.arming_type
            rt.prior_swing_high = (
                _finite(row.get("micro_swing_high"))
                if row.get("micro_swing_high") is not None
                else _finite(row.get("high"))
            )
            rt.prior_swing_low = (
                _finite(row.get("micro_swing_low")) if row.get("micro_swing_low") is not None else None
            )
            rt.last_event = "short_armed"
            events.append("short_armed")
            if cfg.direct_entry:
                ok_ema = _ema_filters_ok(row, cfg, side=-1)
                ok_adx = _adx_filters_ok(row, cfg, side=-1)
                ok_atr, atr_reason = _atr_anti_chase_ok(rt, row, cfg, side=-1)
                ok_mtf, mtf_reason = _mtf_ok(row, cfg, side=-1)
                if ok_ema and ok_adx and ok_atr and ok_mtf:
                    if _opposite_veto_blocks(rt, bar_i=bar_i, cfg=cfg):
                        _terminate(
                            rt,
                            bar_i=bar_i,
                            reason=f"opposite_veto:{cfg.opposite_veto_mode}",
                            events=events,
                        )
                    else:
                        rt.state = "SHORT_ENTERED"
                        rt.entry_bar = bar_i
                        rt.entry_timestamp = ts
                        rt.entry_reason = "direct_structure_entry"
                        rt.entry_price = _fill_price()
                        entry_now = True
                        events.append("short_entered_direct")
                        _mark_entered(rt, bar_i=bar_i, reason="direct_structure_entry", events=events)
                else:
                    reason = atr_reason or mtf_reason or "direct_entry_filter_reject"
                    events.append(f"direct_reject:{reason}")
                    _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        elif allow_long and _arm_signal(row, side=1, arming_type=cfg.arming_type):
            _clear_terminal(rt)
            rt.state = "LONG_ARMED"
            rt.side = 1
            rt.setup_id = _alloc_id()
            rt.start_bar = bar_i
            rt.start_timestamp = ts
            rt.armed_price = _finite(row.get("close"))
            rt.setup_age = 0
            rt.ready_age = 0
            rt.arming_type = cfg.arming_type
            rt.prior_swing_low = (
                _finite(row.get("micro_swing_low"))
                if row.get("micro_swing_low") is not None
                else _finite(row.get("low"))
            )
            rt.prior_swing_high = (
                _finite(row.get("micro_swing_high")) if row.get("micro_swing_high") is not None else None
            )
            rt.last_event = "long_armed"
            events.append("long_armed")
            if cfg.direct_entry:
                ok_ema = _ema_filters_ok(row, cfg, side=1)
                ok_adx = _adx_filters_ok(row, cfg, side=1)
                ok_atr, atr_reason = _atr_anti_chase_ok(rt, row, cfg, side=1)
                ok_mtf, mtf_reason = _mtf_ok(row, cfg, side=1)
                if ok_ema and ok_adx and ok_atr and ok_mtf:
                    if _opposite_veto_blocks(rt, bar_i=bar_i, cfg=cfg):
                        _terminate(
                            rt,
                            bar_i=bar_i,
                            reason=f"opposite_veto:{cfg.opposite_veto_mode}",
                            events=events,
                        )
                    else:
                        rt.state = "LONG_ENTERED"
                        rt.entry_bar = bar_i
                        rt.entry_timestamp = ts
                        rt.entry_reason = "direct_structure_entry"
                        rt.entry_price = _fill_price()
                        entry_now = True
                        events.append("long_entered_direct")
                        _mark_entered(rt, bar_i=bar_i, reason="direct_structure_entry", events=events)
                else:
                    reason = atr_reason or mtf_reason or "direct_entry_filter_reject"
                    events.append(f"direct_reject:{reason}")
                    _terminate(rt, bar_i=bar_i, reason=reason, events=events)

    # --- SHORT path ---
    elif rt.state == "SHORT_ARMED":
        reason = _invalidate_short(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        elif _zone_reached_short(row, cfg):
            rt.state = "SHORT_PULLBACK"
            rt.pullback_start_bar = bar_i
            rt.pullback_start_timestamp = ts
            rt.pullback_high = _finite(row.get("high"))
            rt.pullback_low = _finite(row.get("low"))
            rt.last_event = "short_pullback"
            events.append("short_pullback")

    elif rt.state == "SHORT_PULLBACK":
        reason = _invalidate_short(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))
            rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))
            if _short_rejection_ok(rt, row, cfg):
                rt.state = "SHORT_READY"
                rt.rejection_bar = bar_i
                rt.rejection_timestamp = ts
                rt.breakout_level = rt.pullback_low
                rt.ready_age = 0
                rt.last_event = "short_ready"
                events.append("short_ready")

    elif rt.state == "SHORT_READY":
        reason = _invalidate_short(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            ok, level, br = _breakout_short(rt, row, cfg)
            if ok:
                if not _ema_filters_ok(row, cfg, side=-1):
                    rt.last_reject_reason = "ema_filter"
                    events.append("break_rejected:ema_filter")
                elif not _adx_filters_ok(row, cfg, side=-1):
                    rt.last_reject_reason = "adx_filter"
                    events.append("break_rejected:adx_filter")
                else:
                    atr_ok, atr_reason = _atr_anti_chase_ok(rt, row, cfg, side=-1)
                    mtf_ok, mtf_reason = _mtf_ok(row, cfg, side=-1)
                    if not atr_ok:
                        rt.last_reject_reason = atr_reason
                        events.append(f"break_rejected:{atr_reason}")
                    elif not mtf_ok:
                        rt.last_reject_reason = mtf_reason
                        events.append(f"break_rejected:{mtf_reason}")
                    elif _opposite_veto_blocks(rt, bar_i=bar_i, cfg=cfg):
                        _terminate(
                            rt,
                            bar_i=bar_i,
                            reason=f"opposite_veto:{cfg.opposite_veto_mode}",
                            events=events,
                        )
                    else:
                        rt.state = "SHORT_ENTERED"
                        rt.breakout_level = level
                        rt.entry_bar = bar_i
                        rt.entry_timestamp = ts
                        rt.entry_reason = br or "short_breakout"
                        rt.entry_price = _fill_price()
                        entry_now = True
                        events.append("short_entered")
                        _mark_entered(rt, bar_i=bar_i, reason=rt.entry_reason, events=events)
            else:
                rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))
                rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))

    # --- LONG path ---
    elif rt.state == "LONG_ARMED":
        reason = _invalidate_long(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        elif _zone_reached_long(row, cfg):
            rt.state = "LONG_PULLBACK"
            rt.pullback_start_bar = bar_i
            rt.pullback_start_timestamp = ts
            rt.pullback_high = _finite(row.get("high"))
            rt.pullback_low = _finite(row.get("low"))
            rt.last_event = "long_pullback"
            events.append("long_pullback")

    elif rt.state == "LONG_PULLBACK":
        reason = _invalidate_long(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))
            rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))
            if _long_rejection_ok(rt, row, cfg):
                rt.state = "LONG_READY"
                rt.rejection_bar = bar_i
                rt.rejection_timestamp = ts
                rt.breakout_level = rt.pullback_high
                rt.ready_age = 0
                rt.last_event = "long_ready"
                events.append("long_ready")

    elif rt.state == "LONG_READY":
        reason = _invalidate_long(rt, row, cfg)
        if reason:
            events.append(f"invalidated:{reason}")
            _terminate(rt, bar_i=bar_i, reason=reason, events=events)
        else:
            ok, level, br = _breakout_long(rt, row, cfg)
            if ok:
                if not _ema_filters_ok(row, cfg, side=1):
                    rt.last_reject_reason = "ema_filter"
                    events.append("break_rejected:ema_filter")
                elif not _adx_filters_ok(row, cfg, side=1):
                    rt.last_reject_reason = "adx_filter"
                    events.append("break_rejected:adx_filter")
                else:
                    atr_ok, atr_reason = _atr_anti_chase_ok(rt, row, cfg, side=1)
                    mtf_ok, mtf_reason = _mtf_ok(row, cfg, side=1)
                    if not atr_ok:
                        rt.last_reject_reason = atr_reason
                        events.append(f"break_rejected:{atr_reason}")
                    elif not mtf_ok:
                        rt.last_reject_reason = mtf_reason
                        events.append(f"break_rejected:{mtf_reason}")
                    elif _opposite_veto_blocks(rt, bar_i=bar_i, cfg=cfg):
                        _terminate(
                            rt,
                            bar_i=bar_i,
                            reason=f"opposite_veto:{cfg.opposite_veto_mode}",
                            events=events,
                        )
                    else:
                        rt.state = "LONG_ENTERED"
                        rt.breakout_level = level
                        rt.entry_bar = bar_i
                        rt.entry_timestamp = ts
                        rt.entry_reason = br or "long_breakout"
                        rt.entry_price = _fill_price()
                        entry_now = True
                        events.append("long_entered")
                        _mark_entered(rt, bar_i=bar_i, reason=rt.entry_reason, events=events)
            else:
                rt.pullback_low = min(rt.pullback_low or 1e18, _finite(row.get("low")))
                rt.pullback_high = max(rt.pullback_high or -1e18, _finite(row.get("high")))

    elif rt.state in {"SHORT_ENTERED", "LONG_ENTERED"}:
        # One-shot: reset after the signal bar (fill occurs at next open externally).
        events.append("reset_after_entry")
        _reset(rt)

    diag = {
        "entry_state": rt.state,
        "entry_side": rt.side,
        "setup_id": rt.setup_id,
        "setup_age": rt.setup_age,
        "ready_age": rt.ready_age,
        "armed_price": rt.armed_price,
        "pullback_high": rt.pullback_high,
        "pullback_low": rt.pullback_low,
        "breakout_level": rt.breakout_level,
        "rejection_bar": rt.rejection_bar,
        "invalidation_reason": rt.invalidation_reason,
        "entry_reason": rt.entry_reason,
        "entry_signal": entry_now,
        "entry_price": rt.entry_price if entry_now else None,
        "entry_bar": rt.entry_bar if entry_now else None,
        "events": "|".join(events) if events else None,
        "arming_type": rt.arming_type,
        "variant": cfg.name,
        "opposite_arm_seen": rt.opposite_arm_seen,
        "opposite_arm_bar": rt.opposite_arm_bar,
        "opposite_arm_type": rt.opposite_arm_type,
        "last_reject_reason": rt.last_reject_reason,
        "terminal_outcome": rt.terminal_outcome,
        "terminal_reason": rt.terminal_reason,
        "terminal_state": rt.terminal_state,
        "terminal_bar": rt.terminal_bar,
        "terminal_setup_id": rt.terminal_setup_id,
        "terminal_setup_age": rt.terminal_setup_age,
        "terminal_ready_age": rt.terminal_ready_age,
        "terminal_opposite_arm_seen": rt.terminal_opposite_arm_seen,
        "terminal_opposite_arm_bar": rt.terminal_opposite_arm_bar,
        "terminal_opposite_arm_type": rt.terminal_opposite_arm_type,
        "terminal_direction": rt.terminal_direction,
    }
    return rt, diag



def apply_pullback_entry(
    frame: pd.DataFrame,
    cfg: PullbackEntryConfig,
    *,
    return_lifecycles: bool = False,
) -> tuple[pd.DataFrame, list[dict[str, Any]]] | tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay SM over prepared frame. Returns (timeline_df, entries[, lifecycles])."""
    df = frame.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))
    opens = df["open"].astype(float).tolist()
    rt = SetupRuntime()
    rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    lifecycles: dict[int, dict[str, Any]] = {}
    next_id = 1

    def _alloc() -> int:
        nonlocal next_id
        sid = next_id
        next_id += 1
        return sid

    def _ensure_life(sid: int, *, direction: str, arm_bar: int, arm_ts: Any, arm_px: float | None) -> dict[str, Any]:
        if sid not in lifecycles:
            lifecycles[sid] = {
                "setup_id": sid,
                "direction": direction,
                "variant": cfg.name,
                "arming_type": cfg.arming_type,
                "armed_bar": arm_bar,
                "armed_timestamp": arm_ts,
                "armed_price": arm_px,
                "pullback_bar": None,
                "ready_bar": None,
                "trigger_bar": None,
                "fill_bar": None,
                "terminal_bar": None,
                "terminal_state": None,
                "terminal_outcome": None,
                "terminal_reason": None,
                "setup_age_total": None,
                "ready_age_at_terminal": None,
                "opposite_arm_seen": False,
                "opposite_arm_bar": None,
                "opposite_arm_type": None,
                "entry_created": False,
                "last_reject_reason": None,
            }
        return lifecycles[sid]

    for i in range(len(df)):
        row = df.iloc[i].to_dict()
        next_open = opens[i + 1] if i + 1 < len(opens) else None
        prev_side = rt.side
        prev_id = rt.setup_id
        prev_state = rt.state
        rt, diag = step_pullback_entry(
            rt, row, cfg=cfg, next_open=next_open, setup_id_factory=_alloc
        )
        out = {
            "bar_index": int(row.get("bar_index", i)),
            "timestamp": row.get("timestamp"),
            **diag,
        }
        rows.append(out)
        ev = str(diag.get("events") or "")
        bi = int(out["bar_index"])

        # Arm edge → open lifecycle
        if "short_armed" in ev or "long_armed" in ev:
            sid = int(diag["setup_id"] or prev_id or 0)
            if diag.get("entry_signal") and diag.get("setup_id") is not None:
                sid = int(diag["setup_id"])
            direction = "short" if "short_armed" in ev else "long"
            # For direct entry, setup_id comes from entry_snap
            if diag.get("setup_id") is not None:
                sid = int(diag["setup_id"])
            life = _ensure_life(
                sid,
                direction=direction,
                arm_bar=bi,
                arm_ts=row.get("timestamp"),
                arm_px=diag.get("armed_price"),
            )
            life["arming_type"] = diag.get("arming_type") or cfg.arming_type

        active_id = diag.get("setup_id")
        if active_id is None and prev_id is not None and prev_state != "IDLE" and "terminal:" not in ev:
            active_id = prev_id
        if active_id is not None and int(active_id) in lifecycles:
            life = lifecycles[int(active_id)]
            if "short_pullback" in ev or "long_pullback" in ev:
                life["pullback_bar"] = bi
            if "short_ready" in ev or "long_ready" in ev:
                life["ready_bar"] = bi
            if diag.get("opposite_arm_seen"):
                life["opposite_arm_seen"] = True
                life["opposite_arm_bar"] = diag.get("opposite_arm_bar")
                life["opposite_arm_type"] = diag.get("opposite_arm_type")
            if diag.get("last_reject_reason"):
                life["last_reject_reason"] = diag.get("last_reject_reason")

        if diag.get("entry_signal"):
            sid = int(diag.get("setup_id") or 0)
            if sid not in lifecycles and sid:
                _ensure_life(
                    sid,
                    direction="short" if int(diag.get("entry_side") or 0) < 0 else "long",
                    arm_bar=bi,
                    arm_ts=row.get("timestamp"),
                    arm_px=diag.get("armed_price"),
                )
            if sid in lifecycles:
                life = lifecycles[sid]
                life["trigger_bar"] = bi
                life["fill_bar"] = bi + 1 if next_open is not None else None
                life["entry_created"] = True
                life["ready_age_at_terminal"] = diag.get("ready_age")
                life["setup_age_total"] = diag.get("setup_age")
                life["opposite_arm_seen"] = bool(diag.get("opposite_arm_seen"))
                life["opposite_arm_bar"] = diag.get("opposite_arm_bar")
                life["opposite_arm_type"] = diag.get("opposite_arm_type")
            if cfg.entry_price_mode == "next_open" and next_open is None:
                continue
            entries.append(
                {
                    **out,
                    "side": diag["entry_side"],
                    "close": float(row["close"]),
                    "atr_14": float(row.get("atr_14") or np.nan),
                    "ema_9": float(row.get("ema_9") or np.nan),
                    "ema_20": float(row.get("ema_20") or np.nan),
                    "ema_50": float(row.get("ema_50") or np.nan),
                    "adx": float(row.get("adx") or np.nan),
                    "plus_di": float(row.get("plus_di") or np.nan),
                    "minus_di": float(row.get("minus_di") or np.nan),
                    "armed_price": diag.get("armed_price"),
                    "pullback_high": diag.get("pullback_high"),
                    "pullback_low": diag.get("pullback_low"),
                    "setup_age_at_entry": diag.get("setup_age"),
                    "ready_age_at_entry": diag.get("ready_age"),
                    "m15_major_direction": row.get("m15_major_direction"),
                    "m30_major_direction": row.get("m30_major_direction"),
                    "opposite_arm_seen": diag.get("opposite_arm_seen"),
                    "opposite_arm_bar": diag.get("opposite_arm_bar"),
                    "opposite_arm_type": diag.get("opposite_arm_type"),
                }
            )

        if diag.get("terminal_outcome") and "terminal:" in ev:
            sid = diag.get("terminal_setup_id") or diag.get("setup_id") or prev_id
            if sid is not None and int(sid) in lifecycles:
                life = lifecycles[int(sid)]
                life["terminal_bar"] = diag.get("terminal_bar")
                life["terminal_state"] = diag.get("terminal_state")
                life["terminal_outcome"] = diag.get("terminal_outcome")
                life["terminal_reason"] = diag.get("terminal_reason")
                life["setup_age_total"] = diag.get("terminal_setup_age")
                life["ready_age_at_terminal"] = diag.get("terminal_ready_age")
                if diag.get("terminal_opposite_arm_seen"):
                    life["opposite_arm_seen"] = True
                    life["opposite_arm_bar"] = diag.get("terminal_opposite_arm_bar")
                    life["opposite_arm_type"] = diag.get("terminal_opposite_arm_type")
                if diag.get("entry_signal") or life.get("terminal_outcome") == "entered":
                    life["entry_created"] = bool(diag.get("entry_signal") or life.get("entry_created"))

    # Force-close any open setup at end of data
    if rt.state != "IDLE" and rt.setup_id is not None:
        last_i = int(df.iloc[-1].get("bar_index", len(df) - 1))
        sid = int(rt.setup_id)
        state_before = rt.state
        reason = "end_of_data"
        outcome = classify_terminal_outcome(state_before, reason)
        if sid in lifecycles and lifecycles[sid].get("terminal_outcome") is None:
            lifecycles[sid].update(
                {
                    "terminal_bar": last_i,
                    "terminal_state": state_before,
                    "terminal_outcome": outcome,
                    "terminal_reason": reason,
                    "setup_age_total": rt.setup_age,
                    "ready_age_at_terminal": rt.ready_age,
                    "opposite_arm_seen": rt.opposite_arm_seen,
                    "opposite_arm_bar": rt.opposite_arm_bar,
                    "opposite_arm_type": rt.opposite_arm_type,
                }
            )
        _terminate(rt, bar_i=last_i, reason=reason)

    timeline = pd.DataFrame(rows)
    life_list = [lifecycles[k] for k in sorted(lifecycles.keys())]
    if return_lifecycles:
        return timeline, entries, life_list
    return timeline, entries


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def compute_entry_outcomes(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    *,
    fee_bps_per_side: float = FEE_BPS,
) -> list[dict[str, Any]]:
    if not entries or frame.empty:
        return []
    close = frame["close"].astype(float).to_numpy()
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    atr = frame["atr_14"].astype(float).to_numpy() if "atr_14" in frame.columns else np.full(len(frame), np.nan)
    n = len(frame)
    out: list[dict[str, Any]] = []
    fee = fee_bps_per_side / 10_000.0
    for e in entries:
        i = int(e["bar_index"])
        # Fill at next open → outcome path starts at i+1; for signal_close at i.
        fill_i = i + 1 if e.get("entry_price") is not None and i + 1 < n else i
        # Prefer stored entry_price.
        entry_px = float(e.get("entry_price") or close[i])
        side = int(e.get("side") or e.get("entry_side") or 0)
        if side == 0:
            continue
        a = float(atr[i]) if i < n and np.isfinite(atr[i]) else float("nan")
        row: dict[str, Any] = dict(e)
        row["fill_bar_index"] = fill_i
        row["entry_price"] = entry_px
        # Forward returns
        for h in FORWARD_HORIZONS:
            j = fill_i + h
            if j >= n:
                row[f"fwd_ret_{h}"] = None
                continue
            raw = (close[j] - entry_px) / entry_px
            signed = raw if side > 0 else -raw
            row[f"fwd_ret_{h}"] = float(signed - 2 * fee)
        # MFE/MAE path from fill_i+1 onward up to 80 bars
        horizon = min(80, n - fill_i - 1)
        mfe = 0.0
        mae = 0.0
        t_mfe = None
        t_mae = None
        for k in range(1, horizon + 1):
            j = fill_i + k
            if side > 0:
                up = (high[j] - entry_px) / entry_px
                dn = (low[j] - entry_px) / entry_px
            else:
                up = (entry_px - low[j]) / entry_px
                dn = (entry_px - high[j]) / entry_px
            if up > mfe:
                mfe = up
                t_mfe = k
            if dn < mae:
                mae = dn
                t_mae = k
        row["mfe"] = float(mfe)
        row["mae"] = float(mae)
        row["mfe_mae_ratio"] = float(mfe / abs(mae)) if mae < 0 else (float("inf") if mfe > 0 else None)
        row["time_to_mfe"] = t_mfe
        row["time_to_mae"] = t_mae
        row["mae_before_mfe"] = bool(t_mae is not None and t_mfe is not None and t_mae < t_mfe)
        for rb in (3, 5, 10):
            j = fill_i + rb
            if j >= n:
                row[f"reversal_within_{rb}"] = None
                continue
            # Reversal: adverse close beyond 0.5 ATR
            if not np.isfinite(a) or a <= 0:
                row[f"reversal_within_{rb}"] = None
                continue
            if side < 0:
                row[f"reversal_within_{rb}"] = bool(close[j] > entry_px + 0.5 * a)
            else:
                row[f"reversal_within_{rb}"] = bool(close[j] < entry_px - 0.5 * a)
        # Target/stop races
        for t_atr in TARGET_ATRS:
            for s_atr in STOP_ATRS:
                key = f"t{t_atr}_s{s_atr}"
                if not np.isfinite(a) or a <= 0:
                    row[f"{key}_target_first"] = None
                    row[f"{key}_stop_first"] = None
                    continue
                target = entry_px - t_atr * a if side < 0 else entry_px + t_atr * a
                stop = entry_px + s_atr * a if side < 0 else entry_px - s_atr * a
                t_first = False
                s_first = False
                for k in range(1, horizon + 1):
                    j = fill_i + k
                    if side < 0:
                        hit_t = low[j] <= target
                        hit_s = high[j] >= stop
                    else:
                        hit_t = high[j] >= target
                        hit_s = low[j] <= stop
                    if hit_t and not hit_s:
                        t_first = True
                        break
                    if hit_s and not hit_t:
                        s_first = True
                        break
                    if hit_t and hit_s:
                        # Conservative: stop first on same bar
                        s_first = True
                        break
                row[f"{key}_target_first"] = t_first
                row[f"{key}_stop_first"] = s_first
        # Distances
        lo, hi = _ema_band(e if isinstance(e, dict) else dict(e), "band_9_20")
        # Use frame row for EMA at entry bar
        fr = frame.iloc[i]
        lo, hi = _ema_band(fr.to_dict(), "band_9_20")
        if lo is not None and hi is not None and np.isfinite(a) and a > 0:
            mid = 0.5 * (lo + hi)
            row["entry_dist_ema_atr"] = abs(entry_px - mid) / a
        else:
            row["entry_dist_ema_atr"] = None
        if e.get("armed_price") is not None and np.isfinite(a) and a > 0:
            row["move_since_arm_atr"] = abs(entry_px - float(e["armed_price"])) / a
        else:
            row["move_since_arm_atr"] = None
        if e.get("pullback_high") is not None and e.get("pullback_low") is not None and np.isfinite(a) and a > 0:
            row["pullback_depth_atr"] = (float(e["pullback_high"]) - float(e["pullback_low"])) / a
        else:
            row["pullback_depth_atr"] = None
        # Fake classification
        fake = False
        if row.get("mae_before_mfe"):
            fake = True
        if row.get("reversal_within_5") is True:
            fake = True
        if row.get("fwd_ret_10") is not None and row["fwd_ret_10"] < 0:
            fake = True
        if row.get("t1.0_s1.0_stop_first") is True:
            fake = True
        row["is_fake"] = fake
        late = bool(row.get("move_since_arm_atr") is not None and row["move_since_arm_atr"] > 1.5)
        row["is_late"] = late
        out.append(row)
    return out


def ablation_configs(base_name: str = ABLATION_BASE) -> list[PullbackEntryConfig]:
    base = next(c for c in RESEARCH_VARIANTS if c.name == base_name)
    return [
        base,
        PullbackEntryConfig(**{**base.to_dict(), "name": f"{base_name}_no_ema", "require_ema_direction": False, "require_ema_slope": False}),
        PullbackEntryConfig(**{**base.to_dict(), "name": f"{base_name}_no_lh", "require_lower_high": False, "rejection_mode": "ema_rejection"}),
        PullbackEntryConfig(**{**base.to_dict(), "name": f"{base_name}_no_adx", "require_adx_di": False}),
        PullbackEntryConfig(**{**base.to_dict(), "name": f"{base_name}_no_atr", "require_atr_anti_chase": False}),
        PullbackEntryConfig(**{**base.to_dict(), "name": f"{base_name}_no_15m", "mtf_mode": "none"}),
        PullbackEntryConfig(**{**base.to_dict(), "name": f"{base_name}_no_30m", "mtf_mode": "setup_15m"}),
    ]

"""Phase C3.4A causal market-structure state machine (research-only).

Python is the source of truth. Structure holds the major trend until a
confirmed major break; indicator flips alone do not reverse structure.
Does not modify clean-regime, production bots, or trend_regime_classifier.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe

STRUCTURE_STATES: tuple[str, ...] = (
    "structure_unknown",
    "range_unclear",
    "bullish_structure",
    "bearish_structure",
    "bullish_pullback",
    "bearish_pullback",
    "bullish_break_attempt",
    "bearish_break_attempt",
    "bullish_break_confirmed",
    "bearish_break_confirmed",
    "bullish_retest_pending",
    "bearish_retest_pending",
    "bullish_break_failed",
    "bearish_break_failed",
    "transition_blocked",
)

STRUCTURE_STATE_CODE: dict[str, int] = {
    "structure_unknown": 0,
    "range_unclear": 1,
    "bullish_structure": 2,
    "bullish_pullback": 3,
    "bullish_break_attempt": 4,
    "bullish_break_confirmed": 5,
    "bullish_retest_pending": 6,
    "bullish_break_failed": 7,
    "bearish_structure": -2,
    "bearish_pullback": -3,
    "bearish_break_attempt": -4,
    "bearish_break_confirmed": -5,
    "bearish_retest_pending": -6,
    "bearish_break_failed": -7,
    "transition_blocked": 9,
}

DIRECTION_CODE: dict[str, int] = {
    "structure_unknown": 0,
    "range_unclear": 0,
    "transition_blocked": 0,
    "bullish_structure": 1,
    "bullish_pullback": 1,
    "bullish_break_attempt": 1,
    "bullish_break_confirmed": 1,
    "bullish_retest_pending": 1,
    "bullish_break_failed": 1,
    "bearish_structure": -1,
    "bearish_pullback": -1,
    "bearish_break_attempt": -1,
    "bearish_break_confirmed": -1,
    "bearish_retest_pending": -1,
    "bearish_break_failed": -1,
}

# Predefined research matrix (not a full cartesian product).
RESEARCH_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "name": "balanced_medium",
        "swing_sensitivity": "medium",
        "transition_zone_atr": 0.50,
        "break_mode": "medium",
        "retest_mode": "none",
        "label": "balanced_research_variant",
    },
    {
        "name": "fast_light",
        "swing_sensitivity": "light",
        "transition_zone_atr": 0.25,
        "break_mode": "light",
        "retest_mode": "none",
        "label": "fastest_break_detection",
    },
    {
        "name": "strict_strong",
        "swing_sensitivity": "strong",
        "transition_zone_atr": 0.75,
        "break_mode": "strong",
        "retest_mode": "retest",
        "label": "fewest_false_breaks",
    },
    {
        "name": "wide_zone_medium",
        "swing_sensitivity": "medium",
        "transition_zone_atr": 1.00,
        "break_mode": "medium",
        "retest_mode": "hold",
        "label": "lowest_noise",
    },
    {
        "name": "medium_retest",
        "swing_sensitivity": "medium",
        "transition_zone_atr": 0.50,
        "break_mode": "medium",
        "retest_mode": "retest",
        "label": "retest_required",
    },
)

SWING_PRESETS: dict[str, dict[str, float | int]] = {
    "light": {
        "lookback": 3,
        "confirm_bars": 2,
        "min_reversal_atr": 0.35,
        "major_min_reversal_atr": 0.80,
        "major_min_bars_between": 6,
        "micro_min_bars_between": 2,
    },
    "medium": {
        "lookback": 5,
        "confirm_bars": 3,
        "min_reversal_atr": 0.50,
        "major_min_reversal_atr": 1.20,
        "major_min_bars_between": 10,
        "micro_min_bars_between": 3,
    },
    "strong": {
        "lookback": 8,
        "confirm_bars": 4,
        "min_reversal_atr": 0.70,
        "major_min_reversal_atr": 1.80,
        "major_min_bars_between": 14,
        "micro_min_bars_between": 4,
    },
}

BREAK_PRESETS: dict[str, dict[str, float | int]] = {
    "light": {"min_close_beyond_atr": 0.05, "required_closes": 1},
    "medium": {"min_close_beyond_atr": 0.10, "required_closes": 1},
    "strong": {"min_close_beyond_atr": 0.15, "required_closes": 2},
}


@dataclass(frozen=True)
class MarketStructureConfig:
    """Central C3.4A configuration."""

    variant_name: str = "balanced_medium"
    swing_sensitivity: str = "medium"
    transition_zone_atr: float = 0.50
    break_mode: str = "medium"
    retest_mode: str = "none"  # none | hold | retest
    lookback: int = 5
    confirm_bars: int = 3
    min_reversal_atr: float = 0.50
    major_min_reversal_atr: float = 1.20
    major_min_bars_between: int = 10
    micro_min_bars_between: int = 3
    min_close_beyond_atr: float = 0.10
    required_closes: int = 1
    retest_tolerance_atr: float = 0.25
    retest_hold_bars: int = 2
    rule_spec_version: str = "c3_4a_market_structure_v1"
    label: str = "balanced_research_variant"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_matrix_entry(cls, entry: Mapping[str, Any]) -> MarketStructureConfig:
        swing = SWING_PRESETS[str(entry["swing_sensitivity"])]
        brk = BREAK_PRESETS[str(entry["break_mode"])]
        return cls(
            variant_name=str(entry["name"]),
            swing_sensitivity=str(entry["swing_sensitivity"]),
            transition_zone_atr=float(entry["transition_zone_atr"]),
            break_mode=str(entry["break_mode"]),
            retest_mode=str(entry["retest_mode"]),
            lookback=int(swing["lookback"]),
            confirm_bars=int(swing["confirm_bars"]),
            min_reversal_atr=float(swing["min_reversal_atr"]),
            major_min_reversal_atr=float(swing["major_min_reversal_atr"]),
            major_min_bars_between=int(swing["major_min_bars_between"]),
            micro_min_bars_between=int(swing["micro_min_bars_between"]),
            min_close_beyond_atr=float(brk["min_close_beyond_atr"]),
            required_closes=int(brk["required_closes"]),
            label=str(entry.get("label") or entry["name"]),
        )


def build_rule_spec(cfg: MarketStructureConfig) -> dict[str, Any]:
    return {
        "rule_spec_version": cfg.rule_spec_version,
        "variant_name": cfg.variant_name,
        "states": list(STRUCTURE_STATES),
        "state_codes": dict(STRUCTURE_STATE_CODE),
        "direction_codes": dict(DIRECTION_CODE),
        "research_matrix": list(RESEARCH_MATRIX),
        "swing": {
            "method": "causal_extremum_then_reversal",
            "no_future_right_bars": True,
            "lookback": cfg.lookback,
            "confirm_bars": cfg.confirm_bars,
            "min_reversal_atr": cfg.min_reversal_atr,
            "major_min_reversal_atr": cfg.major_min_reversal_atr,
            "major_min_bars_between": cfg.major_min_bars_between,
            "micro_min_bars_between": cfg.micro_min_bars_between,
            "live_from": "confirmed_timestamp_for_level_activation",
            "extreme_timestamp_source": "pivot_candle_open_when_stamped",
            "note": (
                "Protected/swing level becomes live only at confirmed_timestamp; "
                "extreme_timestamp records the pivot candle open when available."
            ),
        },
        "break": {
            "mode": cfg.break_mode,
            "min_close_beyond_atr": cfg.min_close_beyond_atr,
            "required_closes": cfg.required_closes,
            "wick_alone_insufficient": True,
        },
        "transition_zone_atr": cfg.transition_zone_atr,
        "retest_mode": cfg.retest_mode,
        "retest_tolerance_atr": cfg.retest_tolerance_atr,
        "retest_hold_bars": cfg.retest_hold_bars,
        "policy": {
            "major_structure_holds_until_confirmed_major_break": True,
            "indicator_flip_alone_does_not_reverse_major": True,
            "no_repaint_closed_bars": True,
            "no_future_lookahead": True,
            "no_centered_windows": True,
            "retro_outcomes_excluded_from_state": True,
        },
        "config": cfg.to_dict(),
    }


def config_hash(cfg: MarketStructureConfig) -> str:
    blob = json.dumps(json_safe(cfg.to_dict()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def rule_spec_hash(spec: Mapping[str, Any] | None = None, cfg: MarketStructureConfig | None = None) -> str:
    if spec is None:
        cfg = cfg or MarketStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
        spec = build_rule_spec(cfg)
    blob = json.dumps(json_safe(dict(spec)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def python_rule_hash(cfg: MarketStructureConfig) -> str:
    return rule_spec_hash(cfg=cfg)


def pine_rule_hash(cfg: MarketStructureConfig) -> str:
    return rule_spec_hash(cfg=cfg)


def _finite(value: object, default: float = float("nan")) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


@dataclass
class SwingPoint:
    kind: str  # high | low
    level: float
    extreme_bar: int
    extreme_timestamp: Any
    confirmed_bar: int
    confirmed_timestamp: Any
    confirmation_delay_bars: int
    atr_at_confirm: float
    is_major: bool
    swing_type: str = ""  # HH/HL/LH/LL once classified

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StructureRuntime:
    state: str = "structure_unknown"
    state_age_bars: int = 0
    bars_since_transition: int = 0
    last_transition_reason: str = "init"
    micro_highs: list[SwingPoint] = field(default_factory=list)
    micro_lows: list[SwingPoint] = field(default_factory=list)
    major_highs: list[SwingPoint] = field(default_factory=list)
    major_lows: list[SwingPoint] = field(default_factory=list)
    pending_high_bar: int | None = None
    pending_high_level: float | None = None
    pending_low_bar: int | None = None
    pending_low_level: float | None = None
    last_confirmed_micro_high_source_bar: int | None = None
    last_confirmed_micro_low_source_bar: int | None = None
    close_beyond_up_streak: int = 0
    close_beyond_down_streak: int = 0
    retest_side: str | None = None
    retest_level: float | None = None
    retest_hold_count: int = 0
    transition_zone_started_at: int | None = None
    broken_level: float | None = None
    broken_side: str | None = None
    major_direction: int = 0  # -1/0/1
    micro_direction: int = 0


def _classify_swing_sequence(highs: Sequence[SwingPoint], lows: Sequence[SwingPoint]) -> int:
    """Return +1 bullish HH/HL, -1 bearish LH/LL, else 0."""
    if len(highs) >= 2 and len(lows) >= 2:
        h1, h2 = highs[-2], highs[-1]
        l1, l2 = lows[-2], lows[-1]
        if h2.level > h1.level and l2.level > l1.level:
            return 1
        if h2.level < h1.level and l2.level < l1.level:
            return -1
    if len(highs) >= 2:
        if highs[-1].level > highs[-2].level:
            return 1
        if highs[-1].level < highs[-2].level:
            return -1
    if len(lows) >= 2:
        if lows[-1].level > lows[-2].level:
            return 1
        if lows[-1].level < lows[-2].level:
            return -1
    return 0


def _tag_swing_types(rt: StructureRuntime) -> None:
    for seq, is_high in ((rt.major_highs, True), (rt.micro_highs, True), (rt.major_lows, False), (rt.micro_lows, False)):
        for i, sw in enumerate(seq):
            if i == 0:
                sw.swing_type = "H" if is_high else "L"
                continue
            prev = seq[i - 1]
            if is_high:
                sw.swing_type = "HH" if sw.level > prev.level else "LH"
            else:
                sw.swing_type = "HL" if sw.level > prev.level else "LL"


def _maybe_confirm_swings(
    rt: StructureRuntime,
    *,
    bar_i: int,
    high: float,
    low: float,
    close: float,
    atr: float,
    ts: Any,
    cfg: MarketStructureConfig,
    highs_window: Sequence[float],
    lows_window: Sequence[float],
    timestamps_window: Sequence[Any] | None = None,
) -> list[SwingPoint]:
    """Causal swing confirmation on the current bar only."""
    newly: list[SwingPoint] = []
    atr = max(atr, 1e-12)
    ts_win = list(timestamps_window) if timestamps_window is not None else []

    def _extreme_ts(extreme_bar: int) -> Any:
        if 0 <= extreme_bar < len(ts_win):
            return ts_win[extreme_bar]
        return None

    # Candidate swing high: current lookback max at a past extreme still pending.
    if len(highs_window) >= cfg.lookback:
        # Extreme is the max in the lookback window ending at previous bar.
        window = list(highs_window[-(cfg.lookback + 1) : -1]) if len(highs_window) > cfg.lookback else list(highs_window[:-1])
        if window:
            ext_level = max(window)
            ext_offset = len(window) - 1 - window[::-1].index(ext_level)
            ext_bar = bar_i - (len(window) - ext_offset)
            already = (
                rt.last_confirmed_micro_high_source_bar is not None
                and ext_bar == rt.last_confirmed_micro_high_source_bar
            )
            if not already and (
                rt.pending_high_bar is None
                or ext_bar != rt.pending_high_bar
                or ext_level >= (rt.pending_high_level or -np.inf)
            ):
                rt.pending_high_bar = ext_bar
                rt.pending_high_level = ext_level

    if len(lows_window) >= cfg.lookback:
        window = list(lows_window[-(cfg.lookback + 1) : -1]) if len(lows_window) > cfg.lookback else list(lows_window[:-1])
        if window:
            ext_level = min(window)
            ext_offset = len(window) - 1 - window[::-1].index(ext_level)
            ext_bar = bar_i - (len(window) - ext_offset)
            already = (
                rt.last_confirmed_micro_low_source_bar is not None
                and ext_bar == rt.last_confirmed_micro_low_source_bar
            )
            if not already and (
                rt.pending_low_bar is None
                or ext_bar != rt.pending_low_bar
                or ext_level <= (rt.pending_low_level or np.inf)
            ):
                rt.pending_low_bar = ext_bar
                rt.pending_low_level = ext_level

    # Confirm high: price reversed down enough and enough bars since extreme.
    if rt.pending_high_bar is not None and rt.pending_high_level is not None:
        delay = bar_i - rt.pending_high_bar
        reverse = (rt.pending_high_level - close) / atr
        if delay >= cfg.confirm_bars and reverse >= cfg.min_reversal_atr:
            # Defensive: never re-confirm the same source extreme bar.
            if (
                rt.last_confirmed_micro_high_source_bar is not None
                and rt.pending_high_bar == rt.last_confirmed_micro_high_source_bar
            ):
                rt.pending_high_bar = None
                rt.pending_high_level = None
            elif rt.micro_highs and (bar_i - rt.micro_highs[-1].confirmed_bar) < cfg.micro_min_bars_between:
                # Spacing reject: drop stale pending so the same extreme is not re-tried forever.
                rt.pending_high_bar = None
                rt.pending_high_level = None
            else:
                is_major = reverse >= cfg.major_min_reversal_atr
                if is_major and rt.major_highs and (bar_i - rt.major_highs[-1].confirmed_bar) < cfg.major_min_bars_between:
                    is_major = False
                sw = SwingPoint(
                    kind="high",
                    level=float(rt.pending_high_level),
                    extreme_bar=int(rt.pending_high_bar),
                    extreme_timestamp=_extreme_ts(int(rt.pending_high_bar)),
                    confirmed_bar=bar_i,
                    confirmed_timestamp=ts,
                    confirmation_delay_bars=delay,
                    atr_at_confirm=atr,
                    is_major=is_major,
                )
                rt.micro_highs.append(sw)
                if is_major:
                    rt.major_highs.append(sw)
                newly.append(sw)
                rt.last_confirmed_micro_high_source_bar = int(rt.pending_high_bar)
                rt.pending_high_bar = None
                rt.pending_high_level = None

    if rt.pending_low_bar is not None and rt.pending_low_level is not None:
        delay = bar_i - rt.pending_low_bar
        reverse = (close - rt.pending_low_level) / atr
        if delay >= cfg.confirm_bars and reverse >= cfg.min_reversal_atr:
            if (
                rt.last_confirmed_micro_low_source_bar is not None
                and rt.pending_low_bar == rt.last_confirmed_micro_low_source_bar
            ):
                rt.pending_low_bar = None
                rt.pending_low_level = None
            elif rt.micro_lows and (bar_i - rt.micro_lows[-1].confirmed_bar) < cfg.micro_min_bars_between:
                rt.pending_low_bar = None
                rt.pending_low_level = None
            else:
                is_major = reverse >= cfg.major_min_reversal_atr
                if is_major and rt.major_lows and (bar_i - rt.major_lows[-1].confirmed_bar) < cfg.major_min_bars_between:
                    is_major = False
                sw = SwingPoint(
                    kind="low",
                    level=float(rt.pending_low_level),
                    extreme_bar=int(rt.pending_low_bar),
                    extreme_timestamp=_extreme_ts(int(rt.pending_low_bar)),
                    confirmed_bar=bar_i,
                    confirmed_timestamp=ts,
                    confirmation_delay_bars=delay,
                    atr_at_confirm=atr,
                    is_major=is_major,
                )
                rt.micro_lows.append(sw)
                if is_major:
                    rt.major_lows.append(sw)
                newly.append(sw)
                rt.last_confirmed_micro_low_source_bar = int(rt.pending_low_bar)
                rt.pending_low_bar = None
                rt.pending_low_level = None

    _tag_swing_types(rt)
    # Micro may reclassify freely. Major direction is sticky until a confirmed
    # structure break flips it — swing re-labeling alone must not reverse trend.
    rt.micro_direction = _classify_swing_sequence(rt.micro_highs, rt.micro_lows)
    candidate_major = _classify_swing_sequence(rt.major_highs, rt.major_lows)
    if rt.major_direction == 0:
        rt.major_direction = candidate_major
    elif candidate_major == rt.major_direction:
        pass
    # else keep sticky major_direction
    return newly


def _active_break_levels(rt: StructureRuntime) -> tuple[float | None, float | None]:
    """Up-break = last major high; down-break = last major low (causal)."""
    up = rt.major_highs[-1].level if rt.major_highs else None
    down = rt.major_lows[-1].level if rt.major_lows else None
    return up, down


def step_market_structure_state(
    previous_state: str,
    runtime_state: StructureRuntime | None,
    prepared_bar: Mapping[str, Any],
    config: MarketStructureConfig,
) -> tuple[str, StructureRuntime, dict[str, Any]]:
    """Deterministic causal structure step for one closed candle."""
    rt = runtime_state or StructureRuntime(state=previous_state)
    if rt.state != previous_state:
        rt.state = previous_state
        rt.state_age_bars = 0

    bar_i = int(prepared_bar["bar_index"])
    high = _finite(prepared_bar["high"])
    low = _finite(prepared_bar["low"])
    close = _finite(prepared_bar["close"])
    atr = max(_finite(prepared_bar.get("atr_14"), 1.0), 1e-12)
    ts = prepared_bar.get("decision_time") or prepared_bar.get("timestamp")
    highs_window = prepared_bar.get("highs_window") or [high]
    lows_window = prepared_bar.get("lows_window") or [low]
    clean_state = str(prepared_bar.get("indicator_clean_regime_state") or "neutral")

    timestamps_window = prepared_bar.get("timestamps_window")
    newly = _maybe_confirm_swings(
        rt,
        bar_i=bar_i,
        high=high,
        low=low,
        close=close,
        atr=atr,
        ts=ts,
        cfg=config,
        highs_window=list(highs_window),
        lows_window=list(lows_window),
        timestamps_window=timestamps_window,
    )

    up_break, down_break = _active_break_levels(rt)
    dist_up = ((up_break - close) / atr) if up_break is not None else None
    dist_down = ((close - down_break) / atr) if down_break is not None else None
    dist_up_pct = ((up_break - close) / abs(close) * 100.0) if up_break and close else None
    dist_down_pct = ((close - down_break) / abs(close) * 100.0) if down_break and close else None

    wick_break_up = bool(up_break is not None and high > up_break)
    wick_break_down = bool(down_break is not None and low < down_break)
    close_break_up = bool(
        up_break is not None and close > up_break + config.min_close_beyond_atr * atr
    )
    close_break_down = bool(
        down_break is not None and close < down_break - config.min_close_beyond_atr * atr
    )

    if close_break_up:
        rt.close_beyond_up_streak += 1
    else:
        rt.close_beyond_up_streak = 0
    if close_break_down:
        rt.close_beyond_down_streak += 1
    else:
        rt.close_beyond_down_streak = 0

    confirmed_break_up = rt.close_beyond_up_streak >= config.required_closes
    confirmed_break_down = rt.close_beyond_down_streak >= config.required_closes

    # Transition zone: approaching break level without confirmed break.
    zone_up = bool(
        up_break is not None
        and dist_up is not None
        and 0.0 <= dist_up <= config.transition_zone_atr
        and not confirmed_break_up
    )
    zone_down = bool(
        down_break is not None
        and dist_down is not None
        and 0.0 <= dist_down <= config.transition_zone_atr
        and not confirmed_break_down
    )
    transition_zone_active = zone_up or zone_down
    transition_zone_side = "up" if zone_up else ("down" if zone_down else None)
    if transition_zone_active:
        if rt.transition_zone_started_at is None:
            rt.transition_zone_started_at = bar_i
    else:
        rt.transition_zone_started_at = None
    transition_zone_age = (
        bar_i - rt.transition_zone_started_at if rt.transition_zone_started_at is not None else 0
    )

    break_rejected_up = bool(wick_break_up and not close_break_up and close < (up_break or close))
    break_rejected_down = bool(wick_break_down and not close_break_down and close > (down_break or close))

    prev = previous_state
    reason = "hold"
    new_state = prev
    retest_pending = prev in {"bullish_retest_pending", "bearish_retest_pending"}
    retest_confirmed = False
    break_failed = False
    break_attempt_side = None

    major_dir = rt.major_direction
    # Bootstrap major direction from state if swings sparse.
    if major_dir == 0:
        if prev.startswith("bullish"):
            major_dir = 1
        elif prev.startswith("bearish"):
            major_dir = -1

    # Retest handling first if pending.
    if prev == "bullish_retest_pending" and rt.retest_level is not None:
        tol = config.retest_tolerance_atr * atr
        held = low >= rt.retest_level - tol and close >= rt.retest_level - tol
        failed = close < rt.retest_level - tol
        if failed:
            new_state = "bullish_break_failed"
            reason = "bullish_retest_failed"
            break_failed = True
            rt.retest_side = None
            rt.retest_level = None
            rt.retest_hold_count = 0
        elif held:
            rt.retest_hold_count += 1
            if rt.retest_hold_count >= config.retest_hold_bars:
                new_state = "bullish_structure"
                reason = "bullish_retest_confirmed"
                retest_confirmed = True
                rt.major_direction = 1
                rt.retest_side = None
                rt.retest_level = None
                rt.retest_hold_count = 0
            else:
                new_state = "bullish_retest_pending"
                reason = "bullish_retest_holding"
                retest_pending = True
        else:
            new_state = "bullish_retest_pending"
            retest_pending = True
    elif prev == "bearish_retest_pending" and rt.retest_level is not None:
        tol = config.retest_tolerance_atr * atr
        held = high <= rt.retest_level + tol and close <= rt.retest_level + tol
        failed = close > rt.retest_level + tol
        if failed:
            new_state = "bearish_break_failed"
            reason = "bearish_retest_failed"
            break_failed = True
            rt.retest_side = None
            rt.retest_level = None
            rt.retest_hold_count = 0
        elif held:
            rt.retest_hold_count += 1
            if rt.retest_hold_count >= config.retest_hold_bars:
                new_state = "bearish_structure"
                reason = "bearish_retest_confirmed"
                retest_confirmed = True
                rt.major_direction = -1
                rt.retest_side = None
                rt.retest_level = None
                rt.retest_hold_count = 0
            else:
                new_state = "bearish_retest_pending"
                reason = "bearish_retest_holding"
                retest_pending = True
        else:
            new_state = "bearish_retest_pending"
            retest_pending = True
    else:
        # Main structure logic.
        if major_dir == 0 and len(rt.major_highs) + len(rt.major_lows) < 2:
            new_state = "structure_unknown"
            reason = "awaiting_major_swings"
        elif major_dir == 0:
            new_state = "range_unclear"
            reason = "unclear_major_sequence"
        elif major_dir < 0:
            # Bearish major structure holds until confirmed up-break of major high.
            if confirmed_break_up:
                if config.retest_mode == "retest":
                    new_state = "bullish_retest_pending"
                    reason = "confirmed_up_break_retest_pending"
                    rt.retest_side = "up"
                    rt.retest_level = up_break
                    rt.retest_hold_count = 0
                    rt.broken_level = up_break
                    rt.broken_side = "up"
                    retest_pending = True
                elif config.retest_mode == "hold":
                    new_state = "bullish_break_confirmed"
                    reason = "confirmed_up_break_hold"
                    rt.major_direction = 1
                else:
                    new_state = "bullish_structure"
                    reason = "confirmed_up_break_flip"
                    rt.major_direction = 1
            elif close_break_up or wick_break_up:
                new_state = "bullish_break_attempt"
                reason = "up_break_attempt_unconfirmed"
                break_attempt_side = "up"
            elif transition_zone_active and zone_up:
                new_state = "transition_blocked"
                reason = "near_major_high_transition_zone"
            elif clean_state in {"bullish_building", "bullish_confirmed"} or close > (down_break or close):
                # Indicator bullish against bearish structure -> pullback, not flip.
                new_state = "bearish_pullback"
                reason = "bullish_indicator_or_bounce_inside_bearish_structure"
            else:
                new_state = "bearish_structure"
                reason = "hold_bearish_major"
        else:
            # Bullish major structure holds until confirmed down-break of major low.
            if confirmed_break_down:
                if config.retest_mode == "retest":
                    new_state = "bearish_retest_pending"
                    reason = "confirmed_down_break_retest_pending"
                    rt.retest_side = "down"
                    rt.retest_level = down_break
                    rt.retest_hold_count = 0
                    rt.broken_level = down_break
                    rt.broken_side = "down"
                    retest_pending = True
                elif config.retest_mode == "hold":
                    new_state = "bearish_break_confirmed"
                    reason = "confirmed_down_break_hold"
                    rt.major_direction = -1
                else:
                    new_state = "bearish_structure"
                    reason = "confirmed_down_break_flip"
                    rt.major_direction = -1
            elif close_break_down or wick_break_down:
                new_state = "bearish_break_attempt"
                reason = "down_break_attempt_unconfirmed"
                break_attempt_side = "down"
            elif transition_zone_active and zone_down:
                new_state = "transition_blocked"
                reason = "near_major_low_transition_zone"
            elif clean_state in {"bearish_building", "bearish_confirmed"}:
                new_state = "bullish_pullback"
                reason = "bearish_indicator_inside_bullish_structure"
            else:
                new_state = "bullish_structure"
                reason = "hold_bullish_major"

        # Break failure: attempt rejected back inside.
        if prev in {"bullish_break_attempt", "bullish_break_confirmed"} and break_rejected_up and major_dir < 0:
            new_state = "bullish_break_failed"
            reason = "up_break_rejected"
            break_failed = True
        if prev in {"bearish_break_attempt", "bearish_break_confirmed"} and break_rejected_down and major_dir > 0:
            new_state = "bearish_break_failed"
            reason = "down_break_rejected"
            break_failed = True

    changed = new_state != prev
    if changed:
        rt.state_age_bars = 1
        rt.bars_since_transition = 0
        rt.last_transition_reason = reason
    else:
        rt.state_age_bars += 1
        rt.bars_since_transition += 1
        rt.last_transition_reason = reason
    rt.state = new_state

    # Alignment with clean regime (research only).
    if new_state == "transition_blocked":
        alignment = "transition_blocked"
    elif new_state in {"structure_unknown", "range_unclear"}:
        alignment = "structure_unclear"
    elif clean_state == "neutral":
        alignment = "indicator_neutral"
    elif DIRECTION_CODE.get(new_state, 0) > 0 and clean_state.startswith("bullish"):
        alignment = "aligned_bullish"
    elif DIRECTION_CODE.get(new_state, 0) < 0 and clean_state.startswith("bearish"):
        alignment = "aligned_bearish"
    elif DIRECTION_CODE.get(new_state, 0) < 0 and clean_state.startswith("bullish"):
        alignment = "bullish_indicator_against_bearish_structure"
    elif DIRECTION_CODE.get(new_state, 0) > 0 and clean_state.startswith("bearish"):
        alignment = "bearish_indicator_against_bullish_structure"
    else:
        alignment = "structure_unclear"

    last_maj_h = rt.major_highs[-1] if rt.major_highs else None
    last_maj_l = rt.major_lows[-1] if rt.major_lows else None
    last_mic_h = rt.micro_highs[-1] if rt.micro_highs else None
    last_mic_l = rt.micro_lows[-1] if rt.micro_lows else None

    diag = {
        "market_structure_state": new_state,
        "previous_market_structure_state": prev,
        "market_structure_changed": changed,
        "structure_direction": DIRECTION_CODE.get(new_state, 0),
        "structure_state_code": STRUCTURE_STATE_CODE.get(new_state, 0),
        "structure_age_bars": rt.state_age_bars,
        "bars_since_last_transition": rt.bars_since_transition,
        "micro_structure_direction": rt.micro_direction,
        "major_structure_direction": rt.major_direction,
        "last_confirmed_swing_high": None if last_mic_h is None else last_mic_h.level,
        "last_confirmed_swing_high_time": None if last_mic_h is None else last_mic_h.confirmed_timestamp,
        "last_confirmed_swing_low": None if last_mic_l is None else last_mic_l.level,
        "last_confirmed_swing_low_time": None if last_mic_l is None else last_mic_l.confirmed_timestamp,
        "last_major_high": None if last_maj_h is None else last_maj_h.level,
        "last_major_low": None if last_maj_l is None else last_maj_l.level,
        "active_up_break_level": up_break,
        "active_down_break_level": down_break,
        "distance_to_up_break_atr": dist_up,
        "distance_to_down_break_atr": dist_down,
        "distance_to_up_break_pct": dist_up_pct,
        "distance_to_down_break_pct": dist_down_pct,
        "transition_zone_active": transition_zone_active,
        "transition_zone_side": transition_zone_side,
        "transition_zone_distance_atr": dist_up if zone_up else (dist_down if zone_down else None),
        "transition_zone_started_at": rt.transition_zone_started_at,
        "transition_zone_age_bars": transition_zone_age,
        "break_attempt_side": break_attempt_side,
        "wick_break_up": wick_break_up,
        "wick_break_down": wick_break_down,
        "wick_break": wick_break_up or wick_break_down,
        "close_break_up": close_break_up,
        "close_break_down": close_break_down,
        "close_break": close_break_up or close_break_down,
        "confirmed_break_up": confirmed_break_up,
        "confirmed_break_down": confirmed_break_down,
        "confirmed_break": confirmed_break_up or confirmed_break_down,
        "break_rejected_up": break_rejected_up,
        "break_rejected_down": break_rejected_down,
        "retest_pending": retest_pending,
        "retest_confirmed": retest_confirmed,
        "break_failed": break_failed,
        "transition_reason": reason,
        "indicator_clean_regime_state": clean_state,
        "structure_indicator_alignment": alignment,
        "n_new_swings": len(newly),
        "structural_supply_zone": up_break,
        "structural_demand_zone": down_break,
    }
    return new_state, rt, diag


def apply_market_structure(
    ohlcv: pd.DataFrame,
    cfg: MarketStructureConfig,
    *,
    clean_regime_states: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Apply causal structure machine bar-by-bar (immutable past rows)."""
    if ohlcv.empty:
        return ohlcv.copy()
    df = ohlcv.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))
    if "atr_14" not in df.columns:
        # Causal ATR proxy from true range EMA-like rolling mean (past only).
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (df["high"] - df["low"]).abs(),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr_14"] = tr.rolling(14, min_periods=1).mean()

    cfg_h = config_hash(cfg)
    rule_h = rule_spec_hash(cfg=cfg)
    rows: list[dict[str, Any]] = []
    rt = StructureRuntime()
    prev = "structure_unknown"
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    timestamps = [
        (src_ts if (src_ts := row.get("timestamp") or row.get("decision_time")) is not None else None)
        for row in df.to_dict("records")
    ]

    for i in range(len(df)):
        src = df.iloc[i].to_dict()
        clean = "neutral"
        if clean_regime_states is not None and i < len(clean_regime_states):
            clean = str(clean_regime_states[i])
        prepared = {
            **src,
            "bar_index": int(src.get("bar_index", i)),
            "highs_window": highs[: i + 1],
            "lows_window": lows[: i + 1],
            "timestamps_window": timestamps[: i + 1],
            "indicator_clean_regime_state": clean,
        }
        new_state, rt, diag = step_market_structure_state(prev, rt, prepared, cfg)
        row = {
            "bar_index": int(src.get("bar_index", i)),
            "timestamp": src.get("timestamp"),
            "decision_time": src.get("decision_time") or src.get("timestamp"),
            "symbol": src.get("symbol"),
            "timeframe": src.get("timeframe"),
            "open": src.get("open"),
            "high": src.get("high"),
            "low": src.get("low"),
            "close": src.get("close"),
            "atr_14": src.get("atr_14"),
            **diag,
            "config_variant": cfg.variant_name,
            "config_hash": cfg_h,
            "rule_spec_hash": rule_h,
        }
        rows.append(row)
        prev = new_state

    return pd.DataFrame(rows)


def advance_micro_swings(
    runtime_state: StructureRuntime,
    prepared_bar: Mapping[str, Any],
    config: MarketStructureConfig,
) -> list[SwingPoint]:
    """Research API: confirm causal micro/major swings without running C3.4A state.

    Used by C3.4B protected-structure as the micro-structure base layer.
    """
    bar_i = int(prepared_bar["bar_index"])
    high = _finite(prepared_bar["high"])
    low = _finite(prepared_bar["low"])
    close = _finite(prepared_bar["close"])
    atr = max(_finite(prepared_bar.get("atr_14"), 1.0), 1e-12)
    ts = prepared_bar.get("decision_time") or prepared_bar.get("timestamp")
    highs_window = list(prepared_bar.get("highs_window") or [high])
    lows_window = list(prepared_bar.get("lows_window") or [low])
    timestamps_window = prepared_bar.get("timestamps_window")
    return _maybe_confirm_swings(
        runtime_state,
        bar_i=bar_i,
        high=high,
        low=low,
        close=close,
        atr=atr,
        ts=ts,
        cfg=config,
        highs_window=highs_window,
        lows_window=lows_window,
        timestamps_window=timestamps_window,
    )


def bot_interface_frame(structure_df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "timestamp",
        "decision_time",
        "symbol",
        "timeframe",
        "market_structure_state",
        "previous_market_structure_state",
        "market_structure_changed",
        "structure_direction",
        "structure_age_bars",
        "micro_structure_direction",
        "major_structure_direction",
        "last_confirmed_swing_high",
        "last_confirmed_swing_high_time",
        "last_confirmed_swing_low",
        "last_confirmed_swing_low_time",
        "last_major_high",
        "last_major_low",
        "active_up_break_level",
        "active_down_break_level",
        "distance_to_up_break_atr",
        "distance_to_down_break_atr",
        "transition_zone_active",
        "transition_zone_side",
        "break_attempt_side",
        "wick_break",
        "close_break",
        "confirmed_break",
        "retest_pending",
        "retest_confirmed",
        "break_failed",
        "transition_reason",
        "indicator_clean_regime_state",
        "structure_indicator_alignment",
        "config_variant",
        "config_hash",
        "rule_spec_hash",
        "structure_state_code",
    ]
    present = [c for c in cols if c in structure_df.columns]
    return structure_df[present].copy()

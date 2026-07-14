"""Causal multi-timeframe trend state machine (research-only).

Structure-first transitions. Indicators confirm / veto / score only.
No live wiring. No outcomes in state computation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta
from research.regime_scanner.trend_state_policy import DirectionPolicy, policy_for_state
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    StructureEvent,
    TrendStructureConfig,
    default_trend_structure_config,
    events_of_types,
    has_hh_hl,
    has_lh_ll,
    update_market_structure,
)

TrendState = Literal[
    "neutral",
    "bearish_warning",
    "early_bearish",
    "strong_bearish",
    "bearish_weakening",
    "bottoming",
    "bullish_warning",
    "early_bullish",
    "strong_bullish",
    "bullish_weakening",
    "topping",
    "unavailable",
]

FORBIDDEN_DIRECT = frozenset(
    {
        ("strong_bearish", "strong_bullish"),
        ("strong_bullish", "strong_bearish"),
        ("early_bearish", "early_bullish"),
        ("early_bullish", "early_bearish"),
        ("strong_bearish", "early_bullish"),
        ("strong_bullish", "early_bearish"),
        ("bearish_warning", "bullish_warning"),
        ("bullish_warning", "bearish_warning"),
    }
)

MIN_HOLD_DEFAULTS: dict[str, int] = {
    "neutral": 0,
    "bearish_warning": 2,
    "early_bearish": 3,
    "strong_bearish": 4,
    "bearish_weakening": 2,
    "bottoming": 3,
    "bullish_warning": 2,
    "early_bullish": 3,
    "strong_bullish": 4,
    "bullish_weakening": 2,
    "topping": 3,
    "unavailable": 0,
}

# Multi-bar counter-structure evidence (Phase C1 research; default off = baseline).
WeakeningMultiBarMode = Literal["off", "loose", "strict"]

BULLISH_WEAKENING_COUNTER_CATS: frozenset[str] = frozenset(
    {"bearish_choch", "lower_high", "bearish_bos", "failed_breakout"}
)
BEARISH_WEAKENING_COUNTER_CATS: frozenset[str] = frozenset(
    {"bullish_choch", "higher_low", "bullish_bos", "failed_breakdown"}
)
# Hard structure categories required by C1-C (strict).
STRICT_HARD_CATS_BEARISH: frozenset[str] = frozenset({"bearish_choch", "bearish_bos"})
STRICT_HARD_CATS_BULLISH: frozenset[str] = frozenset({"bullish_choch", "bullish_bos"})


@dataclass(frozen=True)
class TrendStateConfig:
    """Research start values — not calendar-fitted."""

    enabled: bool = False
    min_warmup_5m_bars: int = 220
    min_hold_bars: dict[str, int] = field(default_factory=lambda: dict(MIN_HOLD_DEFAULTS))
    structure: TrendStructureConfig = field(default_factory=default_trend_structure_config)
    allow_violent_reversal: bool = False
    bearish_impulse_min_closes: int = 2
    bullish_impulse_min_closes: int = 2
    exit_opposite_closes: int = 2
    no_ll_lookback: int = 6
    di_spread_confirm: float = 5.0
    adx_confirm: float = 18.0
    max_gap_bars: int = 3
    # Phase C1: accumulate opposing structure across closed 5m bars while in weakening.
    # "off" preserves pre-C1 single-bar exits only (C1-A baseline).
    weakening_multi_bar_mode: WeakeningMultiBarMode = "off"
    weakening_evidence_window_bars: int = 36
    weakening_evidence_min_categories: int = 2

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["structure"] = self.structure.to_dict()
        return payload


def default_trend_state_config() -> TrendStateConfig:
    """Baseline config: multi-bar weakening evidence disabled (C1-A)."""
    return TrendStateConfig(enabled=False, weakening_multi_bar_mode="off")


def trend_state_config_c1(mode: WeakeningMultiBarMode) -> TrendStateConfig:
    """Named Phase-C1 research configs (do not change live policy)."""
    return TrendStateConfig(enabled=False, weakening_multi_bar_mode=mode)


@dataclass
class TrendStateSnapshot:
    current_state: str
    previous_state: str | None
    entered_at: str | None
    age_5m_bars: int
    min_hold_remaining: int
    state_confidence: float
    active_reasons: list[str]
    active_structure_events: list[dict[str, Any]]
    bearish_score: float
    bullish_score: float
    weakening_score: float
    bottoming_score: float
    structure_5m: dict[str, Any]
    structure_15m: dict[str, Any]
    context_30m: dict[str, Any]
    allow_long: bool
    allow_short: bool
    require_stricter_long_confirmation: bool
    require_stricter_short_confirmation: bool
    unavailable_reason: str | None = None
    decision_time: str | None = None
    policy: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrendRuntime:
    state: TrendState = "unavailable"
    previous_state: TrendState | None = None
    entered_at: pd.Timestamp | None = None
    age_5m_bars: int = 0
    structure_5m: MarketStructureState = field(
        default_factory=lambda: MarketStructureState(timeframe="5m")
    )
    structure_15m: MarketStructureState = field(
        default_factory=lambda: MarketStructureState(timeframe="15m")
    )
    structure_30m: MarketStructureState = field(
        default_factory=lambda: MarketStructureState(timeframe="30m")
    )
    last_15m_bucket: str | None = None
    last_30m_bucket: str | None = None
    consecutive_bearish_closes: int = 0
    consecutive_bullish_closes: int = 0
    bars_since_ll: int = 0
    bars_since_hh: int = 0
    unavailable_reason: str | None = "warmup"
    last_decision_time: pd.Timestamp | None = None
    # Phase C1 multi-bar counter-evidence while in *_weakening (cleared on leave/reset).
    # category -> first event identity (type|iso_time|level)
    weakening_evidence_keys: dict[str, str] = field(default_factory=dict)
    # category -> age_5m_bars when first accepted (for window expiry)
    weakening_evidence_seen_age: dict[str, int] = field(default_factory=dict)


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _finite(value: object) -> float | None:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def min_hold_for(state: str, cfg: TrendStateConfig) -> int:
    return int(cfg.min_hold_bars.get(str(state), MIN_HOLD_DEFAULTS.get(str(state), 0)))


def transition_allowed(src: str, dst: str) -> bool:
    if src == dst:
        return True
    if (src, dst) in FORBIDDEN_DIRECT:
        return False
    return True


def _event_types(events: list[StructureEvent]) -> set[str]:
    return {e.event_type for e in events}


def _count_confirms(flags: list[bool]) -> int:
    return sum(1 for f in flags if f)


def _indicator_confirms(row: dict[str, Any], *, side: str, cfg: TrendStateConfig) -> tuple[int, list[str]]:
    codes: list[str] = []
    close = _finite(row.get("close"))
    ema9 = _finite(row.get("ema_9"))
    ema20 = _finite(row.get("ema_20"))
    di = _finite(row.get("di_spread"))
    adx = _finite(row.get("adx"))
    slope9 = _finite(row.get("ema_9_slope_3_pct"))
    slope20 = _finite(row.get("ema_20_slope_3_pct"))
    flags: list[bool] = []
    if side == "bearish":
        f = close is not None and ema20 is not None and close < ema20
        flags.append(f)
        if f:
            codes.append("close_lt_ema20")
        f = di is not None and di <= -cfg.di_spread_confirm
        flags.append(f)
        if f:
            codes.append("di_bearish")
        f = slope9 is not None and slope9 < 0
        flags.append(f)
        if f:
            codes.append("ema9_slope_down")
        f = slope20 is not None and slope20 < 0
        flags.append(f)
        if f:
            codes.append("ema20_slope_down")
        f = adx is not None and adx >= cfg.adx_confirm
        flags.append(f)
        if f:
            codes.append("adx_ok")
        f = ema9 is not None and ema20 is not None and ema9 < ema20
        flags.append(f)
        if f:
            codes.append("ema9_lt_ema20")
    else:
        f = close is not None and ema20 is not None and close > ema20
        flags.append(f)
        if f:
            codes.append("close_gt_ema20")
        f = di is not None and di >= cfg.di_spread_confirm
        flags.append(f)
        if f:
            codes.append("di_bullish")
        f = slope9 is not None and slope9 > 0
        flags.append(f)
        if f:
            codes.append("ema9_slope_up")
        f = slope20 is not None and slope20 > 0
        flags.append(f)
        if f:
            codes.append("ema20_slope_up")
        f = adx is not None and adx >= cfg.adx_confirm
        flags.append(f)
        if f:
            codes.append("adx_ok")
        f = ema9 is not None and ema20 is not None and ema9 > ema20
        flags.append(f)
        if f:
            codes.append("ema9_gt_ema20")
    return _count_confirms(flags), codes


def _htf_bias(state: MarketStructureState) -> str:
    return str(state.current_structure_bias)


def _htf_veto_strong_bullish(s15: MarketStructureState, s30: MarketStructureState) -> bool:
    return _htf_bias(s15) == "bullish" and has_hh_hl(s15) and _htf_bias(s30) == "bullish"


def _htf_veto_strong_bearish(s15: MarketStructureState, s30: MarketStructureState) -> bool:
    return _htf_bias(s15) == "bearish" and has_lh_ll(s15) and _htf_bias(s30) == "bearish"


def _scores(
    events: list[StructureEvent],
    s5: MarketStructureState,
    row: dict[str, Any],
    cfg: TrendStateConfig,
) -> dict[str, float]:
    types = _event_types(events)
    bear = 0.0
    bull = 0.0
    weak = 0.0
    bottom = 0.0
    for t in types:
        if t in {"bearish_bos", "bearish_choch", "lower_high", "lower_low", "failed_breakout"}:
            bear += 1.0
        if t in {"bullish_bos", "bullish_choch", "higher_high", "higher_low", "failed_breakdown"}:
            bull += 1.0
        if t in {"failed_breakdown", "bearish_retest_fails", "bullish_choch"}:
            weak += 1.0
            bottom += 0.5
        if t in {"failed_breakout", "bullish_retest_fails", "bearish_choch"}:
            weak += 0.5
    bc, _ = _indicator_confirms(row, side="bearish", cfg=cfg)
    uc, _ = _indicator_confirms(row, side="bullish", cfg=cfg)
    bear += 0.15 * bc
    bull += 0.15 * uc
    if s5.current_structure_bias == "bearish":
        bear += 0.5
    if s5.current_structure_bias == "bullish":
        bull += 0.5
    return {
        "bearish_score": bear,
        "bullish_score": bull,
        "weakening_score": weak,
        "bottoming_score": bottom,
    }


def _can_leave(rt: TrendRuntime, cfg: TrendStateConfig) -> bool:
    return rt.age_5m_bars >= min_hold_for(rt.state, cfg)


def _structure_level_equal(a: float | None, b: float | None) -> bool:
    """Exact float identity (same semantics as structure last_broken_* checks)."""
    if a is None or b is None:
        return False
    return float(a) == float(b)


def _failed_breakdown_is_trenddefining(ev: StructureEvent, s5: MarketStructureState) -> bool:
    """G6: FB level must match active protective_low or current last_broken_low."""
    if ev.event_type != "failed_breakdown":
        return False
    return _structure_level_equal(ev.level, s5.protective_low_level) or _structure_level_equal(
        ev.level, s5.last_broken_low_level
    )


def _failed_breakout_is_trenddefining(ev: StructureEvent, s5: MarketStructureState) -> bool:
    """G6: FO level must match active protective_high or current last_broken_high."""
    if ev.event_type != "failed_breakout":
        return False
    return _structure_level_equal(ev.level, s5.protective_high_level) or _structure_level_equal(
        ev.level, s5.last_broken_high_level
    )


def _events_are_independent(a: StructureEvent, b: StructureEvent) -> bool:
    """G6 independence: different type, different level, different pivot when both set.

    Missing levels are not treated as independent (cannot prove distinct source levels).
    Missing pivots on one/both sides do not invent independence when levels match;
    when levels differ, type+level difference is sufficient.
    """
    if a.event_type == b.event_type:
        return False
    if a.level is None or b.level is None:
        return False
    if _structure_level_equal(a.level, b.level):
        return False
    if a.reference_pivot_time is not None and b.reference_pivot_time is not None:
        if _ts(a.reference_pivot_time) == _ts(b.reference_pivot_time):
            return False
    return True


def _qualified_failed_breakdown_for_weakening(
    events: list[StructureEvent],
    s5: MarketStructureState,
    *,
    strong: bool,
) -> bool:
    """G6 gate for failed_breakdown contribution (same-bar events only)."""
    fbs = [e for e in events if e.event_type == "failed_breakdown"]
    if not fbs:
        return False
    chochs = [e for e in events if e.event_type == "bullish_choch"]
    pair_ok = has_hh_hl(s5)
    for fb in fbs:
        if not _failed_breakdown_is_trenddefining(fb, s5):
            continue
        indep_choch = any(_events_are_independent(fb, c) for c in chochs)
        if strong:
            if indep_choch:
                return True
        elif indep_choch or pair_ok:
            return True
    return False


def _qualified_failed_breakout_for_weakening(
    events: list[StructureEvent],
    s5: MarketStructureState,
    *,
    strong: bool,
) -> bool:
    """G6 gate for failed_breakout contribution (same-bar events only)."""
    fos = [e for e in events if e.event_type == "failed_breakout"]
    if not fos:
        return False
    chochs = [e for e in events if e.event_type == "bearish_choch"]
    pair_ok = has_lh_ll(s5)
    for fo in fos:
        if not _failed_breakout_is_trenddefining(fo, s5):
            continue
        indep_choch = any(_events_are_independent(fo, c) for c in chochs)
        if strong:
            if indep_choch:
                return True
        elif indep_choch or pair_ok:
            return True
    return False


def _enter(
    rt: TrendRuntime,
    new_state: TrendState,
    *,
    decision_time: pd.Timestamp,
    reasons: list[str],
) -> list[str]:
    if new_state == rt.state:
        return reasons
    if not transition_allowed(rt.state, new_state):
        reasons.append(f"forbidden_direct:{rt.state}->{new_state}")
        return reasons
    rt.previous_state = rt.state
    rt.state = new_state
    rt.entered_at = decision_time
    rt.age_5m_bars = 0
    clear_weakening_evidence(rt)
    reasons.append(f"enter:{new_state}")
    return reasons


def _event_identity(event: StructureEvent) -> str:
    et = event.event_time
    iso = et.isoformat() if hasattr(et, "isoformat") else str(et)
    lvl = "" if event.level is None else f"{float(event.level):.8f}"
    return f"{event.event_type}|{iso}|{lvl}"


def clear_weakening_evidence(rt: TrendRuntime) -> None:
    rt.weakening_evidence_keys.clear()
    rt.weakening_evidence_seen_age.clear()


def _weakening_counter_categories(state: str) -> frozenset[str]:
    if state == "bullish_weakening":
        return BULLISH_WEAKENING_COUNTER_CATS
    if state == "bearish_weakening":
        return BEARISH_WEAKENING_COUNTER_CATS
    return frozenset()


def _weakening_continuation_reset(state: str, types: set[str]) -> bool:
    """Clear counter-evidence when old trend clearly continues."""
    if state == "bullish_weakening":
        return "higher_high" in types or (
            "bullish_bos" in types and ("higher_low" in types or "higher_high" in types)
        )
    if state == "bearish_weakening":
        return "lower_low" in types or (
            "bearish_bos" in types and ("lower_high" in types or "lower_low" in types)
        )
    return False


def update_weakening_evidence(
    rt: TrendRuntime,
    *,
    events: list[StructureEvent],
    cfg: TrendStateConfig,
) -> list[str]:
    """Accumulate / expire / reset multi-bar counter evidence. Diagnostic reason codes."""
    notes: list[str] = []
    if cfg.weakening_multi_bar_mode == "off":
        if rt.weakening_evidence_keys:
            clear_weakening_evidence(rt)
        return notes
    if rt.state not in {"bullish_weakening", "bearish_weakening"}:
        if rt.weakening_evidence_keys:
            clear_weakening_evidence(rt)
        return notes

    types = _event_types(events)
    if _weakening_continuation_reset(rt.state, types):
        clear_weakening_evidence(rt)
        notes.append("weakening_evidence_reset_continuation")
        return notes

    window = max(1, int(cfg.weakening_evidence_window_bars))
    age = int(rt.age_5m_bars)
    expired = [
        cat
        for cat, seen_age in list(rt.weakening_evidence_seen_age.items())
        if age - int(seen_age) > window
    ]
    for cat in expired:
        rt.weakening_evidence_keys.pop(cat, None)
        rt.weakening_evidence_seen_age.pop(cat, None)
        notes.append(f"weakening_evidence_expired:{cat}")

    allowed = _weakening_counter_categories(rt.state)
    for ev in events:
        if ev.event_type not in allowed:
            continue
        key = _event_identity(ev)
        cat = ev.event_type
        prior = rt.weakening_evidence_keys.get(cat)
        if prior == key:
            continue  # same structure event already recorded
        # New distinct event for this category: accept first, or refresh age on new identity
        if prior is None:
            rt.weakening_evidence_keys[cat] = key
            rt.weakening_evidence_seen_age[cat] = age
            notes.append(f"weakening_evidence_add:{cat}")
        else:
            # Replace with newer distinct event of same category (still one category slot)
            rt.weakening_evidence_keys[cat] = key
            rt.weakening_evidence_seen_age[cat] = age
            notes.append(f"weakening_evidence_refresh:{cat}")
    return notes


def multi_bar_weakening_exit(
    rt: TrendRuntime,
    *,
    types: set[str],
    row: dict[str, Any],
    cfg: TrendStateConfig,
) -> tuple[TrendState | None, list[str]]:
    """Optional multi-bar exit from weakening → topping/bottoming (Phase C1-B/C)."""
    mode = cfg.weakening_multi_bar_mode
    if mode == "off":
        return None, []
    if rt.state not in {"bullish_weakening", "bearish_weakening"}:
        return None, []

    cats = set(rt.weakening_evidence_keys.keys())
    min_n = max(2, int(cfg.weakening_evidence_min_categories))
    if len(cats) < min_n:
        return None, [f"multi_bar_need_cats:{len(cats)}<{min_n}"]

    if rt.state == "bullish_weakening":
        if "higher_high" in types:
            return None, ["multi_bar_blocked_hh"]
        hard = cats & STRICT_HARD_CATS_BEARISH
        if mode == "strict":
            if not hard:
                return None, ["multi_bar_strict_need_bos_or_choch"]
            impulse_ok = rt.consecutive_bearish_closes >= int(cfg.bearish_impulse_min_closes)
            htf_ok = _htf_bias(rt.structure_15m) == "bearish"
            bear_conf, _ = _indicator_confirms(row, side="bearish", cfg=cfg)
            if not (impulse_ok or htf_ok or bear_conf >= 2):
                return None, ["multi_bar_strict_need_impulse_or_htf"]
        reasons = ["multi_bar_topping_structure", f"mode:{mode}", *sorted(cats)]
        if hard:
            reasons.append("has_hard_bearish_structure")
        return "topping", reasons

    # bearish_weakening
    if "lower_low" in types:
        return None, ["multi_bar_blocked_ll"]
    hard = cats & STRICT_HARD_CATS_BULLISH
    if mode == "strict":
        if not hard:
            return None, ["multi_bar_strict_need_bos_or_choch"]
        impulse_ok = rt.consecutive_bullish_closes >= int(cfg.bullish_impulse_min_closes)
        htf_ok = _htf_bias(rt.structure_15m) == "bullish"
        bull_conf, _ = _indicator_confirms(row, side="bullish", cfg=cfg)
        if not (impulse_ok or htf_ok or bull_conf >= 2):
            return None, ["multi_bar_strict_need_impulse_or_htf"]
    reasons = ["multi_bar_bottoming_structure", f"mode:{mode}", *sorted(cats)]
    if hard:
        reasons.append("has_hard_bullish_structure")
    return "bottoming", reasons


def _propose_transition(
    rt: TrendRuntime,
    *,
    events: list[StructureEvent],
    row: dict[str, Any],
    cfg: TrendStateConfig,
) -> tuple[TrendState | None, list[str]]:
    """Return proposed next state (or None) and reasons. Structure-mandatory."""
    types = _event_types(events)
    reasons: list[str] = []
    s5 = rt.structure_5m
    s15 = rt.structure_15m
    s30 = rt.structure_30m
    bear_conf, bear_codes = _indicator_confirms(row, side="bearish", cfg=cfg)
    bull_conf, bull_codes = _indicator_confirms(row, side="bullish", cfg=cfg)
    state = rt.state

    def need_hold() -> bool:
        return not _can_leave(rt, cfg)

    # --- Weakening / bottoming / topping paths from strong ---
    if state == "strong_bearish":
        if need_hold():
            return None, ["min_hold_strong_bearish"]
        fb_qualified = _qualified_failed_breakdown_for_weakening(events, s5, strong=True)
        weaken_struct = bool(
            fb_qualified
            or (types & {"bullish_choch", "bearish_retest_fails", "higher_low"})
            or (rt.bars_since_ll >= cfg.no_ll_lookback)
        )
        if weaken_struct and not (
            "bearish_bos" in types and "lower_low" in types
        ):
            reasons.extend(["structure_weakening", *sorted(types & {
                "bullish_choch", "bearish_retest_fails", "higher_low"
            })])
            if fb_qualified:
                reasons.append("trenddefining_failed_breakdown_with_counterstructure")
            if bear_conf < bull_conf:
                reasons.append("indicator_confirm_weakening")
            return "bearish_weakening", reasons
        return None, reasons

    if state == "strong_bullish":
        if need_hold():
            return None, ["min_hold_strong_bullish"]
        fo_qualified = _qualified_failed_breakout_for_weakening(events, s5, strong=True)
        weaken_struct = bool(
            fo_qualified
            or (types & {"bearish_choch", "bullish_retest_fails", "lower_high"})
            or (rt.bars_since_hh >= cfg.no_ll_lookback)
        )
        if weaken_struct and not ("bullish_bos" in types and "higher_high" in types):
            reasons.extend(["structure_weakening"])
            if fo_qualified:
                reasons.append("trenddefining_failed_breakout_with_counterstructure")
            return "bullish_weakening", reasons
        return None, reasons

    if state == "bearish_weakening":
        if need_hold():
            return None, ["min_hold_bearish_weakening"]
        # Failed bottom → back to early/strong bearish
        if "lower_low" in types and "bearish_bos" in types:
            return "early_bearish", ["failed_bottom", "ll_bos"]
        # Spec: bottoming needs structural reclaim evidence (combinatorial Pflichtbasis)
        bottom_hits = types & {"failed_breakdown", "bullish_choch", "higher_low", "bullish_bos"}
        if len(bottom_hits) >= 2 and "lower_low" not in types:
            return "bottoming", ["bottoming_structure", *sorted(bottom_hits)]
        mb_state, mb_reasons = multi_bar_weakening_exit(rt, types=types, row=row, cfg=cfg)
        if mb_state is not None:
            return mb_state, mb_reasons
        return None, reasons

    if state == "bullish_weakening":
        if need_hold():
            return None, ["min_hold_bullish_weakening"]
        if "higher_high" in types and "bullish_bos" in types:
            return "early_bullish", ["failed_top", "hh_bos"]
        top_hits = types & {"failed_breakout", "bearish_choch", "lower_high", "bearish_bos"}
        if len(top_hits) >= 2 and "higher_high" not in types:
            return "topping", ["topping_structure", *sorted(top_hits)]
        mb_state, mb_reasons = multi_bar_weakening_exit(rt, types=types, row=row, cfg=cfg)
        if mb_state is not None:
            return mb_state, mb_reasons
        return None, reasons

    if state == "bottoming":
        if need_hold():
            return None, ["min_hold_bottoming"]
        if "lower_low" in types and ("bearish_bos" in types or "bearish_choch" in types):
            return "early_bearish", ["false_bottom"]
        structure_ok = (
            ("higher_low" in types or s5.last_low_label == "higher_low" or has_hh_hl(s5))
            and ("bullish_bos" in types or "bullish_choch" in types)
        )
        if structure_ok:
            if _htf_bias(s15) == "bearish" and "bearish_bos" in types:
                reasons.append("15m_bearish_bos_blocks_early")
                return None, reasons
            if _htf_veto_strong_bearish(s15, s30) and "bullish_bos" not in types:
                reasons.append("30m_15m_bearish_veto_early")
                return None, reasons
            if rt.consecutive_bullish_closes >= cfg.bullish_impulse_min_closes or bull_conf >= 2:
                reasons.extend(["hl_or_bos", *bull_codes[:2]])
                if _htf_bias(s30) == "bearish" and has_lh_ll(s30) and not cfg.allow_violent_reversal:
                    reasons.append("30m_bearish_context_early_only")
                return "early_bullish", reasons
        return None, reasons

    if state == "topping":
        if need_hold():
            return None, ["min_hold_topping"]
        if "higher_high" in types and ("bullish_bos" in types or "bullish_choch" in types):
            return "early_bullish", ["false_top"]
        if (
            ("lower_high" in types or s5.last_high_label == "lower_high")
            and ("bearish_bos" in types or "bearish_choch" in types)
        ):
            if rt.consecutive_bearish_closes >= cfg.bearish_impulse_min_closes or bear_conf >= 2:
                return "early_bearish", ["lh_or_bos"]
        return None, reasons

    if state == "early_bearish":
        if need_hold():
            return None, ["min_hold_early_bearish"]
        non_fb_invalidation = bool(types & {"bearish_retest_fails"}) or (
            "bullish_choch" in types and "higher_low" in types
        )
        fb_qualified = _qualified_failed_breakdown_for_weakening(events, s5, strong=False)
        if non_fb_invalidation or fb_qualified:
            reasons = ["early_invalidation_toward_weakening"]
            if fb_qualified:
                reasons.append("trenddefining_failed_breakdown_with_counterstructure")
            return "bearish_weakening", reasons
        if (
            has_lh_ll(s5)
            and s5.current_structure_bias == "bearish"
            and (_htf_bias(s15) in {"bearish", "neutral"} or "bearish_bos" in types)
            and not _htf_veto_strong_bullish(s15, s30)
        ):
            if "bearish_retest_holds" in types or bear_conf >= 2:
                reasons.extend(["lh_ll", "15m_ok", *bear_codes[:2]])
                return "strong_bearish", reasons
        return None, reasons

    if state == "early_bullish":
        if need_hold():
            return None, ["min_hold_early_bullish"]
        non_fo_invalidation = bool(types & {"bullish_retest_fails"}) or (
            "bearish_choch" in types and "lower_high" in types
        )
        fo_qualified = _qualified_failed_breakout_for_weakening(events, s5, strong=False)
        if non_fo_invalidation or fo_qualified:
            reasons = ["early_invalidation_toward_weakening"]
            if fo_qualified:
                reasons.append("trenddefining_failed_breakout_with_counterstructure")
            return "bullish_weakening", reasons
        if (
            has_hh_hl(s5)
            and s5.current_structure_bias == "bullish"
            and (_htf_bias(s15) in {"bullish", "neutral"} or "bullish_bos" in types)
        ):
            if _htf_veto_strong_bearish(s15, s30) and not cfg.allow_violent_reversal:
                reasons.append("30m_hard_veto_strong_bullish")
                return None, reasons
            if "bullish_retest_holds" in types or bull_conf >= 2:
                return "strong_bullish", ["hh_hl", "15m_ok"]
        return None, reasons

    if state == "bearish_warning":
        if need_hold():
            return None, ["min_hold_bearish_warning"]
        if types & {"bullish_bos", "bullish_choch", "higher_low"} and rt.consecutive_bullish_closes >= cfg.exit_opposite_closes:
            return "neutral", ["warning_invalidated"]
        if "bearish_bos" in types or (
            "lower_high" in types and rt.consecutive_bearish_closes >= cfg.bearish_impulse_min_closes
        ):
            if _htf_veto_strong_bullish(s15, s30):
                reasons.append("15m_30m_bullish_veto")
                return None, reasons
            if _htf_bias(s15) != "bullish" or bear_conf >= 2:
                reasons.extend(["bos_or_lh", *bear_codes[:2]])
                return "early_bearish", reasons
        return None, reasons

    if state == "bullish_warning":
        if need_hold():
            return None, ["min_hold_bullish_warning"]
        if types & {"bearish_bos", "bearish_choch", "lower_high"} and rt.consecutive_bearish_closes >= cfg.exit_opposite_closes:
            return "neutral", ["warning_invalidated"]
        if "bullish_bos" in types or (
            "higher_low" in types and rt.consecutive_bullish_closes >= cfg.bullish_impulse_min_closes
        ):
            if _htf_veto_strong_bearish(s15, s30):
                return None, ["15m_30m_bearish_veto"]
            if _htf_bias(s15) != "bearish" or bull_conf >= 2:
                return "early_bullish", ["bos_or_hl"]
        return None, reasons

    if state in {"neutral", "unavailable"}:
        # Entry into warnings — structure mandatory
        if "bearish_choch" in types or "failed_breakout" in types or (
            "bearish_bos" in types and s5.current_structure_bias != "bullish"
        ):
            if not _htf_veto_strong_bullish(s15, s30):
                reasons.extend(sorted(types & {"bearish_choch", "failed_breakout", "bearish_bos"}))
                return "bearish_warning", reasons
        if "bullish_choch" in types or "failed_breakdown" in types or (
            "bullish_bos" in types and s5.current_structure_bias != "bearish"
        ):
            if not _htf_veto_strong_bearish(s15, s30):
                reasons.extend(sorted(types & {"bullish_choch", "failed_breakdown", "bullish_bos"}))
                return "bullish_warning", reasons
        return None, reasons

    return None, reasons


def _update_impulse_counters(rt: TrendRuntime, row: dict[str, Any]) -> None:
    close = _finite(row.get("close"))
    open_ = _finite(row.get("open"))
    if close is None or open_ is None:
        return
    if close < open_:
        rt.consecutive_bearish_closes += 1
        rt.consecutive_bullish_closes = 0
    elif close > open_:
        rt.consecutive_bullish_closes += 1
        rt.consecutive_bearish_closes = 0
    else:
        rt.consecutive_bearish_closes = 0
        rt.consecutive_bullish_closes = 0


def _update_swing_age(rt: TrendRuntime, events: list[StructureEvent]) -> None:
    types = _event_types(events)
    if "lower_low" in types:
        rt.bars_since_ll = 0
    else:
        rt.bars_since_ll += 1
    if "higher_high" in types:
        rt.bars_since_hh = 0
    else:
        rt.bars_since_hh += 1


def _update_htf_structure(
    rt: TrendRuntime,
    *,
    candles_5m: pd.DataFrame,
    decision_time: pd.Timestamp,
    cfg: TrendStateConfig,
    scanner_cfg: RegimeScannerConfig,
) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    for tf, slot_attr, last_attr in (
        ("15m", "structure_15m", "last_15m_bucket"),
        ("30m", "structure_30m", "last_30m_bucket"),
    ):
        agg = aggregate_candles(candles_5m, tf, decision_time)
        if agg.empty:
            continue
        last = agg.iloc[-1]
        bucket = str(pd.Timestamp(last["timestamp"]))
        if getattr(rt, last_attr) == bucket:
            continue
        # New closed HTF bar
        tf_cfg = scanner_cfg.with_timeframe(tf)
        ind = compute_indicator_frame(agg, config=tf_cfg)
        pivots = find_confirmed_pivots(ind, config=tf_cfg)
        close_time = _ts(last["timestamp"]) + timeframe_timedelta(tf)
        atr = None
        if "atr" in ind.columns:
            atr = _finite(ind.iloc[-1]["atr"])
        st: MarketStructureState = getattr(rt, slot_attr)
        st, evs = update_market_structure(
            st,
            candle=ind.iloc[-1],
            pivots=pivots,
            decision_time=close_time,
            atr=atr,
            cfg=cfg.structure,
        )
        setattr(rt, slot_attr, st)
        setattr(rt, last_attr, bucket)
        events.extend(evs)
    return events


def build_snapshot(
    rt: TrendRuntime,
    *,
    decision_time: pd.Timestamp,
    events: list[StructureEvent],
    scores: dict[str, float],
    reasons: list[str],
    cfg: TrendStateConfig,
) -> TrendStateSnapshot:
    pol = policy_for_state(rt.state)
    hold = min_hold_for(rt.state, cfg)
    remaining = max(0, hold - rt.age_5m_bars)
    conf = min(
        1.0,
        0.35 * rt.structure_5m.structure_confidence
        + 0.05 * (scores["bearish_score"] + scores["bullish_score"])
        + (0.2 if reasons else 0.0),
    )
    return TrendStateSnapshot(
        current_state=rt.state,
        previous_state=rt.previous_state,
        entered_at=None if rt.entered_at is None else rt.entered_at.isoformat(),
        age_5m_bars=rt.age_5m_bars,
        min_hold_remaining=remaining,
        state_confidence=conf,
        active_reasons=list(reasons),
        active_structure_events=[e.to_dict() for e in events],
        bearish_score=float(scores["bearish_score"]),
        bullish_score=float(scores["bullish_score"]),
        weakening_score=float(scores["weakening_score"]),
        bottoming_score=float(scores["bottoming_score"]),
        structure_5m=rt.structure_5m.summary(),
        structure_15m=rt.structure_15m.summary(),
        context_30m=rt.structure_30m.summary(),
        allow_long=pol.allow_long,
        allow_short=pol.allow_short,
        require_stricter_long_confirmation=pol.require_stricter_long_confirmation,
        require_stricter_short_confirmation=pol.require_stricter_short_confirmation,
        unavailable_reason=rt.unavailable_reason if rt.state == "unavailable" else None,
        decision_time=decision_time.isoformat(),
        policy=pol.to_dict(),
    )


def step_trend_state(
    rt: TrendRuntime,
    *,
    candle_row: dict[str, Any] | pd.Series,
    pivots_5m: list[ConfirmedPivot],
    decision_time: object,
    candles_5m_as_of: pd.DataFrame,
    bar_index: int,
    cfg: TrendStateConfig | None = None,
    scanner_cfg: RegimeScannerConfig | None = None,
) -> tuple[TrendRuntime, TrendStateSnapshot, list[StructureEvent]]:
    config = cfg or default_trend_state_config()
    scfg = scanner_cfg or default_regime_scanner_config()
    decision_ts = _ts(decision_time)
    row = candle_row if isinstance(candle_row, dict) else candle_row.to_dict()

    # Gap detection
    if rt.last_decision_time is not None:
        delta_bars = int(
            round((decision_ts - rt.last_decision_time) / pd.Timedelta(minutes=5))
        )
        if delta_bars > int(config.max_gap_bars) + 1:
            rt.state = "unavailable"
            rt.unavailable_reason = "data_gap"
            rt.age_5m_bars = 0
            snap = build_snapshot(
                rt,
                decision_time=decision_ts,
                events=[],
                scores={"bearish_score": 0, "bullish_score": 0, "weakening_score": 0, "bottoming_score": 0},
                reasons=["data_gap"],
                cfg=config,
            )
            rt.last_decision_time = decision_ts
            return rt, snap, []

    if bar_index + 1 < int(config.min_warmup_5m_bars):
        rt.state = "unavailable"
        rt.unavailable_reason = "warmup"
        rt.last_decision_time = decision_ts
        snap = build_snapshot(
            rt,
            decision_time=decision_ts,
            events=[],
            scores={"bearish_score": 0, "bullish_score": 0, "weakening_score": 0, "bottoming_score": 0},
            reasons=["warmup"],
            cfg=config,
        )
        return rt, snap, []

    if rt.state == "unavailable" and rt.unavailable_reason in {"warmup", "data_gap"}:
        rt.state = "neutral"
        rt.unavailable_reason = None
        rt.entered_at = decision_ts
        rt.age_5m_bars = 0
        rt.previous_state = "unavailable"

    atr = _finite(row.get("atr"))
    rt.structure_5m, events_5m = update_market_structure(
        rt.structure_5m,
        candle=row,
        pivots=pivots_5m,
        decision_time=decision_ts,
        atr=atr,
        cfg=config.structure,
    )
    htf_events = _update_htf_structure(
        rt,
        candles_5m=candles_5m_as_of,
        decision_time=decision_ts,
        cfg=config,
        scanner_cfg=scfg,
    )
    events = events_5m + htf_events
    _update_impulse_counters(rt, row)
    _update_swing_age(rt, events_5m)
    evidence_notes = update_weakening_evidence(rt, events=events_5m, cfg=config)

    scores = _scores(events_5m, rt.structure_5m, row, config)
    proposed, reasons = _propose_transition(rt, events=events_5m, row=row, cfg=config)
    if evidence_notes:
        reasons = [*evidence_notes, *reasons]
    if proposed is not None and proposed != rt.state:
        reasons = _enter(rt, proposed, decision_time=decision_ts, reasons=reasons)
    else:
        rt.age_5m_bars += 1
        if not reasons:
            reasons = ["hold"]

    rt.last_decision_time = decision_ts
    snap = build_snapshot(
        rt,
        decision_time=decision_ts,
        events=events_5m,
        scores=scores,
        reasons=reasons,
        cfg=config,
    )
    return rt, snap, events


def run_trend_state_timeline(
    frame_5m: pd.DataFrame,
    *,
    cfg: TrendStateConfig | None = None,
    scanner_cfg: RegimeScannerConfig | None = None,
    start_decision_time: object | None = None,
    end_decision_time: object | None = None,
) -> tuple[list[TrendStateSnapshot], TrendRuntime, list[StructureEvent]]:
    """Deterministic replay over a 5m indicator frame with timestamp + OHLCV + indicators."""
    config = cfg or default_trend_state_config()
    scfg = scanner_cfg or default_regime_scanner_config().with_timeframe("5m")
    df = frame_5m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    if "decision_time" not in df.columns:
        df["decision_time"] = df["timestamp"] + pd.Timedelta(minutes=5)
    else:
        df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    pivots = find_confirmed_pivots(df, config=scfg)
    rt = TrendRuntime()
    snapshots: list[TrendStateSnapshot] = []
    all_events: list[StructureEvent] = []

    start_ts = None if start_decision_time is None else _ts(start_decision_time)
    end_ts = None if end_decision_time is None else _ts(end_decision_time)

    for i, row in df.iterrows():
        decision_ts = _ts(row["decision_time"])
        candles_as_of = df.iloc[: int(i) + 1][
            [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in df.columns]
        ]
        rt, snap, events = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=candles_as_of,
            bar_index=int(i),
            cfg=config,
            scanner_cfg=scfg,
        )
        all_events.extend(events)
        if start_ts is not None and decision_ts < start_ts:
            continue
        if end_ts is not None and decision_ts > end_ts:
            break
        snapshots.append(snap)
    return snapshots, rt, all_events


def assert_no_outcomes_in_snapshot(snap: TrendStateSnapshot) -> None:
    payload = snap.to_dict()
    forbidden = ("pnl", "tp_hit", "outcome", "forward_return", "good_entry")
    blob = str(payload).lower()
    for key in forbidden:
        if key in blob and key in payload:
            raise AssertionError(f"outcome-like field present: {key}")


__all__ = [
    "TrendState",
    "TrendStateConfig",
    "WeakeningMultiBarMode",
    "default_trend_state_config",
    "trend_state_config_c1",
    "TrendStateSnapshot",
    "TrendRuntime",
    "FORBIDDEN_DIRECT",
    "MIN_HOLD_DEFAULTS",
    "BULLISH_WEAKENING_COUNTER_CATS",
    "BEARISH_WEAKENING_COUNTER_CATS",
    "transition_allowed",
    "clear_weakening_evidence",
    "update_weakening_evidence",
    "multi_bar_weakening_exit",
    "step_trend_state",
    "run_trend_state_timeline",
    "build_snapshot",
    "policy_for_state",
    "DirectionPolicy",
    "assert_no_outcomes_in_snapshot",
    "events_of_types",
]

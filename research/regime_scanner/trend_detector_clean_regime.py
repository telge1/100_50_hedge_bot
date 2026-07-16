"""Clean regime state machine for C3.3B trend detector (research-only).

Python is the source of truth. Rule specs drive Python, Pine export, tests,
and the future bot interface. No production/live/regime classifier changes.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping, Sequence

import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_detector_c3_3b_pine import (
    compute_trend_detector_states,
)

CLEAN_STATES: tuple[str, ...] = (
    "neutral",
    "bullish_building",
    "bullish_confirmed",
    "bearish_building",
    "bearish_confirmed",
)

# Debug / Pine numeric codes (signed).
CLEAN_STATE_CODE: dict[str, int] = {
    "neutral": 0,
    "bullish_building": 1,
    "bullish_confirmed": 2,
    "bearish_building": -1,
    "bearish_confirmed": -2,
}

DIRECTION_CODE: dict[str, int] = {
    "neutral": 0,
    "bullish_building": 1,
    "bullish_confirmed": 1,
    "bearish_building": -1,
    "bearish_confirmed": -1,
}

STRENGTH_CODE: dict[str, int] = {
    "neutral": 0,
    "bullish_building": 1,
    "bullish_confirmed": 2,
    "bearish_building": 1,
    "bearish_confirmed": 2,
}

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "neutral": frozenset({"bullish_building", "bearish_building"}),
    "bullish_building": frozenset({"bullish_confirmed", "neutral"}),
    "bullish_confirmed": frozenset({"bullish_building"}),
    "bearish_building": frozenset({"bearish_confirmed", "neutral"}),
    "bearish_confirmed": frozenset({"bearish_building"}),
}

FORBIDDEN_DIRECT_FLIPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("bullish_confirmed", "bearish_confirmed"),
        ("bearish_confirmed", "bullish_confirmed"),
        ("bullish_building", "bearish_confirmed"),
        ("bearish_building", "bullish_confirmed"),
        ("bullish_confirmed", "bearish_building"),
        ("bearish_confirmed", "bullish_building"),
        ("bullish_building", "bearish_building"),
        ("bearish_building", "bullish_building"),
        ("neutral", "bullish_confirmed"),
        ("neutral", "bearish_confirmed"),
    }
)

VARIANT_PRESETS: dict[str, dict[str, int]] = {
    "light": {
        "building_confirmation": 2,
        "confirmed_confirmation": 2,
        "neutral_confirmation": 2,
        "opposite_confirmation": 3,
        "min_building_hold": 2,
        "min_confirmed_hold": 3,
        "cooldown_bars": 1,
    },
    "medium": {
        "building_confirmation": 2,
        "confirmed_confirmation": 3,
        "neutral_confirmation": 3,
        "opposite_confirmation": 3,
        "min_building_hold": 3,
        "min_confirmed_hold": 4,
        "cooldown_bars": 2,
    },
    "strong": {
        "building_confirmation": 3,
        "confirmed_confirmation": 3,
        "neutral_confirmation": 3,
        "opposite_confirmation": 4,
        "min_building_hold": 3,
        "min_confirmed_hold": 5,
        "cooldown_bars": 2,
    },
}


@dataclass(frozen=True)
class CleanRegimeConfig:
    """Central clean-regime configuration (all variants share component rules)."""

    variant: str = "medium"
    building_confirmation: int = 2
    confirmed_confirmation: int = 3
    neutral_confirmation: int = 3
    opposite_confirmation: int = 3
    min_building_hold: int = 3
    min_confirmed_hold: int = 4
    cooldown_bars: int = 2
    # Component thresholds (mirror C3.3B; do not chart-optimize).
    di_spread_expand_min: float = 0.20
    adx_level_confirmation_min: float = 20.0
    adx_rising_min_delta_1: float = 0.25
    ema_joint_slope_min_atr: float = 0.15
    ema_flat_slope_max_atr: float = 0.10
    band_expand_min_change_atr: float = 0.10
    min_bull_components_building: int = 4
    min_bear_components_building: int = 4
    hold_net_score_floor: int = -1
    emergency_reversal_enabled: bool = False
    rule_spec_version: str = "c3_3b_clean_regime_v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def for_variant(cls, variant: str) -> CleanRegimeConfig:
        key = str(variant).strip().lower()
        if key not in VARIANT_PRESETS:
            raise ValueError(f"unknown clean-regime variant: {variant}")
        return cls(variant=key, **VARIANT_PRESETS[key])


def build_rule_spec(cfg: CleanRegimeConfig) -> dict[str, Any]:
    """Explicit rule specification — single source of truth."""
    return {
        "rule_spec_version": cfg.rule_spec_version,
        "variant": cfg.variant,
        "clean_states": list(CLEAN_STATES),
        "clean_state_codes": dict(CLEAN_STATE_CODE),
        "direction_codes": dict(DIRECTION_CODE),
        "strength_codes": dict(STRENGTH_CODE),
        "allowed_transitions": {k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()},
        "forbidden_direct_flips": [list(p) for p in sorted(FORBIDDEN_DIRECT_FLIPS)],
        "timing": {
            "building_confirmation": cfg.building_confirmation,
            "confirmed_confirmation": cfg.confirmed_confirmation,
            "neutral_confirmation": cfg.neutral_confirmation,
            "opposite_confirmation": cfg.opposite_confirmation,
            "min_building_hold": cfg.min_building_hold,
            "min_confirmed_hold": cfg.min_confirmed_hold,
            "cooldown_bars": cfg.cooldown_bars,
        },
        "component_thresholds": {
            "di_spread_expand_min": cfg.di_spread_expand_min,
            "adx_level_confirmation_min": cfg.adx_level_confirmation_min,
            "adx_rising_min_delta_1": cfg.adx_rising_min_delta_1,
            "ema_joint_slope_min_atr": cfg.ema_joint_slope_min_atr,
            "ema_flat_slope_max_atr": cfg.ema_flat_slope_max_atr,
            "band_expand_min_change_atr": cfg.band_expand_min_change_atr,
            "min_bull_components_building": cfg.min_bull_components_building,
            "min_bear_components_building": cfg.min_bear_components_building,
            "hold_net_score_floor": cfg.hold_net_score_floor,
        },
        "candidate_rules": {
            "building_bull": [
                "raw in {early_bullish, developing_bullish}",
                "OR (di_bull AND net_score > 0 AND bullish_component_count >= min_bull)",
            ],
            "confirmed_bull": [
                "raw == confirmed_bullish",
                "OR (ema_bull_order AND di_bull AND adx_confirm AND band_expand "
                "AND joint_rising AND move_relevant)",
            ],
            "hold_bull_confirmed": [
                "(ema_bull_order OR di_bull)",
                "AND bearish_component_count < bullish_component_count",
                "AND net_score >= hold_net_score_floor",
                "AND NOT multi-confirmed opposite structure this bar",
            ],
            "weaken_bull": [
                "raw == weakening_bullish",
                "OR (adx_falling AND (band_compress OR di_shrinking))",
            ],
            "lose_bull_structure": [
                "raw in {failed_bullish, early_bearish, developing_bearish, confirmed_bearish}",
                "OR (NOT di_bull AND NOT ema_bull_order)",
            ],
            "bearish_mirror": True,
        },
        "transition_policy": {
            "neutral_to_building_requires_building_confirmation": True,
            "building_to_confirmed_requires_confirmed_confirmation": True,
            "confirmed_weakens_to_building_first": True,
            "building_to_neutral_requires_neutral_or_opposite_confirmation": True,
            "direct_direction_flips_forbidden": True,
            "emergency_reversal_enabled": cfg.emergency_reversal_enabled,
            "no_future_lookahead": True,
            "no_repaint_closed_bars": True,
            "no_retro_outcomes_in_state": True,
        },
        "config": cfg.to_dict(),
    }


def config_hash(cfg: CleanRegimeConfig) -> str:
    blob = json.dumps(json_safe(cfg.to_dict()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def rule_spec_hash(spec: Mapping[str, Any] | None = None, cfg: CleanRegimeConfig | None = None) -> str:
    if spec is None:
        if cfg is None:
            cfg = CleanRegimeConfig.for_variant("medium")
        spec = build_rule_spec(cfg)
    # Hash without nested full config duplication noise: use canonical spec.
    blob = json.dumps(json_safe(dict(spec)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def python_rule_hash(cfg: CleanRegimeConfig) -> str:
    """Hash of the executable Python rule payload (same as rule_spec_hash)."""
    return rule_spec_hash(cfg=cfg)


def pine_rule_hash(cfg: CleanRegimeConfig) -> str:
    """Pine is generated from the same rule spec → identical hash."""
    return rule_spec_hash(cfg=cfg)


@dataclass
class CleanRuntimeState:
    clean_state: str = "neutral"
    state_age_bars: int = 0
    bars_since_transition: int = 0
    candidate_state: str | None = None
    candidate_count: int = 0
    opposite_candidate_count: int = 0
    cooldown_remaining: int = 0
    last_transition_reason: str = "init"

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean_state": self.clean_state,
            "state_age_bars": self.state_age_bars,
            "bars_since_transition": self.bars_since_transition,
            "candidate_state": self.candidate_state,
            "candidate_count": self.candidate_count,
            "opposite_candidate_count": self.opposite_candidate_count,
            "cooldown_remaining": self.cooldown_remaining,
            "last_transition_reason": self.last_transition_reason,
        }


def _finite(value: object, default: float = float("nan")) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def prepare_bar_features(row: Mapping[str, Any], cfg: CleanRegimeConfig) -> dict[str, Any]:
    """Map existing as-of component columns into clean-regime feature flags."""
    raw = str(row.get("research_state") or row.get("raw_research_state") or "neutral")
    di_bull = bool(row.get("di_bull"))
    di_bear = bool(row.get("di_bear"))
    ema_bull = bool(row.get("ema_bull_order"))
    ema_bear = bool(row.get("ema_bear_order"))
    adx_rising = bool(row.get("adx_rising"))
    adx_falling = bool(row.get("adx_falling"))
    adx_confirm = bool(row.get("adx_confirm"))
    band_expand = bool(row.get("band_expand"))
    band_compress = bool(row.get("band_compress"))
    di_shrinking = bool(row.get("di_shrinking"))
    joint_rising = bool(row.get("joint_rising"))
    joint_falling = bool(row.get("joint_falling"))
    move_relevant = bool(row.get("move_relevant"))
    bull_n = int(_finite(row.get("bullish_component_count"), 0))
    bear_n = int(_finite(row.get("bearish_component_count"), 0))
    net = int(_finite(row.get("net_score") if row.get("net_score") is not None else row.get("net_research_score"), 0))

    building_bull = raw in {"early_bullish", "developing_bullish"} or (
        di_bull and net > 0 and bull_n >= cfg.min_bull_components_building
    )
    building_bear = raw in {"early_bearish", "developing_bearish"} or (
        di_bear and net < 0 and bear_n >= cfg.min_bear_components_building
    )
    confirmed_bull = raw == "confirmed_bullish" or (
        ema_bull and di_bull and adx_confirm and band_expand and joint_rising and move_relevant
    )
    confirmed_bear = raw == "confirmed_bearish" or (
        ema_bear and di_bear and adx_confirm and band_expand and joint_falling and move_relevant
    )
    hold_bull = (ema_bull or di_bull) and bear_n < bull_n and net >= cfg.hold_net_score_floor
    hold_bear = (ema_bear or di_bear) and bull_n < bear_n and net <= -cfg.hold_net_score_floor
    weaken_bull = raw == "weakening_bullish" or (adx_falling and (band_compress or di_shrinking) and ema_bull)
    weaken_bear = raw == "weakening_bearish" or (adx_falling and (band_compress or di_shrinking) and ema_bear)
    lose_bull = raw in {
        "failed_bullish",
        "early_bearish",
        "developing_bearish",
        "confirmed_bearish",
    } or (not di_bull and not ema_bull)
    lose_bear = raw in {
        "failed_bearish",
        "early_bullish",
        "developing_bullish",
        "confirmed_bullish",
    } or (not di_bear and not ema_bear)

    return {
        "raw_research_state": raw,
        "building_bull": bool(building_bull),
        "building_bear": bool(building_bear),
        "confirmed_bull": bool(confirmed_bull),
        "confirmed_bear": bool(confirmed_bear),
        "hold_bull_confirmed": bool(hold_bull),
        "hold_bear_confirmed": bool(hold_bear),
        "weaken_bull": bool(weaken_bull),
        "weaken_bear": bool(weaken_bear),
        "lose_bull": bool(lose_bull),
        "lose_bear": bool(lose_bear),
        "di_bull": di_bull,
        "di_bear": di_bear,
        "di_direction": 1 if di_bull else (-1 if di_bear else 0),
        "di_diff": _finite(row.get("di_spread"), 0.0),
        "adx": _finite(row.get("adx_14"), float("nan")),
        "adx_rising": adx_rising,
        "adx_slope_3": _finite(row.get("adx_slope_lin_3"), float("nan")),
        "adx_slope_5": _finite(row.get("adx_slope_lin_5"), float("nan")),
        "ema_order_direction": 1 if ema_bull else (-1 if ema_bear else 0),
        "ema_joint_slope_direction": 1 if joint_rising else (-1 if joint_falling else 0),
        "band_expanding": band_expand,
        "atr_relevant": move_relevant,
        "bullish_component_count": bull_n,
        "bearish_component_count": bear_n,
        "net_research_score": net,
    }


def _can_transition(prev: str, nxt: str) -> bool:
    if prev == nxt:
        return True
    if (prev, nxt) in FORBIDDEN_DIRECT_FLIPS:
        return False
    return nxt in ALLOWED_TRANSITIONS.get(prev, frozenset())


def _bump_candidate(runtime: CleanRuntimeState, candidate: str | None) -> CleanRuntimeState:
    if candidate is None:
        runtime.candidate_state = None
        runtime.candidate_count = 0
        return runtime
    if runtime.candidate_state == candidate:
        runtime.candidate_count += 1
    else:
        runtime.candidate_state = candidate
        runtime.candidate_count = 1
    return runtime


def _bump_opposite(runtime: CleanRuntimeState, active: bool) -> CleanRuntimeState:
    if active:
        runtime.opposite_candidate_count += 1
    else:
        runtime.opposite_candidate_count = 0
    return runtime


def step_clean_regime_state(
    previous_state: str,
    prepared_features: Mapping[str, Any],
    runtime_state: CleanRuntimeState | None,
    config: CleanRegimeConfig,
) -> tuple[str, CleanRuntimeState, dict[str, Any]]:
    """Deterministic causal step for one closed candle.

    Uses only current features, previous clean state, and causal counters.
    Never mutates earlier bars. No future look-ahead. No RETRO outcomes.
    """
    rt = runtime_state or CleanRuntimeState(clean_state=previous_state)
    # Ensure runtime starts aligned with previous_state argument.
    if rt.clean_state != previous_state:
        rt.clean_state = previous_state
        rt.state_age_bars = 0
        rt.bars_since_transition = 0

    prev = previous_state
    feat = dict(prepared_features)
    reason = "hold"
    suppressed_flip = False
    hold_active = False
    exit_active = False
    desired: str | None = None

    # Age / cooldown advance for this closed bar.
    age = rt.state_age_bars + 1
    since = rt.bars_since_transition + 1
    cooldown = max(0, rt.cooldown_remaining - 1)

    building_bull = bool(feat["building_bull"])
    building_bear = bool(feat["building_bear"])
    confirmed_bull = bool(feat["confirmed_bull"])
    confirmed_bear = bool(feat["confirmed_bear"])
    hold_bull = bool(feat["hold_bull_confirmed"])
    hold_bear = bool(feat["hold_bear_confirmed"])
    weaken_bull = bool(feat["weaken_bull"])
    weaken_bear = bool(feat["weaken_bear"])
    lose_bull = bool(feat["lose_bull"])
    lose_bear = bool(feat["lose_bear"])

    if prev == "neutral":
        if building_bull and not building_bear:
            desired = "bullish_building"
            _bump_candidate(rt, "bullish_building")
            _bump_opposite(rt, False)
        elif building_bear and not building_bull:
            desired = "bearish_building"
            _bump_candidate(rt, "bearish_building")
            _bump_opposite(rt, False)
        else:
            _bump_candidate(rt, None)
            _bump_opposite(rt, False)
            desired = None

    elif prev == "bullish_building":
        hold_active = building_bull or confirmed_bull or hold_bull
        if confirmed_bull:
            desired = "bullish_confirmed"
            _bump_candidate(rt, "bullish_confirmed")
            _bump_opposite(rt, False)
        elif lose_bull or building_bear:
            exit_active = True
            desired = "neutral"
            _bump_candidate(rt, "neutral")
            _bump_opposite(rt, True)
        elif weaken_bull and not confirmed_bull:
            # Stay in building while weak; accumulate neutral candidate slowly.
            desired = None
            _bump_candidate(rt, None)
            _bump_opposite(rt, False)
        else:
            desired = None
            if hold_active:
                _bump_candidate(rt, None)
            _bump_opposite(rt, False)

    elif prev == "bullish_confirmed":
        hold_active = hold_bull or confirmed_bull
        # Hold hysteresis: a single weak candle does not exit while hold remains.
        if hold_active and not lose_bull:
            desired = None
            _bump_candidate(rt, None)
            _bump_opposite(rt, False)
        elif weaken_bull or (not hold_active and not confirmed_bull):
            exit_active = True
            desired = "bullish_building"
            _bump_candidate(rt, "bullish_building")
            _bump_opposite(rt, False)
        elif lose_bull or building_bear or confirmed_bear:
            exit_active = True
            desired = "bullish_building"
            _bump_candidate(rt, "bullish_building")
            _bump_opposite(rt, True)
        else:
            desired = None

    elif prev == "bearish_building":
        hold_active = building_bear or confirmed_bear or hold_bear
        if confirmed_bear:
            desired = "bearish_confirmed"
            _bump_candidate(rt, "bearish_confirmed")
            _bump_opposite(rt, False)
        elif lose_bear or building_bull:
            exit_active = True
            desired = "neutral"
            _bump_candidate(rt, "neutral")
            _bump_opposite(rt, True)
        else:
            desired = None
            _bump_candidate(rt, None)
            _bump_opposite(rt, False)

    elif prev == "bearish_confirmed":
        hold_active = hold_bear or confirmed_bear
        if hold_active and not lose_bear:
            desired = None
            _bump_candidate(rt, None)
            _bump_opposite(rt, False)
        elif weaken_bear or (not hold_active and not confirmed_bear):
            exit_active = True
            desired = "bearish_building"
            _bump_candidate(rt, "bearish_building")
            _bump_opposite(rt, False)
        elif lose_bear or building_bull or confirmed_bull:
            exit_active = True
            desired = "bearish_building"
            _bump_candidate(rt, "bearish_building")
            _bump_opposite(rt, True)
        else:
            desired = None

    else:
        desired = None
        prev = "neutral"

    # Confirmation thresholds by target.
    needed = 0
    if desired == "bullish_building" or desired == "bearish_building":
        if prev == "neutral":
            needed = config.building_confirmation
        elif prev.endswith("_confirmed"):
            needed = config.opposite_confirmation if exit_active else config.building_confirmation
        else:
            needed = config.building_confirmation
    elif desired in {"bullish_confirmed", "bearish_confirmed"}:
        needed = config.confirmed_confirmation
    elif desired == "neutral":
        needed = max(config.neutral_confirmation, config.opposite_confirmation if exit_active else config.neutral_confirmation)

    # Min hold gates.
    min_hold_ok = True
    if prev.endswith("_confirmed"):
        min_hold_ok = age >= config.min_confirmed_hold
    elif prev.endswith("_building"):
        min_hold_ok = age >= config.min_building_hold

    new_state = prev
    if desired is None or desired == prev:
        reason = "hold"
        new_state = prev
    elif cooldown > 0:
        reason = "suppressed_cooldown"
        suppressed_flip = True
        new_state = prev
        # Keep candidate counting so confirmation can complete after cooldown.
    elif not min_hold_ok and desired != prev:
        reason = "suppressed_min_hold"
        suppressed_flip = True
        new_state = prev
    elif not _can_transition(prev, desired):
        reason = "suppressed_forbidden_transition"
        suppressed_flip = True
        new_state = prev
        rt.candidate_count = 0
        rt.candidate_state = None
    elif rt.candidate_state == desired and rt.candidate_count >= needed:
        new_state = desired
        reason = f"transition_to_{desired}"
    else:
        reason = "awaiting_confirmation"
        new_state = prev

    changed = new_state != prev
    if changed:
        age = 1
        since = 0
        cooldown = config.cooldown_bars
        rt.candidate_state = None
        rt.candidate_count = 0
        rt.opposite_candidate_count = 0
        rt.last_transition_reason = reason
    else:
        rt.last_transition_reason = reason

    rt.clean_state = new_state
    rt.state_age_bars = age
    rt.bars_since_transition = since
    rt.cooldown_remaining = cooldown

    diagnostics = {
        "previous_clean_regime_state": prev,
        "clean_regime_state": new_state,
        "clean_regime_changed": changed,
        "clean_regime_direction": DIRECTION_CODE[new_state],
        "clean_regime_strength": STRENGTH_CODE[new_state],
        "clean_regime_age_bars": age,
        "bars_since_last_transition": since,
        "candidate_state": rt.candidate_state,
        "candidate_state_count": rt.candidate_count,
        "opposite_candidate_count": rt.opposite_candidate_count,
        "hold_condition_active": hold_active,
        "exit_condition_active": exit_active,
        "transition_reason": reason,
        "suppressed_flip": suppressed_flip,
        "desired_state": desired,
        "cooldown_remaining": cooldown,
        "clean_state_code": CLEAN_STATE_CODE[new_state],
    }
    return new_state, rt, diagnostics


def apply_clean_regime(
    feature_frame: pd.DataFrame,
    cfg: CleanRegimeConfig,
) -> pd.DataFrame:
    """Apply causal clean-regime machine bar-by-bar (no repaint)."""
    if feature_frame.empty:
        return feature_frame.copy()

    cfg_h = config_hash(cfg)
    rule_h = rule_spec_hash(cfg=cfg)
    rows: list[dict[str, Any]] = []
    runtime = CleanRuntimeState()
    prev = "neutral"

    for i in range(len(feature_frame)):
        src = feature_frame.iloc[i].to_dict()
        feat = prepare_bar_features(src, cfg)
        new_state, runtime, diag = step_clean_regime_state(prev, feat, runtime, cfg)
        # Non-repaint: append only; never rewrite prior rows.
        row = {
            "bar_index": int(src.get("bar_index", i)),
            "timestamp": src.get("timestamp") or src.get("decision_time"),
            "decision_time": src.get("decision_time") or src.get("timestamp"),
            "symbol": src.get("symbol"),
            "timeframe": src.get("timeframe"),
            "raw_research_state": feat["raw_research_state"],
            **{k: feat[k] for k in (
                "di_direction",
                "di_diff",
                "adx",
                "adx_rising",
                "adx_slope_3",
                "adx_slope_5",
                "ema_order_direction",
                "ema_joint_slope_direction",
                "band_expanding",
                "atr_relevant",
                "bullish_component_count",
                "bearish_component_count",
                "net_research_score",
            )},
            **diag,
            "smoothing_variant": cfg.variant,
            "config_hash": cfg_h,
            "rule_spec_hash": rule_h,
        }
        rows.append(row)
        prev = new_state

    return pd.DataFrame(rows)


def prepare_feature_frame_from_ohlcv_features(
    frame: pd.DataFrame,
    *,
    c33b_cfg: Any | None = None,
) -> pd.DataFrame:
    """Reuse C3.3B as-of components + raw research state."""
    from research.regime_scanner.indicator_pattern_discovery_c3_3b import (
        PatternDiscoveryC33BConfig,
        enrich_discovery_frame,
    )

    base_cfg = c33b_cfg or PatternDiscoveryC33BConfig()
    enriched = enrich_discovery_frame(frame.copy(), base_cfg)
    states = compute_trend_detector_states(enriched, base_cfg)
    return states


def bot_interface_frame(clean_df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "timestamp",
        "decision_time",
        "symbol",
        "timeframe",
        "raw_research_state",
        "clean_regime_state",
        "previous_clean_regime_state",
        "clean_regime_changed",
        "clean_regime_direction",
        "clean_regime_strength",
        "clean_regime_age_bars",
        "bars_since_last_transition",
        "candidate_state",
        "candidate_state_count",
        "opposite_candidate_count",
        "hold_condition_active",
        "exit_condition_active",
        "transition_reason",
        "smoothing_variant",
        "bullish_component_count",
        "bearish_component_count",
        "net_research_score",
        "di_direction",
        "di_diff",
        "adx",
        "adx_rising",
        "adx_slope_3",
        "adx_slope_5",
        "ema_order_direction",
        "ema_joint_slope_direction",
        "band_expanding",
        "atr_relevant",
        "config_hash",
        "rule_spec_hash",
        "clean_state_code",
    ]
    present = [c for c in cols if c in clean_df.columns]
    return clean_df[present].copy()

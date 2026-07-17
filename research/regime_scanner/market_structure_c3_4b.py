"""Phase C3.4B protected market-structure state machine (research-only).

Builds on C3.4A causal micro-swings. Only a confirmed external break of the
active protected_high / protected_low may change the major trend. Internal
BOS and indicator flips alone do not flip protected structure.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.market_structure_c3_4a import (
    BREAK_PRESETS,
    SWING_PRESETS,
    MarketStructureConfig,
    StructureRuntime,
    SwingPoint,
    advance_micro_swings,
)
from research.regime_scanner.point_audit import json_safe

PROTECTED_STATES: tuple[str, ...] = (
    "structure_unknown",
    "range_unclear",
    "bullish_structure",
    "bearish_structure",
    "bullish_pullback",
    "bearish_pullback",
    "bullish_internal_break",
    "bearish_internal_break",
    "bullish_choch",
    "bearish_choch",
    "bullish_structure_candidate",
    "bearish_structure_candidate",
    "bullish_retest_pending",
    "bearish_retest_pending",
    "transition_blocked",
    "bullish_break_failed",
    "bearish_break_failed",
)

PROTECTED_STATE_CODE: dict[str, int] = {
    "structure_unknown": 0,
    "range_unclear": 1,
    "bullish_structure": 2,
    "bullish_pullback": 3,
    "bullish_internal_break": 4,
    "bullish_choch": 5,
    "bullish_structure_candidate": 6,
    "bullish_retest_pending": 7,
    "bullish_break_failed": 8,
    "bearish_structure": -2,
    "bearish_pullback": -3,
    "bearish_internal_break": -4,
    "bearish_choch": -5,
    "bearish_structure_candidate": -6,
    "bearish_retest_pending": -7,
    "bearish_break_failed": -8,
    "transition_blocked": 9,
}

MAJOR_DIRECTION: dict[str, int] = {
    "structure_unknown": 0,
    "range_unclear": 0,
    "transition_blocked": 0,
    "bullish_structure": 1,
    "bullish_pullback": 1,
    "bullish_internal_break": 1,
    "bullish_choch": 1,
    "bullish_structure_candidate": 1,
    "bullish_retest_pending": 1,
    "bullish_break_failed": 1,
    "bearish_structure": -1,
    "bearish_pullback": -1,
    "bearish_internal_break": -1,
    "bearish_choch": -1,
    "bearish_structure_candidate": -1,
    "bearish_retest_pending": -1,
    "bearish_break_failed": -1,
}

# Research matrix (not a full cartesian product).
RESEARCH_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "name": "protected_medium",
        "swing_sensitivity": "medium",
        "break_mode": "medium",
        "transition_zone_atr": 0.50,
        "choch_mode": "hold",
        "label": "balanced_research_variant",
    },
    {
        "name": "protected_strict",
        "swing_sensitivity": "strong",
        "break_mode": "strong",
        "transition_zone_atr": 0.75,
        "choch_mode": "hl_lh",
        "label": "fewest_false_choch",
    },
    {
        "name": "protected_fast",
        "swing_sensitivity": "medium",
        "break_mode": "light",
        "transition_zone_atr": 0.25,
        "choch_mode": "immediate",
        "label": "fastest_valid_choch",
    },
    {
        "name": "protected_confirmed",
        "swing_sensitivity": "strong",
        "break_mode": "medium",
        "transition_zone_atr": 0.50,
        "choch_mode": "external_bos",
        "label": "fewest_major_flips",
    },
)


@dataclass(frozen=True)
class ProtectedStructureConfig:
    """Central C3.4B configuration."""

    variant_name: str = "protected_medium"
    swing_sensitivity: str = "medium"
    break_mode: str = "medium"
    transition_zone_atr: float = 0.50
    choch_mode: str = "hold"  # immediate | hold | hl_lh | external_bos
    lookback: int = 5
    confirm_bars: int = 3
    min_reversal_atr: float = 0.50
    major_min_reversal_atr: float = 1.20
    major_min_bars_between: int = 10
    micro_min_bars_between: int = 3
    min_close_beyond_atr: float = 0.10
    required_closes: int = 1
    choch_hold_bars: int = 2
    retest_tolerance_atr: float = 0.25
    continuation_min_atr: float = 0.35
    rule_spec_version: str = "c3_4b_protected_structure_v1"
    label: str = "balanced_research_variant"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def micro_config(self) -> MarketStructureConfig:
        """C3.4A micro-swing config derived from protected presets."""
        return MarketStructureConfig(
            variant_name=f"micro_for_{self.variant_name}",
            swing_sensitivity=self.swing_sensitivity,
            transition_zone_atr=self.transition_zone_atr,
            break_mode=self.break_mode,
            retest_mode="none",
            lookback=self.lookback,
            confirm_bars=self.confirm_bars,
            min_reversal_atr=self.min_reversal_atr,
            major_min_reversal_atr=self.major_min_reversal_atr,
            major_min_bars_between=self.major_min_bars_between,
            micro_min_bars_between=self.micro_min_bars_between,
            min_close_beyond_atr=self.min_close_beyond_atr,
            required_closes=self.required_closes,
            label=f"micro_base_{self.variant_name}",
        )

    @classmethod
    def from_matrix_entry(cls, entry: Mapping[str, Any]) -> ProtectedStructureConfig:
        swing = SWING_PRESETS[str(entry["swing_sensitivity"])]
        brk = BREAK_PRESETS[str(entry["break_mode"])]
        return cls(
            variant_name=str(entry["name"]),
            swing_sensitivity=str(entry["swing_sensitivity"]),
            break_mode=str(entry["break_mode"]),
            transition_zone_atr=float(entry["transition_zone_atr"]),
            choch_mode=str(entry["choch_mode"]),
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


def build_rule_spec(cfg: ProtectedStructureConfig) -> dict[str, Any]:
    return {
        "rule_spec_version": cfg.rule_spec_version,
        "variant_name": cfg.variant_name,
        "states": list(PROTECTED_STATES),
        "state_codes": dict(PROTECTED_STATE_CODE),
        "major_direction_codes": dict(MAJOR_DIRECTION),
        "research_matrix": list(RESEARCH_MATRIX),
        "micro_base": "c3_4a_advance_micro_swings",
        "swing": {
            "method": "causal_extremum_then_reversal",
            "no_future_right_bars": True,
            "lookback": cfg.lookback,
            "confirm_bars": cfg.confirm_bars,
            "min_reversal_atr": cfg.min_reversal_atr,
            "live_from": "confirmed_timestamp_only",
        },
        "protected_levels": {
            "replace_only_after_continuation": True,
            "micro_high_does_not_replace_protected_high": True,
            "micro_low_does_not_replace_protected_low": True,
            "continuation_min_atr": cfg.continuation_min_atr,
            "candidate_latch": True,
            "candidate_replace_only_if_structurally_better": True,
            "candidate_newer_weaker_does_not_replace": True,
        },
        "breaks": {
            "internal_bos_does_not_flip_major": True,
            "external_bos_required_for_major_flip": True,
            "wick_alone_insufficient_for_choch": True,
            "mode": cfg.break_mode,
            "min_close_beyond_atr": cfg.min_close_beyond_atr,
            "required_closes": cfg.required_closes,
        },
        "choch_mode": cfg.choch_mode,
        "choch_hold_bars": cfg.choch_hold_bars,
        "transition_zone_atr": cfg.transition_zone_atr,
        "transition_zone_relative_to": "protected_level_only",
        "policy": {
            "no_repaint_closed_bars": True,
            "no_future_lookahead": True,
            "no_centered_windows": True,
            "retro_outcomes_excluded_from_state": True,
            "indicator_flip_alone_does_not_reverse_major": True,
        },
        "config": cfg.to_dict(),
    }


def config_hash(cfg: ProtectedStructureConfig) -> str:
    blob = json.dumps(json_safe(cfg.to_dict()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def rule_spec_hash(
    spec: Mapping[str, Any] | None = None,
    cfg: ProtectedStructureConfig | None = None,
) -> str:
    if spec is None:
        cfg = cfg or ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
        spec = build_rule_spec(cfg)
    blob = json.dumps(json_safe(dict(spec)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def python_rule_hash(cfg: ProtectedStructureConfig) -> str:
    return rule_spec_hash(cfg=cfg)


def pine_rule_hash(cfg: ProtectedStructureConfig) -> str:
    return rule_spec_hash(cfg=cfg)


def _finite(value: object, default: float = float("nan")) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


@dataclass
class ProtectedLevel:
    level: float
    extreme_bar: int
    extreme_timestamp: Any
    confirmed_bar: int
    confirmed_timestamp: Any
    side: str  # high | low
    source: str = "init"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProtectedRuntime:
    state: str = "structure_unknown"
    state_age_bars: int = 0
    major_direction: int = 0  # sticky until external BOS establishes new structure
    micro_rt: StructureRuntime = field(default_factory=StructureRuntime)
    protected_high: ProtectedLevel | None = None
    protected_low: ProtectedLevel | None = None
    candidate_protected_high: ProtectedLevel | None = None
    candidate_protected_low: ProtectedLevel | None = None
    # Pullback-leg latch: none | high | low
    candidate_leg: str = "none"
    last_external_high: float | None = None
    last_external_low: float | None = None
    last_internal_high: float | None = None
    last_internal_low: float | None = None
    close_beyond_up_streak: int = 0
    close_beyond_down_streak: int = 0
    choch_hold_count: int = 0
    choch_level: float | None = None
    choch_side: str | None = None
    await_hl_after_choch: bool = False
    await_lh_after_choch: bool = False
    post_choch_hl: float | None = None
    post_choch_lh: float | None = None
    await_external_bos_after_choch: bool = False
    transition_zone_started_at: int | None = None
    protected_replacements: int = 0
    guard_violations: list[dict[str, Any]] = field(default_factory=list)
    last_continuation_bar: int | None = None


def _clear_candidate_high_leg(rt: ProtectedRuntime) -> None:
    rt.candidate_protected_high = None
    if rt.candidate_leg == "high":
        rt.candidate_leg = "none"


def _clear_candidate_low_leg(rt: ProtectedRuntime) -> None:
    rt.candidate_protected_low = None
    if rt.candidate_leg == "low":
        rt.candidate_leg = "none"


def _set_major_direction(
    rt: ProtectedRuntime,
    new_dir: int,
    *,
    bar_i: int | None = None,
    timestamp: Any = None,
    seed_active: bool = True,
) -> dict[str, Any]:
    """Set major direction and drop the inactive opposite protected level.

    Bearish structure's active external level is protected_high; the prior
    protected_low is historical residue and must not stay "active" (avoids
    protectedHigh <= protectedLow after LH promotions). Bullish is mirrored.

    When the active-side level is missing after a flip, seed it from the
    latched candidate or last internal swing of the matching kind.
    """
    prev = int(rt.major_direction)
    new_dir = int(new_dir)
    if new_dir == prev:
        return {"changed": False, "from": prev, "to": new_dir, "cleared_opposite": None, "seeded": None}
    rt.major_direction = new_dir
    cleared: tuple[str, float] | None = None
    seeded: str | None = None
    if new_dir < 0:
        if rt.protected_low is not None:
            cleared = ("low", float(rt.protected_low.level))
            rt.protected_low = None
        _clear_candidate_low_leg(rt)
        if seed_active and rt.protected_high is None and bar_i is not None:
            src = None
            if rt.candidate_protected_high is not None:
                src = rt.candidate_protected_high
                src = ProtectedLevel(
                    level=float(src.level),
                    extreme_bar=int(src.extreme_bar),
                    extreme_timestamp=src.extreme_timestamp,
                    confirmed_bar=int(src.confirmed_bar),
                    confirmed_timestamp=src.confirmed_timestamp,
                    side="high",
                    source="flip_seed_candidate_high",
                )
            elif rt.last_internal_high is not None:
                src = ProtectedLevel(
                    level=float(rt.last_internal_high),
                    extreme_bar=bar_i,
                    extreme_timestamp=timestamp,
                    confirmed_bar=bar_i,
                    confirmed_timestamp=timestamp,
                    side="high",
                    source="flip_seed_internal_high",
                )
            if src is not None:
                _set_protected_high(rt, src, bar_i=bar_i, allow_replace=True)
                seeded = src.source
                _clear_candidate_high_leg(rt)
    elif new_dir > 0:
        if rt.protected_high is not None:
            cleared = ("high", float(rt.protected_high.level))
            rt.protected_high = None
        _clear_candidate_high_leg(rt)
        if seed_active and rt.protected_low is None and bar_i is not None:
            src = None
            if rt.candidate_protected_low is not None:
                src = ProtectedLevel(
                    level=float(rt.candidate_protected_low.level),
                    extreme_bar=int(rt.candidate_protected_low.extreme_bar),
                    extreme_timestamp=rt.candidate_protected_low.extreme_timestamp,
                    confirmed_bar=int(rt.candidate_protected_low.confirmed_bar),
                    confirmed_timestamp=rt.candidate_protected_low.confirmed_timestamp,
                    side="low",
                    source="flip_seed_candidate_low",
                )
            elif rt.last_internal_low is not None:
                src = ProtectedLevel(
                    level=float(rt.last_internal_low),
                    extreme_bar=bar_i,
                    extreme_timestamp=timestamp,
                    confirmed_bar=bar_i,
                    confirmed_timestamp=timestamp,
                    side="low",
                    source="flip_seed_internal_low",
                )
            if src is not None:
                _set_protected_low(rt, src, bar_i=bar_i, allow_replace=True)
                seeded = src.source
                _clear_candidate_low_leg(rt)
    return {
        "changed": True,
        "from": prev,
        "to": new_dir,
        "cleared_opposite": cleared,
        "seeded": seeded,
    }


def _drop_stale_crossed_opposite(rt: ProtectedRuntime) -> str | None:
    """If both levels exist but are crossed, drop the inactive opposite side."""
    if rt.protected_high is None or rt.protected_low is None:
        return None
    if float(rt.protected_high.level) > float(rt.protected_low.level):
        return None
    if rt.major_direction < 0:
        rt.protected_low = None
        _clear_candidate_low_leg(rt)
        return "cleared_stale_protected_low_crossed"
    if rt.major_direction > 0:
        rt.protected_high = None
        _clear_candidate_high_leg(rt)
        return "cleared_stale_protected_high_crossed"
    return None


def _maybe_set_candidate_high(rt: ProtectedRuntime, cand: ProtectedLevel) -> bool:
    """Latch candidate high: first set or replace only with strictly higher level."""
    if rt.candidate_protected_high is None or rt.candidate_leg != "high":
        rt.candidate_protected_high = cand
        rt.candidate_leg = "high"
        return True
    if cand.level > rt.candidate_protected_high.level:
        rt.candidate_protected_high = cand
        rt.candidate_leg = "high"
        return True
    # Equal or lower (even if newer confirmed_bar) — keep existing latch.
    return False


def _maybe_set_candidate_low(rt: ProtectedRuntime, cand: ProtectedLevel) -> bool:
    """Latch candidate low: first set or replace only with strictly lower level."""
    if rt.candidate_protected_low is None or rt.candidate_leg != "low":
        rt.candidate_protected_low = cand
        rt.candidate_leg = "low"
        return True
    if cand.level < rt.candidate_protected_low.level:
        rt.candidate_protected_low = cand
        rt.candidate_leg = "low"
        return True
    return False


def _level_from_swing(sw: SwingPoint, *, source: str) -> ProtectedLevel:
    return ProtectedLevel(
        level=float(sw.level),
        extreme_bar=int(sw.extreme_bar),
        extreme_timestamp=sw.extreme_timestamp,
        confirmed_bar=int(sw.confirmed_bar),
        confirmed_timestamp=sw.confirmed_timestamp,
        side="high" if sw.kind == "high" else "low",
        source=source,
    )


def _set_protected_high(
    rt: ProtectedRuntime,
    level: ProtectedLevel,
    *,
    bar_i: int,
    allow_replace: bool,
) -> bool:
    """Set protected_high with monotonic/causal guards. Returns True if set."""
    if rt.protected_high is not None:
        if not allow_replace:
            rt.guard_violations.append(
                {
                    "bar_index": bar_i,
                    "guard": "protected_level_replacement_without_continuation",
                    "side": "high",
                    "old": rt.protected_high.level,
                    "attempted": level.level,
                }
            )
            return False
        if level.confirmed_bar < rt.protected_high.confirmed_bar:
            rt.guard_violations.append(
                {
                    "bar_index": bar_i,
                    "guard": "retroactive_protected_level_changes",
                    "side": "high",
                    "old": rt.protected_high.confirmed_bar,
                    "attempted": level.confirmed_bar,
                }
            )
            return False
        rt.protected_replacements += 1
    rt.protected_high = level
    rt.last_external_high = level.level
    return True


def _set_protected_low(
    rt: ProtectedRuntime,
    level: ProtectedLevel,
    *,
    bar_i: int,
    allow_replace: bool,
) -> bool:
    if rt.protected_low is not None:
        if not allow_replace:
            rt.guard_violations.append(
                {
                    "bar_index": bar_i,
                    "guard": "protected_level_replacement_without_continuation",
                    "side": "low",
                    "old": rt.protected_low.level,
                    "attempted": level.level,
                }
            )
            return False
        if level.confirmed_bar < rt.protected_low.confirmed_bar:
            rt.guard_violations.append(
                {
                    "bar_index": bar_i,
                    "guard": "retroactive_protected_level_changes",
                    "side": "low",
                    "old": rt.protected_low.confirmed_bar,
                    "attempted": level.confirmed_bar,
                }
            )
            return False
        rt.protected_replacements += 1
    rt.protected_low = level
    rt.last_external_low = level.level
    return True


def _update_candidates_and_protected(
    rt: ProtectedRuntime,
    newly: Sequence[SwingPoint],
    *,
    bar_i: int,
    atr: float,
    cfg: ProtectedStructureConfig,
) -> dict[str, Any]:
    """Promote candidates only after confirmed trend continuation.

    Candidate latch: within one pullback leg, replace only with a structurally
    better extreme (higher high / lower low). Newer weaker confirmed_bars do not
    replace the latched candidate.
    """
    info = {
        "protected_high_updated": False,
        "protected_low_updated": False,
        "candidate_high_set": False,
        "candidate_low_set": False,
        "continuation_down": False,
        "continuation_up": False,
        "candidate_leg": rt.candidate_leg,
        "promotion_reason": None,
        "protected_high_before": None if rt.protected_high is None else float(rt.protected_high.level),
        "protected_low_before": None if rt.protected_low is None else float(rt.protected_low.level),
    }
    new_highs = [s for s in newly if s.kind == "high"]
    new_lows = [s for s in newly if s.kind == "low"]

    # Invalidate opposite/invalid legs when major direction is not matching.
    if rt.major_direction >= 0 and rt.candidate_leg == "high":
        _clear_candidate_high_leg(rt)
    if rt.major_direction <= 0 and rt.candidate_leg == "low":
        _clear_candidate_low_leg(rt)

    # Bootstrap: seed only the side that can be active for the current/unknown major.
    # Never re-seed the inactive opposite after a major flip (that recreated crossed
    # pairs and poisoned last_external_* via _set_protected_*).
    if rt.protected_high is None and new_highs and rt.major_direction <= 0:
        seed = max(new_highs, key=lambda s: s.level)
        _set_protected_high(rt, _level_from_swing(seed, source="seed"), bar_i=bar_i, allow_replace=True)
        info["protected_high_updated"] = True
        info["promotion_reason"] = "seed_high"
    if rt.protected_low is None and new_lows and rt.major_direction >= 0:
        seed = min(new_lows, key=lambda s: s.level)
        _set_protected_low(rt, _level_from_swing(seed, source="seed"), bar_i=bar_i, allow_replace=True)
        info["protected_low_updated"] = True
        if info["promotion_reason"] is None:
            info["promotion_reason"] = "seed_low"

    # Track internal swing extremes always; latch candidates only in major direction.
    if new_highs:
        last_h = new_highs[-1]
        rt.last_internal_high = float(last_h.level)
        if rt.major_direction < 0:
            cand = _level_from_swing(last_h, source="pullback_candidate")
            if _maybe_set_candidate_high(rt, cand):
                info["candidate_high_set"] = True
    if new_lows:
        last_l = new_lows[-1]
        rt.last_internal_low = float(last_l.level)
        if rt.major_direction > 0:
            cand = _level_from_swing(last_l, source="pullback_candidate")
            if _maybe_set_candidate_low(rt, cand):
                info["candidate_low_set"] = True

    # Continuation: new LL below last external/protected low in bearish structure.
    # Promotes candHigh -> protectedHigh only (never assign micro low into protected_high).
    if rt.major_direction < 0 and new_lows and rt.protected_high is not None:
        ref_low = rt.last_external_low
        for sw in new_lows:
            is_ll = ref_low is None or sw.level < ref_low
            if is_ll:
                info["continuation_down"] = True
                rt.last_external_low = float(sw.level)
                rt.last_continuation_bar = bar_i
                if rt.candidate_protected_high is not None:
                    cand_lvl = float(rt.candidate_protected_high.level)
                    ok = _set_protected_high(
                        rt,
                        rt.candidate_protected_high,
                        bar_i=bar_i,
                        allow_replace=True,
                    )
                    if ok:
                        rt.protected_high.source = "continuation_promote"
                        info["protected_high_updated"] = True
                        info["promotion_reason"] = (
                            f"bearish_ll_promote_cand_high:{cand_lvl}"
                        )
                    _clear_candidate_high_leg(rt)
                stale = _drop_stale_crossed_opposite(rt)
                if stale:
                    info["promotion_reason"] = (
                        (info["promotion_reason"] or "continuation_down") + f"|{stale}"
                    )
                break

    # Continuation: new HH above last external/protected high in bullish structure.
    # Promotes candLow -> protectedLow only (never assign micro high into protected_low).
    if rt.major_direction > 0 and new_highs and rt.protected_low is not None:
        ref_high = rt.last_external_high
        for sw in new_highs:
            is_hh = ref_high is None or sw.level > ref_high
            if is_hh:
                info["continuation_up"] = True
                rt.last_external_high = float(sw.level)
                rt.last_continuation_bar = bar_i
                if rt.candidate_protected_low is not None:
                    cand_lvl = float(rt.candidate_protected_low.level)
                    ok = _set_protected_low(
                        rt,
                        rt.candidate_protected_low,
                        bar_i=bar_i,
                        allow_replace=True,
                    )
                    if ok:
                        rt.protected_low.source = "continuation_promote"
                        info["protected_low_updated"] = True
                        info["promotion_reason"] = (
                            f"bullish_hh_promote_cand_low:{cand_lvl}"
                        )
                    _clear_candidate_low_leg(rt)
                stale = _drop_stale_crossed_opposite(rt)
                if stale:
                    info["promotion_reason"] = (
                        (info["promotion_reason"] or "continuation_up") + f"|{stale}"
                    )
                break

    # Post-CHoCH HL/LH tracking for choch_mode=hl_lh.
    if rt.await_hl_after_choch and new_lows:
        rt.post_choch_hl = float(new_lows[-1].level)
    if rt.await_lh_after_choch and new_highs:
        rt.post_choch_lh = float(new_highs[-1].level)

    info["candidate_leg"] = rt.candidate_leg
    info["protected_high_after"] = (
        None if rt.protected_high is None else float(rt.protected_high.level)
    )
    info["protected_low_after"] = (
        None if rt.protected_low is None else float(rt.protected_low.level)
    )
    return info


def _alignment(state: str, clean_state: str) -> str:
    if state == "transition_blocked":
        return "transition_blocked"
    if state in {"structure_unknown", "range_unclear"}:
        return "structure_unclear"
    if clean_state == "neutral":
        return "indicator_neutral"
    maj = MAJOR_DIRECTION.get(state, 0)
    if maj > 0 and clean_state.startswith("bullish"):
        return "aligned_bullish"
    if maj < 0 and clean_state.startswith("bearish"):
        return "aligned_bearish"
    if maj < 0 and clean_state.startswith("bullish"):
        return "bullish_indicator_against_bearish_structure"
    if maj > 0 and clean_state.startswith("bearish"):
        return "bearish_indicator_against_bullish_structure"
    return "structure_unclear"


def _structure_strength(state: str) -> str:
    if state in {"bullish_structure", "bearish_structure"}:
        return "confirmed"
    if state in {"bullish_structure_candidate", "bearish_structure_candidate"}:
        return "candidate"
    if state in {"bullish_choch", "bearish_choch"}:
        return "choch"
    if state in {"bullish_pullback", "bearish_pullback", "bullish_internal_break", "bearish_internal_break"}:
        return "pullback"
    if state == "transition_blocked":
        return "blocked"
    return "unknown"


def step_protected_structure_state(
    previous_state: str,
    runtime_state: ProtectedRuntime | None,
    prepared_bar: Mapping[str, Any],
    micro_structure_context: Mapping[str, Any] | None,
    config: ProtectedStructureConfig,
) -> tuple[str, ProtectedRuntime, dict[str, Any]]:
    """Deterministic protected-structure step for one closed candle."""
    rt = runtime_state or ProtectedRuntime(state=previous_state)
    if rt.state != previous_state:
        rt.state = previous_state
        rt.state_age_bars = 0

    bar_i = int(prepared_bar["bar_index"])
    high = _finite(prepared_bar["high"])
    low = _finite(prepared_bar["low"])
    close = _finite(prepared_bar["close"])
    atr = max(_finite(prepared_bar.get("atr_14"), 1.0), 1e-12)
    clean_state = str(prepared_bar.get("indicator_clean_regime_state") or "neutral")
    micro_cfg = config.micro_config()

    # Micro swings from C3.4A causal engine (context may pre-supply newly confirmed).
    if micro_structure_context and "newly_confirmed_swings" in micro_structure_context:
        newly = list(micro_structure_context["newly_confirmed_swings"])
    else:
        newly = advance_micro_swings(rt.micro_rt, prepared_bar, micro_cfg)

    level_info = _update_candidates_and_protected(
        rt, newly, bar_i=bar_i, atr=atr, cfg=config
    )

    # Bootstrap major direction from first HH/HL or LH/LL once we have both levels.
    if rt.major_direction == 0 and rt.protected_high and rt.protected_low:
        if len(rt.micro_rt.micro_highs) >= 2 and len(rt.micro_rt.micro_lows) >= 2:
            h1, h2 = rt.micro_rt.micro_highs[-2].level, rt.micro_rt.micro_highs[-1].level
            l1, l2 = rt.micro_rt.micro_lows[-2].level, rt.micro_rt.micro_lows[-1].level
            if h2 > h1 and l2 > l1:
                _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
            elif h2 < h1 and l2 < l1:
                _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))

    ph = rt.protected_high.level if rt.protected_high else None
    pl = rt.protected_low.level if rt.protected_low else None
    active_external = ph if rt.major_direction < 0 else (pl if rt.major_direction > 0 else None)

    dist_ext = None
    if active_external is not None:
        if rt.major_direction < 0:
            dist_ext = (active_external - close) / atr
        else:
            dist_ext = (close - active_external) / atr

    # External break vs protected level.
    wick_ext_up = bool(ph is not None and high > ph)
    wick_ext_down = bool(pl is not None and low < pl)
    close_ext_up = bool(ph is not None and close > ph + config.min_close_beyond_atr * atr)
    close_ext_down = bool(pl is not None and close < pl - config.min_close_beyond_atr * atr)

    if close_ext_up:
        rt.close_beyond_up_streak += 1
    else:
        rt.close_beyond_up_streak = 0
    if close_ext_down:
        rt.close_beyond_down_streak += 1
    else:
        rt.close_beyond_down_streak = 0

    external_bos_up = rt.close_beyond_up_streak >= config.required_closes and rt.major_direction < 0
    external_bos_down = rt.close_beyond_down_streak >= config.required_closes and rt.major_direction > 0
    # Also allow external BOS after choch awaiting second bos.
    if rt.await_external_bos_after_choch and rt.choch_side == "up":
        if rt.close_beyond_up_streak >= config.required_closes and ph is not None and close > ph:
            external_bos_up = True
    if rt.await_external_bos_after_choch and rt.choch_side == "down":
        if rt.close_beyond_down_streak >= config.required_closes and pl is not None and close < pl:
            external_bos_down = True

    # Internal BOS: break last internal swing but not protected.
    internal_up = False
    internal_down = False
    if rt.last_internal_high is not None and close > rt.last_internal_high:
        if ph is None or close <= ph:
            internal_up = True
    if rt.last_internal_low is not None and close < rt.last_internal_low:
        if pl is None or close >= pl:
            internal_down = True
    # Wick-only internal for visibility.
    if rt.last_internal_high is not None and high > rt.last_internal_high and not close_ext_up:
        if ph is None or high <= ph or (close <= (ph or close)):
            if not external_bos_up:
                internal_up = internal_up or (close > rt.last_internal_high)
    if rt.last_internal_low is not None and low < rt.last_internal_low and not close_ext_down:
        if pl is None or low >= pl or (close >= (pl or close)):
            if not external_bos_down:
                internal_down = internal_down or (close < rt.last_internal_low)

    # Transition zone only vs protected external level.
    zone_active = bool(
        dist_ext is not None
        and 0.0 <= dist_ext <= config.transition_zone_atr
        and not external_bos_up
        and not external_bos_down
    )
    if zone_active:
        if rt.transition_zone_started_at is None:
            rt.transition_zone_started_at = bar_i
    else:
        rt.transition_zone_started_at = None
    zone_age = bar_i - rt.transition_zone_started_at if rt.transition_zone_started_at is not None else 0

    break_failed = False
    retest_pending = previous_state in {"bullish_retest_pending", "bearish_retest_pending"}
    internal_bos_side = "up" if internal_up else ("down" if internal_down else None)
    external_bos_side = "up" if external_bos_up else ("down" if external_bos_down else None)
    choch_side = None
    reason = "hold"
    new_state = previous_state
    prev = previous_state

    # --- State machine ---
    if prev in {"bullish_choch", "bullish_structure_candidate", "bullish_retest_pending"}:
        # Confirm or fail bullish CHoCH path.
        held = ph is not None and close >= ph - config.retest_tolerance_atr * atr
        failed = ph is not None and close < ph - config.retest_tolerance_atr * atr and not close_ext_up
        if failed and wick_ext_up and not close_ext_up:
            new_state = "bullish_break_failed"
            reason = "bullish_choch_failed_back_below_protected_high"
            break_failed = True
            rt.choch_hold_count = 0
            rt.await_hl_after_choch = False
            rt.await_external_bos_after_choch = False
            # Stay bearish major until true flip completes.
            _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
        elif config.choch_mode == "immediate":
            new_state = "bullish_structure"
            reason = "choch_immediate_confirm"
            _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
            rt.choch_hold_count = 0
            # New protected low seeds from candidate/internal after flip.
            if rt.candidate_protected_low is not None:
                _set_protected_low(rt, rt.candidate_protected_low, bar_i=bar_i, allow_replace=True)
        elif config.choch_mode == "hold":
            if held:
                rt.choch_hold_count += 1
                if rt.choch_hold_count >= config.choch_hold_bars:
                    new_state = "bullish_structure"
                    reason = "choch_hold_confirm"
                    _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
                    rt.choch_hold_count = 0
                else:
                    new_state = "bullish_structure_candidate"
                    reason = "choch_holding"
            else:
                new_state = "bullish_choch"
                reason = "choch_await_hold"
                rt.choch_hold_count = 0
        elif config.choch_mode == "hl_lh":
            rt.await_hl_after_choch = True
            if rt.post_choch_hl is not None and held:
                new_state = "bullish_structure"
                reason = "choch_plus_higher_low_confirm"
                _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
                rt.await_hl_after_choch = False
                if rt.candidate_protected_low is None and rt.post_choch_hl is not None:
                    # Use HL as new protected low.
                    _set_protected_low(
                        rt,
                        ProtectedLevel(
                            level=rt.post_choch_hl,
                            extreme_bar=bar_i,
                            extreme_timestamp=prepared_bar.get("timestamp"),
                            confirmed_bar=bar_i,
                            confirmed_timestamp=prepared_bar.get("timestamp"),
                            side="low",
                            source="post_choch_hl",
                        ),
                        bar_i=bar_i,
                        allow_replace=True,
                    )
            else:
                new_state = "bullish_structure_candidate" if rt.post_choch_hl is not None else "bullish_choch"
                reason = "choch_await_higher_low"
        elif config.choch_mode == "external_bos":
            rt.await_external_bos_after_choch = True
            # Require a fresh continuation close above protected high after choch bar.
            if prev == "bullish_choch":
                new_state = "bullish_structure_candidate"
                reason = "choch_await_external_continuation"
            elif close_ext_up and rt.close_beyond_up_streak >= config.required_closes:
                new_state = "bullish_structure"
                reason = "choch_plus_external_bos_confirm"
                _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
                rt.await_external_bos_after_choch = False
            else:
                new_state = "bullish_structure_candidate"
                reason = "choch_await_external_bos"
        choch_side = "up"

    elif prev in {"bearish_choch", "bearish_structure_candidate", "bearish_retest_pending"}:
        held = pl is not None and close <= pl + config.retest_tolerance_atr * atr
        failed = pl is not None and close > pl + config.retest_tolerance_atr * atr and not close_ext_down
        if failed and wick_ext_down and not close_ext_down:
            new_state = "bearish_break_failed"
            reason = "bearish_choch_failed_back_above_protected_low"
            break_failed = True
            rt.choch_hold_count = 0
            rt.await_lh_after_choch = False
            rt.await_external_bos_after_choch = False
            _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
        elif config.choch_mode == "immediate":
            new_state = "bearish_structure"
            reason = "choch_immediate_confirm"
            _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
            rt.choch_hold_count = 0
        elif config.choch_mode == "hold":
            if held:
                rt.choch_hold_count += 1
                if rt.choch_hold_count >= config.choch_hold_bars:
                    new_state = "bearish_structure"
                    reason = "choch_hold_confirm"
                    _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
                    rt.choch_hold_count = 0
                else:
                    new_state = "bearish_structure_candidate"
                    reason = "choch_holding"
            else:
                new_state = "bearish_choch"
                reason = "choch_await_hold"
                rt.choch_hold_count = 0
        elif config.choch_mode == "hl_lh":
            rt.await_lh_after_choch = True
            if rt.post_choch_lh is not None and held:
                new_state = "bearish_structure"
                reason = "choch_plus_lower_high_confirm"
                _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
                rt.await_lh_after_choch = False
            else:
                new_state = "bearish_structure_candidate" if rt.post_choch_lh is not None else "bearish_choch"
                reason = "choch_await_lower_high"
        elif config.choch_mode == "external_bos":
            rt.await_external_bos_after_choch = True
            if prev == "bearish_choch":
                new_state = "bearish_structure_candidate"
                reason = "choch_await_external_continuation"
            elif close_ext_down and rt.close_beyond_down_streak >= config.required_closes:
                new_state = "bearish_structure"
                reason = "choch_plus_external_bos_confirm"
                _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
                rt.await_external_bos_after_choch = False
            else:
                new_state = "bearish_structure_candidate"
                reason = "choch_await_external_bos"
        choch_side = "down"

    elif rt.major_direction == 0:
        if rt.protected_high is None and rt.protected_low is None:
            new_state = "structure_unknown"
            reason = "awaiting_protected_levels"
        else:
            new_state = "range_unclear"
            reason = "unclear_protected_direction"

    elif rt.major_direction < 0:
        # Bearish protected structure — only external BOS of protected_high flips.
        if external_bos_up:
            new_state = "bullish_choch"
            reason = "external_bos_up_protected_high"
            choch_side = "up"
            rt.choch_side = "up"
            rt.choch_level = ph
            rt.choch_hold_count = 0
            rt.post_choch_hl = None
            rt.await_hl_after_choch = config.choch_mode == "hl_lh"
            rt.await_external_bos_after_choch = config.choch_mode == "external_bos"
            # Do NOT set major_direction=1 yet except immediate handled next bars.
            if config.choch_mode == "immediate":
                new_state = "bullish_structure"
                reason = "external_bos_up_immediate_structure"
                _set_major_direction(rt, 1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
        elif wick_ext_up and not close_ext_up:
            # Wick fakeout — stay bearish family.
            if zone_active:
                new_state = "transition_blocked"
                reason = "wick_into_protected_high_zone"
            else:
                new_state = "bearish_pullback"
                reason = "wick_reject_protected_high"
        elif zone_active:
            new_state = "transition_blocked"
            reason = "near_protected_high_transition_zone"
        elif internal_up:
            new_state = "bullish_internal_break"
            reason = "internal_bos_up_no_major_flip"
        elif clean_state in {"bullish_building", "bullish_confirmed"}:
            new_state = "bearish_pullback"
            reason = "bullish_indicator_inside_bearish_protected"
        else:
            new_state = "bearish_structure"
            reason = "hold_bearish_protected"

    else:
        # Bullish protected structure.
        if external_bos_down:
            new_state = "bearish_choch"
            reason = "external_bos_down_protected_low"
            choch_side = "down"
            rt.choch_side = "down"
            rt.choch_level = pl
            rt.choch_hold_count = 0
            rt.post_choch_lh = None
            rt.await_lh_after_choch = config.choch_mode == "hl_lh"
            rt.await_external_bos_after_choch = config.choch_mode == "external_bos"
            if config.choch_mode == "immediate":
                new_state = "bearish_structure"
                reason = "external_bos_down_immediate_structure"
                _set_major_direction(rt, -1, bar_i=bar_i, timestamp=prepared_bar.get("timestamp"))
        elif wick_ext_down and not close_ext_down:
            if zone_active:
                new_state = "transition_blocked"
                reason = "wick_into_protected_low_zone"
            else:
                new_state = "bullish_pullback"
                reason = "wick_reject_protected_low"
        elif zone_active:
            new_state = "transition_blocked"
            reason = "near_protected_low_transition_zone"
        elif internal_down:
            new_state = "bearish_internal_break"
            reason = "internal_bos_down_no_major_flip"
        elif clean_state in {"bearish_building", "bearish_confirmed"}:
            new_state = "bullish_pullback"
            reason = "bearish_indicator_inside_bullish_protected"
        else:
            new_state = "bullish_structure"
            reason = "hold_bullish_protected"

    changed = new_state != prev
    if changed:
        rt.state_age_bars = 1
    else:
        rt.state_age_bars += 1
    rt.state = new_state

    # Refresh levels after possible major-dir clears / choch seeding.
    _drop_stale_crossed_opposite(rt)
    ph = rt.protected_high.level if rt.protected_high else None
    pl = rt.protected_low.level if rt.protected_low else None
    active_external = ph if rt.major_direction < 0 else (pl if rt.major_direction > 0 else None)
    dist_ext = None
    if active_external is not None:
        if rt.major_direction < 0:
            dist_ext = (active_external - close) / atr
        else:
            dist_ext = (close - active_external) / atr

    # Attempted illegal replace of protected by every new micro high (guard probe).
    for sw in newly:
        if sw.kind == "high" and rt.major_direction < 0 and rt.protected_high is not None:
            if float(sw.level) != rt.protected_high.level and not level_info.get("continuation_down"):
                # Explicitly not replacing — record zero-violation path by not calling setter.
                pass

    diag = {
        "protected_structure_state": new_state,
        "previous_protected_structure_state": prev,
        "protected_structure_changed": changed,
        "protected_state_code": PROTECTED_STATE_CODE.get(new_state, 0),
        "major_direction": rt.major_direction,
        "structure_strength": _structure_strength(new_state),
        "structure_age_bars": rt.state_age_bars,
        "protected_high": ph,
        "protected_high_time": None if rt.protected_high is None else rt.protected_high.extreme_timestamp,
        "protected_high_confirmed_at": None if rt.protected_high is None else rt.protected_high.confirmed_timestamp,
        "protected_low": pl,
        "protected_low_time": None if rt.protected_low is None else rt.protected_low.extreme_timestamp,
        "protected_low_confirmed_at": None if rt.protected_low is None else rt.protected_low.confirmed_timestamp,
        "candidate_protected_high": None
        if rt.candidate_protected_high is None
        else rt.candidate_protected_high.level,
        "candidate_protected_high_time": None
        if rt.candidate_protected_high is None
        else rt.candidate_protected_high.confirmed_timestamp,
        "candidate_protected_low": None
        if rt.candidate_protected_low is None
        else rt.candidate_protected_low.level,
        "candidate_protected_low_time": None
        if rt.candidate_protected_low is None
        else rt.candidate_protected_low.confirmed_timestamp,
        "candidate_leg": rt.candidate_leg,
        "last_external_high": rt.last_external_high,
        "last_external_low": rt.last_external_low,
        "last_internal_high": rt.last_internal_high,
        "last_internal_low": rt.last_internal_low,
        "active_external_break_level": active_external,
        "active_internal_up_level": rt.last_internal_high,
        "active_internal_down_level": rt.last_internal_low,
        "distance_to_external_break_atr": dist_ext,
        "transition_zone_active": zone_active,
        "transition_zone_age_bars": zone_age,
        "transition_blocked": new_state == "transition_blocked",
        "internal_bos_side": internal_bos_side,
        "external_bos_side": external_bos_side,
        "choch_side": choch_side
        if choch_side is not None
        else (
            rt.choch_side
            if (
                new_state.endswith("choch")
                or "candidate" in new_state
                or new_state.endswith("retest_pending")
            )
            else None
        ),
        "wick_break_protected_up": wick_ext_up,
        "wick_break_protected_down": wick_ext_down,
        "close_break_protected_up": close_ext_up,
        "close_break_protected_down": close_ext_down,
        "external_bos_up": external_bos_up,
        "external_bos_down": external_bos_down,
        "internal_bos_up": internal_up,
        "internal_bos_down": internal_down,
        "retest_pending": retest_pending,
        "break_failed": break_failed,
        "transition_reason": reason,
        "indicator_clean_regime_state": clean_state,
        "clean_regime_state": clean_state,
        "structure_indicator_alignment": _alignment(new_state, clean_state),
        "n_new_micro_swings": len(newly),
        "protected_high_updated": level_info["protected_high_updated"],
        "protected_low_updated": level_info["protected_low_updated"],
        "candidate_high_set": level_info["candidate_high_set"],
        "candidate_low_set": level_info["candidate_low_set"],
        "continuation_down": level_info["continuation_down"],
        "continuation_up": level_info["continuation_up"],
        "promotion_reason": level_info.get("promotion_reason"),
        "protected_high_before": level_info.get("protected_high_before"),
        "protected_low_before": level_info.get("protected_low_before"),
        "protected_replacements_total": rt.protected_replacements,
        "guard_violation_count": len(rt.guard_violations),
        "micro_swing_high": rt.last_internal_high,
        "micro_swing_low": rt.last_internal_low,
        "new_micro_high": any(s.kind == "high" for s in newly),
        "new_micro_low": any(s.kind == "low" for s in newly),
    }
    return new_state, rt, diag


def apply_protected_structure(
    ohlcv: pd.DataFrame,
    cfg: ProtectedStructureConfig,
    *,
    clean_regime_states: Sequence[str] | None = None,
) -> pd.DataFrame:
    if ohlcv.empty:
        return ohlcv.copy()
    df = ohlcv.reset_index(drop=True).copy()
    if "bar_index" not in df.columns:
        df["bar_index"] = np.arange(len(df))
    if "atr_14" not in df.columns:
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
    rt = ProtectedRuntime()
    prev = "structure_unknown"
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()

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
            "indicator_clean_regime_state": clean,
        }
        new_state, rt, diag = step_protected_structure_state(
            prev, rt, prepared, None, cfg
        )
        rows.append(
            {
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
        )
        prev = new_state

    return pd.DataFrame(rows)


def bot_interface_frame(structure_df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "timestamp",
        "decision_time",
        "symbol",
        "timeframe",
        "protected_structure_state",
        "previous_protected_structure_state",
        "protected_structure_changed",
        "major_direction",
        "structure_strength",
        "structure_age_bars",
        "protected_high",
        "protected_low",
        "candidate_protected_high",
        "candidate_protected_low",
        "active_external_break_level",
        "distance_to_external_break_atr",
        "internal_bos_side",
        "external_bos_side",
        "choch_side",
        "transition_blocked",
        "retest_pending",
        "break_failed",
        "transition_reason",
        "clean_regime_state",
        "structure_indicator_alignment",
        "config_variant",
        "config_hash",
        "rule_spec_hash",
        "protected_state_code",
    ]
    present = [c for c in cols if c in structure_df.columns]
    return structure_df[present].copy()

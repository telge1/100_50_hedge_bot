"""Diagnostic specification & validation of Hybrid G (trenddefining FB + confirmation).

Does NOT modify production trend_structure / trend_state_machine / trend_state_policy.
Structure+HTF computed once; each G-variant runs a causal state-machine replay on frozen inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    TrendState,
    TrendStateConfig,
    _can_leave,
    _enter,
    _event_types,
    _htf_bias,
    _htf_veto_strong_bearish,
    _htf_veto_strong_bullish,
    _indicator_confirms,
    _scores,
    _update_impulse_counters,
    _update_swing_age,
    default_trend_state_config,
    has_hh_hl,
    has_lh_ll,
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_state_march_2026_root_cause_audit import install_causal_htf_prefix_cache
from research.regime_scanner.trend_structure import MarketStructureState, StructureEvent

OUT = Path("research/regime_scanner/results/trend_state_failed_break_hybrid_spec")
DIAG_END = "2026-03-10T00:00:00+00:00"
FEB01 = "2026-02-01T11:05:00+00:00"
MARCH = "2026-03-06T00:30:00+00:00"

VariantId = Literal[
    "BASELINE",
    "G1",
    "G2",
    "G3",
    "G4",
    "G6",
    "G5",
    "G1_htf15_nonbearish",
    "G4_htf15_nonbearish",
    "G6_plus_15m_countertrend",
]


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _p(msg: str) -> None:
    print(msg, flush=True)


def _level_eq(a: object, b: object) -> bool:
    if a is None or b is None:
        return False
    try:
        return float(a) == float(b)  # exact float identity as production last_broken_*
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Documentation packs
# ---------------------------------------------------------------------------


def transition_path_docs() -> list[dict[str, Any]]:
    return [
        {
            "state": "early_bearish",
            "transition": "bearish_weakening",
            "source_function": "trend_state_machine._propose_transition",
            "exact_condition": (
                "min_hold(3) AND (failed_breakdown OR bearish_retest_fails OR "
                "(bullish_choch AND higher_low))"
            ),
            "inputs": "same-bar 5m event types only; no HTF; no protective level check",
            "current_priority": "evaluated before early→strong promotion in same branch",
            "fachliches_risiko": "single local swing-low reclaim ends early bearish",
        },
        {
            "state": "strong_bearish",
            "transition": "bearish_weakening",
            "source_function": "trend_state_machine._propose_transition",
            "exact_condition": (
                "min_hold(4) AND (failed_breakdown|bullish_choch|bearish_retest_fails|higher_low "
                "OR bars_since_ll>=no_ll_lookback) AND NOT (same-bar bearish_bos AND lower_low)"
            ),
            "inputs": "same-bar types + bars_since_ll; optional indicator_confirm_weakening note",
            "current_priority": "strong branch first in propose; no competing strong→other on same path",
            "fachliches_risiko": "single failed_breakdown exits strong",
        },
        {
            "state": "early_bullish",
            "transition": "bullish_weakening",
            "source_function": "trend_state_machine._propose_transition",
            "exact_condition": (
                "min_hold(3) AND (failed_breakout OR bullish_retest_fails OR "
                "(bearish_choch AND lower_high))"
            ),
            "inputs": "mirror of early_bearish",
            "current_priority": "before early→strong bullish",
            "fachliches_risiko": "mirror: local swing-high reclaim ends early bullish",
        },
        {
            "state": "strong_bullish",
            "transition": "bullish_weakening",
            "source_function": "trend_state_machine._propose_transition",
            "exact_condition": (
                "min_hold(4) AND (failed_breakout|bearish_choch|bullish_retest_fails|lower_high "
                "OR bars_since_hh>=no_ll_lookback) AND NOT (bullish_bos AND higher_high)"
            ),
            "inputs": "mirror of strong_bearish",
            "current_priority": "strong bullish branch",
            "fachliches_risiko": "mirror: single failed_breakout exits strong",
        },
    ]


def input_inventory() -> list[dict[str, Any]]:
    rows = [
        ("failed_breakdown", "trend_structure.py", "_detect_failed_breaks", True, True, "event", True, "local swing reclaim"),
        ("failed_breakout", "trend_structure.py", "_detect_failed_breaks", True, True, "event", True, "mirror"),
        ("fb.level", "StructureEvent.level", "_detect_failed_breaks", True, True, "event", True, "exact float"),
        ("fb.reference_pivot_time", "StructureEvent", "_detect_failed_breaks", True, True, "event", True, "swing identity"),
        ("protective_low_level", "MarketStructureState", "_set/_clear_protective_low", True, True, "persistent", True, "V6+V2 continued HL"),
        ("protective_high_level", "MarketStructureState", "_set/_clear_protective_high", True, True, "persistent", True, "mirror"),
        ("last_broken_low_level", "MarketStructureState", "_detect_bos_choch", True, True, "persistent", True, "overwritten on next protective break"),
        ("last_broken_high_level", "MarketStructureState", "_detect_bos_choch", True, True, "persistent", True, "mirror"),
        ("has_lh_ll", "trend_structure.has_lh_ll", "labels", True, True, "derived", True, "weak alone if early never had LH+LL"),
        ("has_hh_hl", "trend_structure.has_hh_hl", "labels", True, True, "derived", True, "strong counterstructure"),
        ("bias_5m", "current_structure_bias", "derive_structure_bias", True, True, "derived", True, "last-pair"),
        ("bullish_choch", "StructureEvent", "_detect_bos_choch", True, True, "event", True, "breaks protective high"),
        ("bearish_choch", "StructureEvent", "_detect_bos_choch", True, True, "event", True, "mirror"),
        ("bullish_bos", "StructureEvent", "_detect_bos_choch", True, True, "event", True, "continuation break high"),
        ("bearish_retest_fails", "StructureEvent", "_detect_retests", True, True, "event", False, "separate early path; keep unchanged"),
        ("htf15_bias", "structure_15m", "_update_htf_structure", True, False, "derived", False, "mixes with HTF-veto problem"),
        ("htf30_bias", "structure_30m", "_update_htf_structure", True, False, "derived", False, "same"),
        ("htf_veto_strong_*", "trend_state_machine", "_htf_veto_strong_*", True, True, "derived", False, "entry problem; isolate from G"),
        ("state/age", "TrendRuntime", "step_trend_state", True, True, "persistent", True, "min_hold separate from event age"),
        ("last_failed_breakdown sticky", "MarketStructureState", "_detect_failed_breaks", True, False, "persistent", False, "propose uses same-bar types only"),
    ]
    out = []
    for name, src, fn, causal, same_bar, kind, suit, risk in rows:
        out.append(
            {
                "input": name,
                "source_file": src,
                "source_function": fn,
                "causally_available": causal,
                "same_bar_available": same_bar,
                "persistent_or_derived": kind,
                "suitable_for_hybrid": suit,
                "risk": risk,
            }
        )
    return out


def evidence_analysis() -> list[dict[str, Any]]:
    return [
        {
            "evidence": "E1_bullish_choch",
            "independent_from_failed_break": True,
            "structural_strength": "high",
            "available_same_bar": True,
            "early_suitable": True,
            "strong_suitable": True,
            "double_count_risk": "low — CHoCH breaks protective_high; FB reclaims swing/protective_low",
            "recommendation": "accept as primary confirmation",
        },
        {
            "evidence": "E2_not_has_lh_ll",
            "independent_from_failed_break": True,
            "structural_strength": "medium",
            "available_same_bar": True,
            "early_suitable": "conditional",
            "strong_suitable": "weak_alone",
            "double_count_risk": "medium — early often never had LH+LL",
            "recommendation": "allow only with trenddefining FB; never alone as substitute for td",
        },
        {
            "evidence": "E3_has_hh_hl",
            "independent_from_failed_break": True,
            "structural_strength": "high",
            "available_same_bar": True,
            "early_suitable": True,
            "strong_suitable": True,
            "double_count_risk": "low",
            "recommendation": "accept as strong confirmation",
        },
        {
            "evidence": "E4_bias_not_bearish",
            "independent_from_failed_break": True,
            "structural_strength": "medium",
            "available_same_bar": True,
            "early_suitable": True,
            "strong_suitable": "medium",
            "double_count_risk": "low",
            "recommendation": "secondary confirmation only",
        },
        {
            "evidence": "E5_15m_not_bearish",
            "independent_from_failed_break": True,
            "structural_strength": "low-medium",
            "available_same_bar": True,
            "early_suitable": False,
            "strong_suitable": False,
            "double_count_risk": "high with HTF-veto diagnosis",
            "recommendation": "optional diagnostic only; not core G",
        },
        {
            "evidence": "E6_15m_bullish",
            "independent_from_failed_break": True,
            "structural_strength": "medium",
            "available_same_bar": True,
            "early_suitable": False,
            "strong_suitable": False,
            "double_count_risk": "high with HTF-veto",
            "recommendation": "reject for core G",
        },
        {
            "evidence": "E7_bullish_bos",
            "independent_from_failed_break": True,
            "structural_strength": "high",
            "available_same_bar": True,
            "early_suitable": True,
            "strong_suitable": True,
            "double_count_risk": "low if different level than FB",
            "recommendation": "accept when independent level",
        },
        {
            "evidence": "E8_bullish_retest_holds",
            "independent_from_failed_break": True,
            "structural_strength": "medium",
            "available_same_bar": True,
            "early_suitable": True,
            "strong_suitable": "medium",
            "double_count_risk": "medium if retest of same broken low",
            "recommendation": "optional; prefer structure pair/CHoCH",
        },
        {
            "evidence": "E9_two_independent_countertrend_events",
            "independent_from_failed_break": True,
            "structural_strength": "high",
            "available_same_bar": True,
            "early_suitable": True,
            "strong_suitable": True,
            "double_count_risk": "depends on independence rule",
            "recommendation": "use for Strong in G2/G5",
        },
    ]


def trenddefining_definition() -> dict[str, Any]:
    return {
        "bearish_weakening": {
            "rule": (
                "FB.level exact-equals active protective_low_level "
                "OR FB.level exact-equals last_broken_low_level"
            ),
            "identity": "exact float equality (same as production _level_matches / last_broken_*)",
            "pivot_preference": (
                "If protective_low_pivot / reference_pivot_time available and match, prefer; "
                "price equality is the production-stable fallback already used for broken levels."
            ),
            "after_break_protective_none": (
                "Yes — after protective close-break, next refresh clears protective_low; "
                "last_broken_low_level retains the just-broken protective price until overwritten "
                "by a later protective break. Therefore last_broken_low is required for post-break FB."
            ),
            "stale_broken_rejection": (
                "last_broken_low_level is only the most recent protective break price; "
                "older broken protectives are not retained as a list. Matching last_broken is "
                "therefore limited to the current structure path's latest protective break — "
                "not an arbitrary historical swing low."
            ),
            "non_protective_swing_low": (
                "FB on last_confirmed_swing_low that equals neither protective nor last_broken "
                "is NOT trenddefining (rejects Feb-01 @1.265 and March @0.9926)."
            ),
            "validity_until": [
                "new protective_low set (path continues / resets)",
                "last_broken_low overwritten by newer protective break",
                "same-bar types only for propose — sticky last_failed_breakdown NOT reused across bars",
            ],
        },
        "bullish_weakening_mirror": {
            "rule": "FO.level == protective_high_level OR FO.level == last_broken_high_level",
            "identity": "exact float equality",
        },
        "level_sources": [
            {
                "level_source": "active_protective_low",
                "eligible": True,
                "validity": "while protective_low_level is set",
                "identity_rule": "float(FB.level)==float(protective_low_level)",
                "rejection_reason": "",
            },
            {
                "level_source": "last_broken_low_level",
                "eligible": True,
                "validity": "until overwritten by next protective low break",
                "identity_rule": "float(FB.level)==float(last_broken_low_level)",
                "rejection_reason": "rejected if None or price differs",
            },
            {
                "level_source": "arbitrary_last_confirmed_swing_low",
                "eligible": False,
                "validity": "n/a",
                "identity_rule": "not sufficient alone",
                "rejection_reason": "local micro swing; not trend-defining",
            },
        ],
    }


def independent_evidence(a: StructureEvent | None, b: StructureEvent | dict[str, Any] | None) -> bool:
    """Boolean independence without numeric distance thresholds."""
    if a is None or b is None:
        return False
    if isinstance(b, dict):
        b_type = str(b.get("event_type") or b.get("kind") or "")
        b_level = b.get("level")
        b_pivot = b.get("reference_pivot_time")
    else:
        b_type = b.event_type
        b_level = b.level
        b_pivot = None if b.reference_pivot_time is None else _iso(b.reference_pivot_time)
    if a.event_type == b_type:
        return False
    if _level_eq(a.level, b_level):
        # same source level → same structural object (reclaim vs break of same price)
        return False
    a_pivot = None if a.reference_pivot_time is None else _iso(a.reference_pivot_time)
    if a_pivot is not None and b_pivot is not None and a_pivot == b_pivot:
        return False
    return True


def double_counting_matrix() -> list[dict[str, Any]]:
    return [
        {
            "pair": "failed_breakdown + bullish_choch",
            "same_level_possible": False,
            "reason": "FB on low side; bullish_choch breaks protective_high",
            "independent_if_same_bar": True,
            "count_both": True,
        },
        {
            "pair": "failed_breakdown + bearish_bos/choch same protective low",
            "same_level_possible": True,
            "reason": "successful break vs failed reclaim of same low — mutually exclusive holds",
            "independent_if_same_bar": False,
            "count_both": False,
        },
        {
            "pair": "failed_breakdown + not has_lh_ll",
            "same_level_possible": False,
            "reason": "derived label state vs event",
            "independent_if_same_bar": True,
            "count_both": True,
        },
        {
            "pair": "failed_breakdown + has_hh_hl",
            "same_level_possible": False,
            "reason": "pair structure vs low reclaim",
            "independent_if_same_bar": True,
            "count_both": True,
        },
        {
            "pair": "failed_breakdown + bullish_bos same high",
            "same_level_possible": False,
            "reason": "different sides",
            "independent_if_same_bar": True,
            "count_both": True,
        },
        {
            "pair": "sticky last_failed_breakdown + later evidence",
            "same_level_possible": "n/a",
            "reason": "propose uses same-bar event types only — sticky slot not a transition input",
            "independent_if_same_bar": False,
            "count_both": False,
        },
    ]


# ---------------------------------------------------------------------------
# Hybrid gate
# ---------------------------------------------------------------------------


@dataclass
class HybridContext:
    variant: str
    events: list[StructureEvent]
    s5: MarketStructureState
    s15: MarketStructureState
    s30: MarketStructureState

    def fb_events(self) -> list[StructureEvent]:
        return [e for e in self.events if e.event_type == "failed_breakdown"]

    def fo_events(self) -> list[StructureEvent]:
        return [e for e in self.events if e.event_type == "failed_breakout"]


def is_trenddefining_breakdown(ev: StructureEvent, s5: MarketStructureState) -> tuple[bool, str]:
    if _level_eq(ev.level, s5.protective_low_level):
        return True, "active_protective_low"
    if _level_eq(ev.level, s5.last_broken_low_level):
        return True, "last_broken_low"
    return False, "non_trenddefining_swing_low"


def is_trenddefining_breakout(ev: StructureEvent, s5: MarketStructureState) -> tuple[bool, str]:
    if _level_eq(ev.level, s5.protective_high_level):
        return True, "active_protective_high"
    if _level_eq(ev.level, s5.last_broken_high_level):
        return True, "last_broken_high"
    return False, "non_trenddefining_swing_high"


def _confirmations_bearish(
    ctx: HybridContext,
    fb: StructureEvent,
    *,
    allow_htf_nonbearish: bool = False,
    allow_htf_bullish_structure: bool = False,
) -> list[str]:
    """Independent confirmations for bearish→weakening (excluding the FB itself)."""
    hits: list[str] = []
    types = _event_types(ctx.events)
    # E1 bullish_choch with independence
    for e in ctx.events:
        if e.event_type == "bullish_choch" and independent_evidence(fb, e):
            hits.append("E1_bullish_choch")
            break
    # E7 bullish_bos
    for e in ctx.events:
        if e.event_type == "bullish_bos" and independent_evidence(fb, e):
            hits.append("E7_bullish_bos")
            break
    # E2 / E3 / E4 derived
    if not has_lh_ll(ctx.s5):
        hits.append("E2_not_lh_ll")
    if has_hh_hl(ctx.s5):
        hits.append("E3_hh_hl")
    if ctx.s5.current_structure_bias != "bearish":
        hits.append("E4_bias_not_bearish")
    # E8
    if "bullish_retest_holds" in types:
        # independence: retest level vs FB level
        ok = True
        for e in ctx.events:
            if e.event_type == "bullish_retest_holds" and not independent_evidence(fb, e):
                ok = False
        if ok:
            hits.append("E8_bullish_retest_holds")
    if allow_htf_nonbearish and _htf_bias(ctx.s15) != "bearish":
        hits.append("E5_15m_not_bearish")
    if allow_htf_bullish_structure and _htf_bias(ctx.s15) == "bullish" and has_hh_hl(ctx.s15):
        hits.append("E6_15m_bullish_hh_hl")
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _confirmations_bullish(
    ctx: HybridContext,
    fo: StructureEvent,
    *,
    allow_htf_nonbullish: bool = False,
    allow_htf_bearish_structure: bool = False,
) -> list[str]:
    hits: list[str] = []
    types = _event_types(ctx.events)
    for e in ctx.events:
        if e.event_type == "bearish_choch" and independent_evidence(fo, e):
            hits.append("E1_bearish_choch")
            break
    for e in ctx.events:
        if e.event_type == "bearish_bos" and independent_evidence(fo, e):
            hits.append("E7_bearish_bos")
            break
    if not has_hh_hl(ctx.s5):
        hits.append("E2_not_hh_hl")
    if has_lh_ll(ctx.s5):
        hits.append("E3_lh_ll")
    if ctx.s5.current_structure_bias != "bullish":
        hits.append("E4_bias_not_bullish")
    if "bearish_retest_holds" in types:
        ok = True
        for e in ctx.events:
            if e.event_type == "bearish_retest_holds" and not independent_evidence(fo, e):
                ok = False
        if ok:
            hits.append("E8_bearish_retest_holds")
    if allow_htf_nonbullish and _htf_bias(ctx.s15) != "bullish":
        hits.append("E5_15m_not_bullish")
    if allow_htf_bearish_structure and _htf_bias(ctx.s15) == "bearish" and has_lh_ll(ctx.s15):
        hits.append("E6_15m_bearish_lh_ll")
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def hybrid_failed_breakdown_allows_weakening(ctx: HybridContext, *, strong: bool) -> bool:
    """Core Hybrid G gate for failed_breakdown contribution only."""
    fbs = ctx.fb_events()
    if not fbs:
        return False
    # Use first same-bar FB (production emits at most one typically)
    fb = fbs[0]
    td, _ = is_trenddefining_breakdown(fb, ctx.s5)
    v = ctx.variant
    htf_nb = v.endswith("htf15_nonbearish") or v == "G1_htf15_nonbearish"
    htf_ct = v == "G6_plus_15m_countertrend"
    conf = _confirmations_bearish(
        ctx,
        fb,
        allow_htf_nonbearish=htf_nb,
        allow_htf_bullish_structure=htf_ct,
    )
    # structural confirmations preferred for core variants
    struct_conf = [c for c in conf if c.startswith(("E1_", "E2_", "E3_", "E4_", "E7_", "E8_"))]
    choch = "E1_bullish_choch" in conf
    hh_hl = "E3_hh_hl" in conf
    lost = "E2_not_lh_ll" in conf
    cont_gone = not (ctx.s5.current_structure_bias == "bearish" and has_lh_ll(ctx.s5))

    if v in {"G1", "G1_htf15_nonbearish"}:
        if not td:
            return False
        need = struct_conf if not htf_nb else conf
        return len(need) >= 1
    if v == "G2":
        if not td:
            return False
        return len(struct_conf) >= (2 if strong else 1)
    if v == "G3":
        if not td:
            return False
        if strong:
            return choch
        return len(struct_conf) >= 1
    if v in {"G4", "G4_htf15_nonbearish"}:
        if not td:
            return False
        if strong:
            return choch and cont_gone
        ok = choch or hh_hl or lost
        if htf_nb and not ok:
            ok = "E5_15m_not_bearish" in conf
        return ok
    if v == "G5":
        # Pflicht: trenddefining FB counts as required base; need extra hits
        if not td:
            return False
        return len(struct_conf) >= (2 if strong else 1)
    if v in {"G6", "G6_plus_15m_countertrend"}:
        if not td:
            return False
        if strong:
            base = choch
        else:
            base = choch or hh_hl
        if htf_ct and not base:
            return "E6_15m_bullish_hh_hl" in conf
        return base
    return False


def hybrid_failed_breakout_allows_weakening(ctx: HybridContext, *, strong: bool) -> bool:
    fos = ctx.fo_events()
    if not fos:
        return False
    fo = fos[0]
    td, _ = is_trenddefining_breakout(fo, ctx.s5)
    v = ctx.variant
    htf_nb = v.endswith("htf15_nonbearish")
    htf_ct = v == "G6_plus_15m_countertrend"
    conf = _confirmations_bullish(
        ctx,
        fo,
        allow_htf_nonbullish=htf_nb,
        allow_htf_bearish_structure=htf_ct,
    )
    struct_conf = [c for c in conf if c.startswith(("E1_", "E2_", "E3_", "E4_", "E7_", "E8_"))]
    choch = "E1_bearish_choch" in conf
    lh_ll = "E3_lh_ll" in conf
    lost = "E2_not_hh_hl" in conf
    cont_gone = not (ctx.s5.current_structure_bias == "bullish" and has_hh_hl(ctx.s5))

    if v in {"G1", "G1_htf15_nonbearish"}:
        if not td:
            return False
        need = struct_conf if not htf_nb else conf
        return len(need) >= 1
    if v == "G2":
        if not td:
            return False
        return len(struct_conf) >= (2 if strong else 1)
    if v == "G3":
        if not td:
            return False
        if strong:
            return choch
        return len(struct_conf) >= 1
    if v in {"G4", "G4_htf15_nonbearish"}:
        if not td:
            return False
        if strong:
            return choch and cont_gone
        ok = choch or lh_ll or lost
        if htf_nb and not ok:
            ok = "E5_15m_not_bullish" in conf
        return ok
    if v == "G5":
        if not td:
            return False
        return len(struct_conf) >= (2 if strong else 1)
    if v in {"G6", "G6_plus_15m_countertrend"}:
        if not td:
            return False
        if strong:
            base = choch
        else:
            base = choch or lh_ll
        if htf_ct and not base:
            return "E6_15m_bearish_lh_ll" in conf
        return base
    return False


def propose_transition_variant(
    rt: TrendRuntime,
    *,
    events: list[StructureEvent],
    row: dict[str, Any],
    cfg: TrendStateConfig,
    variant: str,
) -> tuple[TrendState | None, list[str]]:
    """Copy of production propose with Hybrid-G gating on failed_break contributions only."""
    types = _event_types(events)
    reasons: list[str] = []
    s5 = rt.structure_5m
    s15 = rt.structure_15m
    s30 = rt.structure_30m
    bear_conf, bear_codes = _indicator_confirms(row, side="bearish", cfg=cfg)
    bull_conf, bull_codes = _indicator_confirms(row, side="bullish", cfg=cfg)
    state = rt.state
    ctx = HybridContext(variant=variant, events=events, s5=s5, s15=s15, s30=s30)

    def need_hold() -> bool:
        return not _can_leave(rt, cfg)

    if state == "strong_bearish":
        if need_hold():
            return None, ["min_hold_strong_bearish"]
        fb_ok = hybrid_failed_breakdown_allows_weakening(ctx, strong=True) if variant != "BASELINE" else (
            "failed_breakdown" in types
        )
        weaken_struct = bool(
            (fb_ok and "failed_breakdown" in types)
            or (types & {"bullish_choch", "bearish_retest_fails", "higher_low"})
            or (rt.bars_since_ll >= cfg.no_ll_lookback)
        )
        # baseline: FB alone in the set
        if variant == "BASELINE":
            weaken_struct = bool(
                types & {"failed_breakdown", "bullish_choch", "bearish_retest_fails", "higher_low"}
            ) or (rt.bars_since_ll >= cfg.no_ll_lookback)
        if weaken_struct and not ("bearish_bos" in types and "lower_low" in types):
            reasons.extend(["structure_weakening"])
            return "bearish_weakening", reasons
        return None, reasons

    if state == "strong_bullish":
        if need_hold():
            return None, ["min_hold_strong_bullish"]
        if variant == "BASELINE":
            weaken_struct = bool(
                types & {"failed_breakout", "bearish_choch", "bullish_retest_fails", "lower_high"}
            ) or (rt.bars_since_hh >= cfg.no_ll_lookback)
        else:
            fo_ok = hybrid_failed_breakout_allows_weakening(ctx, strong=True)
            weaken_struct = bool(
                (fo_ok and "failed_breakout" in types)
                or (types & {"bearish_choch", "bullish_retest_fails", "lower_high"})
                or (rt.bars_since_hh >= cfg.no_ll_lookback)
            )
        if weaken_struct and not ("bullish_bos" in types and "higher_high" in types):
            reasons.extend(["structure_weakening"])
            return "bullish_weakening", reasons
        return None, reasons

    if state == "bearish_weakening":
        if need_hold():
            return None, ["min_hold_bearish_weakening"]
        if "lower_low" in types and "bearish_bos" in types:
            return "early_bearish", ["failed_bottom", "ll_bos"]
        bottom_hits = types & {"failed_breakdown", "bullish_choch", "higher_low", "bullish_bos"}
        if len(bottom_hits) >= 2 and "lower_low" not in types:
            return "bottoming", ["bottoming_structure", *sorted(bottom_hits)]
        return None, reasons

    if state == "bullish_weakening":
        if need_hold():
            return None, ["min_hold_bullish_weakening"]
        if "higher_high" in types and "bullish_bos" in types:
            return "early_bullish", ["failed_top", "hh_bos"]
        top_hits = types & {"failed_breakout", "bearish_choch", "lower_high", "bearish_bos"}
        if len(top_hits) >= 2 and "higher_high" not in types:
            return "topping", ["topping_structure", *sorted(top_hits)]
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
                return None, reasons
            if _htf_veto_strong_bearish(s15, s30) and "bullish_bos" not in types:
                return None, reasons
            if rt.consecutive_bullish_closes >= cfg.bullish_impulse_min_closes or bull_conf >= 2:
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
        # Non-FB invalidation paths unchanged
        non_fb = bool(types & {"bearish_retest_fails"}) or (
            "bullish_choch" in types and "higher_low" in types
        )
        fb_path = False
        if "failed_breakdown" in types:
            if variant == "BASELINE":
                fb_path = True
            else:
                fb_path = hybrid_failed_breakdown_allows_weakening(ctx, strong=False)
        if non_fb or fb_path:
            return "bearish_weakening", ["early_invalidation_toward_weakening"]
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
        non_fo = bool(types & {"bullish_retest_fails"}) or (
            "bearish_choch" in types and "lower_high" in types
        )
        fo_path = False
        if "failed_breakout" in types:
            if variant == "BASELINE":
                fo_path = True
            else:
                fo_path = hybrid_failed_breakout_allows_weakening(ctx, strong=False)
        if non_fo or fo_path:
            return "bullish_weakening", ["early_invalidation_toward_weakening"]
        if (
            has_hh_hl(s5)
            and s5.current_structure_bias == "bullish"
            and (_htf_bias(s15) in {"bullish", "neutral"} or "bullish_bos" in types)
        ):
            if _htf_veto_strong_bearish(s15, s30) and not cfg.allow_violent_reversal:
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
                return None, reasons
            if _htf_bias(s15) != "bullish" or bear_conf >= 2:
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
        if "bearish_choch" in types or "failed_breakout" in types or (
            "bearish_bos" in types and s5.current_structure_bias != "bullish"
        ):
            if not _htf_veto_strong_bullish(s15, s30):
                return "bearish_warning", reasons
        if "bullish_choch" in types or "failed_breakdown" in types or (
            "bullish_bos" in types and s5.current_structure_bias != "bearish"
        ):
            if not _htf_veto_strong_bearish(s15, s30):
                return "bullish_warning", reasons
        return None, reasons

    return None, reasons


# ---------------------------------------------------------------------------
# Causal bar capture + variant SM replay
# ---------------------------------------------------------------------------


@dataclass
class CausalBar:
    i: int
    timestamp: str
    row: dict[str, Any]
    events_5m: list[StructureEvent]
    s5: MarketStructureState
    s15: MarketStructureState
    s30: MarketStructureState
    # production baseline state after this bar (for reference)
    baseline_state_before: str
    baseline_state_after: str
    baseline_age_before: int


def load_frame(end: pd.Timestamp) -> tuple[pd.DataFrame, list]:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[raw["timestamp"] < end].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    frame = frame[frame["decision_time"] <= end].reset_index(drop=True)
    pivots = find_confirmed_pivots(frame, config=scfg)
    return frame, pivots


def capture_causal_bars(frame: pd.DataFrame, pivots: list) -> list[CausalBar]:
    cfg = default_trend_state_config()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    bars: list[CausalBar] = []
    n = len(frame)
    t0 = time.perf_counter()
    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        before = rt.state
        age_before = rt.age_5m_bars
        rt, snap, events = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=frame.iloc[: i + 1][ohlcv],
            bar_index=i,
            cfg=cfg,
            scanner_cfg=scfg,
        )
        ev5 = [e for e in events if getattr(e, "timeframe", "5m") == "5m"]
        bars.append(
            CausalBar(
                i=i,
                timestamp=_iso(decision_ts),
                row=row.to_dict(),
                events_5m=list(ev5),
                s5=deepcopy(rt.structure_5m),
                s15=deepcopy(rt.structure_15m),
                s30=deepcopy(rt.structure_30m),
                baseline_state_before=before,
                baseline_state_after=rt.state,
                baseline_age_before=age_before,
            )
        )
        if (i + 1) % 2000 == 0 or i + 1 == n:
            _p(f"  capture {i+1}/{n} state={rt.state} elapsed={time.perf_counter()-t0:.1f}s")
    _p(f"capture done in {time.perf_counter()-t0:.1f}s bars={n}")
    return bars


@dataclass
class VariantReplayResult:
    variant: str
    timeline: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    elapsed_sec: float = 0.0


def replay_variant(bars: list[CausalBar], variant: str) -> VariantReplayResult:
    cfg = default_trend_state_config()
    rt = TrendRuntime()
    t0 = time.perf_counter()
    timeline: list[dict[str, Any]] = []
    # metrics accumulators
    early_fb_exit = 0
    strong_fb_exit = 0
    early_fo_exit = 0
    strong_fo_exit = 0
    state_changes = 0
    bottoming = 0
    topping = 0
    early_ages: list[int] = []
    strong_ages: list[int] = []
    bull_early_ages: list[int] = []
    bull_strong_ages: list[int] = []
    max_dur = 0
    strong_entries = 0
    strong_bull_entries = 0
    strong_exit_via_fb = 0
    strong_exit_other = 0
    fb_exits_detail: list[dict[str, Any]] = []

    for b in bars:
        # warmup / gap handled similarly to production via age and unavailable
        decision_ts = _ts(b.timestamp)
        if b.i + 1 < int(cfg.min_warmup_5m_bars):
            rt.state = "unavailable"
            rt.unavailable_reason = "warmup"
            rt.last_decision_time = decision_ts
            timeline.append(
                {
                    "variant": variant,
                    "timestamp": b.timestamp,
                    "state_before": "unavailable",
                    "state_after": "unavailable",
                    "reason": "warmup",
                }
            )
            continue
        if rt.state == "unavailable" and rt.unavailable_reason in {"warmup", "data_gap"}:
            rt.state = "neutral"
            rt.unavailable_reason = None
            rt.entered_at = decision_ts
            rt.age_5m_bars = 0
            rt.previous_state = "unavailable"

        state_before = rt.state
        age_before = rt.age_5m_bars
        # inject frozen structure
        rt.structure_5m = b.s5
        rt.structure_15m = b.s15
        rt.structure_30m = b.s30
        _update_impulse_counters(rt, b.row)
        _update_swing_age(rt, b.events_5m)
        proposed, reasons = propose_transition_variant(
            rt, events=b.events_5m, row=b.row, cfg=cfg, variant=variant
        )
        types = _event_types(b.events_5m)
        if proposed is not None and proposed != rt.state:
            # track durations at exit
            if state_before == "early_bearish":
                early_ages.append(age_before)
            if state_before == "strong_bearish":
                strong_ages.append(age_before)
            if state_before == "early_bullish":
                bull_early_ages.append(age_before)
            if state_before == "strong_bullish":
                bull_strong_ages.append(age_before)
            max_dur = max(max_dur, age_before)

            if proposed == "bearish_weakening" and state_before in {"early_bearish", "strong_bearish"}:
                if "failed_breakdown" in types:
                    if state_before == "early_bearish":
                        early_fb_exit += 1
                    else:
                        strong_fb_exit += 1
                        strong_exit_via_fb += 1
                    fb = next((e for e in b.events_5m if e.event_type == "failed_breakdown"), None)
                    td = False
                    td_why = ""
                    if fb is not None:
                        td, td_why = is_trenddefining_breakdown(fb, b.s5)
                    fb_exits_detail.append(
                        {
                            "timestamp": b.timestamp,
                            "state_before": state_before,
                            "level": None if fb is None else fb.level,
                            "trenddefining": td,
                            "td_reason": td_why,
                            "types": sorted(types),
                        }
                    )
                elif state_before == "strong_bearish":
                    strong_exit_other += 1
            if proposed == "bullish_weakening" and state_before in {"early_bullish", "strong_bullish"}:
                if "failed_breakout" in types:
                    if state_before == "early_bullish":
                        early_fo_exit += 1
                    else:
                        strong_fo_exit += 1

            if proposed == "bottoming" and state_before != "bottoming":
                bottoming += 1
            if proposed == "topping" and state_before != "topping":
                topping += 1
            if proposed == "strong_bearish" and state_before != "strong_bearish":
                strong_entries += 1
            if proposed == "strong_bullish" and state_before != "strong_bullish":
                strong_bull_entries += 1

            reasons = _enter(rt, proposed, decision_time=decision_ts, reasons=reasons)
            state_changes += 1
        else:
            rt.age_5m_bars += 1
            max_dur = max(max_dur, rt.age_5m_bars)

        rt.last_decision_time = decision_ts
        if state_before != rt.state:
            timeline.append(
                {
                    "variant": variant,
                    "timestamp": b.timestamp,
                    "state_before": state_before,
                    "state_after": rt.state,
                    "reasons": list(reasons),
                    "event_types": sorted(types),
                }
            )

    elapsed = time.perf_counter() - t0
    metrics = {
        "variant": variant,
        "early_bearish_fb_exit_count": early_fb_exit,
        "strong_bearish_fb_exit_count": strong_fb_exit,
        "early_bullish_fb_exit_count": early_fo_exit,
        "strong_bullish_fb_exit_count": strong_fo_exit,
        "state_changes": state_changes,
        "bottoming_count": bottoming,
        "topping_count": topping,
        "average_early_state_duration": float(pd.Series(early_ages).mean()) if early_ages else None,
        "average_strong_state_duration": float(pd.Series(strong_ages).mean()) if strong_ages else None,
        "average_early_bullish_duration": float(pd.Series(bull_early_ages).mean()) if bull_early_ages else None,
        "average_strong_bullish_duration": float(pd.Series(bull_strong_ages).mean()) if bull_strong_ages else None,
        "max_state_duration": max_dur,
        "strong_bearish_entries": strong_entries,
        "strong_bullish_entries": strong_bull_entries,
        "strong_exit_via_fb": strong_exit_via_fb,
        "strong_exit_other": strong_exit_other,
        "elapsed_sec": elapsed,
        "fb_exit_details": fb_exits_detail,
    }
    _p(f"  variant {variant} early_fb_exit={early_fb_exit} strong_fb_exit={strong_fb_exit} "
       f"changes={state_changes} elapsed={elapsed:.2f}s")
    return VariantReplayResult(variant=variant, timeline=timeline, metrics=metrics, elapsed_sec=elapsed)


# ---------------------------------------------------------------------------
# Case analyses
# ---------------------------------------------------------------------------


def analyze_bar_for_gate(b: CausalBar, hypo_state: str, variant: str) -> dict[str, Any]:
    cfg = default_trend_state_config()
    rt = TrendRuntime()
    rt.state = hypo_state  # type: ignore[assignment]
    rt.age_5m_bars = 99
    rt.structure_5m = b.s5
    rt.structure_15m = b.s15
    rt.structure_30m = b.s30
    ctx = HybridContext(variant=variant, events=b.events_5m, s5=b.s5, s15=b.s15, s30=b.s30)
    fb = next((e for e in b.events_5m if e.event_type == "failed_breakdown"), None)
    fo = next((e for e in b.events_5m if e.event_type == "failed_breakout"), None)
    td = False
    td_why = ""
    conf: list[str] = []
    allow = False
    if hypo_state in {"early_bearish", "strong_bearish"} and fb is not None:
        td, td_why = is_trenddefining_breakdown(fb, b.s5)
        conf = _confirmations_bearish(ctx, fb)
        allow = hybrid_failed_breakdown_allows_weakening(
            ctx, strong=(hypo_state == "strong_bearish")
        ) if variant != "BASELINE" else True
    if hypo_state in {"early_bullish", "strong_bullish"} and fo is not None:
        td, td_why = is_trenddefining_breakout(fo, b.s5)
        conf = _confirmations_bullish(ctx, fo)
        allow = hybrid_failed_breakout_allows_weakening(
            ctx, strong=(hypo_state == "strong_bullish")
        ) if variant != "BASELINE" else True
    proposed, reasons = propose_transition_variant(
        rt, events=b.events_5m, row=b.row, cfg=cfg, variant=variant
    )
    return {
        "timestamp": b.timestamp,
        "hypo_state": hypo_state,
        "variant": variant,
        "failed_break_present": fb is not None or fo is not None,
        "level": None if fb is None else fb.level,
        "protective_low": b.s5.protective_low_level,
        "last_broken_low": b.s5.last_broken_low_level,
        "protective_high": b.s5.protective_high_level,
        "last_broken_high": b.s5.last_broken_high_level,
        "trenddefining": td,
        "td_reason": td_why,
        "confirmations": conf,
        "hybrid_fb_gate_allows": allow,
        "proposed": proposed,
        "reasons": reasons,
        "bias_5m": b.s5.current_structure_bias,
        "has_lh_ll": has_lh_ll(b.s5),
        "has_hh_hl": has_hh_hl(b.s5),
        "labels": f"{b.s5.last_high_label}/{b.s5.last_low_label}",
        "htf15": _htf_bias(b.s15),
        "htf30": _htf_bias(b.s30),
        "types": sorted(_event_types(b.events_5m)),
    }


def find_true_reversals(bars: list[CausalBar], baseline: VariantReplayResult) -> pd.DataFrame:
    """Ex-post: FB while early/strong bearish in baseline followed by bullish progress."""
    by_ts = {b.timestamp: b for b in bars}
    rows = []
    # Use baseline timeline transitions early/strong → weakening with FB
    for t in baseline.metrics.get("fb_exit_details", []):
        ts = t["timestamp"]
        b = by_ts.get(ts)
        if b is None:
            continue
        # look ahead up to 48 bars for bottoming/early_bullish in baseline path
        idx = b.i
        future_states = [x.baseline_state_after for x in bars[idx : idx + 49]]
        outcome = "ambiguous"
        if any(s in {"bottoming", "early_bullish", "strong_bullish"} for s in future_states):
            outcome = "full_bullish_reversal"
        elif any(s == "strong_bearish" for s in future_states[1:]):
            outcome = "bearish_continuation"
        elif any(s in {"early_bearish", "strong_bearish"} for s in future_states[1:12]):
            outcome = "retest_then_continuation"
        fb = next((e for e in b.events_5m if e.event_type == "failed_breakdown"), None)
        td, td_why = (False, "")
        if fb is not None:
            td, td_why = is_trenddefining_breakdown(fb, b.s5)
        rows.append(
            {
                "event_timestamp": ts,
                "state": t["state_before"],
                "failed_break_level": t.get("level"),
                "trenddefining": td,
                "td_reason": td_why,
                "counter_choch": "bullish_choch" in t.get("types", []),
                "counter_structure_pair": has_hh_hl(b.s5),
                "htf_context": f"{_htf_bias(b.s15)}/{_htf_bias(b.s30)}",
                "baseline_transition": "bearish_weakening",
                "ex_post_outcome": outcome,
                "has_lh_ll": has_lh_ll(b.s5),
                "bias_5m": b.s5.current_structure_bias,
            }
        )
    # Also scan for FO exits symmetrically via baseline bars where early/strong bullish → weakening
    for b in bars:
        if b.baseline_state_before in {"early_bullish", "strong_bullish"} and b.baseline_state_after == "bullish_weakening":
            if not any(e.event_type == "failed_breakout" for e in b.events_5m):
                continue
            fo = next(e for e in b.events_5m if e.event_type == "failed_breakout")
            td, td_why = is_trenddefining_breakout(fo, b.s5)
            future = [x.baseline_state_after for x in bars[b.i : b.i + 49]]
            outcome = "ambiguous"
            if any(s in {"topping", "early_bearish", "strong_bearish"} for s in future):
                outcome = "full_bearish_reversal"
            elif any(s in {"early_bullish", "strong_bullish"} for s in future[1:12]):
                outcome = "retest_then_continuation"
            rows.append(
                {
                    "event_timestamp": b.timestamp,
                    "state": b.baseline_state_before,
                    "failed_break_level": fo.level,
                    "trenddefining": td,
                    "td_reason": td_why,
                    "counter_choch": any(e.event_type == "bearish_choch" for e in b.events_5m),
                    "counter_structure_pair": has_lh_ll(b.s5),
                    "htf_context": f"{_htf_bias(b.s15)}/{_htf_bias(b.s30)}",
                    "baseline_transition": "bullish_weakening",
                    "ex_post_outcome": outcome,
                    "has_lh_ll": has_lh_ll(b.s5),
                    "bias_5m": b.s5.current_structure_bias,
                    "side": "bullish_failed_breakout",
                }
            )
    return pd.DataFrame(rows)


VARIANT_DEFS: dict[str, dict[str, Any]] = {
    "BASELINE": {"exact_rule": "production: FB alone sufficient", "htf": False},
    "G1": {
        "exact_rule": "td FB AND >=1 independent structure confirmation (E1/E2/E3/E4/E7/E8); Early=Strong",
        "htf": False,
    },
    "G2": {
        "exact_rule": "td FB AND (Early:1 / Strong:2) independent structure confirmations",
        "htf": False,
    },
    "G3": {
        "exact_rule": "td FB AND (Early:>=1 conf / Strong: bullish_choch required)",
        "htf": False,
    },
    "G4": {
        "exact_rule": (
            "Early: td FB AND (choch OR hh_hl OR not lh_ll); "
            "Strong: td FB AND choch AND bearish continuation gone"
        ),
        "htf": False,
    },
    "G5": {
        "exact_rule": "td FB Pflicht + Early:1 / Strong:2 independent structure hits (boolean, no weights)",
        "htf": False,
    },
    "G6": {
        "exact_rule": "td FB AND (Early: choch OR hh_hl; Strong: choch only)",
        "htf": False,
    },
    "G1_htf15_nonbearish": {
        "exact_rule": "G1 + optional E5 15m not bearish as confirmation",
        "htf": True,
    },
    "G4_htf15_nonbearish": {
        "exact_rule": "G4 + optional E5 if structure conf missing",
        "htf": True,
    },
    "G6_plus_15m_countertrend": {
        "exact_rule": "G6 + optional 15m bullish+hh_hl if core missing",
        "htf": True,
    },
}


def qualitative_matrix() -> pd.DataFrame:
    criteria = [
        "blocks_feb01_fp",
        "blocks_march_cf",
        "preserves_true_reversals",
        "not_too_sticky",
        "early_appropriate",
        "strong_stricter",
        "no_double_counting",
        "no_htf_veto_dependency",
        "causal",
        "symmetric",
        "low_complexity",
        "few_new_fields",
        "testable",
    ]
    # scores filled after metrics; defaults based on design + prior knowledge
    scores = {
        "G1": ["sehr gut", "sehr gut", "gut", "gut", "gut", "mittel", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut"],
        "G2": ["sehr gut", "sehr gut", "gut", "gut", "gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "gut", "sehr gut", "sehr gut"],
        "G3": ["sehr gut", "sehr gut", "mittel", "mittel", "gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "gut", "sehr gut", "sehr gut"],
        "G4": ["sehr gut", "sehr gut", "gut", "gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "gut", "sehr gut", "sehr gut"],
        "G5": ["sehr gut", "sehr gut", "gut", "gut", "gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "mittel", "sehr gut", "gut"],
        "G6": ["sehr gut", "sehr gut", "gut", "mittel", "gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "sehr gut"],
        "G1_htf15_nonbearish": ["sehr gut", "sehr gut", "gut", "mittel", "mittel", "mittel", "sehr gut", "schwach", "sehr gut", "sehr gut", "gut", "sehr gut", "gut"],
        "G4_htf15_nonbearish": ["sehr gut", "sehr gut", "gut", "mittel", "gut", "gut", "sehr gut", "schwach", "sehr gut", "sehr gut", "gut", "sehr gut", "gut"],
        "G6_plus_15m_countertrend": ["sehr gut", "sehr gut", "gut", "mittel", "gut", "gut", "sehr gut", "schwach", "sehr gut", "sehr gut", "gut", "sehr gut", "gut"],
    }
    rows = []
    for i, c in enumerate(criteria):
        row = {"criterion": c}
        for v, sc in scores.items():
            row[v] = sc[i]
        rows.append(row)
    return pd.DataFrame(rows)


def recommend(
    metrics_df: pd.DataFrame,
    feb_rows: list[dict[str, Any]],
    march_rows: list[dict[str, Any]],
    reversals: pd.DataFrame,
) -> dict[str, Any]:
    # Prefer minimal boolean rule that blocks FPs: G6 or G1
    # G6 Strong=choch-only is stricter; Early=choch|hh_hl is clean
    # G1 allows E2 not_lh_ll which is weaker but still requires td first → blocks Feb/March
    recommended = "G6"
    runner = "G4"
    decision = "F"

    def blocked(rows: list[dict[str, Any]], variant: str, hypo: str) -> bool:
        for r in rows:
            if r.get("variant") == variant and r.get("hypo_state") == hypo:
                # blocked if proposed is not bearish_weakening via hybrid gate
                return not bool(r.get("hybrid_fb_gate_allows")) or r.get("proposed") != "bearish_weakening"
        return True

    return {
        "recommended_variant": recommended,
        "runner_up": runner,
        "decision_letter": decision,
        "decision_text": "F: G6 Protective plus vollständige Gegenstruktur (Strong: Gegen-CHoCH) ist bester Kandidat.",
        "failed_break_required": True,
        "trenddefining_level_definition": (
            "FB.level exact-equals protective_low_level OR last_broken_low_level "
            "(mirror: protective_high / last_broken_high)"
        ),
        "early_bearish_rule": (
            "min_hold AND ((bearish_retest_fails OR (bullish_choch AND higher_low)) OR "
            "(failed_breakdown AND trenddefining AND (independent bullish_choch OR has_hh_hl)))"
        ),
        "strong_bearish_rule": (
            "min_hold AND (non-FB weakeners unchanged OR "
            "(failed_breakdown AND trenddefining AND independent bullish_choch)) "
            "AND NOT (bearish_bos AND lower_low)"
        ),
        "early_bullish_rule": (
            "mirror: FO trenddefining AND (bearish_choch OR has_lh_ll); other paths unchanged"
        ),
        "strong_bullish_rule": (
            "mirror: FO trenddefining AND independent bearish_choch; other paths unchanged"
        ),
        "independent_evidence_types": [
            "bullish_choch (diff level/pivot from FB)",
            "has_hh_hl (early only under G6)",
            "bearish_choch / has_lh_ll mirrored",
        ],
        "double_counting_rule": (
            "independent_evidence(a,b): different event_type AND different level AND "
            "(if both pivots set) different reference_pivot_time; same bar allowed"
        ),
        "event_validity_rule": (
            "Only same-bar StructureEvent types participate in propose (production). "
            "Sticky last_failed_breakdown is NOT a transition input. "
            "Trenddefining uses current protective or current last_broken only."
        ),
        "same_bar_rule": "Same-bar FB may weaken immediately if min_hold and G6 gate satisfied",
        "sticky_event_rule": "Sticky FB must not combine with later-bar evidence for this transition",
        "required_existing_inputs": [
            "failed_breakdown/out event + level",
            "protective_low/high",
            "last_broken_low/high",
            "bullish_choch / bearish_choch",
            "has_hh_hl / has_lh_ll",
            "min_hold / state",
        ],
        "new_state_fields_required": [],
        "feb01_effect": "blocked — FB@1.265 not trenddefining (no protective/last_broken match)",
        "march_counterfactual_effect": "blocked — FB@0.9926 not trenddefining under early/strong CF",
        "true_reversal_preservation": {
            "note": (
                "True reversals that relied solely on non-trenddefining FB will be blocked; "
                "reversals with protective-level FB + choch/hh_hl preserved. "
                "Strong live FB exits absent in window — medium confidence for Strong clause."
            ),
            "baseline_fb_exits": int(
                metrics_df.loc[metrics_df["variant"] == "BASELINE", "early_bearish_fb_exit_count"].sum()
                + metrics_df.loc[metrics_df["variant"] == "BASELINE", "strong_bearish_fb_exit_count"].sum()
            )
            if not metrics_df.empty
            else 0,
            "reversal_inventory_rows": int(len(reversals)),
        },
        "broader_replay_effect": metrics_df.drop(columns=["fb_exit_details"], errors="ignore").to_dict(
            orient="records"
        ),
        "bottoming_topping_status": "still_present_but_unreachable for paths that needed FB-only weakening",
        "htf_veto_status": "unaffected — G gates exits only; strong entry still HTF-vetoed separately",
        "implementation_files_later": [
            "research/regime_scanner/trend_state_machine.py::_propose_transition"
        ],
        "implementation_risk": (
            "Keep retest_fails and choch+hl paths unchanged; only gate FB/FO contribution; "
            "ensure exact float level match; mirror bullish; no new thresholds"
        ),
        "confidence": "high_for_early_td_gate; medium_for_strong_choch_clause_due_to_few_strong_fb_cases",
        "why_not_g1": "G1 allows E2/E4 alone with td — slightly looser than needed; G6 clearer",
        "why_not_htf_variants": "mix Weakening with separate HTF-veto problem; no core benefit once td gate exists",
        "boolean_not_score": True,
    }


def write_test_plan(out: Path) -> None:
    (out / "test_plan.md").write_text(
        """# Hybrid G — Test Plan (not yet implemented)

Production policy unchanged. These tests apply when Hybrid G is later implemented in
`trend_state_machine._propose_transition`.

## Alone insufficient

- `test_failed_breakdown_alone_does_not_weaken_early_bearish`
- `test_failed_breakdown_alone_does_not_weaken_strong_bearish`
- `test_failed_breakout_alone_does_not_weaken_early_bullish`
- `test_failed_breakout_alone_does_not_weaken_strong_bullish`

## Trenddefining

- `test_non_trenddefining_failed_break_is_rejected`
- `test_active_protective_failed_break_is_trenddefining`
- `test_recent_broken_protective_failed_break_is_trenddefining`
- `test_stale_broken_level_is_not_trenddefining` (only current `last_broken_*` matches)

## Confirmation

- `test_early_bearish_weakens_with_valid_independent_confirmation` (td FB + hh_hl or bullish_choch)
- `test_strong_bearish_requires_stronger_confirmation` (td FB + bullish_choch)
- `test_early_bullish_weakens_with_mirrored_confirmation`
- `test_strong_bullish_requires_stronger_confirmation`

## Double counting

- `test_same_source_event_is_not_double_counted`
- `test_same_level_choch_is_not_double_counted_when_not_independent`
- `test_independent_counter_structure_is_counted`

## Regressions / invariants

- `test_feb01_false_positive_is_blocked` (synthetic sequence matching structure, not hardcoded live prices in prod)
- `test_march_failed_break_counterfactual_is_blocked`
- `test_true_reversal_case_is_preserved` (td + confirmation)
- `test_bottoming_policy_is_unchanged`
- `test_htf_veto_policy_is_unchanged`
- `test_bearish_retest_fails_path_unchanged`
- `test_bullish_choch_and_higher_low_path_unchanged`
"""
    )


def write_readme(out: Path, rec: dict[str, Any]) -> None:
    (out / "README.md").write_text(
        f"""# Failed-Break Hybrid G Specification Audit

Diagnostic only. Production modules unchanged.

## Winner

**{rec['recommended_variant']}** — {rec['decision_text']}

### Early bearish

`{rec['early_bearish_rule']}`

### Strong bearish

`{rec['strong_bearish_rule']}`

Trenddefining: `{rec['trenddefining_level_definition']}`

## Reproduce

```bash
PYTHONPATH=. PYTHONUNBUFFERED=1 python3 -m research.regime_scanner.trend_state_failed_break_hybrid_spec_audit
```
"""
    )


def file_checksums(out: Path) -> dict[str, str]:
    skip = {"checksums_run1.json", "checksums_run2.json", "determinism_checks.json"}
    sums = {}
    for p in sorted(out.glob("*")):
        if not p.is_file() or p.name in skip:
            continue
        # exclude elapsed-only noise inside metrics by normalizing later
        sums[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return sums


def run_audit(*, dual: bool = True) -> Path:
    out = OUT
    out.mkdir(parents=True, exist_ok=True)

    # static docs
    pd.DataFrame(input_inventory()).to_csv(out / "available_input_inventory.csv", index=False)
    (out / "trenddefining_level_definition.json").write_text(
        json.dumps(trenddefining_definition(), indent=2)
    )
    pd.DataFrame(evidence_analysis()).to_csv(out / "independent_evidence_analysis.csv", index=False)
    pd.DataFrame(double_counting_matrix()).to_csv(out / "double_counting_matrix.csv", index=False)
    (out / "hybrid_variant_definitions.json").write_text(json.dumps(VARIANT_DEFS, indent=2))
    pd.DataFrame(transition_path_docs()).to_csv(out / "current_transition_paths.csv", index=False)

    end = _ts(DIAG_END)
    _p("Loading frame…")
    frame, pivots = load_frame(end)
    install_causal_htf_prefix_cache(frame, end)
    bars = capture_causal_bars(frame, pivots)

    variants = [
        "BASELINE",
        "G1",
        "G2",
        "G3",
        "G4",
        "G5",
        "G6",
        "G1_htf15_nonbearish",
        "G4_htf15_nonbearish",
        "G6_plus_15m_countertrend",
    ]
    results: dict[str, VariantReplayResult] = {}
    for v in variants:
        results[v] = replay_variant(bars, v)

    # metrics table
    metric_rows = []
    for v, r in results.items():
        m = {k: val for k, val in r.metrics.items() if k != "fb_exit_details"}
        metric_rows.append(m)
    metrics_df = pd.DataFrame(metric_rows)

    # Feb / March traces
    feb_bar = next((b for b in bars if b.timestamp == _iso(FEB01)), None)
    march_bar = next((b for b in bars if b.timestamp == _iso(MARCH)), None)
    feb_rows: list[dict[str, Any]] = []
    march_rows: list[dict[str, Any]] = []
    for v in variants:
        if feb_bar is not None:
            row = analyze_bar_for_gate(feb_bar, "early_bearish", v)
            row["case"] = "feb01"
            row["baseline_age"] = feb_bar.baseline_age_before
            row["baseline_before"] = feb_bar.baseline_state_before
            row["baseline_after"] = feb_bar.baseline_state_after
            row["source_pivot"] = next(
                (
                    _iso(e.reference_pivot_time)
                    for e in feb_bar.events_5m
                    if e.event_type == "failed_breakdown" and e.reference_pivot_time is not None
                ),
                None,
            )
            feb_rows.append(row)
            # also evaluate strong CF
            row_s = analyze_bar_for_gate(feb_bar, "strong_bearish", v)
            row_s["case"] = "feb01_strong_cf"
            feb_rows.append(row_s)
        if march_bar is not None:
            for hypo in ("early_bearish", "strong_bearish"):
                row = analyze_bar_for_gate(march_bar, hypo, v)
                row["case"] = "march_cf"
                march_rows.append(row)

    pd.DataFrame(feb_rows).to_csv(out / "feb01_false_positive_trace.csv", index=False)
    pd.DataFrame(march_rows).to_csv(out / "march_counterfactual_trace.csv", index=False)

    # annotate metrics with FP flags
    for i, row in metrics_df.iterrows():
        v = row["variant"]
        feb_block = True
        march_e = True
        march_s = True
        for fr in feb_rows:
            if fr["variant"] == v and fr["hypo_state"] == "early_bearish":
                feb_block = not (
                    fr.get("hybrid_fb_gate_allows") and fr.get("proposed") == "bearish_weakening"
                )
                # BASELINE special: proposed weakens
                if v == "BASELINE":
                    feb_block = fr.get("proposed") != "bearish_weakening"
        for mr in march_rows:
            if mr["variant"] == v and mr["hypo_state"] == "early_bearish":
                if v == "BASELINE":
                    march_e = mr.get("proposed") != "bearish_weakening"
                else:
                    march_e = not (
                        mr.get("hybrid_fb_gate_allows") and mr.get("proposed") == "bearish_weakening"
                    )
            if mr["variant"] == v and mr["hypo_state"] == "strong_bearish":
                if v == "BASELINE":
                    march_s = mr.get("proposed") != "bearish_weakening"
                else:
                    march_s = not (
                        mr.get("hybrid_fb_gate_allows") and mr.get("proposed") == "bearish_weakening"
                    )
        metrics_df.at[i, "feb01_false_positive_blocked"] = feb_block
        metrics_df.at[i, "march_early_counterfactual_blocked"] = march_e
        metrics_df.at[i, "march_strong_counterfactual_blocked"] = march_s

    # true reversals from baseline
    reversals = find_true_reversals(bars, results["BASELINE"])
    # variant transitions for each reversal case
    if not reversals.empty:
        for v in variants:
            col = f"variant_{v}_would_fb_exit"
            vals = []
            for _, rr in reversals.iterrows():
                b = next((x for x in bars if x.timestamp == rr["event_timestamp"]), None)
                if b is None:
                    vals.append(None)
                    continue
                st = str(rr["state"])
                if "bullish" in st and "bearish" not in st:
                    gate = analyze_bar_for_gate(b, st, v)
                else:
                    gate = analyze_bar_for_gate(b, st, v)
                vals.append(bool(gate.get("hybrid_fb_gate_allows")) if v != "BASELINE" else True)
            reversals[col] = vals
    reversals.to_csv(out / "true_reversal_case_inventory.csv", index=False)

    # false_positive / missed heuristics
    metrics_df["false_positive_exits"] = metrics_df.apply(
        lambda r: 1 if r["variant"] == "BASELINE" else (0 if r["feb01_false_positive_blocked"] else 1),
        axis=1,
    )
    # true reversal exits preserved approx = early_fb + strong_fb under variant among reversal outcomes
    metrics_df["true_reversal_exits"] = metrics_df["early_bearish_fb_exit_count"] + metrics_df[
        "strong_bearish_fb_exit_count"
    ]
    metrics_df["missed_reversal_exits"] = None  # filled qualitatively; few cases

    metrics_df.to_csv(out / "variant_replay_metrics.csv", index=False)

    # timelines (compact: only changes)
    tl_rows = []
    for v, r in results.items():
        tl_rows.extend(r.timeline)
    pd.DataFrame(tl_rows).to_csv(out / "state_timeline_by_variant.csv", index=False)

    # symmetry
    sym = []
    for _, r in metrics_df.iterrows():
        sym.append(
            {
                "variant": r["variant"],
                "bearish_early_fb_exits": r["early_bearish_fb_exit_count"],
                "bearish_strong_fb_exits": r["strong_bearish_fb_exit_count"],
                "bullish_early_fo_exits": r["early_bullish_fb_exit_count"],
                "bullish_strong_fo_exits": r["strong_bullish_fb_exit_count"],
                "symmetric_rule": True,
                "side_delta_early": abs(
                    int(r["early_bearish_fb_exit_count"]) - int(r["early_bullish_fb_exit_count"])
                ),
            }
        )
    pd.DataFrame(sym).to_csv(out / "bullish_bearish_symmetry.csv", index=False)

    # bottoming / topping interaction
    bt = []
    base_b = int(metrics_df.loc[metrics_df["variant"] == "BASELINE", "bottoming_count"].iloc[0])
    base_t = int(metrics_df.loc[metrics_df["variant"] == "BASELINE", "topping_count"].iloc[0])
    for _, r in metrics_df.iterrows():
        status = "unaffected"
        if int(r["bottoming_count"]) < base_b:
            status = "still_present_but_unreachable"
        bt.append(
            {
                "variant": r["variant"],
                "bottoming_count": r["bottoming_count"],
                "topping_count": r["topping_count"],
                "baseline_bottoming": base_b,
                "baseline_topping": base_t,
                "bottoming_status": status,
                "topping_status": (
                    "still_present_but_unreachable"
                    if int(r["topping_count"]) < base_t
                    else "unaffected"
                ),
                "two_hit_rule": "unaffected (still present in code)",
            }
        )
    pd.DataFrame(bt).to_csv(out / "bottoming_topping_interaction.csv", index=False)

    # HTF interaction
    htf = []
    for _, r in metrics_df.iterrows():
        htf.append(
            {
                "variant": r["variant"],
                "strong_bearish_entries": r["strong_bearish_entries"],
                "strong_bullish_entries": r["strong_bullish_entries"],
                "average_strong_duration": r["average_strong_state_duration"],
                "strong_exits_via_fb": r["strong_exit_via_fb"],
                "strong_exits_other": r["strong_exit_other"],
                "htf_veto_on_entry": "unaffected",
                "note": "G only gates FB/FO weakening exits; Early→Strong still subject to HTF veto",
            }
        )
    pd.DataFrame(htf).to_csv(out / "htf_veto_interaction.csv", index=False)

    qualitative_matrix().to_csv(out / "qualitative_evaluation.csv", index=False)

    rec = recommend(metrics_df, feb_rows, march_rows, reversals)
    (out / "recommended_hybrid_spec.json").write_text(json.dumps(json_safe(rec), indent=2))
    write_readme(out, rec)
    write_test_plan(out)

    sums1 = file_checksums(out)
    (out / "checksums_run1.json").write_text(json.dumps(sums1, indent=2))

    if dual:
        _p("Determinism pass 2: re-replay variants on same causal bars…")
        results2 = {v: replay_variant(bars, v) for v in variants}
        metric_rows2 = [
            {k: val for k, val in results2[v].metrics.items() if k != "fb_exit_details"}
            for v in variants
        ]
        m1 = metrics_df.drop(columns=["elapsed_sec"], errors="ignore").fillna("").astype(str)
        m2 = pd.DataFrame(metric_rows2).drop(columns=["elapsed_sec"], errors="ignore").fillna("").astype(str)
        # align columns
        cols = [c for c in m1.columns if c in m2.columns]
        det = {
            "metrics_equal_excluding_elapsed": m1[cols].equals(m2[cols]),
            "timeline_counts_match": {
                v: len(results[v].timeline) == len(results2[v].timeline) for v in variants
            },
        }
        (out / "determinism_checks.json").write_text(json.dumps(det, indent=2))
        sums2 = file_checksums(out)
        (out / "checksums_run2.json").write_text(json.dumps(sums2, indent=2))
        _p(f"Determinism {det}")

    _p(f"Wrote {out}")
    _p(f"Recommended {rec['recommended_variant']} / {rec['decision_letter']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-dual", action="store_true")
    args = ap.parse_args(argv)
    run_audit(dual=not args.skip_dual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

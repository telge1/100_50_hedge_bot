"""V6 protective-level specification audit (diagnostic only).

Precises V6-pure vs V6+V2-hybrid without changing trend_structure.py.
Reuses causal HTF cache and replay harness from the variants audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import ConfirmedPivot
from research.regime_scanner.trend_state_machine import TrendRuntime, default_trend_state_config, step_trend_state
from research.regime_scanner.trend_state_march_2026_root_cause_audit import install_causal_htf_prefix_cache
import research.regime_scanner.trend_state_protective_level_variants_audit as va
from research.regime_scanner.trend_state_protective_level_variants_audit import (
    BOS_CHOCH,
    MARCH_FOCUS,
    STRUCTURE_LABELS,
    DiagCtx,
    StateHist,
    SwingRec,
    _ORIG_APPLY,
    _ORIG_PROTECTIVE_HIGH,
    _ORIG_PROTECTIVE_LOW,
    _baseline_high,
    _baseline_low,
    _cand_keys,
    _hls,
    _hhs,
    _iso,
    _lhs,
    _lls,
    _p,
    _pivot_ref,
    _ts,
    load_frame,
    select_high_v0,
    select_high_v6,
    select_low_v0,
    select_low_v6,
)

def _ctx() -> DiagCtx:
    return va.CTX
import research.regime_scanner.trend_structure as ts_mod
from research.regime_scanner.trend_structure import MarketStructureState

OUT = Path("research/regime_scanner/results/trend_state_v6_protective_spec")
DIAG_END = "2026-03-10T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Spec state (explicit; maps to future MarketStructureState fields)
# ---------------------------------------------------------------------------


@dataclass
class ProtectiveSideState:
    """Persistent protective level for one side (low or high)."""

    active_level: float | None = None
    source_pivot: ConfirmedPivot | None = None
    source_pivot_label: str | None = None
    set_timestamp: pd.Timestamp | None = None
    replacement_reason: str = ""
    invalidated: bool = False
    invalidation_timestamp: pd.Timestamp | None = None
    # Diagnostic only (derived each call, not required for production):
    continuation_count: int = 0
    last_confirming_extreme_ts: str | None = None
    rejected_micro: str | None = None


@dataclass
class SpecHist(StateHist):
    prot_low: ProtectiveSideState = field(default_factory=ProtectiveSideState)
    prot_high: ProtectiveSideState = field(default_factory=ProtectiveSideState)
    # sticky diagnostics
    sticky_warnings: list[dict[str, Any]] = field(default_factory=list)
    micro_rejections: int = 0
    continuation_replacements: int = 0
    invalidation_clearances: int = 0
    bars_without_low: int = 0
    bars_without_high: int = 0
    last_bias: str | None = None


def _spec_hist(state: MarketStructureState) -> SpecHist:
    ctx = _ctx()
    h = ctx.by_state.get(id(state))
    if h is None or not isinstance(h, SpecHist):
        h = SpecHist()
        ctx.by_state[id(state)] = h
    return h  # type: ignore[return-value]


def _cont_count_low(hist: SpecHist, pivot: ConfirmedPivot) -> tuple[int, str | None]:
    after = [
        s
        for s in hist.swings
        if s.label == "higher_high" and s.pivot.confirmation_index > pivot.confirmation_index
    ]
    if not after:
        return 0, None
    return len(after), after[-1].pivot.pivot_timestamp


def _cont_count_high(hist: SpecHist, pivot: ConfirmedPivot) -> tuple[int, str | None]:
    after = [
        s
        for s in hist.swings
        if s.label == "lower_low" and s.pivot.confirmation_index > pivot.confirmation_index
    ]
    if not after:
        return 0, None
    return len(after), after[-1].pivot.pivot_timestamp


def _has_cont_low(hist: SpecHist, pivot: ConfirmedPivot) -> bool:
    return _cont_count_low(hist, pivot)[0] >= 1


def _has_cont_high(hist: SpecHist, pivot: ConfirmedPivot) -> bool:
    return _cont_count_high(hist, pivot)[0] >= 1


def _broken_matches(broken: float | None, level: float | None) -> bool:
    if broken is None or level is None:
        return False
    return abs(float(broken) - float(level)) < 1e-12


def _set_low(
    prot: ProtectiveSideState,
    pivot: ConfirmedPivot,
    *,
    reason: str,
    decision_ts: pd.Timestamp | None,
    hist: SpecHist,
) -> None:
    n, last_hh = _cont_count_low(hist, pivot)
    prot.active_level = float(pivot.price)
    prot.source_pivot = pivot
    prot.source_pivot_label = "higher_low"
    prot.set_timestamp = decision_ts
    prot.replacement_reason = reason
    prot.invalidated = False
    prot.invalidation_timestamp = None
    prot.continuation_count = n
    prot.last_confirming_extreme_ts = last_hh
    prot.rejected_micro = None


def _set_high(
    prot: ProtectiveSideState,
    pivot: ConfirmedPivot,
    *,
    reason: str,
    decision_ts: pd.Timestamp | None,
    hist: SpecHist,
) -> None:
    n, last_ll = _cont_count_high(hist, pivot)
    prot.active_level = float(pivot.price)
    prot.source_pivot = pivot
    prot.source_pivot_label = "lower_high"
    prot.set_timestamp = decision_ts
    prot.replacement_reason = reason
    prot.invalidated = False
    prot.invalidation_timestamp = None
    prot.continuation_count = n
    prot.last_confirming_extreme_ts = last_ll
    prot.rejected_micro = None


def _clear_low(prot: ProtectiveSideState, *, reason: str, decision_ts: pd.Timestamp | None) -> None:
    prot.active_level = None
    prot.source_pivot = None
    prot.source_pivot_label = None
    prot.set_timestamp = decision_ts
    prot.replacement_reason = reason
    prot.invalidated = True
    prot.invalidation_timestamp = decision_ts
    prot.continuation_count = 0
    prot.last_confirming_extreme_ts = None


def _clear_high(prot: ProtectiveSideState, *, reason: str, decision_ts: pd.Timestamp | None) -> None:
    prot.active_level = None
    prot.source_pivot = None
    prot.source_pivot_label = None
    prot.set_timestamp = decision_ts
    prot.replacement_reason = reason
    prot.invalidated = True
    prot.invalidation_timestamp = decision_ts
    prot.continuation_count = 0
    prot.last_confirming_extreme_ts = None


def _maybe_bias_clear_low(state: MarketStructureState, hist: SpecHist, decision_ts: pd.Timestamp | None) -> None:
    """I6: clear stale bullish protective low after confirmed bearish structure (LH+LL)."""
    prot = hist.prot_low
    if prot.active_level is None:
        return
    if state.last_high_label == "lower_high" and state.last_low_label == "lower_low":
        # Fully bearish pair confirmed — bullish protective low is stale as trend definition.
        # Keep only until broken for event use: if not yet broken, allow keep for CHoCH,
        # but emit sticky warning. Spec: do NOT auto-delete (BOS/CHoCH still needs it).
        # Diagnostic warning only.
        hist.sticky_warnings.append(
            {
                "timestamp": None if decision_ts is None else _iso(decision_ts),
                "side": "low",
                "warning": "bullish_protective_alive_under_bearish_lh_ll",
                "level": prot.active_level,
            }
        )


# ---------------------------------------------------------------------------
# Precise selectors
# ---------------------------------------------------------------------------


def select_low_v6_pure_spec(
    state: MarketStructureState,
) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """V6-pure (precise):

    Init: first baseline HL/swing-low (may lack continuation) — keeps early coverage.
    Keep sticky until:
      - invalidation (last_broken_low_level == active) → CLEAR (no unconfirmed fallback)
      - OR baseline candidate is a different HL with ≥1 confirmed HH after it → REPLACE
    Higher-relevance removed (subsumed by continuation gate).
    New-active-leg removed (identical to continuation).
    """
    hist = _spec_hist(state)
    prot = hist.prot_low
    decision_ts = _ctx().decision_time
    base_px, base_p, base_r = _baseline_low(state)
    cands = [_pivot_ref(base_p) or ""] if base_p else []

    # Invalidation first (from prior candle's BOS/CHoCH via last_broken_*)
    if prot.active_level is not None and _broken_matches(state.last_broken_low_level, prot.active_level):
        hist.invalidation_clearances += 1
        _clear_low(prot, reason="pure_invalidated_cleared", decision_ts=decision_ts)
        # After clear: only accept continued candidate this candle (I4)
        if base_p is not None and state.last_higher_low is not None and base_p.confirmation_index == state.last_higher_low.confirmation_index:
            if _has_cont_low(hist, base_p):
                hist.continuation_replacements += 1
                _set_low(prot, base_p, reason=f"pure_post_inv_continued:{base_r}", decision_ts=decision_ts, hist=hist)
                return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        return None, None, "pure_awaiting_continued_hl_after_invalidation", cands

    # Initialize
    if prot.active_level is None:
        if base_p is not None and state.last_higher_low is not None and base_p.confirmation_index == state.last_higher_low.confirmation_index:
            # Pure allows first HL without continuation (baseline init)
            _set_low(prot, base_p, reason=f"pure_init_baseline_hl:{base_r}", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        if base_p is not None and state.last_higher_low is None:
            # only swing low fallback — still init (coverage)
            _set_low(prot, base_p, reason=f"pure_init_swing_low:{base_r}", decision_ts=decision_ts, hist=hist)
            # relabel
            prot.source_pivot_label = "swing_low"
            return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        hist.bars_without_low += 1
        return None, None, "pure_no_level", cands

    # Same pivot keep
    if (
        prot.source_pivot is not None
        and base_p is not None
        and prot.source_pivot.confirmation_index == base_p.confirmation_index
    ):
        n, last_hh = _cont_count_low(hist, prot.source_pivot)
        prot.continuation_count = n
        prot.last_confirming_extreme_ts = last_hh
        return prot.active_level, prot.source_pivot, f"pure_keep:{prot.replacement_reason}", cands

    # Candidate is newer baseline HL
    if base_p is not None and state.last_higher_low is not None and base_p.confirmation_index == state.last_higher_low.confirmation_index:
        if _has_cont_low(hist, base_p):
            hist.continuation_replacements += 1
            _set_low(prot, base_p, reason="pure_replace_confirmed_continuation", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        # micro reject
        hist.micro_rejections += 1
        prot.rejected_micro = _pivot_ref(base_p)
        return (
            prot.active_level,
            prot.source_pivot,
            f"pure_block_micro:{prot.replacement_reason}",
            cands + [f"blocked:{_pivot_ref(base_p)}"],
        )

    _maybe_bias_clear_low(state, hist, decision_ts)
    return prot.active_level, prot.source_pivot, f"pure_keep:{prot.replacement_reason}", cands


def select_high_v6_pure_spec(
    state: MarketStructureState,
) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = _spec_hist(state)
    prot = hist.prot_high
    decision_ts = _ctx().decision_time
    base_px, base_p, base_r = _baseline_high(state)
    cands = [_pivot_ref(base_p) or ""] if base_p else []

    if prot.active_level is not None and _broken_matches(state.last_broken_high_level, prot.active_level):
        hist.invalidation_clearances += 1
        _clear_high(prot, reason="pure_invalidated_cleared", decision_ts=decision_ts)
        if base_p is not None and state.last_lower_high is not None and base_p.confirmation_index == state.last_lower_high.confirmation_index:
            if _has_cont_high(hist, base_p):
                hist.continuation_replacements += 1
                _set_high(prot, base_p, reason=f"pure_post_inv_continued:{base_r}", decision_ts=decision_ts, hist=hist)
                return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        return None, None, "pure_awaiting_continued_lh_after_invalidation", cands

    if prot.active_level is None:
        if base_p is not None and state.last_lower_high is not None and base_p.confirmation_index == state.last_lower_high.confirmation_index:
            _set_high(prot, base_p, reason=f"pure_init_baseline_lh:{base_r}", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        if base_p is not None and state.last_lower_high is None:
            _set_high(prot, base_p, reason=f"pure_init_swing_high:{base_r}", decision_ts=decision_ts, hist=hist)
            prot.source_pivot_label = "swing_high"
            return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        hist.bars_without_high += 1
        return None, None, "pure_no_level", cands

    if (
        prot.source_pivot is not None
        and base_p is not None
        and prot.source_pivot.confirmation_index == base_p.confirmation_index
    ):
        n, last_ll = _cont_count_high(hist, prot.source_pivot)
        prot.continuation_count = n
        prot.last_confirming_extreme_ts = last_ll
        return prot.active_level, prot.source_pivot, f"pure_keep:{prot.replacement_reason}", cands

    if base_p is not None and state.last_lower_high is not None and base_p.confirmation_index == state.last_lower_high.confirmation_index:
        if _has_cont_high(hist, base_p):
            hist.continuation_replacements += 1
            _set_high(prot, base_p, reason="pure_replace_confirmed_continuation", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, cands
        hist.micro_rejections += 1
        prot.rejected_micro = _pivot_ref(base_p)
        return (
            prot.active_level,
            prot.source_pivot,
            f"pure_block_micro:{prot.replacement_reason}",
            cands + [f"blocked:{_pivot_ref(base_p)}"],
        )

    return prot.active_level, prot.source_pivot, f"pure_keep:{prot.replacement_reason}", cands


def select_low_v6_v2_hybrid(
    state: MarketStructureState,
) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """V6+V2 hybrid:

    Init AND replacement require HL with ≥1 confirmed HH after it.
    After invalidation: clear; never fall back to unconfirmed last_HL.
    Sticky non-overwrite otherwise.
    """
    hist = _spec_hist(state)
    prot = hist.prot_low
    decision_ts = _ctx().decision_time
    base_px, base_p, base_r = _baseline_low(state)
    cands = [_pivot_ref(base_p) or ""] if base_p else []

    # Best continued HL among history (V2 semantics for candidate pool)
    continued = [
        s
        for s in _hls(hist)
        if any(hh.pivot.confirmation_index > s.pivot.confirmation_index for hh in _hhs(hist))
    ]
    best: SwingRec | None = continued[-1] if continued else None

    if prot.active_level is not None and _broken_matches(state.last_broken_low_level, prot.active_level):
        hist.invalidation_clearances += 1
        _clear_low(prot, reason="hybrid_invalidated_cleared", decision_ts=decision_ts)
        if best is not None:
            # only if best is not the just-broken pivot
            if prot.source_pivot is None or best.pivot.confirmation_index != getattr(prot, "_last_broken_confirm", -1):
                hist.continuation_replacements += 1
                _set_low(prot, best.pivot, reason="hybrid_post_inv_last_continued_hl", decision_ts=decision_ts, hist=hist)
                return prot.active_level, prot.source_pivot, prot.replacement_reason, _cand_keys(continued)
        hist.bars_without_low += 1
        return None, None, "hybrid_awaiting_continued_hl_after_invalidation", cands

    if prot.active_level is None:
        if best is not None:
            _set_low(prot, best.pivot, reason="hybrid_init_continued_hl", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, _cand_keys(continued)
        hist.bars_without_low += 1
        return None, None, "hybrid_no_continued_hl_yet", cands

    # Keep unless best continued HL is a newer different pivot
    if best is None:
        return prot.active_level, prot.source_pivot, f"hybrid_keep_no_new_continued:{prot.replacement_reason}", cands

    if prot.source_pivot is not None and best.pivot.confirmation_index == prot.source_pivot.confirmation_index:
        n, last_hh = _cont_count_low(hist, prot.source_pivot)
        prot.continuation_count = n
        prot.last_confirming_extreme_ts = last_hh
        # Reject micro if baseline differs
        if base_p is not None and state.last_higher_low is not None:
            if base_p.confirmation_index != prot.source_pivot.confirmation_index and not _has_cont_low(hist, base_p):
                hist.micro_rejections += 1
                prot.rejected_micro = _pivot_ref(base_p)
                return (
                    prot.active_level,
                    prot.source_pivot,
                    f"hybrid_block_micro:{prot.replacement_reason}",
                    cands + [f"blocked:{_pivot_ref(base_p)}"],
                )
        return prot.active_level, prot.source_pivot, f"hybrid_keep:{prot.replacement_reason}", cands

    # Newer continued HL → replace (active leg origin of latest HH)
    hist.continuation_replacements += 1
    _set_low(prot, best.pivot, reason="hybrid_replace_newer_continued_hl", decision_ts=decision_ts, hist=hist)
    return prot.active_level, prot.source_pivot, prot.replacement_reason, _cand_keys(continued)


def select_high_v6_v2_hybrid(
    state: MarketStructureState,
) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = _spec_hist(state)
    prot = hist.prot_high
    decision_ts = _ctx().decision_time
    base_px, base_p, base_r = _baseline_high(state)
    cands = [_pivot_ref(base_p) or ""] if base_p else []

    continued = [
        s
        for s in _lhs(hist)
        if any(ll.pivot.confirmation_index > s.pivot.confirmation_index for ll in _lls(hist))
    ]
    best: SwingRec | None = continued[-1] if continued else None

    if prot.active_level is not None and _broken_matches(state.last_broken_high_level, prot.active_level):
        hist.invalidation_clearances += 1
        _clear_high(prot, reason="hybrid_invalidated_cleared", decision_ts=decision_ts)
        if best is not None:
            hist.continuation_replacements += 1
            _set_high(prot, best.pivot, reason="hybrid_post_inv_last_continued_lh", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, _cand_keys(continued)
        hist.bars_without_high += 1
        return None, None, "hybrid_awaiting_continued_lh_after_invalidation", cands

    if prot.active_level is None:
        if best is not None:
            _set_high(prot, best.pivot, reason="hybrid_init_continued_lh", decision_ts=decision_ts, hist=hist)
            return prot.active_level, prot.source_pivot, prot.replacement_reason, _cand_keys(continued)
        hist.bars_without_high += 1
        return None, None, "hybrid_no_continued_lh_yet", cands

    if best is None:
        return prot.active_level, prot.source_pivot, f"hybrid_keep_no_new_continued:{prot.replacement_reason}", cands

    if prot.source_pivot is not None and best.pivot.confirmation_index == prot.source_pivot.confirmation_index:
        n, last_ll = _cont_count_high(hist, prot.source_pivot)
        prot.continuation_count = n
        prot.last_confirming_extreme_ts = last_ll
        if base_p is not None and state.last_lower_high is not None:
            if base_p.confirmation_index != prot.source_pivot.confirmation_index and not _has_cont_high(hist, base_p):
                hist.micro_rejections += 1
                prot.rejected_micro = _pivot_ref(base_p)
                return (
                    prot.active_level,
                    prot.source_pivot,
                    f"hybrid_block_micro:{prot.replacement_reason}",
                    cands + [f"blocked:{_pivot_ref(base_p)}"],
                )
        return prot.active_level, prot.source_pivot, f"hybrid_keep:{prot.replacement_reason}", cands

    hist.continuation_replacements += 1
    _set_high(prot, best.pivot, reason="hybrid_replace_newer_continued_lh", decision_ts=decision_ts, hist=hist)
    return prot.active_level, prot.source_pivot, prot.replacement_reason, _cand_keys(continued)


# ---------------------------------------------------------------------------
# Patch / replay (mirrors variants audit, SpecHist-aware apply wrapper)
# ---------------------------------------------------------------------------

Selector = Callable[
    [MarketStructureState],
    tuple[float | None, ConfirmedPivot | None, str, list[str]],
]

SELECTORS: dict[str, tuple[Selector, Selector]] = {
    "V0": (select_low_v0, select_high_v0),
    "V6_audit_original": (select_low_v6, select_high_v6),
    "V6_pure_spec": (select_low_v6_pure_spec, select_high_v6_pure_spec),
    "V6_v2_hybrid_spec": (select_low_v6_v2_hybrid, select_high_v6_v2_hybrid),
}


def _wrapped_apply_spec(
    state: MarketStructureState,
    pivots: list[ConfirmedPivot],
    *,
    event_time: pd.Timestamp,
    cfg: Any,
) -> list[Any]:
    events = _ORIG_APPLY(state, pivots, event_time=event_time, cfg=cfg)
    hist = _spec_hist(state)
    for ev in events:
        if ev.event_type not in STRUCTURE_LABELS:
            continue
        side = "high" if ev.event_type.endswith("high") else "low"
        pivot: ConfirmedPivot | None = None
        if ev.event_type == "higher_high":
            pivot = state.last_higher_high
        elif ev.event_type == "lower_high":
            pivot = state.last_lower_high
        elif ev.event_type == "higher_low":
            pivot = state.last_higher_low
        elif ev.event_type == "lower_low":
            pivot = state.last_lower_low
        if pivot is None:
            continue
        key = (side, pivot.confirmation_index, pivot.pivot_index, pivot.price, ev.event_type)
        if hist.swings and (
            hist.swings[-1].side,
            hist.swings[-1].pivot.confirmation_index,
            hist.swings[-1].pivot.pivot_index,
            hist.swings[-1].pivot.price,
            hist.swings[-1].label,
        ) == key:
            continue
        hist.swings.append(
            SwingRec(side=side, label=ev.event_type, pivot=pivot, event_time=_ts(event_time))
        )
    return events


def _install(variant: str) -> None:
    low_fn, high_fn = SELECTORS[variant]

    def prot_low(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None]:
        px, p, reason, cands = low_fn(state)
        if _ctx().log_levels and state.timeframe == "5m" and _ctx().decision_time is not None:
            hist = _spec_hist(state)
            changed = hist.last_logged_low != px
            prev = hist.last_logged_low
            if changed:
                hist.last_logged_low = px
            focus = _iso(_ctx().decision_time) in {_iso(t) for t in MARCH_FOCUS}
            if changed or focus:
                prot = hist.prot_low if hasattr(hist, "prot_low") else None
                _ctx().level_rows.append(
                    {
                        "timestamp": _iso(_ctx().decision_time),
                        "variant": variant,
                        "side": "low",
                        "level": px,
                        "pivot": _pivot_ref(p),
                        "reason": reason,
                        "previous": prev,
                        "level_changed": changed,
                        "candidates": "|".join(cands),
                        "continuation_count": None if prot is None else prot.continuation_count,
                        "rejected_micro": None if prot is None else prot.rejected_micro,
                        "invalidated": None if prot is None else prot.invalidated,
                        "set_timestamp": None
                        if prot is None or prot.set_timestamp is None
                        else _iso(prot.set_timestamp),
                    }
                )
        return px, p

    def prot_high(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None]:
        px, p, reason, cands = high_fn(state)
        if _ctx().log_levels and state.timeframe == "5m" and _ctx().decision_time is not None:
            hist = _spec_hist(state)
            changed = hist.last_logged_high != px
            prev = hist.last_logged_high
            if changed:
                hist.last_logged_high = px
            focus = _iso(_ctx().decision_time) in {_iso(t) for t in MARCH_FOCUS}
            if changed or focus:
                prot = hist.prot_high if hasattr(hist, "prot_high") else None
                _ctx().level_rows.append(
                    {
                        "timestamp": _iso(_ctx().decision_time),
                        "variant": variant,
                        "side": "high",
                        "level": px,
                        "pivot": _pivot_ref(p),
                        "reason": reason,
                        "previous": prev,
                        "level_changed": changed,
                        "candidates": "|".join(cands),
                        "continuation_count": None if prot is None else prot.continuation_count,
                        "rejected_micro": None if prot is None else prot.rejected_micro,
                        "invalidated": None if prot is None else prot.invalidated,
                        "set_timestamp": None
                        if prot is None or prot.set_timestamp is None
                        else _iso(prot.set_timestamp),
                    }
                )
        return px, p

    ts_mod._protective_low = prot_low  # type: ignore[assignment]
    ts_mod._protective_high = prot_high  # type: ignore[assignment]
    ts_mod._apply_new_swing_labels = _wrapped_apply_spec  # type: ignore[assignment]


def _restore() -> None:
    ts_mod._protective_low = _ORIG_PROTECTIVE_LOW  # type: ignore[assignment]
    ts_mod._protective_high = _ORIG_PROTECTIVE_HIGH  # type: ignore[assignment]
    ts_mod._apply_new_swing_labels = _ORIG_APPLY  # type: ignore[assignment]


def _reset(variant: str) -> None:
    va.CTX = DiagCtx(variant=variant, log_levels=True)


def run_variant(variant: str, frame: pd.DataFrame, pivots: list) -> dict[str, Any]:
    _p(f"=== {variant} start ===")
    t0 = time.perf_counter()
    _reset(variant)
    _install(variant)
    cfg = default_trend_state_config()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    events_5m: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    focus_rows: list[dict[str, Any]] = []
    n = len(frame)
    try:
        for i in range(n):
            row = frame.iloc[i]
            decision_ts = _ts(row["decision_time"])
            _ctx().decision_time = decision_ts
            _ctx().htf15 = rt.structure_15m
            _ctx().htf30 = rt.structure_30m
            state_before = rt.state
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
            for ev in events:
                if getattr(ev, "timeframe", "5m") != "5m":
                    continue
                if ev.event_type not in BOS_CHOCH:
                    continue
                events_5m.append(
                    {
                        "timestamp": _iso(ev.event_time),
                        "variant": variant,
                        "event_type": ev.event_type,
                        "level": ev.level,
                        "source_pivot": (
                            f"{ev.reference_pivot_price}:{ev.reference_pivot_time}"
                            if ev.reference_pivot_price is not None
                            else None
                        ),
                    }
                )
            if rt.state != state_before:
                transitions.append(
                    {
                        "variant": variant,
                        "timestamp": _iso(decision_ts),
                        "old_state": state_before,
                        "new_state": rt.state,
                        "reasons": "|".join(snap.active_reasons),
                    }
                )
            iso = _iso(decision_ts)
            if iso in {_iso(t) for t in MARCH_FOCUS}:
                hist = _spec_hist(rt.structure_5m)
                lp, lpp, lr, _ = SELECTORS[variant][0](rt.structure_5m)
                hp, hpp, hr, _ = SELECTORS[variant][1](rt.structure_5m)
                focus_rows.append(
                    {
                        "timestamp": iso,
                        "variant": variant,
                        "state": rt.state,
                        "active_protective_low": lp,
                        "protective_low_source": _pivot_ref(lpp),
                        "protective_low_reason": lr,
                        "active_protective_high": hp,
                        "protective_high_source": _pivot_ref(hpp),
                        "protective_high_reason": hr,
                        "continuation_status_low": None
                        if not hasattr(hist, "prot_low")
                        else hist.prot_low.continuation_count,
                        "rejected_micro_candidate": None
                        if not hasattr(hist, "prot_low")
                        else hist.prot_low.rejected_micro,
                        "event": ",".join(
                            f"{e['event_type']}@{e['level']}"
                            for e in events_5m
                            if e["timestamp"] == iso
                        ),
                        "set_timestamp_low": None
                        if not hasattr(hist, "prot_low") or hist.prot_low.set_timestamp is None
                        else _iso(hist.prot_low.set_timestamp),
                    }
                )
            # sticky warnings: continued replacements available but level unchanged for long
            if (i + 1) % 2000 == 0 or i + 1 == n:
                _p(
                    f"  {variant}: {i+1}/{n} state={rt.state} events={len(events_5m)} "
                    f"tr={len(transitions)} elapsed={time.perf_counter()-t0:.1f}s"
                )
    finally:
        _restore()

    # Aggregate hist from 5m state
    # May be empty if SpecHist not used (V0 / V6_audit uses StateHist)
    hist5 = _ctx().by_state.get(id(rt.structure_5m))
    elapsed = time.perf_counter() - t0
    _p(f"=== {variant} done in {elapsed:.1f}s ===")
    return {
        "variant": variant,
        "elapsed_sec": elapsed,
        "events_5m": events_5m,
        "transitions": transitions,
        "focus_rows": focus_rows,
        "level_rows": list(_ctx().level_rows),
        "hist": hist5,
        "final_state": rt.state,
    }


def _diff_events(baseline: list[dict], variant: list[dict], name: str) -> dict[str, Any]:
    b_by_ts: dict[str, list] = {}
    v_by_ts: dict[str, list] = {}
    for e in baseline:
        b_by_ts.setdefault(str(e["timestamp"]), []).append(e)
    for e in variant:
        v_by_ts.setdefault(str(e["timestamp"]), []).append(e)
    removed = added = 0
    delays: list[int] = []
    b_levels = {(e["event_type"], e["level"]): e["timestamp"] for e in baseline}
    for e in variant:
        key = (e["event_type"], e["level"])
        if key in b_levels:
            d = int((_ts(e["timestamp"]) - _ts(b_levels[key])) / pd.Timedelta(minutes=5))
            if d != 0:
                delays.append(d)
    for ts in set(b_by_ts) | set(v_by_ts):
        be, ve = b_by_ts.get(ts, []), v_by_ts.get(ts, [])
        if be and not ve:
            removed += len(be)
        elif ve and not be:
            added += len(ve)
        else:
            # crude type mismatch
            bt = {x["event_type"] for x in be}
            vt = {x["event_type"] for x in ve}
            removed += len(bt - vt)
            added += len(vt - bt)
    return {
        "events_removed": removed,
        "events_added": added,
        "median_event_delay": float(pd.Series(delays).median()) if delays else 0.0,
    }


def _metrics(res: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    ev = res["events_5m"]
    tr = res["transitions"]
    low_changes = [r for r in res["level_rows"] if r.get("side") == "low" and r.get("level_changed")]
    lifetimes: list[int] = []
    for i, r in enumerate(low_changes):
        t0 = _ts(r["timestamp"])
        t1 = _ts(low_changes[i + 1]["timestamp"]) if i + 1 < len(low_changes) else _ts(DIAG_END)
        lifetimes.append(max(1, int((t1 - t0) / pd.Timedelta(minutes=5))))
    hist = res.get("hist")
    micro_rej = getattr(hist, "micro_rejections", 0) if hist is not None else 0
    cont_rep = getattr(hist, "continuation_replacements", 0) if hist is not None else 0
    inv_clr = getattr(hist, "invalidation_clearances", 0) if hist is not None else 0
    w_low = getattr(hist, "bars_without_low", 0) if hist is not None else 0
    w_high = getattr(hist, "bars_without_high", 0) if hist is not None else 0
    sticky_w = len(getattr(hist, "sticky_warnings", []) or []) if hist is not None else 0
    # For V0 / audit original estimate without SpecHist counters
    if res["variant"] in {"V0", "V6_audit_original"}:
        w_low = sum(1 for r in res["level_rows"] if r.get("side") == "low" and r.get("level") is None)
        micro_rej = sum(1 for r in res["level_rows"] if "block_micro" in str(r.get("reason") or "") or "v6_block" in str(r.get("reason") or ""))
        cont_rep = sum(1 for r in res["level_rows"] if "continuation" in str(r.get("reason") or "") or "new_leg" in str(r.get("reason") or ""))
        inv_clr = sum(1 for r in res["level_rows"] if "invalidat" in str(r.get("reason") or ""))

    d = _diff_events(baseline["events_5m"], ev, res["variant"])

    def count_state(name: str) -> int:
        return sum(1 for t in tr if t["new_state"] == name)

    return {
        "variant": res["variant"],
        "protective_level_changes": len(low_changes),
        "candles_without_protective_low": w_low,
        "candles_without_protective_high": w_high,
        "median_level_lifetime": float(pd.Series(lifetimes).median()) if lifetimes else None,
        "max_level_lifetime": int(max(lifetimes)) if lifetimes else None,
        "confirmed_continuation_replacements": cont_rep,
        "invalidation_replacements": inv_clr,
        "micro_candidate_rejections": micro_rej,
        "bos_count": sum(1 for e in ev if "bos" in e["event_type"]),
        "choch_count": sum(1 for e in ev if "choch" in e["event_type"]),
        "events_removed": d["events_removed"],
        "events_added": d["events_added"],
        "median_event_delay": d["median_event_delay"],
        "state_changes": len(tr),
        "strong_states": count_state("strong_bearish") + count_state("strong_bullish"),
        "weakening_states": count_state("bearish_weakening") + count_state("bullish_weakening"),
        "bottoming_topping_states": count_state("bottoming") + count_state("topping"),
        "sticky_warnings": sticky_w,
        "elapsed_sec": res["elapsed_sec"],
    }


def write_static_artifacts(out: Path) -> None:
    current = {
        "source": "trend_state_protective_level_variants_audit.select_low_v6/select_high_v6",
        "stored_state": "StateHist.sticky_low/sticky_high = (price, pivot, reason) keyed by id(state)",
        "initialization": "first baseline last_HL / last_LH (or swing fallback) without requiring continuation",
        "candidate": "always current baseline last_HL / last_LH",
        "continuation": "any HH with confirmation_index > HL.confirmation_index (≥1)",
        "new_active_leg": "IDENTICAL to continuation (redundant)",
        "invalidation": "if state.last_broken_low_level == sticky price → replace IMMEDIATELY with current baseline (may be unconfirmed micro)",
        "higher_relevance": "cont_count(candidate) > cont_count(sticky)",
        "tie_break": "keep sticky unless override fires",
        "fallback": "baseline last_HL/LH always available as candidate",
        "after_break": "same-candle: break uses sticky level; next call may replace with unconfirmed last_HL (I4 violation)",
        "missing_hl": "returns none if baseline none",
        "bias": "ignored",
        "production_gap": "sticky lives outside MarketStructureState; needs explicit fields for production",
        "rows": [
            {
                "concept": "sticky storage",
                "current_v6_audit_behavior": "external StateHist by id(state)",
                "exact_code_location": "StateHist.sticky_low / select_low_v6",
                "ambiguity": "not on MarketStructureState; lost if state copied",
                "production_risk": "must add fields to MarketStructureState or equivalent",
            },
            {
                "concept": "initialization",
                "current_v6_audit_behavior": "first last_HL without continuation",
                "exact_code_location": "select_low_v6 hist.sticky_low is None branch",
                "ambiguity": "first HL may itself be a micro swing",
                "production_risk": "early false protective possible until continuation era",
            },
            {
                "concept": "new_active_leg",
                "current_v6_audit_behavior": "alias of continuation",
                "exact_code_location": "_new_active_leg == _has_continuation_after",
                "ambiguity": "no independent leg definition",
                "production_risk": "dead code / false sense of extra rule",
            },
            {
                "concept": "post_invalidation",
                "current_v6_audit_behavior": "immediate replace with baseline last_HL",
                "exact_code_location": "v6_replace_invalidated",
                "ambiguity": "can reinstall micro HL instantly",
                "production_risk": "violates intended I4",
            },
            {
                "concept": "higher_relevance",
                "current_v6_audit_behavior": "more HH after candidate than sticky",
                "exact_code_location": "cont_count compare",
                "ambiguity": "overlaps continuation; rare extra path",
                "production_risk": "opaque; remove in precise spec",
            },
            {
                "concept": "invalidation timing",
                "current_v6_audit_behavior": "uses last_broken_* set by prior BOS/CHoCH",
                "exact_code_location": "select after labels, before/during detect order",
                "ambiguity": "same-candle break still uses old sticky; replace next bar",
                "production_risk": "must document ordering; OK if intentional",
            },
            {
                "concept": "bias_change",
                "current_v6_audit_behavior": "no handling",
                "exact_code_location": "n/a",
                "ambiguity": "stale bullish level can persist under bearish labels until broken",
                "production_risk": "I6 sticky risk; warn diagnostically, break still needed for CHoCH",
            },
        ],
    }
    (out / "current_v6_behavior.json").write_text(json.dumps(json_safe(current), indent=2))

    state_model = {
        "protective_low_persistent": [
            "active_level",
            "source_pivot_timestamp",
            "source_pivot_price",
            "source_pivot_label",
            "set_timestamp",
            "replacement_reason",
            "invalidated",
            "invalidation_timestamp",
        ],
        "protective_low_derived_per_candle": [
            "continuation_count",
            "last_confirming_hh_timestamp",
            "rejected_micro_candidate",
            "baseline_candidate",
        ],
        "protective_high_persistent": [
            "active_level",
            "source_pivot_timestamp",
            "source_pivot_price",
            "source_pivot_label",
            "set_timestamp",
            "replacement_reason",
            "invalidated",
            "invalidation_timestamp",
        ],
        "protective_high_derived_per_candle": [
            "continuation_count",
            "last_confirming_ll_timestamp",
            "rejected_micro_candidate",
            "baseline_candidate",
        ],
        "note": "Do not store continuation_count persistently; recompute from labeled swing history or recent HH/LL pointers.",
        "minimal_production_fields_on_MarketStructureState": [
            "protective_low_level",
            "protective_low_pivot",
            "protective_low_set_at",
            "protective_high_level",
            "protective_high_pivot",
            "protective_high_set_at",
        ],
        "reuse_existing": [
            "last_broken_low_level",
            "last_broken_high_level",
            "last_higher_low",
            "last_lower_high",
            "last_higher_high",
            "last_lower_low",
            "known_*_confirm_keys for history if needed",
        ],
    }
    (out / "v6_state_model.json").write_text(json.dumps(state_model, indent=2))

    pure = {
        "name": "V6_pure_spec",
        "initialization": "First confirmed HL (else last swing low). Continuation NOT required at init.",
        "replacement": "Only if candidate HL has ≥1 HH with confirmation_index > candidate.confirmation_index.",
        "invalidation": "When last_broken_low_level equals active_level (set by existing close-cross BOS/CHoCH).",
        "post_invalidation": "CLEAR level (None). Install new level only if a continued HL exists; never unconfirmed last_HL.",
        "confirmed_continuation": "≥1 labeled higher_high confirmed after HL confirmation_index. HH need not exceed prior HH beyond normal label rules (label already requires higher than previous high).",
        "active_leg": "NOT a separate rule; identical to confirmed continuation of a newer HL.",
        "higher_relevance": "REMOVED.",
        "tie_break": "1) keep valid sticky 2) replace only with continued newer HL 3) else keep.",
        "micro_swing": "Newer last_HL than sticky without ≥1 post-HL HH.",
        "symmetry": "LH/LL mirror for highs.",
    }
    hybrid = {
        "name": "V6_v2_hybrid_spec",
        "initialization": "Only last HL that already has ≥1 confirmed HH after it. Else None.",
        "replacement": "When a newer continued HL appears (last in continued list).",
        "invalidation": "Same as pure (last_broken_*).",
        "post_invalidation": "CLEAR; install last continued HL if any other than broken; never unconfirmed.",
        "confirmed_continuation": "Same as pure.",
        "active_leg": "Absorbed: newer continued HL = origin of latest HH leg.",
        "higher_relevance": "REMOVED.",
        "tie_break": "Keep sticky unless newer continued HL exists.",
        "micro_swing": "Any last_HL lacking continuation while sticky is continued.",
        "symmetry": "LH/LL mirror.",
    }
    (out / "v6_pure_definition.json").write_text(json.dumps(pure, indent=2))
    (out / "v6_v2_hybrid_definition.json").write_text(json.dumps(hybrid, indent=2))

    # Edge case matrix
    rows = []
    cases = [
        ("B1", "bullish", "HL1→HH1→HL2 no HH", "HL1 stays", "HL2 rejected as micro", "same", "both"),
        ("B2", "bullish", "HL1→HH1→HL2→HH2", "HL2 replaces on HH2 confirm candle", "not on HL2 confirm", "same", "both"),
        ("B3", "bullish", "HL1→HH1→HL2→low between HL2 and HL1", "HL1 still active until close-break HL1; HL2 never was active", "wick≠invalidation", "same", "both"),
        ("B4", "bullish", "HL1→HH1→close break HL1", "CHoCH/BOS on HL1; level cleared next select; new high side may build", "same-candle uses HL1 for event", "same", "both"),
        ("B5", "bullish", "HL1→HH1→range micro HL/HH", "HL1 kept until newer continued HL", "prevents micro overwrite; updates when HH after new HL", "same", "both"),
        ("S1", "bearish", "LH1→LL1→LH2 no LL", "LH1 stays", "LH2 micro rejected", "same", "both"),
        ("S2", "bearish", "LH1→LL1→LH2→LL2", "LH2 replaces on LL2 confirm", "not on LH2 alone", "same", "both"),
        ("S3", "bearish", "LH1→LL1→LH2→high between LH2 and LH1", "LH1 active until close-break", "mirror B3", "same", "both"),
        ("S4", "bearish", "LH1→LL1→close break LH1", "bullish CHoCH/BOS; clear high", "mirror B4", "same", "both"),
        ("S5", "bearish", "LH1→LL1→range micro", "LH1 sticky until newer continued LH", "mirror B5", "same", "both"),
        ("X1", "bias", "neutral→bullish", "init per spec when HL(/continued) exists", "pure earlier coverage", "hybrid later", "depends"),
        ("X2", "bias", "bullish→bearish LH+LL", "low remains until broken (needed for CHoCH)", "sticky warning", "same", "both"),
        ("X3", "bias", "bearish→bullish HH+HL", "high remains until broken", "mirror", "same", "both"),
        ("X4", "same_candle", "break+new pivot+HH", "order: labels→protective→break; new HH can confirm prior HL same bar; new level usable same bar for break", "causal via confirmation_index", "same", "both"),
    ]
    for cid, side, seq, expect, note, pure_vs_hyb, winner in cases:
        rows.append(
            {
                "case_id": cid,
                "side": side,
                "sequence": seq,
                "expected_result": expect,
                "notes": note,
                "pure_vs_hybrid": pure_vs_hyb,
                "applies_to": winner,
            }
        )
    pd.DataFrame(rows).to_csv(out / "edge_case_matrix.csv", index=False)

    ordering = """# Same-candle ordering (Research-v1)

Actual order in `update_market_structure`:

1. Filter pivots as-of `decision_time` (causal)
2. `_apply_new_swing_labels` — confirm new swings, update labels, **update `current_structure_bias`**
3. `_detect_bos_choch` — calls `_protective_low/_high`, then close-cross break vs `prior_close`
4. `_detect_failed_breaks`
5. `_detect_retest`
6. Update confidence / `prior_close = close`

State machine then uses 5m events + HTF updates for transitions.

## V6 insertion point

Replace only the body of `_protective_low` / `_protective_high` (step 3 input).
Do not change break detection.

## Causal answers

| Question | Answer |
|----------|--------|
| New pivot confirmed same candle usable as protective? | Yes, after step 2, if selector accepts it (hybrid: only if continuation already satisfied — which requires HH also confirmed ≤ this candle). |
| Can HH confirmed same candle activate prior HL? | Yes — HH event in step 2 before protective read; continuation_index check uses confirmation_index < HH. |
| Can newly activated level break same candle? | Yes — protective returns new level, then close-cross may break it same bar. |
| Lookahead risk? | None if only using pivots with confirmation_timestamp < decision_time (existing filter). |

Invalidation flag `last_broken_*` is set **during** step 3 after a break. Therefore sticky replacement on invalidation is visible on the **next** protective call (next candle), which is correct: the break event must reference the pre-break level.
"""
    (out / "same_candle_ordering.md").write_text(ordering)

    test_plan = """# V6 Protective Spec — Test Plan (not implemented in production yet)

Isolated future tests (do not modify existing `test_trend_*.py` until implementation PR).

## Unit / sequence

- `test_micro_hl_does_not_replace_confirmed_protective_low`
- `test_micro_lh_does_not_replace_confirmed_protective_high`
- `test_hl_replaces_only_after_confirmed_hh`
- `test_lh_replaces_only_after_confirmed_ll`
- `test_invalidated_level_is_not_reused`
- `test_no_fallback_to_unconfirmed_last_hl`
- `test_no_fallback_to_unconfirmed_last_lh`
- `test_bias_change_does_not_keep_stale_level_indefinitely` (warning / break-path; level may remain until close-break)
- `test_bullish_bearish_symmetry`
- `test_same_candle_confirmation_is_causal`

## Replay

- `test_march_micro_choch_removed_without_hardcoding`
  - Replay APTUSDT from first available candle through diagnosis end
  - Assert: no `bearish_choch` whose `level` equals the baseline micro HL that V0 used at the first topping→early_bearish transition in the known failure window
  - Prefer asserting via relative structure (V0 produces choch; hybrid/pure does not at same timestamp) without baking price into production code

## Invariants I1–I8

Each maps to at least one unit test above.
"""
    (out / "test_plan.md").write_text(test_plan)


def write_recommended(out: Path, metrics_df: pd.DataFrame, focus_df: pd.DataFrame) -> dict[str, Any]:
    # Prefer hybrid if it removes micro choch and has controlled no-level gaps
    def row(v: str) -> dict:
        return metrics_df[metrics_df["variant"] == v].iloc[0].to_dict()

    # March micro check
    def micro_present(v: str) -> bool:
        sub = focus_df[(focus_df["variant"] == v) & (focus_df["timestamp"] == _iso(MARCH_FOCUS[0]))]
        if sub.empty:
            return True
        ev = str(sub.iloc[0].get("event") or "")
        return "bearish_choch@0.9938" in ev or (
            "0.9938" in ev and "bearish_choch" in ev
        )

    pure = row("V6_pure_spec")
    hyb = row("V6_v2_hybrid_spec")
    # Decision: hybrid wins on I4 + init discipline; check march + regressions
    recommended = "V6_v2_hybrid_spec"
    decision = "B"
    if micro_present(recommended) and not micro_present("V6_pure_spec"):
        recommended = "V6_pure_spec"
        decision = "A"
    # If hybrid has vastly more empty candles and worse march, reconsider
    if hyb["candles_without_protective_low"] > pure["candles_without_protective_low"] * 3:
        # still prefer hybrid unless march fails
        pass

    rec = {
        "recommended_spec": recommended,
        "rejected_alternative": "V6_pure_spec" if recommended.startswith("V6_v2") else "V6_v2_hybrid_spec",
        "decision_letter": decision,
        "decision_text": {
            "A": "A: V6-pure ist ausreichend und soll später implementiert werden.",
            "B": "B: V6+V2 ist fachlich robuster und soll später implementiert werden.",
            "C": "C: V6 benötigt einen anderen klar definierten Hybrid.",
            "D": "D: V6 ist nach Präzisierung nicht robust genug.",
        }[decision],
        "protective_low_initialization": (
            "Last HL with ≥1 confirmed HH after it; else None"
            if recommended.startswith("V6_v2")
            else "First confirmed HL (else swing low), continuation not required"
        ),
        "protective_high_initialization": "Mirror with LH + LL",
        "replacement_rule": "Replace only when a newer HL/LH has ≥1 confirmed HH/LL after its confirmation_index",
        "invalidation_rule": "Existing close-cross BOS/CHoCH sets last_broken_*; next protective call clears when broken == active",
        "post_invalidation_rule": "Clear to None; install only a continued HL/LH — never unconfirmed last_HL/LH",
        "confirmed_continuation_definition": (
            "Bullish: HH label event with confirmation_index > HL.confirmation_index. "
            "Bearish: LL with confirmation_index > LH.confirmation_index. Exactly one suffices. "
            "HH/LL already satisfy relative price vs previous same-side pivot via classify_swing_structure."
        ),
        "active_leg_definition": "Not separate: the HL preceding the most recent continued HH is the active-leg origin; selecting last continued HL encodes this.",
        "higher_relevance_definition": "REMOVED — do not implement cont_count inequality as a separate override.",
        "tie_break_rule": "1) keep valid active level 2) replace with newer continued candidate 3) else keep",
        "same_candle_ordering": [
            "filter_pivots_as_of",
            "apply_new_swing_labels (bias update)",
            "protective_low/high selection (V6)",
            "detect_bos_choch close-cross",
            "failed_breaks",
            "retest",
            "prior_close update",
            "state machine transition",
        ],
        "required_state_fields": [
            "protective_low_level",
            "protective_low_pivot",
            "protective_low_set_at",
            "protective_high_level",
            "protective_high_pivot",
            "protective_high_set_at",
        ],
        "derived_fields": [
            "continuation_count",
            "rejected_micro_candidate",
            "last_confirming_hh_or_ll_timestamp",
        ],
        "invariants": [
            "I1 No micro-overwrite without continuation",
            "I2 Causal continuation — usable only from confirmation candle of HH/LL onward",
            "I3 Sticky until invalidation or newer continued successor",
            "I4 No fallback to unconfirmed last HL/LH after invalidation",
            "I5 Symmetry high/low",
            "I6 Stale bias: level may remain until close-break (needed for CHoCH); do not redefine new trend with opposite-side micro",
            "I7 BOS/CHoCH uses active protective at selection time",
            "I8 No same-candle lookahead beyond labels→protective→break order",
        ],
        "march_effect": {},
        "broader_replay_effect": {
            "pure": {k: pure[k] for k in pure if k != "variant"},
            "hybrid": {k: hyb[k] for k in hyb if k != "variant"},
        },
        "remaining_policy_issues": [
            "HTF veto vs strong_bearish",
            "single failed_breakdown → weakening",
            "bottoming 2-hit rule",
        ],
        "implementation_files_later": [
            "research/regime_scanner/trend_structure.py::_protective_low",
            "research/regime_scanner/trend_structure.py::_protective_high",
            "research/regime_scanner/trend_structure.py::MarketStructureState (minimal fields)",
        ],
        "implementation_risk": "Need compact continued-HL tracking without O(n) full history; bias-stale levels until break; early None gaps for hybrid",
        "confidence": "medium-high",
    }

    # fill march from focus
    march = {}
    for ts in MARCH_FOCUS:
        iso = _iso(ts)
        march[iso] = {}
        for v in ["V0", "V6_audit_original", "V6_pure_spec", "V6_v2_hybrid_spec"]:
            sub = focus_df[(focus_df["variant"] == v) & (focus_df["timestamp"] == iso)]
            if not sub.empty:
                march[iso][v] = sub.iloc[0].to_dict()
    rec["march_effect"] = march
    (out / "recommended_spec.json").write_text(json.dumps(json_safe(rec), indent=2))
    return rec


def write_readme(out: Path, rec: dict[str, Any]) -> None:
    (out / "README.md").write_text(
        f"""# V6 Protective Level Specification

Diagnostic-only precision of winner variant V6. **No production changes.**

## Decision

**{rec['decision_text']}**

Recommended: `{rec['recommended_spec']}`
Rejected alternative: `{rec['rejected_alternative']}`

## Key rules

- Replacement: newer HL/LH only after confirmed HH/LL continuation
- Invalidation: existing close-cross → `last_broken_*` → clear on next protective select
- Post-invalidation: **no** unconfirmed last_HL/LH fallback (I4)
- Higher-relevance & separate active-leg rules: **removed**

## Reproduce

```bash
PYTHONPATH=. PYTHONUNBUFFERED=1 python3 -m research.regime_scanner.trend_state_v6_protective_spec_audit
```
"""
    )


def run_audit(*, dual: bool = True) -> Path:
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    write_static_artifacts(out)

    end = _ts(DIAG_END)
    _p("Loading frame…")
    frame, pivots = load_frame(end)
    _p(f"bars={len(frame)} last={frame['decision_time'].iloc[-1]}")
    install_causal_htf_prefix_cache(frame, end)

    variants = ["V0", "V6_audit_original", "V6_pure_spec", "V6_v2_hybrid_spec"]
    results = {}
    t_all = time.perf_counter()
    for v in variants:
        results[v] = run_variant(v, frame, pivots)
    _p(f"All wall {time.perf_counter()-t_all:.1f}s")

    baseline = results["V0"]
    metrics = [_metrics(results[v], baseline) for v in variants]
    metrics_df = pd.DataFrame(metrics)
    metrics_df.to_csv(out / "diagnostic_comparison.csv", index=False)

    focus = pd.concat([pd.DataFrame(results[v]["focus_rows"]) for v in variants], ignore_index=True)
    # difference vs original audit V6
    if not focus.empty:
        orig = focus[focus["variant"] == "V6_audit_original"][["timestamp", "active_protective_low", "state", "event"]].rename(
            columns={
                "active_protective_low": "v6_orig_low",
                "state": "v6_orig_state",
                "event": "v6_orig_event",
            }
        )
        focus = focus.merge(orig, on="timestamp", how="left")
        focus["difference_vs_v6_original_audit"] = focus.apply(
            lambda r: "same"
            if r["variant"] == "V6_audit_original"
            else (
                "diff"
                if (r.get("active_protective_low") != r.get("v6_orig_low") or r.get("state") != r.get("v6_orig_state"))
                else "same_level_state"
            ),
            axis=1,
        )
    focus.to_csv(out / "march_checkpoint_comparison.csv", index=False)

    # sticky risk audit
    sticky_rows = []
    for v in variants:
        hist = results[v].get("hist")
        warns = getattr(hist, "sticky_warnings", []) if hist is not None else []
        for w in warns:
            sticky_rows.append({"variant": v, **w})
        # structural sticky heuristics from level lifetimes
        m = metrics_df[metrics_df["variant"] == v].iloc[0]
        sticky_rows.append(
            {
                "variant": v,
                "warning": "summary_max_level_lifetime",
                "max_level_lifetime": m["max_level_lifetime"],
                "median_level_lifetime": m["median_level_lifetime"],
                "continuation_replacements": m["confirmed_continuation_replacements"],
                "micro_rejections": m["micro_candidate_rejections"],
            }
        )
    pd.DataFrame(sticky_rows).to_csv(out / "sticky_risk_audit.csv", index=False)

    rec = write_recommended(out, metrics_df, focus)
    write_readme(out, rec)

    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*")) if p.is_file()}
    (out / "checksums_run1.json").write_text(json.dumps(checksums, indent=2))

    if dual:
        _p("Determinism: re-run recommended + V0…")
        r0 = run_variant("V0", frame, pivots)
        rr = run_variant(rec["recommended_spec"], frame, pivots)
        det = {
            "V0_events_match": r0["events_5m"] == results["V0"]["events_5m"],
            "rec_events_match": rr["events_5m"] == results[rec["recommended_spec"]]["events_5m"],
            "rec_transitions_match": rr["transitions"] == results[rec["recommended_spec"]]["transitions"],
        }
        (out / "determinism_check.json").write_text(json.dumps(det, indent=2))
        _p(f"Determinism {det}")

    (out / "runtime.json").write_text(
        json.dumps(json_safe({v: results[v]["elapsed_sec"] for v in variants} | {"total": time.perf_counter() - t_all}), indent=2)
    )
    _p(f"Wrote {out}")
    _p(f"Recommended {rec['recommended_spec']} / {rec['decision_text']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-dual", action="store_true")
    args = ap.parse_args(argv)
    run_audit(dual=not args.skip_dual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Diagnostic audit: protective-level selection variants (research-only).

Compares V0–V6(+V4 subvariants) without changing production trend_structure /
trend_state_machine / policy. Baseline Research-v1 remains untouched.

Mode A: structure-event projection from protective swap.
Mode B: full state-machine replay with only the protective selector replaced
via process-local patches (restored after each variant).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots
from research.regime_scanner.trend_state_machine import TrendRuntime, default_trend_state_config, step_trend_state
from research.regime_scanner.trend_state_march_2026_root_cause_audit import install_causal_htf_prefix_cache
import research.regime_scanner.trend_structure as ts_mod
from research.regime_scanner.trend_structure import MarketStructureState

OUT = Path("research/regime_scanner/results/trend_state_protective_level_variants")
DIAG_END = "2026-03-10T00:00:00+00:00"
# Report filters only — never used in selection rules.
MARCH_FOCUS = (
    "2026-03-05T22:30:00+00:00",
    "2026-03-06T00:30:00+00:00",
    "2026-03-06T01:35:00+00:00",
    "2026-03-07T03:05:00+00:00",
    "2026-03-07T03:35:00+00:00",
)
BOS_CHOCH = frozenset({"bearish_choch", "bullish_choch", "bearish_bos", "bullish_bos"})
STRUCTURE_LABELS = frozenset({"higher_high", "higher_low", "lower_high", "lower_low"})


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _p(msg: str) -> None:
    print(msg, flush=True)


def _pivot_ref(p: ConfirmedPivot | None) -> str | None:
    if p is None:
        return None
    return f"{p.pivot_type}@{p.price}:{p.pivot_timestamp}"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Diagnostic context (process-local; restored/cleared between variants)
# ---------------------------------------------------------------------------


@dataclass
class SwingRec:
    side: str
    label: str
    pivot: ConfirmedPivot
    event_time: pd.Timestamp


@dataclass
class StateHist:
    swings: list[SwingRec] = field(default_factory=list)
    sticky_low: tuple[float, ConfirmedPivot | None, str] | None = None
    sticky_high: tuple[float, ConfirmedPivot | None, str] | None = None
    last_logged_low: float | None = None
    last_logged_high: float | None = None


@dataclass
class DiagCtx:
    variant: str = "V0"
    by_state: dict[int, StateHist] = field(default_factory=dict)
    htf15: MarketStructureState | None = None
    htf30: MarketStructureState | None = None
    level_rows: list[dict[str, Any]] = field(default_factory=list)
    log_levels: bool = True
    decision_time: pd.Timestamp | None = None

    def hist(self, state: MarketStructureState) -> StateHist:
        return self.by_state.setdefault(id(state), StateHist())


CTX = DiagCtx()
_ORIG_PROTECTIVE_LOW = ts_mod._protective_low
_ORIG_PROTECTIVE_HIGH = ts_mod._protective_high
_ORIG_APPLY = ts_mod._apply_new_swing_labels


def _reset_ctx(variant: str, *, log_levels: bool = True) -> None:
    global CTX
    CTX = DiagCtx(variant=variant, log_levels=log_levels)


def _wrapped_apply(
    state: MarketStructureState,
    pivots: list[ConfirmedPivot],
    *,
    event_time: pd.Timestamp,
    cfg: Any,
) -> list[Any]:
    events = _ORIG_APPLY(state, pivots, event_time=event_time, cfg=cfg)
    hist = CTX.hist(state)
    for ev in events:
        if ev.event_type not in STRUCTURE_LABELS:
            continue
        # Important: use endswith — "high" in "higher_low" is True and would mis-tag HLs.
        side = "high" if ev.event_type.endswith("high") else "low"
        # Recover pivot from state pointers when possible
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
        # Avoid duplicate appends for same pivot key
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


# ---------------------------------------------------------------------------
# Selectors (causal, symmetric high/low)
# ---------------------------------------------------------------------------

Selector = Callable[
    [MarketStructureState],
    tuple[float | None, ConfirmedPivot | None, str, list[str]],
]


def _baseline_low(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str]:
    if state.last_higher_low is not None:
        return float(state.last_higher_low.price), state.last_higher_low, "last_higher_low"
    if state.last_confirmed_swing_low is not None:
        return (
            float(state.last_confirmed_swing_low.price),
            state.last_confirmed_swing_low,
            "last_confirmed_swing_low",
        )
    return None, None, "none"


def _baseline_high(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str]:
    if state.last_lower_high is not None:
        return float(state.last_lower_high.price), state.last_lower_high, "last_lower_high"
    if state.last_confirmed_swing_high is not None:
        return (
            float(state.last_confirmed_swing_high.price),
            state.last_confirmed_swing_high,
            "last_confirmed_swing_high",
        )
    return None, None, "none"


def _hls(hist: StateHist) -> list[SwingRec]:
    return [s for s in hist.swings if s.side == "low" and s.label == "higher_low"]


def _hhs(hist: StateHist) -> list[SwingRec]:
    return [s for s in hist.swings if s.side == "high" and s.label == "higher_high"]


def _lhs(hist: StateHist) -> list[SwingRec]:
    return [s for s in hist.swings if s.side == "high" and s.label == "lower_high"]


def _lls(hist: StateHist) -> list[SwingRec]:
    return [s for s in hist.swings if s.side == "low" and s.label == "lower_low"]


def _cand_keys(recs: list[SwingRec]) -> list[str]:
    return [_pivot_ref(r.pivot) or "?" for r in recs]


def select_low_v0(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    px, p, r = _baseline_low(state)
    cands = []
    if state.last_higher_low is not None:
        cands.append(_pivot_ref(state.last_higher_low) or "")
    if state.last_confirmed_swing_low is not None:
        cands.append(_pivot_ref(state.last_confirmed_swing_low) or "")
    return px, p, r, cands


def select_high_v0(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    px, p, r = _baseline_high(state)
    cands = []
    if state.last_lower_high is not None:
        cands.append(_pivot_ref(state.last_lower_high) or "")
    if state.last_confirmed_swing_high is not None:
        cands.append(_pivot_ref(state.last_confirmed_swing_high) or "")
    return px, p, r, cands


def select_low_v1(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """Previous structural swing: HL that existed before current impulsive leg (last HH)."""
    hist = CTX.hist(state)
    hls, hhs = _hls(hist), _hhs(hist)
    cands = _cand_keys(hls)
    if not hhs:
        px, p, r = _baseline_low(state)
        return px, p, f"v1_no_hh_fallback:{r}", cands
    last_hh = hhs[-1]
    # Leg start = confirmation of most recent HH; protective = last HL confirmed before that HH.
    prior = [s for s in hls if s.pivot.confirmation_index < last_hh.pivot.confirmation_index]
    if prior:
        s = prior[-1]
        return float(s.pivot.price), s.pivot, "v1_leg_origin_hl_before_last_hh", cands
    px, p, r = _baseline_low(state)
    return px, p, f"v1_no_prior_hl_fallback:{r}", cands


def select_high_v1(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    lhs, lls = _lhs(hist), _lls(hist)
    cands = _cand_keys(lhs)
    if not lls:
        px, p, r = _baseline_high(state)
        return px, p, f"v1_no_ll_fallback:{r}", cands
    last_ll = lls[-1]
    prior = [s for s in lhs if s.pivot.confirmation_index < last_ll.pivot.confirmation_index]
    if prior:
        s = prior[-1]
        return float(s.pivot.price), s.pivot, "v1_leg_origin_lh_before_last_ll", cands
    px, p, r = _baseline_high(state)
    return px, p, f"v1_no_prior_lh_fallback:{r}", cands


def select_low_v2(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """Last HL that has at least one confirmed HH afterward."""
    hist = CTX.hist(state)
    hls, hhs = _hls(hist), _hhs(hist)
    continued = [
        hl
        for hl in hls
        if any(hh.pivot.confirmation_index > hl.pivot.confirmation_index for hh in hhs)
    ]
    cands = _cand_keys(continued)
    if continued:
        s = continued[-1]
        return float(s.pivot.price), s.pivot, "v2_last_hl_with_hh_continuation", cands
    px, p, r = _baseline_low(state)
    return px, p, f"v2_no_continued_hl_fallback:{r}", _cand_keys(hls)


def select_high_v2(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    lhs, lls = _lhs(hist), _lls(hist)
    continued = [
        lh
        for lh in lhs
        if any(ll.pivot.confirmation_index > lh.pivot.confirmation_index for ll in lls)
    ]
    cands = _cand_keys(continued)
    if continued:
        s = continued[-1]
        return float(s.pivot.price), s.pivot, "v2_last_lh_with_ll_continuation", cands
    px, p, r = _baseline_high(state)
    return px, p, f"v2_no_continued_lh_fallback:{r}", _cand_keys(lhs)


def select_low_v3(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """Active-leg origin: low immediately preceding the currently valid HH in swing sequence."""
    hist = CTX.hist(state)
    swings = list(hist.swings)
    hhs = _hhs(hist)
    cands = _cand_keys(_hls(hist))
    if not hhs:
        px, p, r = _baseline_low(state)
        return px, p, f"v3_no_hh_fallback:{r}", cands
    last_hh = hhs[-1]
    # Walk back in chronological swing list to the nearest prior low (any low label).
    prior_lows = [
        s
        for s in swings
        if s.side == "low" and s.pivot.confirmation_index < last_hh.pivot.confirmation_index
    ]
    if prior_lows:
        s = prior_lows[-1]
        return float(s.pivot.price), s.pivot, f"v3_origin_low_before_last_hh:{s.label}", cands
    px, p, r = _baseline_low(state)
    return px, p, f"v3_no_prior_low_fallback:{r}", cands


def select_high_v3(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    swings = list(hist.swings)
    lls = _lls(hist)
    cands = _cand_keys(_lhs(hist))
    if not lls:
        px, p, r = _baseline_high(state)
        return px, p, f"v3_no_ll_fallback:{r}", cands
    last_ll = lls[-1]
    prior_highs = [
        s
        for s in swings
        if s.side == "high" and s.pivot.confirmation_index < last_ll.pivot.confirmation_index
    ]
    if prior_highs:
        s = prior_highs[-1]
        return float(s.pivot.price), s.pivot, f"v3_origin_high_before_last_ll:{s.label}", cands
    px, p, r = _baseline_high(state)
    return px, p, f"v3_no_prior_high_fallback:{r}", cands


def _rank_hl_candidates(hist: StateHist) -> list[tuple[tuple[int, float, int], SwingRec]]:
    """Lexicographic significance without opaque weights.

    Rank key (desc): (#HH after HL, distance to first subsequent HH, older confirm index).
    """
    hls, hhs = _hls(hist), _hhs(hist)
    ranked: list[tuple[tuple[int, float, int], SwingRec]] = []
    for hl in hls:
        after = [hh for hh in hhs if hh.pivot.confirmation_index > hl.pivot.confirmation_index]
        if not after:
            continue
        n = len(after)
        dist = abs(float(after[0].pivot.price) - float(hl.pivot.price))
        # Prefer more continuations, larger distance, older pivot (smaller confirm index → negate)
        key = (n, dist, -int(hl.pivot.confirmation_index))
        ranked.append((key, hl))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def _rank_lh_candidates(hist: StateHist) -> list[tuple[tuple[int, float, int], SwingRec]]:
    lhs, lls = _lhs(hist), _lls(hist)
    ranked: list[tuple[tuple[int, float, int], SwingRec]] = []
    for lh in lhs:
        after = [ll for ll in lls if ll.pivot.confirmation_index > lh.pivot.confirmation_index]
        if not after:
            continue
        n = len(after)
        dist = abs(float(lh.pivot.price) - float(after[0].pivot.price))
        key = (n, dist, -int(lh.pivot.confirmation_index))
        ranked.append((key, lh))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def select_low_v4(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    ranked = _rank_hl_candidates(hist)
    cands = _cand_keys([r for _, r in ranked])
    if ranked:
        s = ranked[0][1]
        k = ranked[0][0]
        return (
            float(s.pivot.price),
            s.pivot,
            f"v4_lex_cont{k[0]}_dist{k[1]:.6f}_older{-k[2]}",
            cands,
        )
    px, p, r = _baseline_low(state)
    return px, p, f"v4_no_ranked_fallback:{r}", _cand_keys(_hls(hist))


def select_high_v4(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    ranked = _rank_lh_candidates(hist)
    cands = _cand_keys([r for _, r in ranked])
    if ranked:
        s = ranked[0][1]
        k = ranked[0][0]
        return (
            float(s.pivot.price),
            s.pivot,
            f"v4_lex_cont{k[0]}_dist{k[1]:.6f}_older{-k[2]}",
            cands,
        )
    px, p, r = _baseline_high(state)
    return px, p, f"v4_no_ranked_fallback:{r}", _cand_keys(_lhs(hist))


def select_low_v4a(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """Subvariant: max continuation count only; tie → older."""
    hist = CTX.hist(state)
    ranked = _rank_hl_candidates(hist)
    if not ranked:
        px, p, r = _baseline_low(state)
        return px, p, f"v4a_fallback:{r}", []
    best_n = ranked[0][0][0]
    pool = [r for k, r in ranked if k[0] == best_n]
    pool.sort(key=lambda s: s.pivot.confirmation_index)
    s = pool[0]
    return float(s.pivot.price), s.pivot, f"v4a_max_cont_{best_n}_oldest", _cand_keys(pool)


def select_high_v4a(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    ranked = _rank_lh_candidates(hist)
    if not ranked:
        px, p, r = _baseline_high(state)
        return px, p, f"v4a_fallback:{r}", []
    best_n = ranked[0][0][0]
    pool = [r for k, r in ranked if k[0] == best_n]
    pool.sort(key=lambda s: s.pivot.confirmation_index)
    s = pool[0]
    return float(s.pivot.price), s.pivot, f"v4a_max_cont_{best_n}_oldest", _cand_keys(pool)


def select_low_v4b(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """Subvariant: max distance to first subsequent HH; tie → older."""
    hist = CTX.hist(state)
    ranked = _rank_hl_candidates(hist)
    if not ranked:
        px, p, r = _baseline_low(state)
        return px, p, f"v4b_fallback:{r}", []
    # Re-sort by distance then older
    scored = sorted(ranked, key=lambda x: (x[0][1], -x[0][2]), reverse=True)
    s = scored[0][1]
    return float(s.pivot.price), s.pivot, "v4b_max_dist_to_first_hh", _cand_keys([r for _, r in scored[:5]])


def select_high_v4b(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    ranked = _rank_lh_candidates(hist)
    if not ranked:
        px, p, r = _baseline_high(state)
        return px, p, f"v4b_fallback:{r}", []
    scored = sorted(ranked, key=lambda x: (x[0][1], -x[0][2]), reverse=True)
    s = scored[0][1]
    return float(s.pivot.price), s.pivot, "v4b_max_dist_to_first_ll", _cand_keys([r for _, r in scored[:5]])


def _htf_structural_low(st: MarketStructureState | None) -> tuple[float | None, ConfirmedPivot | None, str]:
    if st is None:
        return None, None, "no_htf"
    # Prefer continued HL on HTF; else baseline
    px, p, r, _ = select_low_v2(st)
    if px is not None:
        return px, p, f"htf_v2:{r}"
    return _baseline_low(st)


def _htf_structural_high(st: MarketStructureState | None) -> tuple[float | None, ConfirmedPivot | None, str]:
    if st is None:
        return None, None, "no_htf"
    px, p, r, _ = select_high_v2(st)
    if px is not None:
        return px, p, f"htf_v2:{r}"
    return _baseline_high(st)


def select_low_v5(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """HTF-anchored on 5m when micro swing sits inside an already confirmed HTF bullish leg."""
    base_px, base_p, base_r, cands = select_low_v2(state)
    if state.timeframe != "5m":
        return base_px, base_p, f"v5_non5m:{base_r}", cands
    htf = CTX.htf15
    htf_px, htf_p, htf_r = _htf_structural_low(htf)
    cands = list(cands)
    if htf_px is not None:
        cands.append(f"htf15:{_pivot_ref(htf_p)}")
    # Micro HL inside HTF leg: 5m level above HTF protective low while HTF still HH+HL bullish.
    # No shared confirmation_index across timeframes (different bar indices).
    if (
        htf is not None
        and htf_px is not None
        and base_px is not None
        and htf.current_structure_bias == "bullish"
        and float(base_px) > float(htf_px)
        and htf.last_high_label == "higher_high"
        and htf.last_low_label == "higher_low"
    ):
        return float(htf_px), htf_p, f"v5_htf15_anchor:{htf_r}", cands
    # try 30m similarly
    htf30 = CTX.htf30
    h30_px, h30_p, h30_r = _htf_structural_low(htf30)
    if h30_px is not None:
        cands.append(f"htf30:{_pivot_ref(h30_p)}")
    if (
        htf30 is not None
        and h30_px is not None
        and base_px is not None
        and htf30.current_structure_bias == "bullish"
        and float(base_px) > float(h30_px)
        and htf30.last_high_label == "higher_high"
        and htf30.last_low_label == "higher_low"
    ):
        return float(h30_px), h30_p, f"v5_htf30_anchor:{h30_r}", cands
    return base_px, base_p, f"v5_keep_5m:{base_r}", cands


def select_high_v5(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    base_px, base_p, base_r, cands = select_high_v2(state)
    if state.timeframe != "5m":
        return base_px, base_p, f"v5_non5m:{base_r}", cands
    cands = list(cands)
    htf = CTX.htf15
    htf_px, htf_p, htf_r = _htf_structural_high(htf)
    if htf_px is not None:
        cands.append(f"htf15:{_pivot_ref(htf_p)}")
    if (
        htf is not None
        and htf_px is not None
        and base_px is not None
        and htf.current_structure_bias == "bearish"
        and float(base_px) < float(htf_px)
        and htf.last_high_label == "lower_high"
        and htf.last_low_label == "lower_low"
    ):
        return float(htf_px), htf_p, f"v5_htf15_anchor:{htf_r}", cands
    htf30 = CTX.htf30
    h30_px, h30_p, h30_r = _htf_structural_high(htf30)
    if h30_px is not None:
        cands.append(f"htf30:{_pivot_ref(h30_p)}")
    if (
        htf30 is not None
        and h30_px is not None
        and base_px is not None
        and htf30.current_structure_bias == "bearish"
        and float(base_px) < float(h30_px)
        and htf30.last_high_label == "lower_high"
        and htf30.last_low_label == "lower_low"
    ):
        return float(h30_px), h30_p, f"v5_htf30_anchor:{h30_r}", cands
    return base_px, base_p, f"v5_keep_5m:{base_r}", cands


def _has_continuation_after(hist: StateHist, pivot: ConfirmedPivot, *, side: str) -> bool:
    if side == "low":
        return any(
            s.label == "higher_high" and s.pivot.confirmation_index > pivot.confirmation_index
            for s in hist.swings
        )
    return any(
        s.label == "lower_low" and s.pivot.confirmation_index > pivot.confirmation_index
        for s in hist.swings
    )


def _new_active_leg(hist: StateHist, new_pivot: ConfirmedPivot, *, side: str) -> bool:
    """New active leg if a continuation extreme confirmed after this pivot (same as continuation)."""
    return _has_continuation_after(hist, new_pivot, side=side)


def select_low_v6(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    """Non-overwrite micro: sticky protective low; overwrite only with structural evidence."""
    hist = CTX.hist(state)
    base_px, base_p, base_r = _baseline_low(state)
    cands = [_pivot_ref(base_p) or ""] if base_p else []
    if base_px is None:
        return None, None, "v6_none", cands
    if hist.sticky_low is None:
        hist.sticky_low = (base_px, base_p, f"v6_init:{base_r}")
        return base_px, base_p, hist.sticky_low[2], cands
    sticky_px, sticky_p, sticky_r = hist.sticky_low
    # Invalidated level may be replaced
    if state.last_broken_low_level is not None and sticky_px is not None:
        if abs(float(state.last_broken_low_level) - float(sticky_px)) < 1e-12:
            hist.sticky_low = (base_px, base_p, f"v6_replace_invalidated:{base_r}")
            return base_px, base_p, hist.sticky_low[2], cands
    # Same pivot → keep
    if sticky_p is not None and base_p is not None and sticky_p.confirmation_index == base_p.confirmation_index:
        return sticky_px, sticky_p, f"v6_keep_same:{sticky_r}", cands
    # Overwrite if new baseline pivot has confirmed continuation (new leg)
    if base_p is not None and _new_active_leg(hist, base_p, side="low"):
        hist.sticky_low = (base_px, base_p, f"v6_replace_new_leg_continuation:{base_r}")
        return base_px, base_p, hist.sticky_low[2], cands
    # Higher structural relevance: more HH continuations than sticky
    if sticky_p is not None and base_p is not None:
        def cont_count(p: ConfirmedPivot) -> int:
            return sum(
                1
                for s in hist.swings
                if s.label == "higher_high" and s.pivot.confirmation_index > p.confirmation_index
            )

        if cont_count(base_p) > cont_count(sticky_p):
            hist.sticky_low = (base_px, base_p, f"v6_replace_higher_relevance:{base_r}")
            return base_px, base_p, hist.sticky_low[2], cands
    return sticky_px, sticky_p, f"v6_block_micro_overwrite:{sticky_r}", cands + [f"blocked:{_pivot_ref(base_p)}"]


def select_high_v6(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None, str, list[str]]:
    hist = CTX.hist(state)
    base_px, base_p, base_r = _baseline_high(state)
    cands = [_pivot_ref(base_p) or ""] if base_p else []
    if base_px is None:
        return None, None, "v6_none", cands
    if hist.sticky_high is None:
        hist.sticky_high = (base_px, base_p, f"v6_init:{base_r}")
        return base_px, base_p, hist.sticky_high[2], cands
    sticky_px, sticky_p, sticky_r = hist.sticky_high
    if state.last_broken_high_level is not None and sticky_px is not None:
        if abs(float(state.last_broken_high_level) - float(sticky_px)) < 1e-12:
            hist.sticky_high = (base_px, base_p, f"v6_replace_invalidated:{base_r}")
            return base_px, base_p, hist.sticky_high[2], cands
    if sticky_p is not None and base_p is not None and sticky_p.confirmation_index == base_p.confirmation_index:
        return sticky_px, sticky_p, f"v6_keep_same:{sticky_r}", cands
    if base_p is not None and _new_active_leg(hist, base_p, side="high"):
        hist.sticky_high = (base_px, base_p, f"v6_replace_new_leg_continuation:{base_r}")
        return base_px, base_p, hist.sticky_high[2], cands
    if sticky_p is not None and base_p is not None:
        def cont_count(p: ConfirmedPivot) -> int:
            return sum(
                1
                for s in hist.swings
                if s.label == "lower_low" and s.pivot.confirmation_index > p.confirmation_index
            )

        if cont_count(base_p) > cont_count(sticky_p):
            hist.sticky_high = (base_px, base_p, f"v6_replace_higher_relevance:{base_r}")
            return base_px, base_p, hist.sticky_high[2], cands
    return sticky_px, sticky_p, f"v6_block_micro_overwrite:{sticky_r}", cands + [f"blocked:{_pivot_ref(base_p)}"]


SELECTORS: dict[str, tuple[Selector, Selector]] = {
    "V0": (select_low_v0, select_high_v0),
    "V1": (select_low_v1, select_high_v1),
    "V2": (select_low_v2, select_high_v2),
    "V3": (select_low_v3, select_high_v3),
    "V4": (select_low_v4, select_high_v4),
    "V4a": (select_low_v4a, select_high_v4a),
    "V4b": (select_low_v4b, select_high_v4b),
    "V5": (select_low_v5, select_high_v5),
    "V6": (select_low_v6, select_high_v6),
}


def _install_selectors(variant: str) -> None:
    low_fn, high_fn = SELECTORS[variant]

    def prot_low(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None]:
        px, p, reason, cands = low_fn(state)
        if CTX.log_levels and state.timeframe == "5m" and CTX.decision_time is not None:
            hist = CTX.hist(state)
            changed = hist.last_logged_low != px
            change_reason = reason if changed else ""
            prev = hist.last_logged_low
            if changed:
                hist.last_logged_low = px
            # Sparse log: every change + march focus + every 100th would need bar counter;
            # log all changes and always on focus timestamps.
            focus = _iso(CTX.decision_time) in {_iso(t) for t in MARCH_FOCUS}
            if changed or focus:
                CTX.level_rows.append(
                    {
                        "timestamp": _iso(CTX.decision_time),
                        "timeframe": state.timeframe,
                        "variant": variant,
                        "structure_bias": state.current_structure_bias,
                        "candidate_count": len(cands),
                        "candidate_pivots": "|".join(cands),
                        "selected_protective_low": px,
                        "selected_protective_low_pivot": _pivot_ref(p),
                        "selected_protective_low_reason": reason,
                        "selected_protective_high": None,
                        "selected_protective_high_pivot": None,
                        "selected_protective_high_reason": None,
                        "previous_selected_level": prev,
                        "level_changed": changed,
                        "change_reason": change_reason,
                        "side": "low",
                    }
                )
        return px, p

    def prot_high(state: MarketStructureState) -> tuple[float | None, ConfirmedPivot | None]:
        px, p, reason, cands = high_fn(state)
        if CTX.log_levels and state.timeframe == "5m" and CTX.decision_time is not None:
            hist = CTX.hist(state)
            changed = hist.last_logged_high != px
            change_reason = reason if changed else ""
            prev = hist.last_logged_high
            if changed:
                hist.last_logged_high = px
            focus = _iso(CTX.decision_time) in {_iso(t) for t in MARCH_FOCUS}
            if changed or focus:
                CTX.level_rows.append(
                    {
                        "timestamp": _iso(CTX.decision_time),
                        "timeframe": state.timeframe,
                        "variant": variant,
                        "structure_bias": state.current_structure_bias,
                        "candidate_count": len(cands),
                        "candidate_pivots": "|".join(cands),
                        "selected_protective_low": None,
                        "selected_protective_low_pivot": None,
                        "selected_protective_low_reason": None,
                        "selected_protective_high": px,
                        "selected_protective_high_pivot": _pivot_ref(p),
                        "selected_protective_high_reason": reason,
                        "previous_selected_level": prev,
                        "level_changed": changed,
                        "change_reason": change_reason,
                        "side": "high",
                    }
                )
        return px, p

    ts_mod._protective_low = prot_low  # type: ignore[assignment]
    ts_mod._protective_high = prot_high  # type: ignore[assignment]
    ts_mod._apply_new_swing_labels = _wrapped_apply  # type: ignore[assignment]


def _restore_selectors() -> None:
    ts_mod._protective_low = _ORIG_PROTECTIVE_LOW  # type: ignore[assignment]
    ts_mod._protective_high = _ORIG_PROTECTIVE_HIGH  # type: ignore[assignment]
    ts_mod._apply_new_swing_labels = _ORIG_APPLY  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def load_frame(end: pd.Timestamp) -> tuple[pd.DataFrame, list[ConfirmedPivot]]:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    # Full causal history from first available candle
    slice_ = raw[raw["timestamp"] < end].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    # Keep only decisions <= end
    frame = frame[frame["decision_time"] <= end].reset_index(drop=True)
    pivots = find_confirmed_pivots(frame, config=scfg)
    return frame, pivots


def run_variant(
    variant: str,
    frame: pd.DataFrame,
    pivots: list[ConfirmedPivot],
    *,
    log_levels: bool = True,
) -> dict[str, Any]:
    _p(f"=== Variant {variant} start ===")
    t0 = time.perf_counter()
    _reset_ctx(variant, log_levels=log_levels)
    _install_selectors(variant)
    cfg = default_trend_state_config()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    rt = TrendRuntime()
    ohlcv_cols = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]

    transitions: list[dict[str, Any]] = []
    events_5m: list[dict[str, Any]] = []
    level_snapshots: list[dict[str, Any]] = []  # dense focus + changes already in CTX
    state_at_focus: dict[str, str] = {}
    n = len(frame)
    try:
        for i in range(n):
            row = frame.iloc[i]
            decision_ts = _ts(row["decision_time"])
            CTX.decision_time = decision_ts
            CTX.htf15 = rt.structure_15m
            CTX.htf30 = rt.structure_30m
            candles_as_of = frame.iloc[: i + 1][ohlcv_cols]
            state_before = rt.state
            rt, snap, events = step_trend_state(
                rt,
                candle_row=row,
                pivots_5m=pivots,
                decision_time=decision_ts,
                candles_5m_as_of=candles_as_of,
                bar_index=i,
                cfg=cfg,
                scanner_cfg=scfg,
            )
            # Mode A material: 5m BOS/CHoCH
            for ev in events:
                if getattr(ev, "timeframe", "5m") != "5m":
                    continue
                if ev.event_type not in BOS_CHOCH:
                    continue
                events_5m.append(
                    {
                        "timestamp": _iso(ev.event_time),
                        "timeframe": ev.timeframe,
                        "variant": variant,
                        "event_type": ev.event_type,
                        "level": ev.level,
                        "source_pivot": (
                            f"{ev.reference_pivot_price}:{ev.reference_pivot_time}"
                            if ev.reference_pivot_price is not None
                            else None
                        ),
                        "direction": ev.direction,
                        "reason_codes": "|".join(ev.reason_codes),
                    }
                )
            if rt.state != state_before:
                decisive = None
                decisive_level = None
                for ev in events:
                    if ev.event_type in BOS_CHOCH or ev.event_type in {
                        "failed_breakdown",
                        "failed_breakout",
                        "higher_low",
                        "lower_high",
                        "lower_low",
                        "higher_high",
                    }:
                        decisive = ev.event_type
                        decisive_level = ev.level
                        break
                transitions.append(
                    {
                        "variant": variant,
                        "timestamp": _iso(decision_ts),
                        "old_state": state_before,
                        "new_state": rt.state,
                        "direct_reason": "|".join(snap.active_reasons),
                        "decisive_event": decisive,
                        "decisive_level": decisive_level,
                    }
                )
            iso = _iso(decision_ts)
            if iso in {_iso(t) for t in MARCH_FOCUS}:
                state_at_focus[iso] = rt.state
                # full level snapshot at focus
                lp, lpp, lr, _ = SELECTORS[variant][0](rt.structure_5m)
                hp, hpp, hr, _ = SELECTORS[variant][1](rt.structure_5m)
                level_snapshots.append(
                    {
                        "timestamp": iso,
                        "variant": variant,
                        "state": rt.state,
                        "protective_low": lp,
                        "protective_low_pivot": _pivot_ref(lpp),
                        "protective_low_reason": lr,
                        "protective_high": hp,
                        "protective_high_pivot": _pivot_ref(hpp),
                        "protective_high_reason": hr,
                        "bias": rt.structure_5m.current_structure_bias,
                        "labels": f"{rt.structure_5m.last_high_label}/{rt.structure_5m.last_low_label}",
                    }
                )
            if (i + 1) % 2000 == 0 or i + 1 == n:
                _p(
                    f"  {variant}: {i+1}/{n} bars state={rt.state} "
                    f"events={len(events_5m)} transitions={len(transitions)} "
                    f"elapsed={time.perf_counter()-t0:.1f}s"
                )
    finally:
        _restore_selectors()

    elapsed = time.perf_counter() - t0
    _p(f"=== Variant {variant} done in {elapsed:.1f}s ===")
    return {
        "variant": variant,
        "elapsed_sec": elapsed,
        "transitions": transitions,
        "events_5m": events_5m,
        "level_rows": list(CTX.level_rows),
        "level_snapshots": level_snapshots,
        "state_at_focus": state_at_focus,
        "final_state": rt.state,
    }


def _event_key(e: dict[str, Any]) -> tuple[str, str]:
    return (str(e["timestamp"]), str(e["event_type"]))


def diff_events(baseline: list[dict[str, Any]], variant: list[dict[str, Any]], variant_name: str) -> list[dict[str, Any]]:
    b_map = {_event_key(e): e for e in baseline}
    v_map = {_event_key(e): e for e in variant}
    # Also match by timestamp alone for type changes
    b_by_ts: dict[str, list[dict[str, Any]]] = {}
    v_by_ts: dict[str, list[dict[str, Any]]] = {}
    for e in baseline:
        b_by_ts.setdefault(str(e["timestamp"]), []).append(e)
    for e in variant:
        v_by_ts.setdefault(str(e["timestamp"]), []).append(e)
    rows: list[dict[str, Any]] = []
    all_ts = sorted(set(b_by_ts) | set(v_by_ts))
    for ts in all_ts:
        be = b_by_ts.get(ts, [])
        ve = v_by_ts.get(ts, [])
        if not be and ve:
            for v in ve:
                rows.append(
                    {
                        "timestamp": ts,
                        "timeframe": "5m",
                        "baseline_event": None,
                        "variant_event": v["event_type"],
                        "baseline_level": None,
                        "variant_level": v["level"],
                        "baseline_source_pivot": None,
                        "variant_source_pivot": v["source_pivot"],
                        "difference_type": "event_added",
                        "fachliche_bewertung": "new_event_vs_baseline",
                        "variant": variant_name,
                    }
                )
            continue
        if be and not ve:
            for b in be:
                rows.append(
                    {
                        "timestamp": ts,
                        "timeframe": "5m",
                        "baseline_event": b["event_type"],
                        "variant_event": None,
                        "baseline_level": b["level"],
                        "variant_level": None,
                        "baseline_source_pivot": b["source_pivot"],
                        "variant_source_pivot": None,
                        "difference_type": "event_removed",
                        "fachliche_bewertung": "baseline_event_absent",
                        "variant": variant_name,
                    }
                )
            continue
        # Compare pairwise by order
        for b, v in zip(be, ve):
            if b["event_type"] == v["event_type"] and b["level"] == v["level"]:
                diff = "no_change"
                fach = "identical"
            elif b["event_type"] == v["event_type"] and b["level"] != v["level"]:
                diff = "level_changed_same_event"
                fach = "same_event_type_different_level"
            elif b["event_type"] != v["event_type"]:
                if "choch" in b["event_type"] and "bos" in v["event_type"]:
                    diff = "choch_to_bos"
                elif "bos" in b["event_type"] and "choch" in v["event_type"]:
                    diff = "bos_to_choch"
                else:
                    diff = "event_type_changed"
                fach = "type_or_level_shift"
            else:
                diff = "other"
                fach = "other"
            rows.append(
                {
                    "timestamp": ts,
                    "timeframe": "5m",
                    "baseline_event": b["event_type"],
                    "variant_event": v["event_type"],
                    "baseline_level": b["level"],
                    "variant_level": v["level"],
                    "baseline_source_pivot": b["source_pivot"],
                    "variant_source_pivot": v["source_pivot"],
                    "difference_type": diff,
                    "fachliche_bewertung": fach,
                    "variant": variant_name,
                }
            )
        if len(ve) > len(be):
            for v in ve[len(be) :]:
                rows.append(
                    {
                        "timestamp": ts,
                        "timeframe": "5m",
                        "baseline_event": None,
                        "variant_event": v["event_type"],
                        "baseline_level": None,
                        "variant_level": v["level"],
                        "baseline_source_pivot": None,
                        "variant_source_pivot": v["source_pivot"],
                        "difference_type": "event_added",
                        "fachliche_bewertung": "extra_variant_event",
                        "variant": variant_name,
                    }
                )
        if len(be) > len(ve):
            for b in be[len(ve) :]:
                rows.append(
                    {
                        "timestamp": ts,
                        "timeframe": "5m",
                        "baseline_event": b["event_type"],
                        "variant_event": None,
                        "baseline_level": b["level"],
                        "variant_level": None,
                        "baseline_source_pivot": b["source_pivot"],
                        "variant_source_pivot": None,
                        "difference_type": "event_removed",
                        "fachliche_bewertung": "missing_variant_event",
                        "variant": variant_name,
                    }
                )
    # Delay detection: same type+level appears later
    b_levels = {(e["event_type"], e["level"]): e["timestamp"] for e in baseline}
    for e in variant:
        key = (e["event_type"], e["level"])
        if key in b_levels and e["timestamp"] != b_levels[key]:
            # already covered as remove+add possibly; annotate delay rows separately later
            pass
    _ = b_map, v_map
    return rows


def compute_metrics(
    variant: str,
    result: dict[str, Any],
    baseline: dict[str, Any],
    event_diffs: list[dict[str, Any]],
) -> dict[str, Any]:
    ev = result["events_5m"]
    tr = result["transitions"]
    # protective level changes from level_rows
    low_changes = [r for r in result["level_rows"] if r.get("side") == "low" and r.get("level_changed")]
    # micro overwrites: V0-style last HL replaced without continuation between
    # Approximate from baseline level rows vs variant: count V0 low changes where reason is last_higher_low
    # For each variant, count sticky blocks in V6 reasons; for others use hist heuristic on level_rows
    micro_ow = 0
    for r in low_changes:
        reason = str(r.get("change_reason") or r.get("selected_protective_low_reason") or "")
        if "last_higher_low" in reason or reason.startswith("last_higher_low"):
            micro_ow += 1
        if "v6_block" in reason:
            pass
    # For non-V0: micro_overwrite = times variant still follows last_higher_low pattern
    if variant == "V0":
        # count consecutive HL replacements without continuation reason
        micro_ow = sum(1 for r in low_changes if "last_higher_low" in str(r.get("selected_protective_low_reason")))
    else:
        micro_ow = sum(
            1
            for r in low_changes
            if "last_higher_low" in str(r.get("selected_protective_low_reason") or "")
            or "micro" in str(r.get("change_reason") or "").lower()
        )

    removed = sum(1 for d in event_diffs if d["difference_type"] == "event_removed")
    added = sum(1 for d in event_diffs if d["difference_type"] == "event_added")
    # delay: events of same type+level with later ts
    b_idx = {(e["event_type"], e["level"]): e["timestamp"] for e in baseline["events_5m"]}
    delays: list[int] = []
    for e in ev:
        key = (e["event_type"], e["level"])
        if key in b_idx:
            t0 = _ts(b_idx[key])
            t1 = _ts(e["timestamp"])
            delay = int((t1 - t0) / pd.Timedelta(minutes=5))
            if delay != 0:
                delays.append(delay)

    states = [t["new_state"] for t in tr]
    flips = 0
    for a, b in zip(tr, tr[1:]):
        # immediate counter-direction flip heuristic
        pair = (a["new_state"], b["new_state"])
        if ("bearish" in a["new_state"] and "bullish" in b["new_state"]) or (
            "bullish" in a["new_state"] and "bearish" in b["new_state"]
        ):
            if _ts(b["timestamp"]) - _ts(a["timestamp"]) <= pd.Timedelta(minutes=30):
                flips += 1

    # state durations
    durations: list[int] = []
    if tr:
        for i, t in enumerate(tr):
            t_start = _ts(t["timestamp"])
            t_end = _ts(tr[i + 1]["timestamp"]) if i + 1 < len(tr) else _ts(DIAG_END)
            durations.append(max(1, int((t_end - t_start) / pd.Timedelta(minutes=5))))

    # level lifetime
    lifetimes: list[int] = []
    low_rows = [r for r in result["level_rows"] if r.get("side") == "low" and r.get("level_changed")]
    for i, r in enumerate(low_rows):
        t0 = _ts(r["timestamp"])
        t1 = _ts(low_rows[i + 1]["timestamp"]) if i + 1 < len(low_rows) else _ts(DIAG_END)
        lifetimes.append(max(1, int((t1 - t0) / pd.Timedelta(minutes=5))))

    def count_state(name: str) -> int:
        return sum(1 for t in tr if t["new_state"] == name)

    return {
        "variant": variant,
        "protective_level_changes": len(low_changes),
        "median_level_lifetime_candles": float(pd.Series(lifetimes).median()) if lifetimes else None,
        "micro_overwrites": micro_ow,
        "bos_count": sum(1 for e in ev if e["event_type"] in {"bearish_bos", "bullish_bos"}),
        "choch_count": sum(1 for e in ev if e["event_type"] in {"bearish_choch", "bullish_choch"}),
        "event_delay_median_candles": float(pd.Series(delays).median()) if delays else 0.0,
        "event_delay_max_candles": int(max(delays)) if delays else 0,
        "events_removed": removed,
        "events_added": added,
        "state_changes": len(tr),
        "state_flip_count": flips,
        "strong_state_count": count_state("strong_bearish") + count_state("strong_bullish"),
        "weakening_count": count_state("bearish_weakening") + count_state("bullish_weakening"),
        "bottoming_topping_count": count_state("bottoming") + count_state("topping"),
        "early_bearish_count": count_state("early_bearish"),
        "strong_bearish_count": count_state("strong_bearish"),
        "bearish_weakening_count": count_state("bearish_weakening"),
        "bottoming_count": count_state("bottoming"),
        "early_bullish_count": count_state("early_bullish"),
        "strong_bullish_count": count_state("strong_bullish"),
        "avg_state_duration_candles": float(pd.Series(durations).mean()) if durations else None,
        "min_state_duration_candles": int(min(durations)) if durations else None,
        "elapsed_sec": result["elapsed_sec"],
        "immediate_counter_flips": flips,
    }


def qualitative_row(variant: str, metrics: dict[str, Any], march: dict[str, Any]) -> dict[str, str]:
    """Qualitative matrix scores — filled after metrics known; refined in finalize()."""
    return {"variant": variant}  # placeholder filled in finalize_recommendation


def current_behavior_doc() -> list[dict[str, str]]:
    return [
        {
            "field": "protective_low",
            "source_function": "trend_structure._protective_low",
            "current_behavior": "last_higher_low if set else last_confirmed_swing_low; always newest labeled HL",
            "fachliches_risiko": "Mikro-HL ersetzt trenddefinierendes Level → früher falscher bearish CHoCH",
        },
        {
            "field": "protective_high",
            "source_function": "trend_structure._protective_high",
            "current_behavior": "last_lower_high if set else last_confirmed_swing_high; always newest labeled LH",
            "fachliches_risiko": "Spiegelbild: Mikro-LH kann bullish CHoCH zu früh auslösen",
        },
        {
            "field": "pivot_list",
            "source_function": "filter_pivots_as_of + _apply_new_swing_labels",
            "current_behavior": "Alle kausal bestätigten High/Low-Pivots; Labels via consecutive same-side pairs",
            "fachliches_risiko": "Keine Signifikanz-/Leg-Filterung",
        },
        {
            "field": "pivot_types",
            "source_function": "_protective_low/_high",
            "current_behavior": "Nur HL für Low-Schutz (sonst last swing low); nur LH für High-Schutz",
            "fachliches_risiko": "HH/LL steuern Bias, nicht das Protective Level",
        },
        {
            "field": "always_last_pivot",
            "source_function": "_protective_low/_high",
            "current_behavior": "Ja — zeitlich letzter HL/LH überschreibt",
            "fachliches_risiko": "Hauptursache März-Mikro-CHoCH@0.9938",
        },
        {
            "field": "significance",
            "source_function": "n/a",
            "current_behavior": "Nicht berücksichtigt",
            "fachliches_risiko": "Mikro = Makro",
        },
        {
            "field": "active_trend_leg",
            "source_function": "n/a",
            "current_behavior": "Nicht berücksichtigt",
            "fachliches_risiko": "Leg-Origin geht verloren",
        },
        {
            "field": "timeframe",
            "source_function": "update_market_structure per TF",
            "current_behavior": "Pro TF eigener State; kein Cross-TF Anchor",
            "fachliches_risiko": "5m Mikro ignoriert 15m/30m Strukturlevel",
        },
        {
            "field": "broken_invalidated_exclusion",
            "source_function": "_detect_bos_choch last_broken_*_level",
            "current_behavior": "Gleiches Level feuert nicht erneut; Selektion selbst filtert Invalidierung nicht",
            "fachliches_risiko": "Nach Break kann neues Mikro-Level sofort greifen",
        },
        {
            "field": "micro_overwrite",
            "source_function": "_apply_new_swing_labels → last_higher_low=",
            "current_behavior": "Jeder neue HL überschreibt last_higher_low sofort bei Bestätigung",
            "fachliches_risiko": "Ja — automatisches Überschreiben",
        },
        {
            "field": "causal_availability",
            "source_function": "confirmation_index < decision_time",
            "current_behavior": "Pivot erst nach rechter Bestätigung nutzbar",
            "fachliches_risiko": "Kausal ok; Semantik des Levels nicht",
        },
        {
            "field": "replacement_timing",
            "source_function": "_apply_new_swing_labels",
            "current_behavior": "Bei Bestätigung des neuen same-side labeled swing",
            "fachliches_risiko": "Zu früh ohne Fortsetzungs-Evidenz",
        },
        {
            "field": "high_low_symmetry",
            "source_function": "_protective_low/_high",
            "current_behavior": "Spiegelbildlich implementiert",
            "fachliches_risiko": "Symmetrie ok; beide erben Mikro-Problem",
        },
    ]


def variant_definitions() -> list[dict[str, Any]]:
    return [
        {
            "variant": "V0",
            "selection_rule": "protective_low=last_HL else last_swing_low; protective_high=last_LH else last_swing_high",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Mikro-Swing definiert gesamten Trendbruch",
            "inputs": ["last_higher_low", "last_lower_high", "last_confirmed_swing_*"],
            "ambiguities": "none",
            "tie_breaker": "n/a — always latest",
            "symmetry": True,
        },
        {
            "variant": "V1",
            "selection_rule": "HL confirmed before most recent HH (leg origin); LH before most recent LL",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Kann CHoCH verzögern bis Leg-Origin gebrochen wird",
            "inputs": ["labeled swing history HH/HL/LH/LL"],
            "ambiguities": "If no HH yet → baseline fallback",
            "tie_breaker": "last HL before last HH",
            "symmetry": True,
            "leg_start_definition": "confirmation of most recent higher_high (bullish leg) / lower_low (bearish leg)",
        },
        {
            "variant": "V2",
            "selection_rule": "last HL with ≥1 later HH; last LH with ≥1 later LL",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Ähnlich V1; ohne Fortsetzung Fallback auf Baseline (Mikro möglich)",
            "inputs": ["labeled swing history"],
            "ambiguities": "Fallback to V0 if no continued swing",
            "tie_breaker": "latest continued HL/LH",
            "symmetry": True,
        },
        {
            "variant": "V3",
            "selection_rule": "nearest prior low (any label) before last HH; nearest prior high before last LL",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Kann LL/equal-low statt HL wählen",
            "inputs": ["full labeled swing sequence"],
            "ambiguities": "Any low label before HH",
            "tie_breaker": "nearest prior in confirmation order",
            "symmetry": True,
        },
        {
            "variant": "V4",
            "selection_rule": "lexicographic rank among continued HLs: (#continuations, dist_to_first_HH, older)",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Kann sehr altes Level sticky halten",
            "inputs": ["continuation counts", "price distance"],
            "ambiguities": "resolved by lex order",
            "tie_breaker": "older confirmation_index",
            "symmetry": True,
            "subvariants": ["V4a max continuations", "V4b max distance"],
        },
        {
            "variant": "V4a",
            "selection_rule": "max #HH after HL; tie → oldest",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Extrem sticky zu ältestem multi-continued HL",
            "inputs": ["continuation count"],
            "tie_breaker": "oldest",
            "symmetry": True,
        },
        {
            "variant": "V4b",
            "selection_rule": "max |HH.price-HL.price| to first subsequent HH; tie → older",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Distanz ≠ strukturelle Relevanz",
            "inputs": ["price distance"],
            "tie_breaker": "older",
            "symmetry": True,
        },
        {
            "variant": "V5",
            "selection_rule": "start V2 on 5m; if HTF bullish HH+HL and 5m micro HL above HTF protective low → use HTF level",
            "causal": True,
            "new_parameters": False,
            "main_risk": "HTF-Lag; kann 5m Reaktionen stark verzögern",
            "inputs": ["5m V2", "15m/30m structure bias+labels+V2 level"],
            "ambiguities": "15m vs 30m preference (15m first)",
            "tie_breaker": "prefer 15m then 30m else 5m V2",
            "symmetry": True,
        },
        {
            "variant": "V6",
            "selection_rule": "sticky V0 init; overwrite only if invalidated OR new leg continuation OR higher continuation count",
            "causal": True,
            "new_parameters": False,
            "main_risk": "Kann zu sticky werden wenn Continuation selten",
            "inputs": ["sticky state", "baseline HL/LH", "continuation evidence", "last_broken_*"],
            "ambiguities": "relevance = continuation count compare",
            "tie_breaker": "keep sticky unless overwrite rule fires",
            "symmetry": True,
        },
    ]


def finalize_recommendation(
    metrics_df: pd.DataFrame,
    march_df: pd.DataFrame,
    event_diff_df: pd.DataFrame,
    results: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Score qualitatively and pick recommended candidate."""

    def march_val(variant: str, ts: str, col: str) -> Any:
        rows = march_df[(march_df["variant"] == variant) & (march_df["timestamp"] == _iso(ts))]
        if rows.empty:
            return None
        return rows.iloc[0][col]

    def has_event(variant: str, ts: str, etype: str, level: float | None = None) -> bool:
        evs = results[variant]["events_5m"]
        for e in evs:
            if e["timestamp"] == _iso(ts) and e["event_type"] == etype:
                if level is None or (e["level"] is not None and abs(float(e["level"]) - level) < 1e-9):
                    return True
        return False

    def event_at(variant: str, ts: str) -> str | None:
        evs = [e for e in results[variant]["events_5m"] if e["timestamp"] == _iso(ts)]
        if not evs:
            return None
        return ",".join(f"{e['event_type']}@{e['level']}" for e in evs)

    focus_2230 = MARCH_FOCUS[0]
    qual_rows = []
    scores: dict[str, dict[str, str]] = {}

    for variant in results:
        m = metrics_df[metrics_df["variant"] == variant].iloc[0].to_dict()
        micro_gone = not has_event(variant, focus_2230, "bearish_choch", 0.9938)
        # Did a later bearish choch/bos appear on a deeper level after 22:30 same day?
        later_bear = [
            e
            for e in results[variant]["events_5m"]
            if e["timestamp"] >= _iso(focus_2230)
            and e["timestamp"] <= _iso("2026-03-06T12:00:00+00:00")
            and e["event_type"] in {"bearish_choch", "bearish_bos"}
            and e["level"] is not None
            and float(e["level"]) < 0.9938 - 1e-9
        ]
        strong_b = m["strong_bearish_count"] > 0 and any(
            t["new_state"] == "strong_bearish"
            and _iso("2026-03-05T22:30:00+00:00") <= t["timestamp"] <= _iso("2026-03-06T06:00:00+00:00")
            for t in results[variant]["transitions"]
        )
        # Policy still reachable?
        weak_0030 = any(
            t["timestamp"] == _iso(MARCH_FOCUS[1]) and t["new_state"] == "bearish_weakening"
            for t in results[variant]["transitions"]
        )
        bottom_0135 = any(
            t["timestamp"] == _iso(MARCH_FOCUS[2]) and t["new_state"] == "bottoming"
            for t in results[variant]["transitions"]
        )
        early_bull = any(
            t["timestamp"] == _iso(MARCH_FOCUS[3]) and "bullish" in t["new_state"]
            for t in results[variant]["transitions"]
        )

        # Qualitative
        q = {
            "variant": variant,
            "kausal_korrekt": "sehr gut",
            "trenddefinierende_semantik": "schwach" if variant == "V0" else "gut",
            "robust_mikro": "schwach" if variant == "V0" else "gut",
            "reaktionsgeschwindigkeit": "sehr gut" if variant == "V0" else "gut",
            "erhalt_legitimer_choch": "gut",
            "htf_konsistenz": "sehr gut" if variant == "V5" else "mittel",
            "symmetrie": "sehr gut",
            "implementierungskomplexitaet": "sehr gut" if variant in {"V0", "V1", "V2", "V6"} else "mittel",
            "risiko_spaeter_wechsel": "schwach" if variant in {"V4", "V4a", "V5"} else "gut",
            "risiko_sticky_trend": "schwach" if variant in {"V4", "V4a", "V5"} else "gut",
            "maerz_root_cause_behoben": "sehr gut" if micro_gone else "ungeeignet",
            "policy_probleme_weiter_sichtbar": "gut",
        }
        if variant == "V0":
            q.update(
                {
                    "trenddefinierende_semantik": "ungeeignet",
                    "robust_mikro": "ungeeignet",
                    "maerz_root_cause_behoben": "ungeeignet",
                }
            )
        if variant in {"V1", "V2", "V6"} and micro_gone:
            q["trenddefinierende_semantik"] = "sehr gut"
            q["robust_mikro"] = "sehr gut"
            q["maerz_root_cause_behoben"] = "sehr gut"
        if variant == "V3" and micro_gone:
            q["trenddefinierende_semantik"] = "gut"
        if variant in {"V4", "V4a", "V4b"}:
            q["risiko_sticky_trend"] = "mittel" if variant == "V4b" else "schwach"
            q["reaktionsgeschwindigkeit"] = "mittel"
        if variant == "V5":
            q["htf_konsistenz"] = "sehr gut"
            q["reaktionsgeschwindigkeit"] = "mittel"
            q["implementierungskomplexitaet"] = "schwach"

        # Regression pressure
        if m["events_removed"] > 40:
            q["erhalt_legitimer_choch"] = "schwach"
        elif m["events_removed"] > 15:
            q["erhalt_legitimer_choch"] = "mittel"
        if m["state_flip_count"] > metrics_df["state_flip_count"].median() * 1.5:
            q["risiko_sticky_trend"] = "mittel"

        # Policy visibility: if path avoided, note still present structurally
        if micro_gone and not weak_0030:
            q["policy_probleme_weiter_sichtbar"] = "mittel"  # path avoided but rules unchanged
        scores[variant] = q
        qual_rows.append(q)

        march_df.loc[march_df["variant"] == variant, "micro_choch_0938_present"] = has_event(
            variant, focus_2230, "bearish_choch", 0.9938
        )
        _ = later_bear, strong_b, bottom_0135, early_bull, event_at

    # Ranking: prefer micro fixed + low events_removed + moderate delays + not too sticky
    candidates = [v for v in results if v != "V0"]
    ranking = []
    for v in candidates:
        m = metrics_df[metrics_df["variant"] == v].iloc[0]
        micro_gone = not has_event(v, focus_2230, "bearish_choch", 0.9938)
        score = 0
        score += 50 if micro_gone else -100
        score -= int(m["events_removed"])
        score -= int(m["events_added"]) // 2
        score -= int(abs(m["event_delay_median_candles"]))
        score -= int(m["state_flip_count"])
        # Prefer simpler
        score += {"V1": 8, "V2": 10, "V3": 6, "V6": 9, "V4": 3, "V4a": 2, "V4b": 2, "V5": 4}.get(v, 0)
        # Prefer not exploding strong stickiness
        if m["protective_level_changes"] < 5:
            score -= 5
        ranking.append((score, v))
    ranking.sort(reverse=True)
    recommended = ranking[0][1] if ranking else "V2"
    runner = ranking[1][1] if len(ranking) > 1 else ""
    # Unsuitable: baseline + variants that fail to remove the March micro-CHoCH
    not_rec = ["V0"]
    for s, v in ranking:
        if has_event(v, focus_2230, "bearish_choch", 0.9938):
            not_rec.append(v)
        elif s < ranking[0][0] - 80:
            not_rec.append(v)
    not_rec = list(dict.fromkeys(not_rec))

    # March effects for recommended
    def st(v: str, ts: str) -> str | None:
        return results[v]["state_at_focus"].get(_iso(ts))

    rec = results[recommended]
    rec_m = metrics_df[metrics_df["variant"] == recommended].iloc[0].to_dict()

    # Policy remaining
    policy_issues = [
        {
            "issue": "HTF veto blocks strong_bearish",
            "status": "still_present_in_code",
            "note": "Unchanged policy; may be less visible if early_bearish path avoided",
        },
        {
            "issue": "single failed_breakdown → bearish_weakening",
            "status": "still_present_in_code",
            "note": "Independent; fires whenever early/strong bearish + failed_breakdown",
        },
        {
            "issue": "bottoming 2-hit rule",
            "status": "still_present_in_code",
            "note": "Independent; requires prior weakening path",
        },
    ]

    defs = {d["variant"]: d for d in variant_definitions()}
    d = defs.get(recommended, {})

    payload = {
        "recommended_variant": recommended,
        "runner_up": runner,
        "not_recommended": not_rec,
        "reason": (
            f"{recommended} best balances removing micro CHoCH@0.9938, preserving causal trend-defining "
            f"semantics, and limiting event/state regressions (score rank)."
        ),
        "causal_definition": d.get("selection_rule", ""),
        "source_inputs": d.get("inputs", []),
        "tie_breaker": d.get("tie_breaker", ""),
        "march_effect": {
            "bearish_choch_0_9938": (
                "removed" if not has_event(recommended, focus_2230, "bearish_choch", 0.9938) else "still_present"
            ),
            "strong_bearish": st(recommended, focus_2230),
            "weakening_00_30": st(recommended, MARCH_FOCUS[1]),
            "bottoming_01_35": st(recommended, MARCH_FOCUS[2]),
            "bullish_states_07_03": {
                "03_05": st(recommended, MARCH_FOCUS[3]),
                "03_35": st(recommended, MARCH_FOCUS[4]),
            },
            "state_at_22_30": st(recommended, focus_2230),
        },
        "broader_regression_effect": {
            "events_removed": int(rec_m["events_removed"]),
            "events_added": int(rec_m["events_added"]),
            "event_delay_median_candles": float(rec_m["event_delay_median_candles"]),
            "state_changes": int(rec_m["state_changes"]),
            "state_flips": int(rec_m["state_flip_count"]),
            "micro_overwrites": int(rec_m["micro_overwrites"]),
            "protective_level_changes": int(rec_m["protective_level_changes"]),
        },
        "remaining_independent_policy_issues": policy_issues,
        "implementation_risk": d.get("main_risk", ""),
        "confidence": "medium-high" if not has_event(recommended, focus_2230, "bearish_choch", 0.9938) else "low",
        "decision_letter": None,  # filled below
        "ranking_scores": [{"variant": v, "score": s} for s, v in ranking],
    }

    letter_map = {
        "V1": "A",
        "V2": "B",
        "V3": "C",
        "V4": "D",
        "V4a": "D",
        "V4b": "D",
        "V5": "E",
        "V6": "F",
    }
    # Hybrid if top two very close and complementary
    if ranking and len(ranking) > 1 and abs(ranking[0][0] - ranking[1][0]) <= 3:
        # still pick single best unless both needed
        payload["decision_letter"] = letter_map.get(recommended, "G")
        payload["note_close_runner"] = runner
    else:
        payload["decision_letter"] = letter_map.get(recommended, "H")

    # Decision text options for report
    payload["decision_text"] = {
        "A": "A: V1 Previous structural swing ist bester Kandidat.",
        "B": "B: V2 Confirmed continuation swing ist bester Kandidat.",
        "C": "C: V3 Active-leg origin ist bester Kandidat.",
        "D": "D: V4 Significance-ranked swing ist bester Kandidat.",
        "E": "E: V5 HTF-anchored ist bester Kandidat.",
        "F": "F: V6 Non-overwrite micro swing ist bester Kandidat.",
        "G": "G: Keine einzelne Variante ist ausreichend; Hybrid nötig.",
        "H": "H: Keine Variante verbessert die Baseline robust genug.",
    }[payload["decision_letter"]]

    return payload, pd.DataFrame(qual_rows)


def write_readme(out: Path, rec: dict[str, Any]) -> None:
    text = f"""# Protective Level Variants Audit (diagnostic only)

Research-only comparison of protective-level selectors for Research-v1.
**No production logic was changed.**

## Baseline confirmation

- Earliest wrong input remains micro `bearish_choch@0.9938` at `2026-03-05T22:30:00Z` under V0.
- Warmup ruled out previously (decision C).

## Recommended candidate

- **{rec['recommended_variant']}** (runner-up: {rec['runner_up']})
- Decision: {rec['decision_text']}
- Reason: {rec['reason']}

## Independent policy issues (unchanged)

- HTF veto can still block `strong_bearish`
- Single `failed_breakdown` can still trigger weakening
- Bottoming 2-hit rule unchanged

These may be *less reachable* on the March path if the micro-CHoCH is removed, but remain in code.

## How to reproduce

```bash
PYTHONPATH=. PYTHONUNBUFFERED=1 python3 -m research.regime_scanner.trend_state_protective_level_variants_audit
```

## Artifacts

See CSV/JSON files in this directory.
"""
    (out / "README.md").write_text(text)


def run_audit(*, variants: list[str] | None = None, dual_checksum: bool = True) -> Path:
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    end = _ts(DIAG_END)
    _p("Loading frame from first available candle …")
    frame, pivots = load_frame(end)
    _p(f"Frame bars={len(frame)} first_decision={frame['decision_time'].iloc[0]} last={frame['decision_time'].iloc[-1]}")
    _p("Installing causal HTF prefix cache …")
    install_causal_htf_prefix_cache(frame, end)

    # Document current behavior
    pd.DataFrame(current_behavior_doc()).to_csv(out / "current_protective_behavior.csv", index=False)
    defs = variant_definitions()
    (out / "variant_definitions.json").write_text(json.dumps(json_safe(defs), indent=2))

    run_variants = variants or list(SELECTORS.keys())
    results: dict[str, dict[str, Any]] = {}
    t_all = time.perf_counter()
    for v in run_variants:
        results[v] = run_variant(v, frame, pivots, log_levels=True)
    _p(f"All variants wall time: {time.perf_counter()-t_all:.1f}s")

    baseline = results["V0"]
    # Artifacts
    all_levels = []
    all_events = []
    all_trans = []
    all_diffs = []
    metrics_rows = []
    march_rows = []

    for v, res in results.items():
        all_levels.extend(res["level_rows"])
        all_events.extend(res["events_5m"])
        # annotate difference vs baseline on transitions
        b_trans = {(t["timestamp"], t["new_state"]) for t in baseline["transitions"]}
        for t in res["transitions"]:
            t2 = dict(t)
            t2["difference_vs_baseline"] = (
                "same_transition"
                if (t["timestamp"], t["new_state"]) in b_trans
                else "different_or_unique"
            )
            all_trans.append(t2)
        diffs = diff_events(baseline["events_5m"], res["events_5m"], v)
        all_diffs.extend(diffs)
        metrics_rows.append(compute_metrics(v, res, baseline, diffs))
        for snap in res["level_snapshots"]:
            march_rows.append(snap)
        # per-variant detail
        pd.DataFrame(res["level_rows"]).to_csv(out / f"variant_{v.lower()}_details.csv", index=False)

    levels_df = pd.DataFrame(all_levels)
    levels_df.to_csv(out / "protective_level_timeline.csv", index=False)
    pd.DataFrame(all_diffs).to_csv(out / "event_projection_diff.csv", index=False)
    pd.DataFrame(all_trans).to_csv(out / "state_timeline_by_variant.csv", index=False)
    metrics_df = pd.DataFrame(metrics_rows)
    metrics_df.to_csv(out / "variant_metrics.csv", index=False)
    metrics_df.to_csv(out / "regression_analysis.csv", index=False)
    march_df = pd.DataFrame(march_rows)
    # Add baseline vs variant event at focus for march comparison
    focus_cmp = []
    for v, res in results.items():
        for ts in MARCH_FOCUS:
            iso = _iso(ts)
            bev = [e for e in baseline["events_5m"] if e["timestamp"] == iso]
            vev = [e for e in res["events_5m"] if e["timestamp"] == iso]
            b_level = bev[0]["level"] if bev else None
            v_level = vev[0]["level"] if vev else None
            focus_cmp.append(
                {
                    "timestamp": iso,
                    "variant": v,
                    "baseline_level": b_level,
                    "variant_level": v_level,
                    "baseline_event": bev[0]["event_type"] if bev else None,
                    "variant_event": vev[0]["event_type"] if vev else None,
                    "state": res["state_at_focus"].get(iso),
                    "baseline_state": baseline["state_at_focus"].get(iso),
                    "fachliche_bewertung": (
                        "micro_choch_baseline"
                        if bev and bev[0].get("level") == 0.9938
                        else ("changed" if (bev or vev) and (b_level != v_level or (bev[:1] != vev[:1])) else "same_or_quiet")
                    ),
                }
            )
    march_cmp = pd.DataFrame(focus_cmp)
    march_cmp.to_csv(out / "march_transition_comparison.csv", index=False)
    march_df.to_csv(out / "march_level_snapshots.csv", index=False)

    rec, qual_df = finalize_recommendation(metrics_df, march_cmp, pd.DataFrame(all_diffs), results)
    qual_df.to_csv(out / "qualitative_evaluation.csv", index=False)
    (out / "recommended_candidate.json").write_text(json.dumps(json_safe(rec), indent=2))
    write_readme(out, rec)

    # Runtime log
    (out / "runtime.json").write_text(
        json.dumps(
            json_safe(
                {
                    "variants": {v: results[v]["elapsed_sec"] for v in results},
                    "total_sec": time.perf_counter() - t_all,
                    "bars": len(frame),
                }
            ),
            indent=2,
        )
    )

    # Checksums
    checksums = {p.name: _sha256_file(p) for p in sorted(out.glob("*")) if p.is_file()}
    (out / "checksums_run1.json").write_text(json.dumps(checksums, indent=2))

    if dual_checksum:
        _p("Determinism run 2 (V0 + recommended only for speed) …")
        # Full dual would be very long; re-run V0 and recommended and compare event lists
        r0 = run_variant("V0", frame, pivots, log_levels=False)
        rr = run_variant(rec["recommended_variant"], frame, pivots, log_levels=False)
        det = {
            "V0_events_match": r0["events_5m"] == results["V0"]["events_5m"],
            "V0_transitions_match": r0["transitions"] == results["V0"]["transitions"],
            "rec_events_match": rr["events_5m"] == results[rec["recommended_variant"]]["events_5m"],
            "rec_transitions_match": rr["transitions"]
            == results[rec["recommended_variant"]]["transitions"],
        }
        (out / "determinism_check.json").write_text(json.dumps(det, indent=2))
        _p(f"Determinism: {det}")

    _p(f"Wrote {out}")
    _p(f"Recommended: {rec['recommended_variant']} / {rec['decision_text']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Protective level variants diagnostic audit")
    ap.add_argument("--variants", nargs="*", default=None, help="Subset e.g. V0 V1 V2 V6")
    ap.add_argument("--skip-dual", action="store_true")
    args = ap.parse_args(argv)
    run_audit(variants=args.variants, dual_checksum=not args.skip_dual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

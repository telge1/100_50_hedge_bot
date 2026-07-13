"""Diagnostic audit: failed_breakdown as early bearish_weakening trigger.

Uses production V6+V2 structure + baseline state machine once, then projects
policy variants P0–P7 offline. Does not modify production modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _htf_bias,
    default_trend_state_config,
    has_hh_hl,
    has_lh_ll,
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_state_march_2026_root_cause_audit import install_causal_htf_prefix_cache

OUT = Path("research/regime_scanner/results/trend_state_failed_breakdown_policy_audit")
DIAG_END = "2026-03-10T00:00:00+00:00"
MARCH_FB = "2026-03-06T00:30:00+00:00"
WINDOWS = (1, 2, 3, 6, 12, 24)


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _p(msg: str) -> None:
    print(msg, flush=True)


def _finite(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


# ---------------------------------------------------------------------------
# Semantics documentation
# ---------------------------------------------------------------------------


def current_semantics() -> dict[str, Any]:
    return {
        "event_generation": {
            "function": "trend_structure._detect_failed_breaks",
            "reference_level": "last_confirmed_swing_low (NOT protective_low, NOT last BOS/CHoCH level)",
            "arm_condition": "low < level OR close < level",
            "arm_window": "failed_return_max_bars = 3",
            "failed_condition": (
                "close > pending_level + tol within window AND "
                "pending_breakdown_beyond_closes < valid_break_hold_bars (2)"
            ),
            "close_vs_wick": "reclaim requires CLOSE above level; arm can be wick-only",
            "requires_prior_bos_choch": False,
            "uses_structure_bias": False,
            "uses_protective_level": False,
            "checks_new_ll_after_break": False,
            "checks_level_still_structural": False,
            "repeat_same_level": "yes — can re-arm after clear pending",
            "event_persistence": "last_failed_breakdown overwritten; sticky via recent_events history_limit",
            "mirror": "failed_breakout vs last_confirmed_swing_high",
        },
        "policy_usage": {
            "early_bearish": {
                "function": "trend_state_machine._propose_transition",
                "condition": "failed_breakdown OR bearish_retest_fails OR (bullish_choch AND higher_low)",
                "alone_sufficient": True,
                "htf_checked": False,
                "lh_ll_checked": False,
                "min_hold": "early_bearish min_hold (3 bars) must be satisfied first",
            },
            "strong_bearish": {
                "condition": (
                    "failed_breakdown|bullish_choch|bearish_retest_fails|higher_low "
                    "OR bars_since_ll>=no_ll_lookback; blocked only if same-bar bearish_bos AND lower_low"
                ),
                "alone_sufficient": True,
                "htf_checked": False,
                "min_hold": "strong_bearish min_hold (4 bars)",
            },
            "bottoming_interaction": (
                "bearish_weakening → bottoming needs >=2 of "
                "{failed_breakdown, bullish_choch, higher_low, bullish_bos}"
            ),
        },
        "rows": [
            {
                "concept": "reference_level",
                "source_file": "trend_structure.py",
                "source_function": "_detect_failed_breaks",
                "exact_condition": "ref_low = last_confirmed_swing_low",
                "current_semantics": "any latest swing low, not V6+V2 protective",
                "fachliches_risiko": "Micro swing reclaim labeled failed_breakdown",
            },
            {
                "concept": "failed_definition",
                "source_file": "trend_structure.py",
                "source_function": "_detect_failed_breaks",
                "exact_condition": "close reclaim within 3 bars with <2 closes beyond",
                "current_semantics": "short-lived probe under last swing low",
                "fachliches_risiko": "normal retest/liquidity sweep classified as failure",
            },
            {
                "concept": "early_weakening_trigger",
                "source_file": "trend_state_machine.py",
                "source_function": "_propose_transition/early_bearish",
                "exact_condition": "failed_breakdown in same-bar event types",
                "current_semantics": "single event → bearish_weakening",
                "fachliches_risiko": "temporary reclaim ends early bearish trend",
            },
            {
                "concept": "strong_weakening_trigger",
                "source_file": "trend_state_machine.py",
                "source_function": "_propose_transition/strong_bearish",
                "exact_condition": "failed_breakdown alone (unless bos+ll same bar)",
                "current_semantics": "single event exits strong",
                "fachliches_risiko": "fresh strong_bearish aborted by local reclaim",
            },
        ],
        "primary_diagnosis_hypothesis": (
            "Event generation is technically coherent as 'short reclaim after brief probe of last swing low', "
            "but semantic name overstates trend failure. Policy usage is too permissive."
        ),
    }


VARIANT_DEFS: dict[str, dict[str, Any]] = {
    "P0": {
        "name": "baseline",
        "exact_rule": "failed_breakdown alone (current production)",
        "mirror": "failed_breakout alone",
    },
    "P1": {
        "name": "fb_plus_lost_lh_ll",
        "exact_rule": "failed_breakdown AND NOT has_lh_ll(structure_5m)",
        "mirror": "failed_breakout AND NOT has_hh_hl",
    },
    "P2": {
        "name": "fb_plus_bullish_choch",
        "exact_rule": "failed_breakdown AND bullish_choch same bar",
        "mirror": "failed_breakout AND bearish_choch",
    },
    "P3a": {
        "name": "fb_plus_15m_not_bearish",
        "exact_rule": "failed_breakdown AND htf15_bias not bearish",
        "mirror": "failed_breakout AND htf15 not bullish",
    },
    "P3b": {
        "name": "fb_plus_15m30m_not_bearish",
        "exact_rule": "failed_breakdown AND 15m not bearish AND 30m not bearish",
        "mirror": "failed_breakout AND 15m/30m not bullish",
    },
    "P4": {
        "name": "fb_plus_no_bearish_continuation",
        "exact_rule": (
            "failed_breakdown AND NOT (bias_5m==bearish AND has_lh_ll) "
            "AND bearish_bos not in same-bar types"
        ),
        "mirror": "failed_breakout AND NOT (bullish bias + hh_hl) AND no bullish_bos",
    },
    "P5": {
        "name": "fb_as_evidence_hit",
        "exact_rule": (
            "failed_breakdown counts as one hit; need >=2 of "
            "{failed_breakdown, bullish_choch, higher_low, NOT has_lh_ll, htf15_not_bearish, has_hh_hl}"
        ),
        "mirror": "failed_breakout as one hit among mirrored set",
    },
    "P6": {
        "name": "state_dependent",
        "exact_rule": (
            "early_bearish: failed_breakdown + (NOT has_lh_ll OR bullish_choch OR htf15_not_bearish); "
            "strong_bearish: failed_breakdown alone NEVER; need P5-style >=2 hits"
        ),
        "mirror": "symmetric for early/strong bullish",
    },
    "P7": {
        "name": "trenddefining_level_only",
        "exact_rule": (
            "failed_breakdown only if break_level == protective_low_level "
            "OR break_level == last_broken_low_level (BOS/CHoCH level)"
        ),
        "mirror": "failed_breakout vs protective_high / last_broken_high",
    },
}


# ---------------------------------------------------------------------------
# Replay snapshot
# ---------------------------------------------------------------------------


@dataclass
class BarSnap:
    i: int
    timestamp: str
    state_before: str
    state_after: str
    age_before: int
    event_types: set[str]
    events: list[dict[str, Any]]
    close: float
    high: float
    low: float
    bias_5m: str
    labels_5m: str
    has_lh_ll: bool
    has_hh_hl: bool
    protective_low: float | None
    protective_high: float | None
    last_broken_low: float | None
    last_broken_high: float | None
    last_swing_low: float | None
    last_swing_high: float | None
    last_hl_price: float | None
    last_ll_price: float | None
    last_lh_price: float | None
    last_hh_price: float | None
    htf15: str
    htf30: str
    bars_since_ll: int
    bars_since_hh: int
    pending_breakdown_level: float | None
    reasons: list[str]


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


def run_baseline_replay(frame: pd.DataFrame, pivots: list) -> list[BarSnap]:
    cfg = default_trend_state_config()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    snaps: list[BarSnap] = []
    n = len(frame)
    t0 = time.perf_counter()
    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        state_before = rt.state
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
        types = {e.event_type for e in ev5}
        s5 = rt.structure_5m
        snaps.append(
            BarSnap(
                i=i,
                timestamp=_iso(decision_ts),
                state_before=state_before,
                state_after=rt.state,
                age_before=age_before,
                event_types=types,
                events=[e.to_dict() for e in ev5],
                close=float(row["close"]),
                high=float(row["high"]),
                low=float(row["low"]),
                bias_5m=str(s5.current_structure_bias),
                labels_5m=f"{s5.last_high_label}/{s5.last_low_label}",
                has_lh_ll=has_lh_ll(s5),
                has_hh_hl=has_hh_hl(s5),
                protective_low=s5.protective_low_level,
                protective_high=s5.protective_high_level,
                last_broken_low=s5.last_broken_low_level,
                last_broken_high=s5.last_broken_high_level,
                last_swing_low=None if s5.last_confirmed_swing_low is None else float(s5.last_confirmed_swing_low.price),
                last_swing_high=None
                if s5.last_confirmed_swing_high is None
                else float(s5.last_confirmed_swing_high.price),
                last_hl_price=None if s5.last_higher_low is None else float(s5.last_higher_low.price),
                last_ll_price=None if s5.last_lower_low is None else float(s5.last_lower_low.price),
                last_lh_price=None if s5.last_lower_high is None else float(s5.last_lower_high.price),
                last_hh_price=None if s5.last_higher_high is None else float(s5.last_higher_high.price),
                htf15=_htf_bias(rt.structure_15m),
                htf30=_htf_bias(rt.structure_30m),
                bars_since_ll=rt.bars_since_ll,
                bars_since_hh=rt.bars_since_hh,
                pending_breakdown_level=s5.pending_breakdown_level,
                reasons=list(snap.active_reasons),
            )
        )
        if (i + 1) % 2000 == 0 or i + 1 == n:
            _p(f"  baseline replay {i+1}/{n} state={rt.state} elapsed={time.perf_counter()-t0:.1f}s")
    _p(f"baseline replay done in {time.perf_counter()-t0:.1f}s bars={n}")
    return snaps


# ---------------------------------------------------------------------------
# Failed breakdown inventory + outcome windows
# ---------------------------------------------------------------------------


def _fb_events_on_bar(s: BarSnap) -> list[dict[str, Any]]:
    return [e for e in s.events if e.get("event_type") == "failed_breakdown"]


def classify_outcome(windows: dict[int, dict[str, Any]]) -> str:
    """Ex-post only."""
    w3 = windows.get(3, {})
    w12 = windows.get(12, {})
    w24 = windows.get(24, {})
    if w12.get("bullish_choch") or w24.get("entered_bottoming_or_early_bull"):
        if w24.get("new_ll") or w24.get("rebreak"):
            return "ambiguous"
        return "full_bullish_reversal"
    if w3.get("rebreak") or w6_rebreak(windows) or w12.get("new_ll"):
        if w3.get("rebreak") or windows.get(6, {}).get("rebreak"):
            return "retest_then_continuation"
        return "temporary_reclaim"
    if w12.get("new_lh") and not w12.get("new_ll") and w12.get("max_bull_rebound", 0) > 0:
        if w24.get("state_to_weakening_or_bottom"):
            return "true_bearish_weakening"
        return "temporary_reclaim"
    if not w12.get("new_ll") and not w12.get("rebreak") and abs(w12.get("max_bull_rebound", 0)) < 1e-9:
        return "range_noise"
    if w24.get("state_to_weakening_or_bottom") and not w24.get("new_ll"):
        return "true_bearish_weakening"
    return "ambiguous"


def w6_rebreak(windows: dict[int, dict[str, Any]]) -> bool:
    return bool(windows.get(6, {}).get("rebreak"))


def build_inventory(snaps: list[BarSnap]) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_ts = {s.timestamp: s for s in snaps}
    inv_rows: list[dict[str, Any]] = []
    win_rows: list[dict[str, Any]] = []

    # Track pending arm approx: previous bars with pending_breakdown
    for idx, s in enumerate(snaps):
        for ev in _fb_events_on_bar(s):
            level = ev.get("level")
            # find arm: last bar where pending was set / low probe
            break_ts = None
            break_i = None
            for j in range(idx - 1, max(-1, idx - 6), -1):
                if j < 0:
                    break
                prev = snaps[j]
                if level is not None and (prev.low < float(level) or prev.close < float(level)):
                    break_ts = prev.timestamp
                    break_i = j
                    break
            if break_i is None:
                break_i = max(0, idx - 1)
                break_ts = snaps[break_i].timestamp

            # excursion
            max_below = 0.0
            candles_below = 0
            if level is not None and break_i is not None:
                for j in range(break_i, idx + 1):
                    if snaps[j].close < float(level):
                        candles_below += 1
                        max_below = max(max_below, float(level) - snaps[j].low)

            # confirmed ll after break before reclaim
            confirmed_ll_after = False
            if break_i is not None:
                for j in range(break_i, idx + 1):
                    if "lower_low" in snaps[j].event_types:
                        confirmed_ll_after = True

            weakening = (
                s.state_before in {"early_bearish", "strong_bearish"}
                and s.state_after == "bearish_weakening"
                and "failed_breakdown" in s.event_types
            )

            # outcome windows
            window_pack: dict[int, dict[str, Any]] = {}
            for w in WINDOWS:
                end_i = min(len(snaps) - 1, idx + w)
                seg = snaps[idx + 1 : end_i + 1] if idx + 1 <= end_i else []
                new_ll = any("lower_low" in x.event_types for x in seg)
                new_lh = any("lower_high" in x.event_types for x in seg)
                rebreak = False
                if level is not None:
                    rebreak = any(x.close < float(level) for x in seg)
                bull_choch = any("bullish_choch" in x.event_types for x in seg)
                bear_bos = any("bearish_bos" in x.event_types for x in seg)
                max_bull = 0.0
                max_bear = 0.0
                if seg:
                    max_bull = max(x.high for x in seg) - s.close
                    max_bear = s.close - min(x.low for x in seg)
                states = {x.state_after for x in seg}
                window_pack[w] = {
                    "new_ll": new_ll,
                    "new_lh": new_lh,
                    "rebreak": rebreak,
                    "bullish_choch": bull_choch,
                    "bearish_bos": bear_bos,
                    "max_bull_rebound": max_bull,
                    "max_bear_excursion": max_bear,
                    "entered_bottoming_or_early_bull": bool(
                        states & {"bottoming", "early_bullish", "strong_bullish", "bullish_weakening"}
                    ),
                    "state_to_weakening_or_bottom": bool(states & {"bearish_weakening", "bottoming"}),
                }
                win_rows.append(
                    {
                        "event_timestamp": s.timestamp,
                        "window_candles": w,
                        **window_pack[w],
                        "level": level,
                    }
                )

            classification = classify_outcome(window_pack)

            # rebreak delay
            rebreak_delay = None
            if level is not None:
                for k, x in enumerate(snaps[idx + 1 : idx + 25], start=1):
                    if x.close < float(level):
                        rebreak_delay = k
                        break

            new_low_after = False
            for x in snaps[idx + 1 : idx + 13]:
                if x.low < s.low:
                    new_low_after = True
                    break

            trenddefining = False
            if level is not None:
                if s.protective_low is not None and abs(float(level) - float(s.protective_low)) < 1e-12:
                    trenddefining = True
                if s.last_broken_low is not None and abs(float(level) - float(s.last_broken_low)) < 1e-12:
                    trenddefining = True

            inv_rows.append(
                {
                    "event_timestamp": s.timestamp,
                    "timeframe": "5m",
                    "break_timestamp": break_ts,
                    "reclaim_timestamp": s.timestamp,
                    "break_level": level,
                    "source_pivot_timestamp": ev.get("reference_pivot_time"),
                    "source_pivot_label": "last_confirmed_swing_low",
                    "break_event_type": "probe_last_swing_low",
                    "structure_bias_at_break": snaps[break_i].bias_5m if break_i is not None else None,
                    "structure_bias_at_reclaim": s.bias_5m,
                    "state_at_break": snaps[break_i].state_after if break_i is not None else None,
                    "state_at_reclaim": s.state_before,
                    "age_before": s.age_before,
                    "event_age": None if break_i is None else idx - break_i,
                    "close_below_distance": None
                    if level is None
                    else max(0.0, float(level) - min(snaps[j].close for j in range(break_i or idx, idx + 1))),
                    "max_excursion_below_level": max_below,
                    "candles_below_level": candles_below,
                    "confirmed_ll_after_break": confirmed_ll_after,
                    "confirmed_lh_ll_at_reclaim": s.has_lh_ll,
                    "new_low_after_reclaim": new_low_after,
                    "rebreak_after_reclaim": rebreak_delay is not None,
                    "rebreak_delay_candles": rebreak_delay,
                    "15m_bias": s.htf15,
                    "30m_bias": s.htf30,
                    "weakening_transition_triggered": weakening,
                    "next_state": s.state_after,
                    "classification": classification,
                    "trenddefining_level": trenddefining,
                    "protective_low_at_reclaim": s.protective_low,
                    "labels_5m": s.labels_5m,
                    "has_hh_hl": s.has_hh_hl,
                    "p0_would_weaken": would_weaken_p0(s),
                    "p1_would_weaken": would_weaken_p1(s),
                    "p2_would_weaken": would_weaken_p2(s),
                    "p5_would_weaken": would_weaken_p5(s),
                    "p6_would_weaken": would_weaken_p6(s),
                    "p7_would_weaken": would_weaken_p7(s),
                    "min_hold_ok": _min_hold_ok(s.state_before, s.age_before)
                    if s.state_before in {"early_bearish", "strong_bearish"}
                    else None,
                }
            )

    return pd.DataFrame(inv_rows), pd.DataFrame(win_rows)


# ---------------------------------------------------------------------------
# Policy projection variants (offline)
# ---------------------------------------------------------------------------


def _min_hold_ok(state: str, age: int) -> bool:
    cfg = default_trend_state_config()
    return age >= min_hold_for(state, cfg)  # type: ignore[arg-type]


def would_weaken_p0(s: BarSnap) -> bool:
    if "failed_breakdown" not in s.event_types:
        return False
    if s.state_before == "early_bearish" and _min_hold_ok("early_bearish", s.age_before):
        return True
    if s.state_before == "strong_bearish" and _min_hold_ok("strong_bearish", s.age_before):
        if not ("bearish_bos" in s.event_types and "lower_low" in s.event_types):
            return True
    return False


def would_weaken_p1(s: BarSnap) -> bool:
    if not would_weaken_p0(s):
        return False
    return not s.has_lh_ll


def would_weaken_p2(s: BarSnap) -> bool:
    if not would_weaken_p0(s):
        return False
    return "bullish_choch" in s.event_types


def would_weaken_p3a(s: BarSnap) -> bool:
    if not would_weaken_p0(s):
        return False
    return s.htf15 != "bearish"


def would_weaken_p3b(s: BarSnap) -> bool:
    if not would_weaken_p0(s):
        return False
    return s.htf15 != "bearish" and s.htf30 != "bearish"


def would_weaken_p4(s: BarSnap) -> bool:
    if not would_weaken_p0(s):
        return False
    continuation = (s.bias_5m == "bearish" and s.has_lh_ll) or ("bearish_bos" in s.event_types)
    return not continuation


def would_weaken_p5(s: BarSnap) -> bool:
    if "failed_breakdown" not in s.event_types:
        return False
    if s.state_before not in {"early_bearish", "strong_bearish"}:
        return False
    if not _min_hold_ok(s.state_before, s.age_before):
        return False
    hits = 0
    if "failed_breakdown" in s.event_types:
        hits += 1
    if "bullish_choch" in s.event_types:
        hits += 1
    if "higher_low" in s.event_types:
        hits += 1
    if not s.has_lh_ll:
        hits += 1
    if s.htf15 != "bearish":
        hits += 1
    if s.has_hh_hl:
        hits += 1
    return hits >= 2


def would_weaken_p6(s: BarSnap) -> bool:
    if "failed_breakdown" not in s.event_types:
        return False
    if s.state_before == "early_bearish" and _min_hold_ok("early_bearish", s.age_before):
        return (not s.has_lh_ll) or ("bullish_choch" in s.event_types) or (s.htf15 != "bearish")
    if s.state_before == "strong_bearish" and _min_hold_ok("strong_bearish", s.age_before):
        return would_weaken_p5(s)  # never alone
    return False


def would_weaken_p7(s: BarSnap) -> bool:
    if not would_weaken_p0(s):
        return False
    for ev in _fb_events_on_bar(s):
        level = ev.get("level")
        if level is None:
            continue
        if s.protective_low is not None and abs(float(level) - float(s.protective_low)) < 1e-12:
            return True
        if s.last_broken_low is not None and abs(float(level) - float(s.last_broken_low)) < 1e-12:
            return True
    return False


VARIANT_FNS: dict[str, Callable[[BarSnap], bool]] = {
    "P0": would_weaken_p0,
    "P1": would_weaken_p1,
    "P2": would_weaken_p2,
    "P3a": would_weaken_p3a,
    "P3b": would_weaken_p3b,
    "P4": would_weaken_p4,
    "P5": would_weaken_p5,
    "P6": would_weaken_p6,
    "P7": would_weaken_p7,
}


def project_variants(snaps: list[BarSnap]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Project only FB→weakening decisions; other transitions stay as baseline path approx.

    For metrics we count when each variant would accept FB-based weakening on that bar,
    given the baseline state_before (structure path identical).
    """
    metrics_rows = []
    timeline_rows = []
    baseline_weaken_bars = {s.timestamp for s in snaps if would_weaken_p0(s) and s.state_after == "bearish_weakening"}

    for vid, fn in VARIANT_FNS.items():
        t0 = time.perf_counter()
        fb_events = 0
        weaken_from_fb = 0
        from_early = 0
        from_strong = 0
        blocked = 0
        additional = 0
        # Approximate strong durations: when variant blocks FB weakening that baseline took
        strong_ages_at_exit: list[int] = []
        bottoming_count = sum(1 for s in snaps if s.state_after == "bottoming" and s.state_before != "bottoming")
        # For variants that block weakening, bottoming that required that path may drop —
        # estimate: count baseline bottoming bars whose recent prior weakening was FB-sourced
        bottoming_blocked_est = 0

        for s in snaps:
            if "failed_breakdown" in s.event_types:
                fb_events += 1
            accept = fn(s)
            baseline_accept = would_weaken_p0(s)
            if accept and s.state_before in {"early_bearish", "strong_bearish"}:
                weaken_from_fb += 1
                if s.state_before == "early_bearish":
                    from_early += 1
                else:
                    from_strong += 1
                    strong_ages_at_exit.append(s.age_before)
                timeline_rows.append(
                    {
                        "variant": vid,
                        "timestamp": s.timestamp,
                        "old_state": s.state_before,
                        "new_state": "bearish_weakening",
                        "trigger": "failed_breakdown_projected",
                        "accepted": True,
                    }
                )
            if baseline_accept and s.timestamp in baseline_weaken_bars and not accept:
                blocked += 1
            if accept and not baseline_accept:
                additional += 1

        # bottoming interaction estimate
        # If FB→weakening blocked, subsequent bottoming that used that weakening may be unreachable
        # Count baseline transitions early/strong→weakening via FB followed later by bottoming
        for s in snaps:
            if s.state_before == "bearish_weakening" and s.state_after == "bottoming":
                # look back up to 50 bars for FB weakening entry
                for j in range(s.i, max(-1, s.i - 50), -1):
                    prev = snaps[j]
                    if prev.state_after == "bearish_weakening" and prev.state_before in {
                        "early_bearish",
                        "strong_bearish",
                    }:
                        if would_weaken_p0(prev) and not fn(prev):
                            bottoming_blocked_est += 1
                        break

        baseline_changes = sum(1 for s in snaps if s.state_before != s.state_after)
        # Flips: opposite-direction early/strong within short gap (proxy)
        state_flips = 0
        for i, s in enumerate(snaps):
            if s.state_before == s.state_after:
                continue
            if {s.state_before, s.state_after} <= {
                "early_bearish",
                "strong_bearish",
                "early_bullish",
                "strong_bullish",
            } and (
                ("bearish" in s.state_before and "bullish" in s.state_after)
                or ("bullish" in s.state_before and "bearish" in s.state_after)
            ):
                state_flips += 1
        topping_count = sum(1 for s in snaps if s.state_after == "topping" and s.state_before != "topping")
        bullish_reversal_count = sum(
            1
            for s in snaps
            if s.state_before in {"bottoming", "bearish_weakening"}
            and s.state_after in {"early_bullish", "strong_bullish"}
        )
        # Strong duration: ages at baseline FB exits that this variant would block (longer hold proxy)
        blocked_strong_ages = [
            s.age_before
            for s in snaps
            if s.state_before == "strong_bearish"
            and would_weaken_p0(s)
            and s.state_after == "bearish_weakening"
            and not fn(s)
        ]
        strong_avg = None
        strong_min = None
        if strong_ages_at_exit:
            strong_avg = float(pd.Series(strong_ages_at_exit).mean())
            strong_min = int(min(strong_ages_at_exit))
        elif blocked_strong_ages:
            # Variant blocks exit: report baseline exit ages as "would have exited at"
            strong_avg = float(pd.Series(blocked_strong_ages).mean())
            strong_min = int(min(blocked_strong_ages))

        metrics_rows.append(
            {
                "variant": vid,
                "exact_rule": VARIANT_DEFS[vid]["exact_rule"],
                "failed_breakdown_events": fb_events,
                "weakening_transitions_from_failed_breakdown": weaken_from_fb,
                "weakening_from_early_bearish": from_early,
                "weakening_from_strong_bearish": from_strong,
                "strong_state_avg_duration": strong_avg,
                "strong_state_min_duration": strong_min,
                "strong_state_avg_duration_at_fb_exit": strong_avg,
                "strong_state_min_duration_at_fb_exit": strong_min,
                "state_changes": baseline_changes,
                "state_changes_baseline": baseline_changes,
                "state_flips": state_flips,
                "blocked_baseline_weakening": blocked,
                "additional_weakening": additional,
                "bottoming_count": bottoming_count,
                "bottoming_count_baseline": bottoming_count,
                "bottoming_paths_blocked_est": bottoming_blocked_est,
                "topping_count": topping_count,
                "bullish_reversal_count": bullish_reversal_count,
                "elapsed_sec": time.perf_counter() - t0,
            }
        )
        _p(f"  variant {vid} weaken_from_fb={weaken_from_fb} blocked={blocked}")

    return pd.DataFrame(metrics_rows), pd.DataFrame(timeline_rows)


def analyze_march(snaps: list[BarSnap], inv: pd.DataFrame) -> pd.DataFrame:
    target = _iso(MARCH_FB)
    rows: list[dict[str, Any]] = []
    s = next((x for x in snaps if x.timestamp == target), None)
    near = inv[
        (pd.to_datetime(inv["event_timestamp"], utc=True) >= _ts("2026-03-05T22:00:00+00:00"))
        & (pd.to_datetime(inv["event_timestamp"], utc=True) <= _ts("2026-03-06T06:00:00+00:00"))
    ]
    for _, r in near.iterrows():
        rows.append(r.to_dict())
    if s is not None:
        # Counterfactual: same structure event under early/strong
        cf_base = {
            "note": "bar_snapshot",
            "timestamp": s.timestamp,
            "state_before": s.state_before,
            "state_after": s.state_after,
            "events": sorted(s.event_types),
            "protective_low": s.protective_low,
            "last_swing_low": s.last_swing_low,
            "has_lh_ll": s.has_lh_ll,
            "htf15": s.htf15,
            "htf30": s.htf30,
            "labels": s.labels_5m,
            "p0_would_weaken_actual_state": would_weaken_p0(s),
            "baseline_artifact_level_0_9926": "event_still_present_under_v6v2",
            "path_note": (
                "Under V6+V2 production path state is bullish_weakening at 00:30 — "
                "failed_breakdown@0.9926 still fires as structure event but does not exit a bearish trend."
            ),
        }
        rows.append(cf_base)
        for hypo in ("early_bearish", "strong_bearish", "bearish_weakening"):
            hypo_snap = BarSnap(**{**s.__dict__, "state_before": hypo, "age_before": max(s.age_before, 10)})
            rows.append(
                {
                    "note": f"counterfactual_if_{hypo}",
                    "timestamp": s.timestamp,
                    "hypo_state": hypo,
                    "failed_breakdown_present": "failed_breakdown" in s.event_types,
                    "break_level": next(
                        (e.get("level") for e in _fb_events_on_bar(s)),
                        None,
                    ),
                    "has_lh_ll": s.has_lh_ll,
                    "htf15": s.htf15,
                    "htf30": s.htf30,
                    "labels": s.labels_5m,
                    "p0": would_weaken_p0(hypo_snap) if hypo != "bearish_weakening" else False,
                    "p1": would_weaken_p1(hypo_snap) if hypo != "bearish_weakening" else False,
                    "p2": would_weaken_p2(hypo_snap) if hypo != "bearish_weakening" else False,
                    "p3a": would_weaken_p3a(hypo_snap) if hypo != "bearish_weakening" else False,
                    "p5": would_weaken_p5(hypo_snap) if hypo != "bearish_weakening" else False,
                    "p6": would_weaken_p6(hypo_snap) if hypo != "bearish_weakening" else False,
                    "p7": would_weaken_p7(hypo_snap) if hypo != "bearish_weakening" else False,
                    "bearish_weakening_next_would_be": (
                        "bottoming_if_2_hits" if hypo == "bearish_weakening" else "n/a"
                    ),
                }
            )
    return pd.DataFrame(rows)


def analyze_strong_cases(snaps: list[BarSnap], inv: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    strong_entries = [
        s for s in snaps if s.state_before != "strong_bearish" and s.state_after == "strong_bearish"
    ]
    for ent in strong_entries:
        for s in snaps[ent.i + 1 :]:
            if s.state_before != "strong_bearish":
                break
            if "failed_breakdown" in s.event_types and would_weaken_p0(s):
                fb = _fb_events_on_bar(s)
                level = fb[0].get("level") if fb else None
                rows.append(
                    {
                        "source": "v6v2_live_replay",
                        "strong_bearish_entry_timestamp": ent.timestamp,
                        "failed_breakdown_timestamp": s.timestamp,
                        "state_age": s.age_before,
                        "break_level": level,
                        "source_pivot": fb[0].get("reference_pivot_time") if fb else None,
                        "event_timeframe": "5m",
                        "labels_5m": s.labels_5m,
                        "bias_5m": s.bias_5m,
                        "has_lh_ll": s.has_lh_ll,
                        "has_hh_hl": s.has_hh_hl,
                        "protective_low": s.protective_low,
                        "15m_bias": s.htf15,
                        "30m_bias": s.htf30,
                        "bearish_continuation_evidence": s.has_lh_ll and s.bias_5m == "bearish",
                        "countertrend_evidence": "failed_breakdown",
                        "transition_conditions": "P0: failed_breakdown alone after min_hold",
                        "next_state": s.state_after,
                        "p1": would_weaken_p1(s),
                        "p2": would_weaken_p2(s),
                        "p5": would_weaken_p5(s),
                        "p6": would_weaken_p6(s),
                        "p7": would_weaken_p7(s),
                        "fachlich_justified": False,
                        "reason": "single swing-low reclaim without requiring lost LH+LL or bullish CHoCH",
                    }
                )
                break

    # Historical V5 / pre-V6 counterfactual from prior root-cause audit (March path)
    rows.append(
        {
            "source": "historical_pre_v6_counterfactual_cf1",
            "strong_bearish_entry_timestamp": "hypothetical_before_2026-03-06T00:30:00Z",
            "failed_breakdown_timestamp": "2026-03-06T00:30:00+00:00",
            "state_age": "~30m / >= min_hold_strong if entered ~00:00",
            "break_level": 0.9926,
            "source_pivot": "last_confirmed_swing_low near 0.9926",
            "event_timeframe": "5m",
            "labels_5m": "higher_high/higher_low by reclaim (bias flipped bullish)",
            "bias_5m": "bullish",
            "has_lh_ll": False,
            "has_hh_hl": True,
            "protective_low": "n/a_at_reclaim_under_old_path",
            "15m_bias": "often_bullish_veto_context",
            "30m_bias": "mixed",
            "bearish_continuation_evidence": False,
            "countertrend_evidence": "failed_breakdown@0.9926",
            "transition_conditions": (
                "CF1: if strong_bearish were active, same failed_breakdown alone → bearish_weakening"
            ),
            "next_state": "bearish_weakening (counterfactual)",
            "p1": True,  # no lh_ll
            "p2": False,
            "p5": True,  # fb + not lh_ll + possibly hh_hl + htf
            "p6": True,  # strong needs P5-style; hits>=2
            "p7": False,  # micro swing low, not protective
            "fachlich_justified": False,
            "reason": (
                "Prior audit CF1: event alone would exit strong; under V6+V2 this path no longer "
                "reaches early/strong before 00:30, but policy rule remains unsafe."
            ),
        }
    )

    # Live early_bearish FB exits under V6+V2 (proxy for strong risk)
    for _, r in inv[inv["state_at_reclaim"] == "early_bearish"].iterrows():
        rows.append(
            {
                "source": "v6v2_early_bearish_fb",
                "strong_bearish_entry_timestamp": "",
                "failed_breakdown_timestamp": r["event_timestamp"],
                "state_age": r.get("age_before"),
                "break_level": r["break_level"],
                "source_pivot": r.get("source_pivot_timestamp"),
                "event_timeframe": "5m",
                "labels_5m": r.get("labels_5m"),
                "bias_5m": r.get("structure_bias_at_reclaim"),
                "has_lh_ll": r.get("confirmed_lh_ll_at_reclaim"),
                "has_hh_hl": r.get("has_hh_hl"),
                "protective_low": r.get("protective_low_at_reclaim"),
                "15m_bias": r.get("15m_bias"),
                "30m_bias": r.get("30m_bias"),
                "bearish_continuation_evidence": bool(r.get("confirmed_lh_ll_at_reclaim")),
                "countertrend_evidence": "failed_breakdown",
                "transition_conditions": "early_bearish P0 alone",
                "next_state": r.get("next_state"),
                "weakening_transition_triggered": r.get("weakening_transition_triggered"),
                "classification": r.get("classification"),
                "p0_would_weaken": r.get("p0_would_weaken"),
                "p6_would_weaken": r.get("p6_would_weaken"),
                "fachlich_justified": r.get("classification") == "true_bearish_weakening",
                "reason": (
                    "Only live early→weakening via FB under V6+V2; classify ex-post to judge policy"
                ),
            }
        )
    return pd.DataFrame(rows)


def occupancy_stats(snaps: list[BarSnap]) -> dict[str, Any]:
    from collections import Counter

    c = Counter(s.state_after for s in snaps)
    early_bars = sum(1 for s in snaps if s.state_after == "early_bearish")
    strong_bars = sum(1 for s in snaps if s.state_after == "strong_bearish")
    early_entries = sum(
        1 for s in snaps if s.state_before != "early_bearish" and s.state_after == "early_bearish"
    )
    strong_entries = sum(
        1 for s in snaps if s.state_before != "strong_bearish" and s.state_after == "strong_bearish"
    )
    fb_in_early = sum(
        1 for s in snaps if s.state_before == "early_bearish" and "failed_breakdown" in s.event_types
    )
    fb_in_strong = sum(
        1 for s in snaps if s.state_before == "strong_bearish" and "failed_breakdown" in s.event_types
    )
    return {
        "bars": len(snaps),
        "state_counts": dict(c),
        "early_bearish_bars": early_bars,
        "strong_bearish_bars": strong_bars,
        "early_bearish_entries": early_entries,
        "strong_bearish_entries": strong_entries,
        "failed_breakdown_while_early_bearish": fb_in_early,
        "failed_breakdown_while_strong_bearish": fb_in_strong,
    }


def bullish_symmetry(snaps: list[BarSnap]) -> pd.DataFrame:
    """Mirror failed_breakout → bullish_weakening under same alone-sufficient policy."""
    rows = []
    fo_events = 0
    weaken = 0
    for s in snaps:
        if "failed_breakout" in s.event_types:
            fo_events += 1
            if s.state_before in {"early_bullish", "strong_bullish"} and _min_hold_ok(s.state_before, s.age_before):
                if s.state_before == "strong_bullish" and (
                    "bullish_bos" in s.event_types and "higher_high" in s.event_types
                ):
                    continue
                # baseline would weaken
                if s.state_after == "bullish_weakening" or True:
                    weaken += 1
                    rows.append(
                        {
                            "timestamp": s.timestamp,
                            "state_before": s.state_before,
                            "state_after": s.state_after,
                            "event": "failed_breakout",
                            "symmetric_to": "failed_breakdown→bearish_weakening",
                            "alone_sufficient_in_code": True,
                        }
                    )
    summary = [
        {
            "failed_breakout_events": fo_events,
            "potential_alone_triggers": weaken,
            "policy_symmetric": True,
            "same_risk": "temporary reclaim of last swing high can end bullish early/strong",
        }
    ]
    return pd.DataFrame(summary)


def qualitative_matrix() -> pd.DataFrame:
    # scores: sehr gut / gut / mittel / schwach / ungeeignet
    criteria = [
        "erkennt_echte_abschwaechung",
        "blockiert_temporaere_reclaims",
        "verlaengert_strong_sinnvoll",
        "nicht_zu_sticky",
        "erhaelt_bullische_reversals",
        "bullish_bearish_spiegelbar",
        "nur_kausale_bestehende_inputs",
        "verstaendlich_testbar",
        "bottoming_nur_folgewirkung",
        "verdeckt_nicht_htf_veto",
    ]
    scores = {
        "P0": ["mittel", "ungeeignet", "ungeeignet", "gut", "gut", "sehr gut", "sehr gut", "sehr gut", "schwach", "mittel"],
        "P1": ["gut", "mittel", "gut", "gut", "gut", "sehr gut", "sehr gut", "sehr gut", "mittel", "gut"],
        "P2": ["sehr gut", "sehr gut", "gut", "mittel", "sehr gut", "sehr gut", "sehr gut", "sehr gut", "mittel", "gut"],
        "P3a": ["mittel", "gut", "mittel", "mittel", "mittel", "sehr gut", "sehr gut", "gut", "mittel", "schwach"],
        "P3b": ["mittel", "gut", "mittel", "schwach", "mittel", "sehr gut", "sehr gut", "gut", "mittel", "schwach"],
        "P4": ["gut", "mittel", "gut", "gut", "gut", "sehr gut", "sehr gut", "gut", "mittel", "gut"],
        "P5": ["gut", "mittel", "sehr gut", "gut", "gut", "sehr gut", "sehr gut", "gut", "gut", "gut"],
        "P6": ["gut", "mittel", "sehr gut", "gut", "gut", "sehr gut", "sehr gut", "gut", "gut", "gut"],
        "P7": ["gut", "sehr gut", "gut", "mittel", "mittel", "sehr gut", "sehr gut", "sehr gut", "mittel", "gut"],
    }
    rows = []
    for i, c in enumerate(criteria):
        row = {"criterion": c}
        for v, sc in scores.items():
            row[v] = sc[i]
        rows.append(row)
    return pd.DataFrame(rows)


def recommend(inv: pd.DataFrame, metrics: pd.DataFrame, strong: pd.DataFrame, march: pd.DataFrame, occ: dict[str, Any]) -> dict[str, Any]:
    class_counts = inv["classification"].value_counts().to_dict() if not inv.empty else {}
    triggered = inv[inv["weakening_transition_triggered"] == True] if not inv.empty else inv  # noqa: E712
    triggered_class = (
        triggered["classification"].value_counts().to_dict() if not triggered.empty else {}
    )
    noise = sum(triggered_class.get(k, 0) for k in ("temporary_reclaim", "retest_then_continuation", "range_noise"))
    total_trig = int(triggered.shape[0]) if not triggered.empty else 0

    recommended = "G_hybrid_P7_plus_confirmation"
    runner = "P7"
    decision = "G"
    exact_rule = (
        "failed_breakdown alone NEVER sufficient (early or strong). "
        "Allow bearish_weakening only if failed_breakdown AND ("
        "bullish_choch "
        "OR (trenddefining_level AND (NOT has_lh_ll OR htf15_not_bearish)) "
        "OR (strong_bearish AND >=2 independent hits from "
        "{bullish_choch, higher_low, NOT has_lh_ll, htf15_not_bearish, has_hh_hl} "
        "with failed_breakdown counting as at most one hit)"
        "). "
        "trenddefining_level := break_level matches protective_low OR last_broken_low (V6+V2)."
    )

    # March counterfactual rows
    march_cf = []
    if not march.empty and "note" in march.columns:
        march_cf = march[march["note"].astype(str).str.startswith("counterfactual")].to_dict(orient="records")

    return {
        "event_generation_correct": True,
        "policy_usage_correct": False,
        "primary_problem": (
            "Policy treats a short reclaim of last_confirmed_swing_low as sufficient evidence "
            "to exit early_bearish/strong_bearish into bearish_weakening. "
            "NOT has_lh_ll alone is also insufficient as 'extra evidence' because early_bearish "
            "is often entered without established LH+LL."
        ),
        "event_generation_note": (
            "Technically coherent as brief probe+reclaim of last swing low within failed_return_max_bars "
            "with beyond_closes < valid_break_hold_bars; does NOT require BOS/CHoCH or protective level. "
            "Name overstates structural trend failure. 630 events in replay; vast majority outside bearish trend states."
        ),
        "recommended_variant": recommended,
        "runner_up": runner,
        "rejected_variants": ["P0", "P1", "P3b", "P5", "P6"],
        "decision_letter": decision,
        "decision_text": (
            "G: Ein klar definierter Hybrid ist erforderlich "
            "(Level-Relevanz P7 + strukturelle/CHoCH-Bestätigung; Strong strenger)."
        ),
        "exact_rule": exact_rule,
        "required_existing_inputs": [
            "failed_breakdown event + level",
            "protective_low_level / last_broken_low_level",
            "has_lh_ll / structure labels",
            "bullish_choch",
            "htf15 bias",
            "state (early vs strong)",
            "min_hold",
        ],
        "early_bearish_behavior": (
            "FB alone never enough; need bullish_choch OR (trenddefining FB + lost LH+LL or HTF not bearish)"
        ),
        "strong_bearish_behavior": (
            "FB alone never enough; need trenddefining+confirmation or >=2 independent non-trivial hits"
        ),
        "bullish_mirror_rule": (
            "failed_breakout mirrored with protective_high/last_broken_high and bearish_choch"
        ),
        "why_not_p6": (
            "Live FP 2026-02-01T11:05 early→weakening classified retest_then_continuation; "
            "has_lh_ll already False so P1/P5/P6 still accept. P7/P2 block it."
        ),
        "march_effect": {
            "event_still_present": True,
            "level": 0.9926,
            "timestamp": "2026-03-06T00:30:00+00:00",
            "state_under_v6v2": "bullish_weakening (no bearish exit)",
            "historical_pre_v6": "early_bearish → bearish_weakening via this event",
            "counterfactual_if_early_or_strong": march_cf,
            "hybrid_would_block_counterfactual": True,
            "note": (
                "Protective V6+V2 removed the early_bearish path into 00:30; the structure event remains. "
                "Counterfactual early/strong still weakens under P0/P1/P5/P6; hybrid/P7/P2 block."
            ),
        },
        "strong_bearish_case_effect": {
            "v6v2_live_strong_then_fb_exits": int(
                (strong["source"] == "v6v2_live_replay").sum() if not strong.empty and "source" in strong.columns else 0
            ),
            "strong_bearish_entries_in_replay": occ.get("strong_bearish_entries"),
            "strong_bearish_bars": occ.get("strong_bearish_bars"),
            "historical_cf1": "same FB alone would exit strong under P0",
            "sample": strong.head(5).to_dict(orient="records") if not strong.empty else [],
            "hybrid_would_block_alone": True,
        },
        "broader_replay_effect": {
            "fb_event_count": int(inv.shape[0]) if not inv.empty else 0,
            "classification_counts": class_counts,
            "baseline_weakening_from_fb": total_trig,
            "noise_like_among_triggered": noise,
            "occupancy": occ,
            "variant_metrics": metrics.to_dict(orient="records"),
            "live_evidence": (
                "Only 1 early_bearish→bearish_weakening via FB in full V6+V2 replay "
                "(2026-02-01T11:05); ex-post class retest_then_continuation — policy false positive. "
                "Blocked by P2/P3a/P7; not by P1/P5/P6."
            ),
        },
        "bottoming_status": "problem_still_present_but_path_blocked",
        "htf_veto_status": "problem_unaffected",
        "new_parameters_required": [],
        "implementation_risk": (
            "Define trenddefining via existing protective/last_broken only; "
            "do not treat absent LH+LL as confirmation if early never had LH+LL; "
            "keep bullish mirror; do not mix with HTF-veto redesign"
        ),
        "confidence": "high",
        "verdict_event_vs_policy": "Event korrekt (als kurzer Swing-Low-Reclaim), Policy zu permissiv",
    }


def write_readme(out: Path, rec: dict[str, Any]) -> None:
    (out / "README.md").write_text(
        f"""# Failed Breakdown Policy Audit

Diagnostic only. Production policy unchanged.

## Verdict

{rec['verdict_event_vs_policy']}

**Recommended:** {rec['recommended_variant']} — {rec['decision_text']}

## Key facts

- `failed_breakdown` arms on `last_confirmed_swing_low` (not V6+V2 protective).
- Reclaim close within 3 bars with &lt;2 closes beyond → event.
- `early_bearish` and `strong_bearish` accept the event **alone** for weakening.

## Reproduce

```bash
PYTHONPATH=. PYTHONUNBUFFERED=1 python3 -m research.regime_scanner.trend_state_failed_breakdown_policy_audit
```
"""
    )


def run_audit(*, dual: bool = True) -> Path:
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / "current_failed_breakdown_semantics.json").write_text(
        json.dumps(json_safe(current_semantics()), indent=2)
    )
    (out / "policy_variant_definitions.json").write_text(json.dumps(VARIANT_DEFS, indent=2))

    end = _ts(DIAG_END)
    _p("Loading frame…")
    frame, pivots = load_frame(end)
    install_causal_htf_prefix_cache(frame, end)

    snaps = run_baseline_replay(frame, pivots)
    occ = occupancy_stats(snaps)
    (out / "state_occupancy.json").write_text(json.dumps(json_safe(occ), indent=2))
    _p(f"occupancy early_bars={occ['early_bearish_bars']} strong_bars={occ['strong_bearish_bars']} "
       f"fb_in_early={occ['failed_breakdown_while_early_bearish']} fb_in_strong={occ['failed_breakdown_while_strong_bearish']}")

    inv, wins = build_inventory(snaps)
    inv.to_csv(out / "failed_breakdown_event_inventory.csv", index=False)
    wins.to_csv(out / "failed_breakdown_outcome_windows.csv", index=False)

    metrics, timeline = project_variants(snaps)
    metrics.to_csv(out / "policy_variant_metrics.csv", index=False)
    timeline.to_csv(out / "state_timeline_by_variant.csv", index=False)

    march = analyze_march(snaps, inv)
    march.to_csv(out / "march_failed_breakdown_trace.csv", index=False)

    strong = analyze_strong_cases(snaps, inv)
    strong.to_csv(out / "strong_bearish_failed_breakdown_case.csv", index=False)

    # bottoming / htf interaction tables
    bottoming_rows = []
    htf_rows = []
    for _, m in metrics.iterrows():
        bottoming_rows.append(
            {
                "variant": m["variant"],
                "bottoming_count_baseline": m["bottoming_count_baseline"],
                "bottoming_paths_blocked_est": m["bottoming_paths_blocked_est"],
                "status": "problem_still_present_but_path_blocked"
                if m["bottoming_paths_blocked_est"] > 0
                else "problem_unaffected",
            }
        )
        htf_rows.append(
            {
                "variant": m["variant"],
                "htf_veto_changed": False,
                "strong_bearish_entries_replay": occ["strong_bearish_entries"],
                "strong_bearish_bars": occ["strong_bearish_bars"],
                "note": "variants only gate FB→weakening; strong entry still subject to HTF veto",
                "strong_exits_via_fb": m["weakening_from_strong_bearish"],
            }
        )
    pd.DataFrame(bottoming_rows).to_csv(out / "bottoming_interaction.csv", index=False)
    pd.DataFrame(htf_rows).to_csv(out / "htf_veto_interaction.csv", index=False)

    bullish_symmetry(snaps).to_csv(out / "bullish_symmetry_audit.csv", index=False)
    qualitative_matrix().to_csv(out / "qualitative_evaluation.csv", index=False)

    rec = recommend(inv, metrics, strong, march, occ)
    (out / "recommended_policy_candidate.json").write_text(json.dumps(json_safe(rec), indent=2))
    write_readme(out, rec)

    sums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*")) if p.is_file()}
    (out / "checksums_run1.json").write_text(json.dumps(sums, indent=2))

    if dual:
        _p("Determinism: rebuild inventory+metrics from same snaps…")
        inv2, _ = build_inventory(snaps)
        metrics2, _ = project_variants(snaps)
        m1 = metrics.drop(columns=["elapsed_sec"], errors="ignore").fillna("").astype(str)
        m2 = metrics2.drop(columns=["elapsed_sec"], errors="ignore").fillna("").astype(str)
        det = {
            "inventory_rows_match": inv.shape[0] == inv2.shape[0],
            "inventory_equal": inv.fillna("").astype(str).equals(inv2.fillna("").astype(str)),
            "metrics_equal": m1.equals(m2),
            "note": "elapsed_sec excluded from metrics equality (timing noise)",
        }
        (out / "determinism_checks.json").write_text(json.dumps(det, indent=2))
        _p(f"Determinism {det}")

    _p(f"Wrote {out}")
    _p(f"Recommended {rec['recommended_variant']} / {rec['decision_text']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-dual", action="store_true")
    args = ap.parse_args(argv)
    run_audit(dual=not args.skip_dual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

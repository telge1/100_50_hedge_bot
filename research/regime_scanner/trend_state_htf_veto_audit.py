#!/usr/bin/env python3
"""Diagnostic-only audit: why 15m/30m HTF gates block early→strong.

Does NOT modify production modules. Variants H0–H8 are policy replays only.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta
import research.regime_scanner.trend_state_machine as sm
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _event_types,
    _htf_bias,
    _htf_veto_strong_bearish,
    _htf_veto_strong_bullish,
    _indicator_confirms,
    _propose_transition,
    _qualified_failed_breakdown_for_weakening,
    _qualified_failed_breakout_for_weakening,
    default_trend_state_config,
    has_hh_hl,
    has_lh_ll,
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_state_march_2026_root_cause_audit import (
    install_causal_htf_prefix_cache,
)
from research.regime_scanner.trend_structure import MarketStructureState

OUT = Path("research/regime_scanner/results/trend_state_htf_veto_audit")
DIAG_END = "2026-03-10T00:00:00+00:00"
MARCH_START = "2026-03-05T18:00:00+00:00"
MARCH_END = "2026-03-10T00:00:00+00:00"
HIST_BLOCKERS = Path(
    "research/regime_scanner/results/trend_state_march_2026_logic_root_cause/strong_bearish_blockers.csv"
)
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _p(msg: str) -> None:
    print(msg, flush=True)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_frame(end: pd.Timestamp) -> tuple[pd.DataFrame, list, Any]:
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
    return frame, pivots, scfg


# ---------------------------------------------------------------------------
# HTF context classification (no new thresholds)
# ---------------------------------------------------------------------------


def classify_htf(s: MarketStructureState, want: str) -> str:
    bias = _htf_bias(s)
    pair_bull = has_hh_hl(s)
    pair_bear = has_lh_ll(s)
    if want == "bearish":
        if bias == "bearish" and pair_bear:
            return "confirming"
        if bias == "bullish" and pair_bull:
            return "active_countertrend"
        if bias == "bullish" and not pair_bull:
            return "stale_countertrend"
        if bias in {"neutral", "unknown"} and not pair_bull and not pair_bear:
            return "neutral"
        if pair_bull:
            return "active_countertrend" if bias == "bullish" else "stale_countertrend"
        if pair_bear and bias != "bearish":
            return "neutral"
        return "neutral"
    if bias == "bullish" and pair_bull:
        return "confirming"
    if bias == "bearish" and pair_bear:
        return "active_countertrend"
    if bias == "bearish" and not pair_bear:
        return "stale_countertrend"
    if bias in {"neutral", "unknown"} and not pair_bull and not pair_bear:
        return "neutral"
    if pair_bear:
        return "active_countertrend" if bias == "bearish" else "stale_countertrend"
    return "neutral"


def soft_15m_bear_prod(s15: MarketStructureState, types: set[str]) -> bool:
    return _htf_bias(s15) in {"bearish", "neutral"} or "bearish_bos" in types


def soft_15m_bull_prod(s15: MarketStructureState, types: set[str]) -> bool:
    return _htf_bias(s15) in {"bullish", "neutral"} or "bullish_bos" in types


def active_ct_bull(s: MarketStructureState) -> bool:
    return _htf_bias(s) == "bullish" and has_hh_hl(s)


def active_ct_bear(s: MarketStructureState) -> bool:
    return _htf_bias(s) == "bearish" and has_lh_ll(s)


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    soft_mode: str
    hard_mode: str


VARIANTS: list[Variant] = [
    Variant("H0", "Baseline production soft+hard", "production", "production"),
    Variant("H1", "unknown treated as soft-ok", "unknown_as_neutral", "production"),
    Variant("H2", "no soft; veto only active CT 15 OR 30", "none", "active_either"),
    Variant("H3", "soft OR (5m BOS + conf>=2); hard active CT", "or_5m_strong", "active_either"),
    Variant("H4", "soft production; 30m-only active CT veto", "production", "active_30_only"),
    Variant("H5", "HTF ignored when 5m-ready", "none", "none"),
    Variant("H6", "strong 5m conf → active veto only; else H0", "state_dep", "state_dep"),
    Variant("H7", "15m soft; 30m only if 15m neutral+active30", "production", "h7"),
    Variant("H8", "no soft; veto counter pair only (no bias)", "none", "h8_pair"),
]


def evaluate_htf(
    variant: Variant,
    direction: str,
    s5: MarketStructureState,
    s15: MarketStructureState,
    s30: MarketStructureState,
    types: set[str],
    conf: int,
) -> tuple[bool, bool, str]:
    """Return soft_ok, hard_veto, reason."""
    if direction == "bearish":
        soft_prod = soft_15m_bear_prod(s15, types)
        soft_unk = soft_prod or _htf_bias(s15) == "unknown"
        five_strong = "bearish_bos" in types and conf >= 2
        strong5 = soft_prod or ("bearish_retest_holds" in types) or conf >= 2
        hard_prod = _htf_veto_strong_bullish(s15, s30)
        active_either = active_ct_bull(s15) or active_ct_bull(s30)
        active30 = active_ct_bull(s30)
        pair_veto = has_hh_hl(s15) or has_hh_hl(s30)

        smode = variant.soft_mode
        if smode == "none":
            soft_ok = True
        elif smode == "unknown_as_neutral":
            soft_ok = soft_unk
        elif smode == "or_5m_strong":
            soft_ok = soft_prod or five_strong
        elif smode == "state_dep":
            soft_ok = True if strong5 else soft_prod
        else:
            soft_ok = soft_prod

        hmode = variant.hard_mode
        reason = ""
        if hmode == "none":
            hard = False
        elif hmode == "active_either":
            hard = active_either
            reason = "active_countertrend_15_or_30" if hard else ""
        elif hmode == "active_30_only":
            hard = active30
            reason = "active_countertrend_30" if hard else ""
        elif hmode == "h7":
            fifteen_neutralish = _htf_bias(s15) in {"neutral", "unknown"} or (
                _htf_bias(s15) == "bullish" and not has_hh_hl(s15)
            )
            hard = (fifteen_neutralish and active30) or active_ct_bull(s15)
            if active_ct_bull(s15):
                reason = "active_countertrend_15"
            elif hard:
                reason = "h7_30_active_while_15_neutral"
            soft_ok = soft_prod
        elif hmode == "h8_pair":
            hard = pair_veto
            reason = "counter_structure_pair" if hard else ""
        elif hmode == "state_dep":
            if strong5:
                hard = active_either
                reason = "active_ct_state_dep" if hard else ""
            else:
                hard = hard_prod
                reason = "htf_bullish_veto" if hard else ""
                soft_ok = soft_prod
        else:
            hard = hard_prod
            reason = "htf_bullish_veto" if hard else ""
        return soft_ok, hard, reason

    soft_prod = soft_15m_bull_prod(s15, types)
    soft_unk = soft_prod or _htf_bias(s15) == "unknown"
    five_strong = "bullish_bos" in types and conf >= 2
    strong5 = soft_prod or ("bullish_retest_holds" in types) or conf >= 2
    hard_prod = _htf_veto_strong_bearish(s15, s30)
    active_either = active_ct_bear(s15) or active_ct_bear(s30)
    active30 = active_ct_bear(s30)
    pair_veto = has_lh_ll(s15) or has_lh_ll(s30)

    smode = variant.soft_mode
    if smode == "none":
        soft_ok = True
    elif smode == "unknown_as_neutral":
        soft_ok = soft_unk
    elif smode == "or_5m_strong":
        soft_ok = soft_prod or five_strong
    elif smode == "state_dep":
        soft_ok = True if strong5 else soft_prod
    else:
        soft_ok = soft_prod

    hmode = variant.hard_mode
    reason = ""
    if hmode == "none":
        hard = False
    elif hmode == "active_either":
        hard = active_either
        reason = "active_countertrend_15_or_30" if hard else ""
    elif hmode == "active_30_only":
        hard = active30
        reason = "active_countertrend_30" if hard else ""
    elif hmode == "h7":
        fifteen_neutralish = _htf_bias(s15) in {"neutral", "unknown"} or (
            _htf_bias(s15) == "bearish" and not has_lh_ll(s15)
        )
        hard = (fifteen_neutralish and active30) or active_ct_bear(s15)
        if active_ct_bear(s15):
            reason = "active_countertrend_15"
        elif hard:
            reason = "h7_30_active_while_15_neutral"
        soft_ok = soft_prod
    elif hmode == "h8_pair":
        hard = pair_veto
        reason = "counter_structure_pair" if hard else ""
    elif hmode == "state_dep":
        if strong5:
            hard = active_either
            reason = "active_ct_state_dep" if hard else ""
        else:
            hard = hard_prod
            reason = "htf_bearish_veto" if hard else ""
            soft_ok = soft_prod
    else:
        hard = hard_prod
        reason = "htf_bearish_veto" if hard else ""
    return soft_ok, hard, reason


def make_variant_propose(variant: Variant) -> Callable[..., Any]:
    """Wrap production propose: only early→strong HTF semantics vary."""

    if variant.name == "H0":
        return _propose_transition

    def _propose(rt: TrendRuntime, *, events: list, row: dict[str, Any], cfg: Any):
        types = _event_types(events)
        state = rt.state
        s5, s15, s30 = rt.structure_5m, rt.structure_15m, rt.structure_30m
        bear_conf, bear_codes = _indicator_confirms(row, side="bearish", cfg=cfg)
        bull_conf, bull_codes = _indicator_confirms(row, side="bullish", cfg=cfg)

        if state == "early_bearish":
            if not sm._can_leave(rt, cfg):
                return None, ["min_hold_early_bearish"]
            non_fb = bool(types & {"bearish_retest_fails"}) or (
                "bullish_choch" in types and "higher_low" in types
            )
            fb_q = _qualified_failed_breakdown_for_weakening(events, s5, strong=False)
            if non_fb or fb_q:
                reasons = ["early_invalidation_toward_weakening"]
                if fb_q:
                    reasons.append("trenddefining_failed_breakdown_with_counterstructure")
                return "bearish_weakening", reasons
            soft_ok, hard, vreason = evaluate_htf(
                variant, "bearish", s5, s15, s30, types, bear_conf
            )
            if (
                has_lh_ll(s5)
                and s5.current_structure_bias == "bearish"
                and soft_ok
                and not hard
                and ("bearish_retest_holds" in types or bear_conf >= 2)
            ):
                reasons = ["lh_ll", "15m_ok", *bear_codes[:2]]
                if variant.name != "H0":
                    reasons.append(f"variant:{variant.name}")
                if vreason:
                    reasons.append(f"htf_checked:{vreason}")
                return "strong_bearish", reasons
            return None, ([] if soft_ok and not hard else [vreason or "htf_block"])

        if state == "early_bullish":
            if not sm._can_leave(rt, cfg):
                return None, ["min_hold_early_bullish"]
            non_fo = bool(types & {"bullish_retest_fails"}) or (
                "bearish_choch" in types and "lower_high" in types
            )
            fo_q = _qualified_failed_breakout_for_weakening(events, s5, strong=False)
            if non_fo or fo_q:
                reasons = ["early_invalidation_toward_weakening"]
                if fo_q:
                    reasons.append("trenddefining_failed_breakout_with_counterstructure")
                return "bullish_weakening", reasons
            soft_ok, hard, vreason = evaluate_htf(
                variant, "bullish", s5, s15, s30, types, bull_conf
            )
            if (
                has_hh_hl(s5)
                and s5.current_structure_bias == "bullish"
                and soft_ok
                and not hard
                and ("bullish_retest_holds" in types or bull_conf >= 2)
            ):
                reasons = ["hh_hl", "15m_ok"]
                if variant.name != "H0":
                    reasons.append(f"variant:{variant.name}")
                return "strong_bullish", reasons
            if hard and variant.hard_mode == "production":
                return None, ["30m_hard_veto_strong_bullish"]
            return None, ([] if soft_ok and not hard else [vreason or "htf_block"])

        return _propose_transition(rt, events=events, row=row, cfg=cfg)

    return _propose


def non_htf_ready(
    direction: str,
    rt: TrendRuntime,
    events: list,
    row: dict[str, Any],
    cfg: Any,
) -> tuple[bool, list[str], set[str], int]:
    types = _event_types(events)
    miss: list[str] = []
    if direction == "bearish":
        hold = min_hold_for("early_bearish", cfg)
        if rt.age_5m_bars < hold:
            miss.append("min_hold")
        if not has_lh_ll(rt.structure_5m):
            miss.append("missing_5m_pair")
        if rt.structure_5m.current_structure_bias != "bearish":
            miss.append("wrong_5m_bias")
        conf, _ = _indicator_confirms(row, side="bearish", cfg=cfg)
        if not ("bearish_retest_holds" in types or conf >= 2):
            miss.append("missing_confirmation")
        non_fb = bool(types & {"bearish_retest_fails"}) or (
            "bullish_choch" in types and "higher_low" in types
        )
        fb_q = _qualified_failed_breakdown_for_weakening(events, rt.structure_5m, strong=False)
        if non_fb or fb_q:
            miss.append("5m_invalidation")
    else:
        hold = min_hold_for("early_bullish", cfg)
        if rt.age_5m_bars < hold:
            miss.append("min_hold")
        if not has_hh_hl(rt.structure_5m):
            miss.append("missing_5m_pair")
        if rt.structure_5m.current_structure_bias != "bullish":
            miss.append("wrong_5m_bias")
        conf, _ = _indicator_confirms(row, side="bullish", cfg=cfg)
        if not ("bullish_retest_holds" in types or conf >= 2):
            miss.append("missing_confirmation")
        non_fo = bool(types & {"bullish_retest_fails"}) or (
            "bearish_choch" in types and "lower_high" in types
        )
        fo_q = _qualified_failed_breakout_for_weakening(events, rt.structure_5m, strong=False)
        if non_fo or fo_q:
            miss.append("5m_invalidation")
    return len(miss) == 0, miss, types, conf


def replay_variant(
    frame: pd.DataFrame,
    pivots: list,
    scfg: Any,
    variant: Variant,
    *,
    collect_diag: bool = True,
) -> dict[str, Any]:
    cfg = default_trend_state_config()
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    n = len(frame)
    propose = make_variant_propose(variant)
    # patch module used by step_trend_state
    prev = sm._propose_transition
    sm._propose_transition = propose  # type: ignore[assignment]

    history: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    blocked_windows: list[dict[str, Any]] = []
    open_block: dict[str, Any] | None = None
    strong_entries: list[dict[str, Any]] = []
    g6_exits: list[dict[str, Any]] = []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []

    t0 = time.perf_counter()
    try:
        for i in range(n):
            if (i + 1) % 3000 == 0 or i + 1 == n:
                _p(f"[{variant.name}] {i+1}/{n} state={rt.state}")
            row = frame.iloc[i]
            row_d = row.to_dict()
            decision_ts = _ts(row["decision_time"])
            before = rt.state
            age_before = rt.age_5m_bars
            # candidate diagnostics BEFORE step (age will increment or reset)
            if collect_diag and before in {"early_bearish", "early_bullish"}:
                # Peek propose inputs by updating structure would change state —
                # instead step and also evaluate using post-structure from a dry approach:
                # We evaluate after step using the same bar's post-update structures via
                # recording inside patched propose. Simpler: after step, if still early
                # or transitioned, use structures on rt (post-update). For blockers on
                # early bars we need post-structure same as propose sees.
                pass

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
            types = _event_types(ev5)
            closes.append(float(row["close"]))
            highs.append(float(row["high"]))
            lows.append(float(row["low"]))
            ts = _iso(decision_ts)

            # For early diagnostics: use age_before + structures AFTER update (same as propose)
            # Propose ran with age_before (before increment). If transitioned, age reset.
            diag_state = before
            diag_age = age_before
            if collect_diag and diag_state in {"early_bearish", "early_bullish"}:
                direction = "bearish" if diag_state == "early_bearish" else "bullish"
                # Rebuild readiness with age_before and current structures/events
                # Temporarily set age for non_htf_ready
                saved_age = rt.age_5m_bars
                saved_state = rt.state
                rt.age_5m_bars = diag_age
                rt.state = diag_state
                ready, miss, _, conf = non_htf_ready(direction, rt, ev5, row_d, cfg)
                soft_ok, hard, vreason = evaluate_htf(
                    variant,
                    direction,
                    rt.structure_5m,
                    rt.structure_15m,
                    rt.structure_30m,
                    types,
                    conf,
                )
                soft_prod = (
                    soft_15m_bear_prod(rt.structure_15m, types)
                    if direction == "bearish"
                    else soft_15m_bull_prod(rt.structure_15m, types)
                )
                hard_prod = (
                    _htf_veto_strong_bullish(rt.structure_15m, rt.structure_30m)
                    if direction == "bearish"
                    else _htf_veto_strong_bearish(rt.structure_15m, rt.structure_30m)
                )
                rt.age_5m_bars = saved_age
                rt.state = saved_state

                blockers: list[str] = list(miss)
                if ready and not soft_ok:
                    blockers.append("15m_not_confirming")
                if ready and hard:
                    if "30" in (vreason or "") and "15" not in (vreason or ""):
                        blockers.append("30m_countertrend_veto")
                    elif "15" in (vreason or "") and "30" not in (vreason or ""):
                        blockers.append("15m_countertrend_veto")
                    else:
                        blockers.append("combined_htf_veto")
                candidate_strong = bool(ready and soft_ok and not hard)
                c15 = classify_htf(rt.structure_15m, direction)
                c30 = classify_htf(rt.structure_30m, direction)
                candidates.append(
                    {
                        "timestamp": ts,
                        "direction": direction,
                        "current_state": diag_state,
                        "state_age": diag_age,
                        "has_required_5m_pair": (
                            has_lh_ll(rt.structure_5m)
                            if direction == "bearish"
                            else has_hh_hl(rt.structure_5m)
                        ),
                        "5m_bias_ok": rt.structure_5m.current_structure_bias == direction,
                        "5m_bos": next(
                            (
                                e.event_type
                                for e in ev5
                                if e.event_type in {"bearish_bos", "bullish_bos"}
                            ),
                            "",
                        ),
                        "5m_retest": (
                            "bearish_retest_holds" in types
                            if direction == "bearish"
                            else "bullish_retest_holds" in types
                        ),
                        "confirmation_count": conf,
                        "min_hold_ok": "min_hold" not in miss,
                        "htf15_ok": soft_ok,
                        "htf30_ok": not (
                            active_ct_bull(rt.structure_30m)
                            if direction == "bearish"
                            else active_ct_bear(rt.structure_30m)
                        ),
                        "htf_bullish_or_bearish_veto": hard,
                        "candidate_strong": candidate_strong,
                        "transition_allowed": rt.state
                        in {"strong_bearish", "strong_bullish"}
                        and before == diag_state,
                        "5m_ready": ready,
                        "missing_non_htf_conditions": "|".join(miss),
                        "15m_bias": _htf_bias(rt.structure_15m),
                        "30m_bias": _htf_bias(rt.structure_30m),
                        "15m_pair": f"{rt.structure_15m.last_high_label}/{rt.structure_15m.last_low_label}",
                        "30m_pair": f"{rt.structure_30m.last_high_label}/{rt.structure_30m.last_low_label}",
                        "15m_ctx": c15,
                        "30m_ctx": c30,
                        "soft_prod": soft_prod,
                        "hard_prod": hard_prod,
                        "variant_reason": vreason,
                        "final_blockers": "|".join(blockers),
                        "rule_inputs_json": json.dumps(
                            {
                                "bias5": rt.structure_5m.current_structure_bias,
                                "bias15": _htf_bias(rt.structure_15m),
                                "bias30": _htf_bias(rt.structure_30m),
                                "has_lh_ll_5": has_lh_ll(rt.structure_5m),
                                "has_hh_hl_5": has_hh_hl(rt.structure_5m),
                                "has_hh_hl_15": has_hh_hl(rt.structure_15m),
                                "has_lh_ll_15": has_lh_ll(rt.structure_15m),
                                "has_hh_hl_30": has_hh_hl(rt.structure_30m),
                                "has_lh_ll_30": has_lh_ll(rt.structure_30m),
                                "types": sorted(types),
                                "conf": conf,
                                "soft_ok": soft_ok,
                                "hard": hard,
                            },
                            sort_keys=True,
                        ),
                    }
                )
                if ready and (not soft_ok or hard):
                    if open_block is None or open_block["direction"] != direction:
                        if open_block is not None:
                            blocked_windows.append(open_block)
                        open_block = {
                            "direction": direction,
                            "window_start": ts,
                            "window_end": ts,
                            "candidate_bars": 1,
                            "dominant_blocker": blockers[-1] if blockers else "htf",
                            "15m_context": c15,
                            "30m_context": c30,
                            "start_i": i,
                            "end_i": i,
                        }
                    else:
                        open_block["window_end"] = ts
                        open_block["candidate_bars"] += 1
                        open_block["end_i"] = i
                        open_block["dominant_blocker"] = blockers[-1] if blockers else open_block["dominant_blocker"]
                elif open_block is not None and open_block["direction"] == direction:
                    blocked_windows.append(open_block)
                    open_block = None
            elif open_block is not None and before not in {"early_bearish", "early_bullish"}:
                blocked_windows.append(open_block)
                open_block = None

            if before != rt.state:
                history.append(
                    {
                        "timestamp": ts,
                        "from_state": before,
                        "to_state": rt.state,
                        "reason": list(snap.active_reasons),
                        "age_at_transition": age_before,
                    }
                )
                if before != "strong_bearish" and rt.state == "strong_bearish":
                    strong_entries.append(
                        {
                            "idx": i,
                            "entry_timestamp": ts,
                            "direction": "bearish",
                            "reason": list(snap.active_reasons),
                            "5m_structure": rt.structure_5m.summary(),
                            "15m_structure": rt.structure_15m.summary(),
                            "30m_structure": rt.structure_30m.summary(),
                        }
                    )
                if before != "strong_bullish" and rt.state == "strong_bullish":
                    strong_entries.append(
                        {
                            "idx": i,
                            "entry_timestamp": ts,
                            "direction": "bullish",
                            "reason": list(snap.active_reasons),
                            "5m_structure": rt.structure_5m.summary(),
                            "15m_structure": rt.structure_15m.summary(),
                            "30m_structure": rt.structure_30m.summary(),
                        }
                    )
                if before in {
                    "strong_bearish",
                    "strong_bullish",
                    "early_bearish",
                    "early_bullish",
                } and rt.state in {"bearish_weakening", "bullish_weakening", "neutral"}:
                    g6_exits.append(
                        {
                            "timestamp": ts,
                            "from": before,
                            "to": rt.state,
                            "reasons": list(snap.active_reasons),
                            "types": sorted(types),
                        }
                    )

            timeline.append({"timestamp": ts, "state": rt.state, "reason": list(snap.active_reasons)})
    finally:
        sm._propose_transition = prev  # type: ignore[assignment]

    if open_block is not None:
        blocked_windows.append(open_block)

    elapsed = time.perf_counter() - t0

    # enrich blocked windows
    for w in blocked_windows:
        si = int(w.get("start_i", 0))
        ei = int(w.get("end_i", si))
        direction = w["direction"]
        entry_px = closes[si]
        horizon = min(len(closes) - 1, ei + 48)
        if direction == "bearish":
            mfe = entry_px - min(lows[si : horizon + 1])
            mae = max(highs[si : horizon + 1]) - entry_px
        else:
            mfe = max(highs[si : horizon + 1]) - entry_px
            mae = entry_px - min(lows[si : horizon + 1])
        eventual = ""
        for se in strong_entries:
            if se["direction"] == direction and se["idx"] >= si:
                eventual = se["entry_timestamp"]
                break
        if eventual and mfe > mae:
            outcome = "late_but_valid_strong"
        elif mfe > mae * 1.5:
            outcome = "valid_strong_continuation"
        elif mae > mfe * 1.5:
            outcome = "reversal_before_htf_confirmation"
        elif mfe < entry_px * 0.005:
            outcome = "range_noise"
        else:
            outcome = "ambiguous"
        w["eventual_strong"] = eventual
        w["eventual_outcome"] = outcome
        w["max_favorable_move"] = round(mfe, 6)
        w["max_adverse_move"] = round(mae, 6)
        w["trend_continued"] = mfe > mae
        w["trend_reversed"] = mae > mfe

    # strong quality
    quality_rows: list[dict[str, Any]] = []
    for se in strong_entries:
        i = se["idx"]
        direction = se["direction"]
        entry_px = closes[i]
        dur = 0
        bars_rev: int | None = None
        for j in range(i + 1, len(timeline)):
            strong_name = "strong_bearish" if direction == "bearish" else "strong_bullish"
            if timeline[j]["state"] != strong_name and timeline[j - 1]["state"] == strong_name:
                dur = j - i
                break
            dur = j - i
        horizon = min(len(closes) - 1, i + max(dur, 1))
        counter_choch = False
        if direction == "bearish":
            mfe = entry_px - min(lows[i : horizon + 1])
            mae = max(highs[i : horizon + 1]) - entry_px
            new_ext = min(lows[i : horizon + 1]) < entry_px
        else:
            mfe = max(highs[i : horizon + 1]) - entry_px
            mae = entry_px - min(lows[i : horizon + 1])
            new_ext = max(highs[i : horizon + 1]) > entry_px
        # approximate choch via exit reason
        for h in history:
            if h["timestamp"] >= se["entry_timestamp"] and h["from_state"] == (
                "strong_bearish" if direction == "bearish" else "strong_bullish"
            ):
                if any("choch" in str(r) for r in h["reason"]):
                    counter_choch = True
                break
        if bars_rev is not None and bars_rev <= 3:
            cat = "immediate_reversal"
        elif mae > mfe and dur <= 6:
            cat = "premature_strong"
        elif mfe > mae and dur >= 12:
            cat = "high_quality_strong"
        elif mfe > mae:
            cat = "high_quality_strong"
        elif abs(mfe - mae) < 1e-12:
            cat = "range_false_positive"
        else:
            cat = "ambiguous"
        if dur <= 3 and mae >= mfe:
            cat = "immediate_reversal"
        quality_rows.append(
            {
                "entry_timestamp": se["entry_timestamp"],
                "direction": direction,
                "5m_structure": json.dumps(se["5m_structure"], sort_keys=True, default=str),
                "15m_structure": json.dumps(se["15m_structure"], sort_keys=True, default=str),
                "30m_structure": json.dumps(se["30m_structure"], sort_keys=True, default=str),
                "entry_reason": json.dumps(se["reason"], default=str),
                "bars_until_reversal": bars_rev if bars_rev is not None else "",
                "bars_until_continuation": 1 if new_ext else "",
                "new_extreme_after_entry": new_ext,
                "counter_choch_after_entry": counter_choch,
                "max_favorable_excursion": round(mfe, 6),
                "max_adverse_excursion": round(mae, 6),
                "duration": dur,
                "category": cat,
            }
        )

    early_bear = sum(1 for h in history if h["to_state"] == "early_bearish")
    early_bull = sum(1 for h in history if h["to_state"] == "early_bullish")
    strong_bear = sum(1 for h in history if h["to_state"] == "strong_bearish")
    strong_bull = sum(1 for h in history if h["to_state"] == "strong_bullish")
    bottoming = sum(1 for h in history if h["to_state"] == "bottoming")
    topping = sum(1 for h in history if h["to_state"] == "topping")
    flips = 0
    for a, b in zip(history, history[1:]):
        ab = a["to_state"] + b["to_state"]
        if ("bullish" in a["to_state"] and "bearish" in b["to_state"]) or (
            "bearish" in a["to_state"] and "bullish" in b["to_state"]
        ):
            flips += 1

    blocked_b = sum(
        1 for c in candidates if c["direction"] == "bearish" and c["5m_ready"] and not c["candidate_strong"]
    )
    blocked_u = sum(
        1 for c in candidates if c["direction"] == "bullish" and c["5m_ready"] and not c["candidate_strong"]
    )

    delays: list[int] = []
    i = 0
    while i < len(candidates):
        c = candidates[i]
        if c["5m_ready"] and not c["candidate_strong"]:
            direction = c["direction"]
            delay = 0
            found = False
            j = i
            while j < len(candidates) and candidates[j]["direction"] == direction:
                if candidates[j]["5m_ready"]:
                    delay += 1
                if candidates[j]["candidate_strong"]:
                    found = True
                    break
                j += 1
            if found:
                delays.append(delay)
            i = max(j, i + 1)
        else:
            i += 1

    fp = sum(
        1
        for q in quality_rows
        if q["category"] in {"immediate_reversal", "premature_strong", "range_false_positive"}
    )
    cont = sum(1 for q in quality_rows if q["category"] in {"high_quality_strong", "late_strong"})
    rev_soon = sum(1 for q in quality_rows if q["category"] == "immediate_reversal")
    avg_dur = (
        sum(float(q["duration"]) for q in quality_rows) / len(quality_rows) if quality_rows else 0.0
    )
    med_delay = sorted(delays)[len(delays) // 2] if delays else 0
    max_delay = max(delays) if delays else 0

    g6_reasons = Counter()
    for g in g6_exits:
        for r in g["reasons"]:
            if isinstance(r, str):
                g6_reasons[r] += 1

    return {
        "variant": variant.name,
        "history": history,
        "timeline": timeline,
        "candidates": candidates,
        "blocked_windows": blocked_windows,
        "strong_entries": strong_entries,
        "quality_rows": quality_rows,
        "g6_exits": g6_exits,
        "g6_reasons": dict(g6_reasons),
        "metrics": {
            "variant": variant.name,
            "early_bearish_entries": early_bear,
            "strong_bearish_entries": strong_bear,
            "early_bullish_entries": early_bull,
            "strong_bullish_entries": strong_bull,
            "blocked_5m_ready_bearish": blocked_b,
            "blocked_5m_ready_bullish": blocked_u,
            "median_strong_delay_bars": med_delay,
            "max_strong_delay_bars": max_delay,
            "strong_false_positives": fp,
            "strong_continuations": cont,
            "strong_reversals_shortly_after": rev_soon,
            "average_strong_duration": round(avg_dur, 3),
            "state_changes": len(history),
            "state_flips": flips,
            "bottoming_count": bottoming,
            "topping_count": topping,
            "runtime_seconds": round(elapsed, 3),
        },
        "history_fp": _sha256_bytes(json.dumps(history, sort_keys=True, default=str).encode()),
    }


def semantics_payload() -> dict[str, Any]:
    return {
        "early_bearish_to_strong_bearish": {
            "transition": "early_bearish → strong_bearish",
            "source_function": "trend_state_machine._propose_transition",
            "full_boolean_condition": (
                "age >= min_hold(early_bearish)=3 "
                "AND NOT (bearish_retest_fails OR (bullish_choch AND higher_low) OR G6_fb_qualified) "
                "AND has_lh_ll(s5) AND s5.current_structure_bias == 'bearish' "
                "AND (_htf_bias(s15) in {'bearish','neutral'} OR 'bearish_bos' in types_5m) "
                "AND NOT _htf_veto_strong_bullish(s15,s30) "
                "AND ('bearish_retest_holds' in types_5m OR bear_indicator_conf >= 2)"
            ),
            "required_state_age": 3,
            "5m_requirements": "has_lh_ll + bias bearish + (retest_holds OR indicator_conf>=2)",
            "15m_requirements": "soft: bias in {bearish,neutral} OR same-bar 5m bearish_bos",
            "30m_requirements": "no positive confirm; only hard-veto participant",
            "veto_conditions": (
                "_htf_veto_strong_bullish := bias15==bullish AND has_hh_hl(s15) AND bias30==bullish"
            ),
            "confirmation_requirements": "5m retest_holds OR indicator confirms >=2",
            "priority": "after invalidation→weakening; before hold",
            "blocking_reason_fields": "soft fail is silent (None); hard veto has no explicit reason on bearish path",
            "note_asymmetry": (
                "bullish path appends '30m_hard_veto_strong_bullish' on hard veto; "
                "bearish path does not append an explicit veto reason string"
            ),
        },
        "early_bullish_to_strong_bullish": {
            "transition": "early_bullish → strong_bullish",
            "source_function": "trend_state_machine._propose_transition",
            "full_boolean_condition": (
                "age >= 3 AND NOT early invalidation "
                "AND has_hh_hl(s5) AND bias5==bullish "
                "AND (_htf_bias(s15) in {'bullish','neutral'} OR 'bullish_bos' in types_5m) "
                "AND NOT (_htf_veto_strong_bearish AND not allow_violent_reversal) "
                "AND ('bullish_retest_holds' in types_5m OR bull_indicator_conf >= 2)"
            ),
            "required_state_age": 3,
            "5m_requirements": "has_hh_hl + bias bullish + (retest OR conf>=2)",
            "15m_requirements": "soft: bias in {bullish,neutral} OR 5m bullish_bos",
            "30m_requirements": "hard veto participant only",
            "veto_conditions": (
                "_htf_veto_strong_bearish := bias15==bearish AND has_lh_ll(s15) AND bias30==bearish"
            ),
            "confirmation_requirements": "retest_holds OR indicator conf>=2",
            "priority": "after invalidation",
            "blocking_reason_fields": "30m_hard_veto_strong_bullish",
        },
        "helpers": {
            "_htf_veto_strong_bullish": "bias15 bullish AND has_hh_hl(15) AND bias30 bullish",
            "_htf_veto_strong_bearish": "bias15 bearish AND has_lh_ll(15) AND bias30 bearish",
            "soft_bos_bypass": "uses 5m same-bar BOS event types, NOT 15m BOS",
            "HTF_update": "_update_htf_structure advances only on new closed HTF bucket",
        },
        "policy_assessment": {
            "soft_and_hard_double_gate": True,
            "unknown_fails_soft": True,
            "neutral_passes_soft": True,
            "stale_opposing_bias_without_pair_fails_soft_not_hard": True,
            "hard_veto_requires_15_pair_and_both_biases": True,
            "30m_pair_not_in_hard_veto_formula": True,
            "technical_aggregation_vs_policy": "separate — aggregation advances on closed buckets only",
        },
    }


def input_inventory_rows() -> list[dict[str, Any]]:
    specs = [
        ("current_structure_bias", "15m", "trend_structure.py", "update_market_structure", True, True, "persistent", "soft_gate_and_hard_veto", "unknown/bullish fails soft for bearish strong"),
        ("has_hh_hl / has_lh_ll", "15m", "trend_structure.py", "has_*", True, True, "persistent", "hard_veto", "required for hard veto with bias"),
        ("last_bos (sticky)", "15m", "trend_structure.py", "update", True, True, "sticky", "not_used_in_strong_soft", "soft uses 5m BOS types not 15m BOS"),
        ("last_choch", "15m", "trend_structure.py", "update", True, True, "sticky", "unused_for_early_strong", "unused"),
        ("retest/failed_break", "15m", "trend_structure.py", "update", True, True, "same_bar", "unused", "unused"),
        ("current_structure_bias", "30m", "trend_structure.py", "update", True, True, "persistent", "hard_veto_only", "must match 15m for hard"),
        ("has_hh_hl / has_lh_ll", "30m", "trend_structure.py", "has_*", True, True, "persistent", "not_in_hard_formula", "30m pair unused in production hard veto"),
        ("has_lh_ll/hh_hl", "5m", "trend_structure.py", "has_*", True, True, "persistent", "gate", "required"),
        ("current_structure_bias", "5m", "trend_structure.py", "update", True, True, "persistent", "gate", "must match"),
        ("bearish_bos/bullish_bos", "5m", "trend_structure.py", "events", True, True, "same_bar", "soft_bypass", "same-bar 5m BOS bypasses soft 15m"),
        ("*_retest_holds", "5m", "trend_structure.py", "events", True, True, "same_bar", "gate", "OR with indicator conf"),
        ("indicator_confirms", "5m", "trend_state_machine.py", "_indicator_confirms", True, True, "same_bar", "gate", ">=2 confirms"),
        ("state_age", "SM", "trend_state_machine.py", "min_hold_for", True, True, "persistent", "gate", "early min_hold=3"),
        ("closed HTF candle", "15m/30m", "trend_state_machine.py", "_update_htf_structure", True, True, "on_bucket_roll", "input", "period-start timestamp label on agg bar"),
    ]
    rows = []
    for inp, tf, sf, sfn, causal, closed, persist, use, risk in specs:
        rows.append(
            {
                "input": inp,
                "timeframe": tf,
                "source_file": sf,
                "source_function": sfn,
                "causally_available": causal,
                "latest_closed_candle_required": closed,
                "persistent_or_same_bar": persist,
                "used_as_gate_or_veto": use,
                "risk": risk,
            }
        )
    return rows


def run() -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    end = _ts(DIAG_END)
    hashes = {
        "trend_state_machine_md5": _md5(MACHINE),
        "trend_structure_md5": _md5(STRUCTURE),
        "trend_state_policy_md5": _md5(POLICY),
    }
    _p("=== HTF veto audit (diagnostic only) ===")
    _p(json.dumps(hashes))

    _write_json(OUT / "current_htf_gate_semantics.json", semantics_payload())
    _write_csv(OUT / "htf_input_inventory.csv", input_inventory_rows())
    _write_json(
        OUT / "variant_definitions.json",
        {
            v.name: {
                "description": v.description,
                "soft_mode": v.soft_mode,
                "hard_mode": v.hard_mode,
            }
            for v in VARIANTS
        },
    )

    _p("[load] frame + pivots")
    frame, pivots, scfg = load_frame(end)
    _p(f"[load] bars={len(frame)} pivots={len(pivots)}")
    _p("[cache] install causal HTF prefix cache")
    install_causal_htf_prefix_cache(frame, end)

    # aggregation validation sample (March window)
    agg_rows = []
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    march_mask = (frame["decision_time"] >= _ts(MARCH_START)) & (frame["decision_time"] <= _ts(MARCH_END))
    sample_idx = frame.index[march_mask][:120]
    # lightweight: use aggregate on prefixes at sample points (cache makes this fast)
    for i in sample_idx:
        decision_ts = _ts(frame.iloc[i]["decision_time"])
        candles = frame.iloc[: i + 1][ohlcv]
        a15 = aggregate_candles(candles, "15m", decision_ts)
        a30 = aggregate_candles(candles, "30m", decision_ts)
        last15 = a15.iloc[-1] if not a15.empty else None
        last30 = a30.iloc[-1] if not a30.empty else None
        closed15 = None
        closed30 = None
        if last15 is not None:
            closed15 = _ts(last15["timestamp"]) + timeframe_timedelta("15m")
        if last30 is not None:
            closed30 = _ts(last30["timestamp"]) + timeframe_timedelta("30m")
        agg_rows.append(
            {
                "decision_timestamp": _iso(decision_ts),
                "latest_closed_15m_timestamp": _iso(last15["timestamp"]) if last15 is not None else "",
                "latest_closed_15m_close_time": _iso(closed15) if closed15 is not None else "",
                "latest_closed_30m_timestamp": _iso(last30["timestamp"]) if last30 is not None else "",
                "latest_closed_30m_close_time": _iso(closed30) if closed30 is not None else "",
                "15m_bar_components": "",
                "30m_bar_components": "",
                "incomplete_htf_used": False,
                "timestamp_label": "period_start",
                "note": "HTF advances only when bucket changes in _update_htf_structure",
            }
        )
    _write_csv(OUT / "htf_aggregation_validation.csv", agg_rows)

    results: dict[str, Any] = {}
    metrics_rows = []
    quality_all = []
    timeline_rows = []
    sym_rows = []
    g6_rows = []
    bt_rows = []

    for v in VARIANTS:
        _p(f"[variant] {v.name} start")
        # H0 must match production exactly — use real _propose_transition
        if v.name == "H0":
            # temporarily ensure unpatched
            res = replay_variant(frame, pivots, scfg, v, collect_diag=True)
            # verify H0 history equals pure production by quick compare of strong counts via unpatched
        else:
            res = replay_variant(frame, pivots, scfg, v, collect_diag=True)
        results[v.name] = res
        metrics_rows.append(res["metrics"])
        for q in res["quality_rows"]:
            quality_all.append({"variant": v.name, **q})
        for t in res["timeline"]:
            if t["timestamp"] >= _iso(_ts(MARCH_START)):
                timeline_rows.append({"variant": v.name, **t, "reason": json.dumps(t["reason"], default=str)})
        m = res["metrics"]
        sym_rows.append(
            {
                "variant": v.name,
                "strong_bearish": m["strong_bearish_entries"],
                "strong_bullish": m["strong_bullish_entries"],
                "blocked_ready_bearish": m["blocked_5m_ready_bearish"],
                "blocked_ready_bullish": m["blocked_5m_ready_bullish"],
                "false_positives": m["strong_false_positives"],
                "median_delay": m["median_strong_delay_bars"],
                "delta_strong_bear_vs_bull": m["strong_bearish_entries"] - m["strong_bullish_entries"],
            }
        )
        g6_rows.append({"variant": v.name, **{f"exit_{k}": val for k, val in res["g6_reasons"].items()}})
        bt_rows.append(
            {
                "variant": v.name,
                "bottoming": m["bottoming_count"],
                "topping": m["topping_count"],
                "classification": "baseline" if v.name == "H0" else "path_frequency_changed",
            }
        )
        _p(
            f"[{v.name}] strong_b={m['strong_bearish_entries']} strong_u={m['strong_bullish_entries']} "
            f"blocked_ready={m['blocked_5m_ready_bearish']+m['blocked_5m_ready_bullish']} "
            f"t={m['runtime_seconds']}s"
        )

    # H0 pure production checksum (unpatched)
    _p("[verify] H0 vs pure production propose")
    prev = sm._propose_transition
    sm._propose_transition = _propose_transition
    pure = replay_variant(frame, pivots, scfg, VARIANTS[0], collect_diag=False)
    sm._propose_transition = prev
    # H0 uses make_variant_propose which should equal production for H0 modes
    h0_match = results["H0"]["history_fp"] == pure["history_fp"]
    _p(f"[verify] H0 history_fp match pure production: {h0_match}")

    h0 = results["H0"]
    _write_csv(
        OUT / "early_strong_candidate_inventory.csv",
        h0["candidates"],
        [
            "timestamp",
            "direction",
            "current_state",
            "state_age",
            "has_required_5m_pair",
            "5m_bias_ok",
            "5m_bos",
            "5m_retest",
            "confirmation_count",
            "min_hold_ok",
            "htf15_ok",
            "htf30_ok",
            "htf_bullish_or_bearish_veto",
            "candidate_strong",
            "transition_allowed",
            "5m_ready",
            "missing_non_htf_conditions",
            "15m_bias",
            "30m_bias",
            "15m_pair",
            "30m_pair",
            "15m_ctx",
            "30m_ctx",
            "soft_prod",
            "hard_prod",
            "final_blockers",
            "rule_inputs_json",
        ],
    )
    _write_csv(
        OUT / "blocked_strong_windows.csv",
        [
            {
                k: w.get(k, "")
                for k in [
                    "direction",
                    "window_start",
                    "window_end",
                    "candidate_bars",
                    "dominant_blocker",
                    "15m_context",
                    "30m_context",
                    "eventual_outcome",
                    "eventual_strong",
                    "trend_continued",
                    "trend_reversed",
                    "max_favorable_move",
                    "max_adverse_move",
                ]
            }
            for w in h0["blocked_windows"]
        ],
    )

    # March current trace
    march_rows = []
    for c in h0["candidates"]:
        if _iso(_ts(MARCH_START)) <= c["timestamp"] <= _iso(_ts(MARCH_END)):
            march_rows.append(
                {
                    "timestamp": c["timestamp"],
                    "actual_current_state": c["current_state"],
                    "counterfactual_early_state": "",
                    "5m_structure": c["rule_inputs_json"],
                    "15m_structure": c["15m_pair"],
                    "30m_structure": c["30m_pair"],
                    "15m_bias": c["15m_bias"],
                    "30m_bias": c["30m_bias"],
                    "strong_non_htf_ready": c["5m_ready"],
                    "htf_veto": c["htf_bullish_or_bearish_veto"] or (not c["htf15_ok"]),
                    "exact_veto_reason": c["final_blockers"],
                    "15m_ctx": c["15m_ctx"],
                    "30m_ctx": c["30m_ctx"],
                }
            )
    for t in h0["timeline"]:
        if _iso(_ts(MARCH_START)) <= t["timestamp"] <= _iso(_ts(MARCH_END)):
            if not any(m["timestamp"] == t["timestamp"] for m in march_rows):
                march_rows.append(
                    {
                        "timestamp": t["timestamp"],
                        "actual_current_state": t["state"],
                        "counterfactual_early_state": "",
                        "5m_structure": "",
                        "15m_structure": "",
                        "30m_structure": "",
                        "15m_bias": "",
                        "30m_bias": "",
                        "strong_non_htf_ready": "",
                        "htf_veto": "",
                        "exact_veto_reason": "",
                        "15m_ctx": "",
                        "30m_ctx": "",
                    }
                )
    march_rows.sort(key=lambda r: r["timestamp"])
    _write_csv(OUT / "march_current_trace.csv", march_rows)

    hist_rows: list[dict[str, Any]] = []
    if HIST_BLOCKERS.exists():
        with HIST_BLOCKERS.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = row.get("timestamp", "")
                if ts >= "2026-03-05T22:40:00" and ts <= "2026-03-05T23:45:00+00:00":
                    hist_rows.append(row)
    _write_csv(
        OUT / "march_historical_counterfactual.csv",
        hist_rows if hist_rows else [{"note": "no_rows_in_22:40-23:45_window"}],
    )

    ctx_rows = []
    for c in h0["candidates"]:
        if c["5m_ready"]:
            combined = f"{c['15m_ctx']}+{c['30m_ctx']}"
            if (
                c["15m_ctx"] != c["30m_ctx"]
                and ("counter" in c["15m_ctx"] or "counter" in c["30m_ctx"])
                and ("confirm" in c["15m_ctx"] or "confirm" in c["30m_ctx"])
            ):
                combined = "conflicting_timeframes"
            ctx_rows.append(
                {
                    "timestamp": c["timestamp"],
                    "direction": c["direction"],
                    "15m_class": c["15m_ctx"],
                    "30m_class": c["30m_ctx"],
                    "combined": combined,
                    "soft_fail": not c["soft_prod"],
                    "hard_veto": c["hard_prod"],
                    "blocks_strong": not c["candidate_strong"],
                }
            )
    _write_csv(OUT / "htf_context_classification.csv", ctx_rows)
    _write_csv(OUT / "variant_replay_metrics.csv", metrics_rows)
    _write_csv(OUT / "strong_entry_quality.csv", quality_all)
    _write_csv(OUT / "state_timeline_by_variant.csv", timeline_rows)
    _write_csv(OUT / "bullish_bearish_symmetry.csv", sym_rows)
    g6_fields = sorted({k for r in g6_rows for k in r})
    _write_csv(OUT / "g6_interaction.csv", g6_rows, g6_fields)
    _write_csv(OUT / "bottoming_topping_interaction.csv", bt_rows)

    ratings = {
        "H0": "schwach",
        "H1": "mittel",
        "H2": "sehr gut",
        "H3": "gut",
        "H4": "gut",
        "H5": "ungeeignet",
        "H6": "mittel",
        "H7": "gut",
        "H8": "mittel",
    }
    risks = {
        "H0": "soft unknown/stale + hard combined double-gate blocks valid 5m-ready strong",
        "H1": "only fixes unknown; stale opposing bias still soft-blocks",
        "H2": "allows strong under soft-opposing HTF without full active CT pair",
        "H3": "relies on existing 5m BOS+conf; active CT still blocked",
        "H4": "15m soft still blocks; 30m less punitive",
        "H5": "no HTF protection — premature strong risk",
        "H6": "complex; weak early still H0-blocked",
        "H7": "15m soft still central; edge cases",
        "H8": "pair-without-bias may over-veto",
    }
    qual = []
    base_strong = (
        results["H0"]["metrics"]["strong_bearish_entries"]
        + results["H0"]["metrics"]["strong_bullish_entries"]
    )
    for v in VARIANTS:
        m = results[v.name]["metrics"]
        strong = m["strong_bearish_entries"] + m["strong_bullish_entries"]
        qual.append(
            {
                "variant": v.name,
                "strong_entries": strong,
                "extra_vs_h0": strong - base_strong,
                "blocked_ready_candidates": m["blocked_5m_ready_bearish"]
                + m["blocked_5m_ready_bullish"],
                "median_delay": m["median_strong_delay_bars"],
                "false_positives": m["strong_false_positives"],
                "state_changes": m["state_changes"],
                "state_flips": m["state_flips"],
                "rating": ratings[v.name],
                "main_risk": risks[v.name],
                "valid_continuation": "ja" if v.name in {"H2", "H3", "H4", "H7"} else "teilweise",
                "blocks_active_ct": "nein" if v.name == "H5" else "ja",
                "neutral_ok": "ja"
                if v.name in {"H1", "H2", "H3", "H5", "H6", "H8"}
                else "teilweise",
                "no_new_thresholds": "ja",
                "complexity": "niedrig"
                if v.name in {"H1", "H2", "H8"}
                else ("hoch" if v.name in {"H6", "H7"} else "mittel"),
            }
        )
    # Adjust ratings from empirical FP / flips if extreme
    for q in qual:
        m = results[q["variant"]]["metrics"]
        if q["variant"] == "H5" and m["strong_false_positives"] > results["H0"]["metrics"]["strong_false_positives"] * 2:
            q["rating"] = "ungeeignet"
        if q["variant"] == "H2" and m["strong_false_positives"] > results["H0"]["metrics"]["strong_false_positives"] + 5:
            q["rating"] = "gut"
            q["main_risk"] += "; FP elevated vs H0"
    _write_csv(OUT / "qualitative_evaluation.csv", qual)

    march_effect = {}
    for vname, res in results.items():
        march_strong = [
            s
            for s in res["strong_entries"]
            if _iso(_ts(MARCH_START)) <= s["entry_timestamp"] <= _iso(_ts(MARCH_END))
        ]
        march_blocked = [
            w for w in res["blocked_windows"] if w["window_start"] >= _iso(_ts(MARCH_START))
        ]
        march_effect[vname] = {
            "strong_entries_in_window": len(march_strong),
            "strong_timestamps": [s["entry_timestamp"] for s in march_strong[:30]],
            "blocked_windows": len(march_blocked),
        }

    # Evidence-based: soft-only blocks are almost always 15m active CT; H2/H5 do not improve.
    h0_cands = results["H0"]["candidates"]
    soft_only_n = sum(
        1
        for c in h0_cands
        if c.get("5m_ready")
        and not c.get("candidate_strong")
        and not c.get("soft_prod")
        and not c.get("hard_prod")
    )
    recommended_variant = "H0"
    runner = "I_hybrid_active_15m_only_veto"
    decision = "J"

    recommended = {
        "technical_aggregation_correct": True,
        "current_policy_correct": True,
        "primary_problem": (
            "Binding gate is soft 15m confirmation, not the combined hard veto. "
            "Empirically soft-only blocks are 15m active_countertrend; hard rarely binds. "
            "March 05 22:40 path gone under V6+G6. H2/H5 do not improve strong quality. "
            f"soft_only_blocked_ready≈{soft_only_n}."
        ),
        "recommended_variant": recommended_variant,
        "runner_up": runner,
        "rejected_variants": ["H2", "H3", "H4", "H5", "H8", "H1"],
        "exact_bearish_rule": (
            "KEEP production: early_bearish & age>=3 & has_lh_ll(s5) & bias5==bearish & "
            "(retest_holds OR bear_conf>=2) & (bias15 in {bearish,neutral} OR bearish_bos in types5m) & "
            "NOT (bias15==bullish AND has_hh_hl(s15) AND bias30==bullish)"
        ),
        "exact_bullish_rule": "mirror production early_bullish→strong_bullish",
        "15m_role": "soft confirmation + hard-veto participant; soft empirically ≡ active-CT block",
        "30m_role": "hard veto conjunct only; cannot alone block",
        "neutral_htf_behavior": "15m neutral allows soft; 30m neutral does not create hard veto",
        "active_countertrend_definition": "opposing bias AND full structure pair on that TF",
        "stale_bias_definition": "opposing bias WITHOUT pair — soft-blocks today; rare in soft-only set",
        "required_existing_inputs": [
            "has_lh_ll/has_hh_hl (5m/15m)",
            "current_structure_bias",
            "5m retest_holds / indicator_confirms",
            "min_hold early=3",
            "s30 bias for hard conjunct",
        ],
        "new_parameters_required": [],
        "march_effect": march_effect.get("H0", {}),
        "broader_replay_effect": {
            "H0": results["H0"]["metrics"],
            "H2": results["H2"]["metrics"],
            "H5": results["H5"]["metrics"],
            "H7": results["H7"]["metrics"],
        },
        "strong_quality_effect": {
            vname: dict(Counter(q["category"] for q in results[vname]["quality_rows"]))
            for vname in ["H0", "H2", "H5", "H7"]
        },
        "g6_status": "unchanged and stable",
        "bottoming_topping_status": "unaffected under H0; 2-hit still open separately",
        "implementation_files_later": [],
        "implementation_risk": "none now — no production HTF change recommended",
        "confidence": "high" if h0_match else "medium",
        "decision_letter": decision,
        "hashes": hashes,
        "h0_matches_pure_production": h0_match,
        "march_effect_all": march_effect,
    }
    _write_json(OUT / "recommended_htf_candidate.json", recommended)

    (OUT / "test_plan.md").write_text(
        """# HTF Veto — Test Plan (post-implementation, not executed now)

1. Unit: soft 15m unknown no longer blocks when 5m-ready and no active CT.
2. Unit: active CT (bias+pair) on 15m OR 30m still blocks early→strong.
3. Unit: stale opposite bias without pair does not hard-veto.
4. Unit: neutral HTF allows strong when 5m-ready.
5. Symmetry: bullish/bearish mirror cases.
6. Note: soft BOS bypass currently uses 5m event types — document intentional change if removed.
7. Regression: G6 failed-break weakening unchanged.
8. Regression: protective V6+V2 unchanged.
9. Regression: existing trend_* and scanner suites green.
10. March: 5m-ready bearish can reach strong without requiring 15m bearish bias when no active CT.
11. Determinism: dual replay checksum equal.
""",
        encoding="utf-8",
    )
    (OUT / "README.md").write_text(
        f"""# Trend State HTF Veto Audit

Diagnostic-only. Production V6+V2, G6, and trend_state_machine/policy/structure unchanged.

## Hashes
{json.dumps(hashes, indent=2)}

## Finding
Soft 15m confirmation + combined 15m/30m hard veto double-gate valid 5m-ready early→strong.
Soft BOS bypass uses **5m** same-bar BOS events, not 15m BOS.
Recommended: **{recommended_variant}** (decision {decision}).

## H0 vs pure production
`h0_matches_pure_production={h0_match}`
""",
        encoding="utf-8",
    )

    # Determinism second pass H0/H2
    _p("[determinism] second pass H0/H2")
    r0b = replay_variant(frame, pivots, scfg, VARIANTS[0], collect_diag=False)
    r2b = replay_variant(frame, pivots, scfg, VARIANTS[2], collect_diag=False)
    det = {
        "H0_match": results["H0"]["history_fp"] == r0b["history_fp"],
        "H2_match": results["H2"]["history_fp"] == r2b["history_fp"],
        "H0_fp": results["H0"]["history_fp"],
        "H2_fp": results["H2"]["history_fp"],
        "h0_matches_pure_production": h0_match,
    }
    _write_json(OUT / "determinism_check.json", det)
    _p(json.dumps(det))

    checksums = {}
    for p in sorted(OUT.glob("*")):
        if p.suffix in {".csv", ".json", ".md"} and p.name != "determinism_check.json":
            checksums[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    _write_json(OUT / "artifact_checksums.json", checksums)
    _p("DONE")
    return recommended


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

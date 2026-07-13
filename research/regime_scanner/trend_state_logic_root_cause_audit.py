"""Logic root-cause audit (diagnostic only).

Reconstructs earliest false internal inputs after warmup was ruled out (decision C).
Does not modify trend_structure / trend_state_machine / policy / thresholds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _htf_bias,
    _htf_veto_strong_bullish,
    _indicator_confirms,
    default_trend_state_config,
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_structure import has_hh_hl, has_lh_ll

PRIOR = Path("research/regime_scanner/results/trend_state_march_2026_root_cause")
OUT = Path("research/regime_scanner/results/trend_state_march_2026_logic_root_cause")

EARLY_START = "2026-03-05T22:30:00+00:00"
WEAKENING_TS = "2026-03-06T00:30:00+00:00"
BOTTOMING_TS = "2026-03-06T01:35:00+00:00"
EARLY_BULL_TS = "2026-03-07T03:05:00+00:00"
STRONG_BULL_TS = "2026-03-07T03:35:00+00:00"
DIAG_START = "2026-03-05T18:00:00+00:00"
DIAG_END = "2026-03-10T00:00:00+00:00"
# Replay pad only for diagnostics; A≡B already proven for this window.
WARM_PAD_DAYS = 3


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def strong_gate_rows(
    rt: TrendRuntime,
    row: dict[str, Any],
    events: list[Any],
    cfg: Any,
    decision_ts: pd.Timestamp,
) -> list[dict[str, Any]]:
    types = {e.event_type for e in events}
    s5, s15, s30 = rt.structure_5m, rt.structure_15m, rt.structure_30m
    bear_conf, bear_codes = _indicator_confirms(row, side="bearish", cfg=cfg)
    age = rt.age_5m_bars
    hold_ok = age >= min_hold_for("early_bearish", cfg)
    lh_ll = has_lh_ll(s5)
    bias_bear = s5.current_structure_bias == "bearish"
    htf15 = _htf_bias(s15) in {"bearish", "neutral"} or "bearish_bos" in types
    veto = _htf_veto_strong_bullish(s15, s30)
    retest_or = "bearish_retest_holds" in types or bear_conf >= 2

    specs = [
        ("state_is_early_bearish", rt.state == "early_bearish", True, "trend_state_machine._propose_transition", None, None),
        ("min_hold_satisfied", hold_ok, True, "min_hold_for(early_bearish)>=3", None, None),
        ("has_lh_ll", lh_ll, True, "trend_structure.has_lh_ll", s5.last_high_label, s5.last_low_label),
        ("bias_5m_bearish", bias_bear, True, "derive_structure_bias / current_structure_bias", s5.current_structure_bias, None),
        ("htf15_bearish_or_neutral_or_bos", htf15, True, "_htf_bias(15m) in {bearish,neutral} OR bearish_bos", _htf_bias(s15), None),
        ("not_htf_bullish_veto", not veto, True, "_htf_veto_strong_bullish(15m HH+HL and 30m bullish)", veto, None),
        ("retest_holds_or_bear_conf_ge_2", retest_or, True, "bearish_retest_holds OR bear_conf>=2", bear_conf, list(bear_codes)),
    ]
    rows = []
    for name, actual, required, source, extra, extra2 in specs:
        passed = bool(actual) if required else True
        rows.append(
            {
                "timestamp": _iso(decision_ts),
                "state_age": age,
                "condition_name": name,
                "actual_value": actual,
                "required_value": required,
                "passed": passed,
                "source_timeframe": "5m/15m/30m",
                "source_function": source,
                "source_event": sorted(types),
                "source_level": None,
                "source_pivot": None,
                "labels_5m": f"{s5.last_high_label}/{s5.last_low_label}",
                "bias_5m": s5.current_structure_bias,
                "bias_15m": _htf_bias(s15),
                "bias_30m": _htf_bias(s30),
                "extra": extra,
                "extra2": extra2,
                "blocking_reason": None if passed else name,
            }
        )
    return rows


def load_prior_transition(ts: str) -> dict[str, Any]:
    tr = pd.read_csv(PRIOR / "state_transition_trace.csv")
    row = tr[tr["timestamp"] == ts]
    if row.empty:
        # try with Z vs +00:00
        tr["timestamp"] = pd.to_datetime(tr["timestamp"], utc=True)
        row = tr[tr["timestamp"] == _ts(ts)]
    if row.empty:
        raise KeyError(ts)
    r = row.iloc[0].to_dict()
    # parse embedded json-ish fields
    for k in (
        "transition_reason",
        "active_5m_events",
        "failed_break_event",
        "candle",
        "structure_5m",
        "early_strong_inputs",
    ):
        if k in r and isinstance(r[k], str):
            try:
                r[k] = json.loads(r[k].replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false"))
            except Exception:
                import ast

                try:
                    r[k] = ast.literal_eval(r[k])
                except Exception:
                    pass
    return r


def run_early_window_gate_audit() -> tuple[pd.DataFrame, dict[str, Any]]:
    """Causal replay with same machine; window-focused gate reconstruction."""
    # Reuse same short pad as Run A (proven identical to full in this window).
    start = _ts(DIAG_START) - pd.Timedelta(days=WARM_PAD_DAYS)
    end = _ts(WEAKENING_TS) + pd.Timedelta(minutes=5)
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[(raw["timestamp"] >= start) & (raw["timestamp"] < end)].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    pivots = find_confirmed_pivots(frame, config=scfg)
    cfg = default_trend_state_config()
    rt = TrendRuntime()

    gate_rows: list[dict[str, Any]] = []
    early_start = _ts(EARLY_START)
    weak_ts = _ts(WEAKENING_TS)

    for i, row in frame.iterrows():
        decision_ts = _ts(row["decision_time"])
        candles_as_of = frame.iloc[: int(i) + 1][
            [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
        ]
        state_before = rt.state
        rt, snap, events = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=candles_as_of,
            bar_index=int(i),
            cfg=cfg,
            scanner_cfg=scfg,
        )
        if early_start <= decision_ts <= weak_ts and state_before == "early_bearish":
            # evaluate gate as of post-update structure while still early OR at exit bar
            view_state = state_before if rt.state != "early_bearish" else rt.state
            # For exit bar, evaluate with age_before semantics: use current structures
            class V:
                pass

            v = V()
            v.state = "early_bearish"
            v.age_5m_bars = rt.age_5m_bars if rt.state == "early_bearish" else snap.age_5m_bars
            # At exit, age was reset — use snap previous age from hysteresis if available
            if rt.state != "early_bearish":
                # age just before enter was in transition; recover from reasons path: use hold already satisfied
                v.age_5m_bars = max(3, int(getattr(snap, "age_5m_bars", 0)))
            v.structure_5m = rt.structure_5m
            v.structure_15m = rt.structure_15m
            v.structure_30m = rt.structure_30m
            rows = strong_gate_rows(v, row.to_dict(), events, cfg, decision_ts)  # type: ignore[arg-type]
            # annotate candle
            for r in rows:
                r["candle_open"] = float(row["open"])
                r["candle_high"] = float(row["high"])
                r["candle_low"] = float(row["low"])
                r["candle_close"] = float(row["close"])
                r["final_state_after_bar"] = rt.state
                r["active_reasons"] = list(snap.active_reasons)
            gate_rows.extend(rows)
        if decision_ts > weak_ts:
            break

    df = pd.DataFrame(gate_rows)
    # summary: per timestamp which conditions fail
    summary = {}
    if not df.empty:
        for ts, g in df.groupby("timestamp"):
            fails = g.loc[~g["passed"], "condition_name"].tolist()
            summary[str(ts)] = {
                "failures": fails,
                "last_blocker": fails[-1] if fails else None,
                "all_passed": len(fails) == 0,
                "bias_5m": g.iloc[0]["bias_5m"],
                "bias_15m": g.iloc[0]["bias_15m"],
                "bias_30m": g.iloc[0]["bias_30m"],
                "labels_5m": g.iloc[0]["labels_5m"],
                "state_age": int(g.iloc[0]["state_age"]),
            }
    return df, summary


def build_report(gate_df: pd.DataFrame, gate_summary: dict[str, Any]) -> dict[str, Any]:
    tr = pd.read_csv(PRIOR / "state_transition_trace.csv")
    tr["timestamp"] = pd.to_datetime(tr["timestamp"], utc=True)
    events = pd.read_csv(PRIOR / "structure_event_timeline.csv")
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)
    tl = pd.read_csv(PRIOR / "state_timeline_diag_full.csv")
    tl["decision_time"] = pd.to_datetime(tl["decision_time"], utc=True)

    def trans(ts: str) -> dict[str, Any]:
        hit = tr[tr["timestamp"] == _ts(ts)]
        if hit.empty:
            raise KeyError(ts)
        r = hit.iloc[0]
        out = r.to_dict()
        for k in ("transition_reason", "active_5m_events", "failed_break_event", "candle", "structure_5m"):
            if isinstance(out.get(k), str):
                import ast

                out[k] = ast.literal_eval(out[k])
        return out

    t_early = trans(EARLY_START)
    t_weak = trans(WEAKENING_TS)
    t_bot = trans(BOTTOMING_TS)
    t_eb = trans(EARLY_BULL_TS)
    t_sb = trans(STRONG_BULL_TS)

    # Dominant blockers across early window
    blocker_counts: dict[str, int] = {}
    last_blockers: list[str] = []
    lh_ll_bars = 0
    for ts, info in sorted(gate_summary.items()):
        for f in info["failures"]:
            blocker_counts[f] = blocker_counts.get(f, 0) + 1
        if info["last_blocker"]:
            last_blockers.append(info["last_blocker"])
        if "has_lh_ll" not in info["failures"] and "bias_5m_bearish" not in info["failures"]:
            # structure side ok
            pass
        labels = str(info.get("labels_5m") or "")
        if labels.startswith("lower_high/lower_low") or info.get("bias_5m") == "bearish":
            if "has_lh_ll" not in info["failures"]:
                lh_ll_bars += 1

    # Find bars where only HTF blocked
    htf_only = []
    for ts, info in sorted(gate_summary.items()):
        fails = set(info["failures"])
        if fails and fails <= {"htf15_bearish_or_neutral_or_bos", "not_htf_bullish_veto", "retest_holds_or_bear_conf_ge_2", "min_hold_satisfied"}:
            if "has_lh_ll" not in fails and "bias_5m_bearish" not in fails:
                htf_only.append({"timestamp": ts, **info})

    # Earliest wrong input analysis
    # Primary: at 22:30, bearish_choch on micro HL 0.9938 classified via protective_low=last_higher_low
    # That enables early_bearish from topping without LH+LL (has_lh_ll false at entry).
    # Then failed_breakdown on micro HL 0.9926 at 00:30 exits early (permissive invalidation).
    # Then micro bullish_choch 1.0011 + failed_bd 0.9945 → bottoming.

    earliest = {
        "timestamp": EARLY_START,
        "timeframe": "5m",
        "candle_ohlc": t_early["candle"],
        "internal_field": "protective_low / bearish_choch.level",
        "actual_value": {
            "event": "bearish_choch",
            "level": 0.9938,
            "protective_low": t_early["protective_low"],
            "source_pivot": "2026-03-05T21:35:00+00:00",
            "selection": "last_higher_low",
            "has_lh_ll_after": t_early["structure_5m"]["has_lh_ll"],
            "bias_after": t_early["structure_5m"]["bias"],
            "labels_after": (
                t_early["structure_5m"]["last_high_label"],
                t_early["structure_5m"]["last_low_label"],
            ),
            "15m_bias": t_early["15m_structure_bias"],
            "30m_bias": t_early["30m_structure_bias"],
        },
        "expected_value": {
            "note": (
                "With only confirmed pivots as-of 22:30, breaking the most recent HL (0.9938) "
                "is a micro protective-low break. A trend-character change would require breaking "
                "a swing that defines the active bullish leg / a more significant HL, not merely "
                "last_higher_low. Entering early_bearish without LH+LL leaves Strong unreachable "
                "until structure catches up — while invalidation via micro failed_breakdown remains easy."
            ),
            "fachlich": "micro_HL_break_should_not_alone_open_early_bearish_path_from_topping_as_trend_commitment",
        },
        "source_level": 0.9938,
        "source_pivot": "2026-03-05T21:35:00+00:00 @ 0.9938",
        "source_event": "bearish_choch",
        "code_function": "trend_structure._protective_low + _detect_bos_choch → topping→early_bearish via lh_or_bos",
    }

    # Counterfactuals (analytic)
    # CF1: if strong_bearish had been active at 00:30
    # strong→weakening needs failed_breakdown|bullish_choch|bearish_retest_fails|higher_low OR no_ll lookback
    # At 00:30 events include failed_breakdown — so strong WOULD still allow weakening on same bar
    # (and min_hold strong=4; if just entered strong earlier might block — depends)
    cf1 = {
        "assumption": "strong_bearish active before 00:30",
        "would_weakening_still_fire_at_0030": True,
        "reason": (
            "strong_bearish→bearish_weakening also accepts failed_breakdown in types "
            "(trend_state_machine._propose_transition strong_bearish branch). "
            "Same failed_breakdown@0.9926 would still trigger weakening if min_hold_strong satisfied."
        ),
        "would_bottoming_still_be_reachable": True,
        "note": "Therefore missing strong alone does not prevent 00:30/01:35 path; invalidation/bottoming rules are independent.",
    }
    cf2 = {
        "assumption": "failed_breakdown@00:30 not active / not accepted for early invalidation",
        "would_early_remain": True,
        "would_strong_later": "possible_if_lh_ll+htf_align_before_other_invalidation",
        "would_bottoming_prevented_at_0135": (
            "bottoming requires state bearish_weakening first; without weakening, "
            "bottoming rule not evaluated — so 01:35 bottoming would not occur from this path"
        ),
    }
    cf3 = {
        "assumption": "protective_low used a more significant HL than last_higher_low 0.9938 at 22:30",
        "effect_on_22_30": "bearish_choch on 0.9938 might not fire → early_bearish may not start at 22:30",
        "effect_on_01_35": "bullish_choch used protective_high=last_lower_high 1.0011; different protective would change CHoCH",
        "bullish_07_mar": "path depends on being in bottoming; if early path never entered bottoming, 07.03 early_bullish from bottoming would not occur",
    }
    cf4 = {
        "assumption": "sticky last_failed_breakdown invalidated on first clear new LL continuation",
        "first_different_transition": (
            "Not primary at 00:30 — trigger is same-bar new failed_breakdown, not a stale sticky slot. "
            "Sticky last_bos/choch display fields are secondary; transition uses current-bar event types."
        ),
        "impact": "low_for_this_path",
    }

    # Classification
    classifications = [
        {
            "finding": "protective_level_selection_micro_last_hl_lh",
            "classification": "primary_root_cause",
            "reason": "last_higher_low/last_lower_high chosen as break levels enabling micro CHoCH at 22:30 and 01:35",
            "would_exist_without_previous_error": True,
            "confidence": "high",
        },
        {
            "finding": "early_bearish_entry_on_micro_choch_without_lh_ll",
            "classification": "primary_root_cause",
            "reason": "topping→early_bearish on bearish_choch@0.9938 while has_lh_ll=false; opens invalidation-sensitive path",
            "would_exist_without_previous_error": True,
            "confidence": "high",
        },
        {
            "finding": "missing_strong_bearish",
            "classification": "downstream_consequence",
            "reason": "AND-gate never clears: often no lh_ll/bias_bearish; when structure briefly ok, HTF bullish veto; then early exits at 00:30",
            "would_exist_without_previous_error": True,
            "confidence": "high",
        },
        {
            "finding": "early_bearish_weakening",
            "classification": "secondary_independent_error",
            "reason": "failed_breakdown alone invalidates early; fires on micro HL reclaim 0.9926 — independent of whether strong was reached (CF1)",
            "would_exist_without_previous_error": True,
            "confidence": "high",
        },
        {
            "finding": "early_bottoming",
            "classification": "downstream_consequence",
            "reason": "requires bearish_weakening state; 2-hit micro choch+failed_bd; enabled by prior weakening",
            "would_exist_without_previous_error": False,
            "confidence": "high",
        },
        {
            "finding": "early_bullish",
            "classification": "downstream_consequence",
            "reason": "bottoming→early_bullish path only reachable because bottoming was active",
            "would_exist_without_previous_error": False,
            "confidence": "high",
        },
        {
            "finding": "strong_bullish",
            "classification": "downstream_consequence",
            "reason": "follows early_bullish after bottoming path",
            "would_exist_without_previous_error": False,
            "confidence": "high",
        },
        {
            "finding": "bos_choch_classification",
            "classification": "primary_root_cause",
            "reason": "CHoCH defined as close-cross of last micro protective swing under last-pair bias",
            "would_exist_without_previous_error": True,
            "confidence": "high",
        },
        {
            "finding": "sticky_event_behavior",
            "classification": "correct_behavior",
            "reason": "00:30/01:35 triggers are same-bar events, not multi-hour sticky reuse",
            "would_exist_without_previous_error": True,
            "confidence": "high",
        },
        {
            "finding": "15m_confirmation",
            "classification": "secondary_independent_error",
            "reason": "blocks strong while early (bullish HTF veto) but does not veto bottoming at 01:35 despite 15m bearish — asymmetric",
            "would_exist_without_previous_error": True,
            "confidence": "medium",
        },
        {
            "finding": "30m_context",
            "classification": "correct_behavior",
            "reason": "participates in strong bullish veto with 15m; not the earliest false input",
            "would_exist_without_previous_error": True,
            "confidence": "medium",
        },
        {
            "finding": "structure_bias",
            "classification": "secondary_independent_error",
            "reason": "last-pair bias flips to bullish HH+HL by 00:30, remapping later breaks; follows micro swings",
            "would_exist_without_previous_error": True,
            "confidence": "medium",
        },
    ]

    # Decision letter among A-J
    # Earliest wrong is protective micro level + CHoCH on it at 22:30 — combines A and B.
    # User asks for ONE. The earliest false INPUT is protective level selection enabling false CHoCH.
    decision = "A"
    decision_text = (
        "A: Hauptursache ist eine falsche Protective-Level-Auswahl "
        "(last_higher_low/last_lower_high als Break-Level → Mikro-CHoCH)."
    )

    # If we must pick one: protective level is earliest wrong *input*;
    # early invalidation (C) and bottoming (D) are later.
    # Independent secondary: C (weakening rule) would still fire even from strong (CF1).

    final = {
        "warmup_cause": False,
        "decision_warmup": "C",
        "decision": decision,
        "decision_text": decision_text,
        "primary_root_cause": {
            "category": "protective_level_selection",
            "timestamp": EARLY_START,
            "timeframe": "5m",
            "source_file": "research/regime_scanner/trend_structure.py",
            "source_function": "_protective_low / _detect_bos_choch",
            "condition": (
                "protective_low = last_higher_low if set else last_confirmed_swing_low; "
                "close cross below → bearish_choch when bias in {bullish,neutral,unknown}"
            ),
            "actual_value": {
                "protective_low": 0.9938,
                "event": "bearish_choch",
                "pivot_timestamp": "2026-03-05T21:35:00+00:00",
                "candle": t_early["candle"],
            },
            "fachlich_expected_value": (
                "Break level for character change should be a trend-defining swing, "
                "not necessarily the most recent confirmed HL"
            ),
            "source_level": 0.9938,
            "source_pivot_timestamp": "2026-03-05T21:35:00+00:00",
            "source_event": "bearish_choch",
            "evidence": [
                "transition topping→early_bearish reasons lh_or_bos",
                "has_lh_ll false after entry",
                "15m/30m still bullish at entry",
                "identical under full warmup (not warmup artifact)",
            ],
        },
        "secondary_independent_errors": [
            {
                "category": "early_invalidation_failed_breakdown_alone",
                "timestamp": WEAKENING_TS,
                "condition": "types & {failed_breakdown, bearish_retest_fails}",
                "level": 0.9926,
                "note": "Would still exit strong_bearish via same event (CF1)",
            },
            {
                "category": "asymmetric_15m_usage",
                "note": "15m vetoes strong_bearish but does not veto bottoming while 15m bearish",
            },
        ],
        "downstream_consequences": [
            "missing_strong_bearish",
            "early_bottoming@01:35",
            "early_bullish@07.03 03:05",
            "strong_bullish@07.03 03:35",
        ],
        "missing_strong_bearish_reason": {
            "window": f"{EARLY_START} .. {WEAKENING_TS}",
            "blocker_counts": blocker_counts,
            "htf_only_structure_ok_bars": htf_only[:5],
            "narrative": (
                "AND-gate rarely satisfied; when 5m LH+LL/bias briefly align, "
                "15m/30m bullish veto blocks; episode ends via failed_breakdown@00:30 "
                "before sustained strong entry."
            ),
        },
        "early_weakening_reason": {
            "timestamp": WEAKENING_TS,
            "candle": t_weak["candle"],
            "event": "failed_breakdown",
            "level": 0.9926,
            "pivot": "2026-03-05T23:55:00+00:00",
            "code": "early_bearish: types & {failed_breakdown,...} → bearish_weakening",
            "bias_5m_after": t_weak["5m_structure_bias"],
            "structure": t_weak["structure_5m"],
        },
        "early_bottoming_reason": {
            "timestamp": BOTTOMING_TS,
            "candle": t_bot["candle"],
            "events": ["bullish_choch@1.0011", "failed_breakdown@0.9945"],
            "code": "len(bottom_hits)>=2 with hits from {failed_breakdown,bullish_choch,...}",
            "15m_bias": t_bot["15m_structure_bias"],
            "prior_state_required": "bearish_weakening",
        },
        "bullish_state_reason": {
            "early_bullish": {
                "timestamp": EARLY_BULL_TS,
                "from": "bottoming",
                "trigger": t_eb["transition_reason"],
                "broken_level": t_eb.get("broken_level"),
                "classification": "downstream_consequence",
            },
            "strong_bullish": {
                "timestamp": STRONG_BULL_TS,
                "from": "early_bullish",
                "trigger": t_sb["transition_reason"],
                "classification": "downstream_consequence",
            },
        },
        "confidence": "high",
        "counterfactuals": {"cf1": cf1, "cf2": cf2, "cf3": cf3, "cf4": cf4},
        "classifications": classifications,
        "earliest_wrong_input": earliest,
        "gate_summary_size": len(gate_summary),
    }
    return final, {
        "t_early": t_early,
        "t_weak": t_weak,
        "t_bot": t_bot,
        "t_eb": t_eb,
        "t_sb": t_sb,
        "gate_summary": gate_summary,
        "blocker_counts": blocker_counts,
    }


def write_artifacts(
    out: Path,
    gate_df: pd.DataFrame,
    gate_summary: dict[str, Any],
    final: dict[str, Any],
    bundle: dict[str, Any],
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    gate_df.to_csv(out / "strong_bearish_blockers.csv", index=False)

    t_weak = bundle["t_weak"]
    t_bot = bundle["t_bot"]
    t_eb = bundle["t_eb"]
    t_sb = bundle["t_sb"]
    t_early = bundle["t_early"]

    pd.DataFrame(
        [
            {
                "timestamp": WEAKENING_TS,
                "previous_state": "early_bearish",
                "final_state": "bearish_weakening",
                "trigger_event": "failed_breakdown",
                "event_level": 0.9926,
                "source_pivot_timestamp": "2026-03-05T23:55:00+00:00",
                "source_pivot_price": 0.9926,
                "protective_low": t_weak.get("protective_low"),
                "event_created_at": WEAKENING_TS,
                "event_age_candles": 0,
                "bias_5m": t_weak["5m_structure_bias"],
                "bias_15m": t_weak["15m_structure_bias"],
                "bias_30m": t_weak["30m_structure_bias"],
                "code_condition": "early_bearish and (failed_breakdown in types)",
                "candle": json.dumps(t_weak["candle"]),
                "labels": f"{t_weak['structure_5m']['last_high_label']}/{t_weak['structure_5m']['last_low_label']}",
                "has_hh_hl": t_weak["structure_5m"]["has_hh_hl"],
                "has_lh_ll": t_weak["structure_5m"]["has_lh_ll"],
            }
        ]
    ).to_csv(out / "weakening_trigger_trace.csv", index=False)

    pd.DataFrame(
        [
            {
                "timestamp": BOTTOMING_TS,
                "previous_state": "bearish_weakening",
                "final_state": "bottoming",
                "trigger_events": "bullish_choch+failed_breakdown",
                "bullish_choch_level": 1.0011,
                "bullish_choch_pivot": "2026-03-06T00:35:00+00:00",
                "failed_breakdown_level": 0.9945,
                "failed_breakdown_pivot": "2026-03-06T00:55:00+00:00",
                "protective_high": t_bot.get("protective_high"),
                "event_age_candles": 0,
                "bias_5m": t_bot["5m_structure_bias"],
                "bias_15m": t_bot["15m_structure_bias"],
                "bias_30m": t_bot["30m_structure_bias"],
                "code_condition": "len({failed_breakdown,bullish_choch,higher_low,bullish_bos}∩types)>=2",
                "candle": json.dumps(t_bot["candle"]),
                "confirmed_higher_low_label": t_bot["structure_5m"]["last_low_label"] == "higher_low",
                "has_lh_ll": t_bot["structure_5m"]["has_lh_ll"],
            }
        ]
    ).to_csv(out / "bottoming_trigger_trace.csv", index=False)

    pd.DataFrame(
        [
            {
                "timestamp": EARLY_BULL_TS,
                "state": "early_bullish",
                "from_state": "bottoming",
                "trigger": json.dumps(t_eb["transition_reason"]),
                "broken_level": t_eb.get("broken_level"),
                "protective_high": t_eb.get("protective_high"),
                "bias_5m": t_eb["5m_structure_bias"],
                "bias_15m": t_eb["15m_structure_bias"],
                "classification": "downstream_consequence",
                "depends_on_bottoming": True,
                "candle": json.dumps(t_eb["candle"]),
            },
            {
                "timestamp": STRONG_BULL_TS,
                "state": "strong_bullish",
                "from_state": "early_bullish",
                "trigger": json.dumps(t_sb["transition_reason"]),
                "broken_level": t_sb.get("broken_level"),
                "protective_high": t_sb.get("protective_high"),
                "bias_5m": t_sb["5m_structure_bias"],
                "bias_15m": t_sb["15m_structure_bias"],
                "classification": "downstream_consequence",
                "depends_on_bottoming": True,
                "candle": json.dumps(t_sb["candle"]),
            },
        ]
    ).to_csv(out / "bullish_transition_trace.csv", index=False)

    pd.DataFrame(
        [
            {
                "final_wrong_state": "early_bearish",
                "transition_timestamp": EARLY_START,
                "direct_trigger": "bearish_choch",
                "trigger_created_at": EARLY_START,
                "trigger_source_function": "_detect_bos_choch",
                "source_event": "bearish_choch",
                "source_level": 0.9938,
                "source_pivot": "2026-03-05T21:35:00+00:00",
                "previous_dependency": "_protective_low=last_higher_low",
                "earliest_wrong_dependency": "protective_level_selection",
            },
            {
                "final_wrong_state": "bearish_weakening",
                "transition_timestamp": WEAKENING_TS,
                "direct_trigger": "failed_breakdown",
                "trigger_created_at": WEAKENING_TS,
                "trigger_source_function": "_detect_failed_breaks / early_invalidation",
                "source_event": "failed_breakdown",
                "source_level": 0.9926,
                "source_pivot": "2026-03-05T23:55:00+00:00",
                "previous_dependency": "state==early_bearish (opened by micro choch)",
                "earliest_wrong_dependency": "protective_level_selection + early_invalidation_rule",
            },
            {
                "final_wrong_state": "bottoming",
                "transition_timestamp": BOTTOMING_TS,
                "direct_trigger": "bullish_choch+failed_breakdown",
                "trigger_created_at": BOTTOMING_TS,
                "trigger_source_function": "_detect_bos_choch + bottoming 2-hit rule",
                "source_event": "bullish_choch",
                "source_level": 1.0011,
                "source_pivot": "2026-03-06T00:35:00+00:00",
                "previous_dependency": "state==bearish_weakening",
                "earliest_wrong_dependency": "protective_high=last_lower_high + prior weakening",
            },
            {
                "final_wrong_state": "early_bullish",
                "transition_timestamp": EARLY_BULL_TS,
                "direct_trigger": json.dumps(t_eb["transition_reason"]),
                "trigger_created_at": EARLY_BULL_TS,
                "trigger_source_function": "bottoming→early_bullish branch",
                "source_event": "bullish_choch",
                "source_level": t_eb.get("broken_level"),
                "source_pivot": None,
                "previous_dependency": "state==bottoming",
                "earliest_wrong_dependency": "bottoming path from micro protective breaks",
            },
            {
                "final_wrong_state": "strong_bullish",
                "transition_timestamp": STRONG_BULL_TS,
                "direct_trigger": json.dumps(t_sb["transition_reason"]),
                "trigger_created_at": STRONG_BULL_TS,
                "trigger_source_function": "early_bullish→strong_bullish",
                "source_event": "hh_hl",
                "source_level": None,
                "source_pivot": None,
                "previous_dependency": "state==early_bullish",
                "earliest_wrong_dependency": "downstream of bottoming path",
            },
        ]
    ).to_csv(out / "causal_dependency_chain.csv", index=False)

    pd.DataFrame(
        [
            {
                "timestamp": EARLY_START,
                "timeframe": "5m",
                "internal_value": "protective_low / choch level",
                "actual_system_value": 0.9938,
                "fachlich_expected_value": "trend-defining HL, not merely last confirmed HL",
                "evidence": "bearish_choch on last_higher_low; has_lh_ll=false; HTF bullish",
                "downstream_effect": "topping→early_bearish without strong-capable structure",
            },
            {
                "timestamp": WEAKENING_TS,
                "timeframe": "5m",
                "internal_value": "early_invalidation via failed_breakdown",
                "actual_system_value": "failed_breakdown@0.9926 accepted alone",
                "fachlich_expected_value": "micro HL reclaim should not exit early bearish commitment alone while HTF not confirming reverse",
                "evidence": json.dumps(t_weak["candle"]),
                "downstream_effect": "early_bearish→bearish_weakening",
            },
            {
                "timestamp": BOTTOMING_TS,
                "timeframe": "5m",
                "internal_value": "bottom_hits>=2 on micro LH break",
                "actual_system_value": "bullish_choch@1.0011 + failed_bd@0.9945",
                "fachlich_expected_value": "not a structural bottom vs intact larger swing context; 15m already bearish unused as veto",
                "evidence": json.dumps(t_bot["candle"]),
                "downstream_effect": "bearish_weakening→bottoming; locks selloff window",
            },
        ]
    ).to_csv(out / "earliest_wrong_input.csv", index=False)

    (out / "independent_vs_downstream.json").write_text(
        json.dumps(json_safe(final["classifications"]), indent=2), encoding="utf-8"
    )
    (out / "counterfactual_analysis.json").write_text(
        json.dumps(json_safe(final["counterfactuals"]), indent=2), encoding="utf-8"
    )
    (out / "final_root_cause.json").write_text(
        json.dumps(json_safe(final), indent=2), encoding="utf-8"
    )
    (out / "code_field_map.json").write_text(
        json.dumps(
            [
                {
                    "artifact_field": "protective_high/low",
                    "source_file": "trend_structure.py",
                    "source_function": "_protective_high/_protective_low",
                    "code_condition": "last_lower_high / last_higher_low else last swing",
                    "meaning": "Break reference for BOS/CHoCH",
                },
                {
                    "artifact_field": "bearish_choch/bullish_choch",
                    "source_file": "trend_structure.py",
                    "source_function": "_detect_bos_choch",
                    "code_condition": "prior_close on side of level and close crosses; bias maps choch vs bos",
                    "meaning": "Character vs continuation break",
                },
                {
                    "artifact_field": "failed_breakdown",
                    "source_file": "trend_structure.py",
                    "source_function": "_detect_failed_breaks",
                    "code_condition": "probe beyond last swing low then close back inside within failed_return_max_bars",
                    "meaning": "Micro reclaim event",
                },
                {
                    "artifact_field": "early→strong gate",
                    "source_file": "trend_state_machine.py",
                    "source_function": "_propose_transition early_bearish",
                    "code_condition": "has_lh_ll & bias bearish & htf15 ok & not veto & (retest|conf>=2)",
                    "meaning": "Promotion to strong_bearish",
                },
                {
                    "artifact_field": "early→weakening",
                    "source_file": "trend_state_machine.py",
                    "source_function": "_propose_transition early_bearish",
                    "code_condition": "failed_breakdown|bearish_retest_fails|(bullish_choch&higher_low)",
                    "meaning": "Early invalidation",
                },
                {
                    "artifact_field": "weakening→bottoming",
                    "source_file": "trend_state_machine.py",
                    "source_function": "_propose_transition bearish_weakening",
                    "code_condition": "len(bottom_hits)>=2",
                    "meaning": "Enter bottoming",
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    readme = [
        "# Logic Root Cause (post-warmup exclusion)",
        "",
        final["decision_text"],
        "",
        f"Primary timestamp: `{EARLY_START}` protective_low/micro bearish_choch @ 0.9938",
        "",
        "## Reproduce",
        "",
        "```bash",
        "PYTHONPATH=. python3 -m research.regime_scanner.trend_state_logic_root_cause_audit",
        "```",
        "",
    ]
    (out / "README.md").write_text("\n".join(readme), encoding="utf-8")


def checksum_dir(out: Path) -> dict[str, str]:
    skip = {"checksums.json", "checksums_pass1.json", "checksums_pass2.json"}
    out_map = {}
    for f in sorted(out.iterdir()):
        if f.is_file() and f.name not in skip:
            out_map[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out_map


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default=str(OUT))
    args = p.parse_args(argv)
    out = Path(args.out_dir)

    print("Reconstructing early→strong gate (22:30–00:30)...", flush=True)
    gate_df, gate_summary = run_early_window_gate_audit()
    final, bundle = build_report(gate_df, gate_summary)
    write_artifacts(out, gate_df, gate_summary, final, bundle)
    c1 = checksum_dir(out)
    (out / "checksums_pass1.json").write_text(json.dumps(c1, indent=2), encoding="utf-8")
    # second pass determinism: rebuild without re-running heavy gate if file exists — re-run write from same data
    write_artifacts(out, gate_df, gate_summary, final, bundle)
    c2 = checksum_dir(out)
    (out / "checksums_pass2.json").write_text(json.dumps(c2, indent=2), encoding="utf-8")
    assert c1 == c2, "non-deterministic artifact write"
    print(json.dumps(json_safe({"decision": final["decision"], "primary": final["primary_root_cause"]["category"], "ts": final["primary_root_cause"]["timestamp"]}), indent=2))
    print("blocker_counts", final["missing_strong_bearish_reason"]["blocker_counts"])
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

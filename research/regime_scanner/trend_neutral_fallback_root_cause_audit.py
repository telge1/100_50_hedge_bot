"""Phase C2B2A: read-only diagnostic for a possible topping/bottoming → neutral fallback.

Single replay under recommended C2B1 research stack:
  weakening_multi_bar_mode=strict
  turning_multi_bar_mode=strict
  turning_evidence_window_bars=24

Does **not** implement neutral transitions, change policy, or modify the SM.
Exports only compact summary (+ README). No large candle timelines.

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_neutral_fallback_root_cause_audit \\
    --output-dir research/regime_scanner/results_trend_neutral_fallback_phase_c2b2a
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_robustness_audit import (
    ANALYZE_END,
    ANALYZE_START,
    LOAD_END,
    LOAD_START,
    ground_truth_label,
    install_htf_cache,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _can_leave,
    _event_types,
    _htf_bias,
    _indicator_confirms,
    default_trend_state_config,
    step_trend_state,
    trend_state_config_c2b,
)
from research.regime_scanner.trend_structure import has_hh_hl, has_lh_ll

DEFAULT_OUT = Path("research/regime_scanner/results_trend_neutral_fallback_phase_c2b2a")
FORBIDDEN = (
    Path("research/regime_scanner/results"),
    Path("research/regime_scanner/results_trend_robustness_phase_b"),
    Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0"),
    Path("research/regime_scanner/results_trend_weakening_multi_bar_phase_c1"),
    Path("research/regime_scanner/results_trend_topping_bottoming_phase_c2a"),
    Path("research/regime_scanner/results_trend_topping_bottoming_multibar_phase_c2b1"),
)

AGE_CANDIDATES = (24, 48, 96)
LONG_MIN = 96
SHORT_EARLY_MAX = 5  # duration < 6
MARCH06 = ("2026-03-06T00:00:00+00:00", "2026-03-07T00:00:00+00:00")
MARCH0809 = ("2026-03-08T00:00:00+00:00", "2026-03-10T00:00:00+00:00")


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    return None if v is None else _ts(v).isoformat()


def assert_safe_output_dir(path: Path) -> None:
    resolved = path.resolve()
    for f in FORBIDDEN:
        if resolved == f.resolve():
            raise ValueError(f"refusing forbidden path: {f}")


def recommended_c2b_stack_config():
    cfg = trend_state_config_c2b("strict", weakening_mode="strict", turning_window_bars=24)
    assert cfg.weakening_multi_bar_mode == "strict"
    assert cfg.turning_multi_bar_mode == "strict"
    assert cfg.turning_evidence_window_bars == 24
    assert default_trend_state_config().turning_multi_bar_mode == "off"
    return cfg


def _diag_bar(
    rt: TrendRuntime,
    *,
    types: set[str],
    row: dict[str, Any],
    cfg,
) -> dict[str, Any]:
    """Per-bar structural/impulse/HTF snapshot while in topping|bottoming (audit-only)."""
    s5 = rt.structure_5m
    state = rt.state
    if state == "topping":
        swing_ok = "lower_high" in types or s5.last_high_label == "lower_high"
        hard_same = "bearish_bos" in types or "bearish_choch" in types
        hard_evid = bool(set(rt.turning_evidence_keys) & {"bearish_bos", "bearish_choch"})
        cont = "higher_high" in types or (
            "bullish_bos" in types and ("higher_low" in types or "higher_high" in types)
        )
        bear_conf, _ = _indicator_confirms(row, side="bearish", cfg=cfg)
        impulse = rt.consecutive_bearish_closes >= int(cfg.bearish_impulse_min_closes) or bear_conf >= 2
        htf = _htf_bias(rt.structure_15m)
        htf_neutralish = htf in {"neutral", "unknown", "bullish"}  # not confirming bearish turn
    else:
        swing_ok = (
            "higher_low" in types or s5.last_low_label == "higher_low" or has_hh_hl(s5)
        )
        hard_same = "bullish_bos" in types or "bullish_choch" in types
        hard_evid = bool(set(rt.turning_evidence_keys) & {"bullish_bos", "bullish_choch"})
        cont = "lower_low" in types or (
            "bearish_bos" in types and ("lower_high" in types or "lower_low" in types)
        )
        bull_conf, _ = _indicator_confirms(row, side="bullish", cfg=cfg)
        impulse = rt.consecutive_bullish_closes >= int(cfg.bullish_impulse_min_closes) or bull_conf >= 2
        htf = _htf_bias(rt.structure_15m)
        htf_neutralish = htf in {"neutral", "unknown", "bearish"}

    hard_any = hard_same or hard_evid
    # Neutral-candidate gate (diagnostic): no hard BOS/CHoCH evidence, no impulse, HTF not confirming turn
    gate_ok = (not hard_any) and (not impulse) and htf in {"neutral", "unknown"}
    return {
        "swing_ok": swing_ok,
        "hard_same": hard_same,
        "hard_evid": hard_evid,
        "hard_any": hard_any,
        "impulse": impulse,
        "continuation": cont,
        "htf_15m": htf,
        "htf_neutralish": htf_neutralish,
        "gate_ok": gate_ok,
        "min_hold_blocks": not _can_leave(rt, cfg),
        "evidence_cats": ",".join(sorted(rt.turning_evidence_keys.keys())),
    }


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = recommended_c2b_stack_config()
    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    a0, a1 = _ts(analyze_start), _ts(analyze_end)
    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)

    scfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    closes = frame["close"].to_numpy(dtype=float)

    long_runs: list[dict[str, Any]] = []
    short_early: list[dict[str, Any]] = []
    open_tb: dict[str, Any] | None = None
    open_early: dict[str, Any] | None = None

    # Threshold eligibility counters (bars in TB with age>=T and gate_ok)
    thresh_eligible_bars = {t: 0 for t in AGE_CANDIDATES}
    thresh_eligible_run_hits = {t: 0 for t in AGE_CANDIDATES}  # long runs that ever hit gate at age>=T

    case_notes: dict[str, Any] = {
        "mar06": {"long_tb_bars": 0, "gate_bars": 0, "short_early": 0, "states": Counter()},
        "mar0809": {"long_tb_bars": 0, "gate_bars": 0, "short_early": 0, "states": Counter()},
        "2026-04": {"long_tb_bars": 0, "gate_bars": 0, "short_early": 0},
        "2026-05": {"long_tb_bars": 0, "gate_bars": 0, "short_early": 0},
    }

    m6a, m6b = _ts(MARCH06[0]), _ts(MARCH06[1])
    m8a, m8b = _ts(MARCH0809[0]), _ts(MARCH0809[1])

    dangerous_examples: list[dict[str, Any]] = []

    n = len(frame)
    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        prev = rt.state
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
        if not (a0 <= decision_ts <= a1):
            continue

        types = {
            str(e.get("event_type"))
            for e in (snap.active_structure_events or [])
            if isinstance(e, dict) and e.get("event_type")
        } or _event_types(events)
        row_d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        ym = f"{decision_ts.year:04d}-{decision_ts.month:02d}"

        # GT at bar
        c0 = float(closes[i])
        j48 = max(0, i - 48)
        j288 = max(0, i - 288)
        net48 = 100.0 * (c0 / float(closes[j48]) - 1.0) if closes[j48] else 0.0
        net288 = 100.0 * (c0 / float(closes[j288]) - 1.0) if closes[j288] else 0.0
        adx = float(row["adx"]) if "adx" in frame.columns and pd.notna(row["adx"]) else 0.0
        di = float(row["di_spread"]) if "di_spread" in frame.columns and pd.notna(row["di_spread"]) else 0.0
        gt = ground_truth_label(
            has_hh_hl_flag=has_hh_hl(rt.structure_5m),
            has_lh_ll_flag=has_lh_ll(rt.structure_5m),
            net_48=net48,
            net_288=net288,
            adx=adx,
            di_spread=di,
        )

        # --- early short runs ---
        if snap.current_state != prev:
            if open_early is not None and open_early["state"] == prev:
                open_early["end"] = _iso(decision_ts)
                open_early["exit_to"] = snap.current_state
                open_early["length_bars"] = int(open_early["length_bars"])
                if open_early["length_bars"] < 6:
                    short_early.append(open_early)
                    if m6a <= decision_ts < m6b:
                        case_notes["mar06"]["short_early"] += 1
                    if m8a <= decision_ts < m8b:
                        case_notes["mar0809"]["short_early"] += 1
                open_early = None
            if snap.current_state in {"early_bearish", "early_bullish"}:
                open_early = {
                    "state": snap.current_state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "|".join(snap.active_reasons),
                    "gt_at_entry": gt,
                    "gt_sideways_or_ambiguous": gt in {"CLEAR_SIDEWAYS", "AMBIGUOUS"},
                }
            # close TB run
            if open_tb is not None and open_tb["state"] == prev:
                open_tb["end"] = _iso(decision_ts)
                open_tb["exit_to"] = snap.current_state
                open_tb["exit_reasons"] = "|".join(snap.active_reasons)
                open_tb["length_bars"] = int(open_tb["length_bars"])
                if open_tb["length_bars"] >= LONG_MIN:
                    long_runs.append(open_tb)
                open_tb = None
            if snap.current_state in {"topping", "bottoming"}:
                open_tb = {
                    "state": snap.current_state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "year_month": ym,
                    "prev_state": prev,
                    "entry_reasons": "|".join(snap.active_reasons),
                    "gt_hist": Counter({gt: 1}),
                    "bars_no_cont_no_hard": 0,
                    "bars_gate_ok": 0,
                    "first_gate_age": None,
                    "max_streak_no_progress": 0,
                    "_streak": 0,
                    "block_profile": Counter(),
                    "hit_age_gate": {str(t): False for t in AGE_CANDIDATES},
                }
        else:
            if snap.current_state in {"early_bearish", "early_bullish"}:
                if open_early is None:
                    open_early = {
                        "state": snap.current_state,
                        "start": _iso(decision_ts),
                        "length_bars": 1,
                        "year_month": ym,
                        "prev_state": prev,
                        "entry_reasons": "window_start",
                        "gt_at_entry": gt,
                        "gt_sideways_or_ambiguous": gt in {"CLEAR_SIDEWAYS", "AMBIGUOUS"},
                    }
                else:
                    open_early["length_bars"] = int(open_early["length_bars"]) + 1

            if snap.current_state in {"topping", "bottoming"}:
                if open_tb is None:
                    open_tb = {
                        "state": snap.current_state,
                        "start": _iso(decision_ts),
                        "length_bars": 1,
                        "year_month": ym,
                        "prev_state": prev,
                        "entry_reasons": "window_start",
                        "gt_hist": Counter({gt: 1}),
                        "bars_no_cont_no_hard": 0,
                        "bars_gate_ok": 0,
                        "first_gate_age": None,
                        "max_streak_no_progress": 0,
                        "_streak": 0,
                        "block_profile": Counter(),
                        "hit_age_gate": {str(t): False for t in AGE_CANDIDATES},
                    }
                else:
                    open_tb["length_bars"] = int(open_tb["length_bars"]) + 1
                    open_tb["gt_hist"][gt] += 1

                diag = _diag_bar(rt, types=types, row=row_d, cfg=cfg)
                age = int(rt.age_5m_bars)
                no_progress = (not diag["continuation"]) and (not diag["hard_any"])
                if no_progress:
                    open_tb["bars_no_cont_no_hard"] += 1
                    open_tb["_streak"] += 1
                    open_tb["max_streak_no_progress"] = max(
                        int(open_tb["max_streak_no_progress"]), int(open_tb["_streak"])
                    )
                else:
                    open_tb["_streak"] = 0

                if not diag["hard_any"]:
                    open_tb["block_profile"]["no_bos_choch"] += 1
                if not diag["impulse"]:
                    open_tb["block_profile"]["no_impulse"] += 1
                if diag["htf_15m"] not in {"bearish", "bullish"} or (
                    (snap.current_state == "topping" and diag["htf_15m"] != "bearish")
                    or (snap.current_state == "bottoming" and diag["htf_15m"] != "bullish")
                ):
                    open_tb["block_profile"]["htf_not_confirming"] += 1

                if diag["gate_ok"]:
                    open_tb["bars_gate_ok"] += 1
                    if open_tb["first_gate_age"] is None:
                        open_tb["first_gate_age"] = age
                    for t in AGE_CANDIDATES:
                        if age >= t:
                            thresh_eligible_bars[t] += 1
                            open_tb["hit_age_gate"][str(t)] = True

                # case windows
                in_longish = int(open_tb["length_bars"]) >= LONG_MIN or age >= 48
                if m6a <= decision_ts < m6b:
                    case_notes["mar06"]["states"][snap.current_state] += 1
                    if in_longish:
                        case_notes["mar06"]["long_tb_bars"] += 1
                    if diag["gate_ok"] and age >= 48:
                        case_notes["mar06"]["gate_bars"] += 1
                if m8a <= decision_ts < m8b:
                    case_notes["mar0809"]["states"][snap.current_state] += 1
                    if in_longish:
                        case_notes["mar0809"]["long_tb_bars"] += 1
                    if diag["gate_ok"] and age >= 48:
                        case_notes["mar0809"]["gate_bars"] += 1
                if ym in {"2026-04", "2026-05"}:
                    if in_longish:
                        case_notes[ym]["long_tb_bars"] += 1
                    if diag["gate_ok"] and age >= 48:
                        case_notes[ym]["gate_bars"] += 1

                # dangerous: gate would fire while CLEAR up/down aligned with old trend
                if diag["gate_ok"] and age >= 48:
                    if snap.current_state == "topping" and gt == "CLEAR_DOWNTREND":
                        # neutral would abort a real down move after topping — maybe OK actually
                        pass
                    if snap.current_state == "topping" and gt == "CLEAR_UPTREND":
                        dangerous_examples.append(
                            {
                                "why": "neutral_during_clear_uptrend_while_topping",
                                "decision_time": _iso(decision_ts),
                                "age": age,
                                "gt": gt,
                                "state": snap.current_state,
                            }
                        )
                    if snap.current_state == "bottoming" and gt == "CLEAR_DOWNTREND":
                        dangerous_examples.append(
                            {
                                "why": "neutral_during_clear_downtrend_while_bottoming",
                                "decision_time": _iso(decision_ts),
                                "age": age,
                                "gt": gt,
                                "state": snap.current_state,
                            }
                        )
                    if gt == "CLEAR_UPTREND" and snap.current_state == "bottoming" and age < 24:
                        dangerous_examples.append(
                            {
                                "why": "early_neutral_in_nascent_bottoming_uptrend",
                                "decision_time": _iso(decision_ts),
                                "age": age,
                                "gt": gt,
                                "state": snap.current_state,
                            }
                        )

    if open_tb is not None:
        open_tb["end"] = None
        open_tb["exit_to"] = open_tb["state"]
        open_tb["exit_reasons"] = "open_at_end"
        if int(open_tb["length_bars"]) >= LONG_MIN:
            long_runs.append(open_tb)
    if open_early is not None:
        open_early["end"] = None
        open_early["exit_to"] = open_early["state"]
        if int(open_early["length_bars"]) < 6:
            short_early.append(open_early)

    # finalize long run summaries
    long_compact = []
    for r in long_runs:
        gt_hist = r.get("gt_hist") or Counter()
        if isinstance(gt_hist, Counter):
            gt_share = {k: v / max(1, sum(gt_hist.values())) for k, v in gt_hist.items()}
            dominant_gt = gt_hist.most_common(1)[0][0] if gt_hist else None
        else:
            gt_share, dominant_gt = {}, None
        bp = r.get("block_profile") or Counter()
        causes = []
        if bp.get("no_bos_choch", 0) >= int(r["length_bars"]) * 0.5:
            causes.append("missing_bos_choch_majority")
        if bp.get("no_impulse", 0) >= int(r["length_bars"]) * 0.5:
            causes.append("missing_impulse_majority")
        if bp.get("htf_not_confirming", 0) >= int(r["length_bars"]) * 0.5:
            causes.append("htf_not_confirming_majority")
        if int(r.get("bars_no_cont_no_hard", 0)) >= int(r["length_bars"]) * 0.4:
            causes.append("long_no_progress_stretch")
        if dominant_gt in {"CLEAR_SIDEWAYS", "AMBIGUOUS"}:
            causes.append(f"gt_mostly_{dominant_gt}")

        hit = r.get("hit_age_gate") or {}
        for t in AGE_CANDIDATES:
            if hit.get(str(t)):
                thresh_eligible_run_hits[t] += 1

        long_compact.append(
            {
                "state": r["state"],
                "start": r["start"],
                "end": r.get("end"),
                "length_bars": r["length_bars"],
                "year_month": r["year_month"],
                "prev_state": r["prev_state"],
                "exit_to": r.get("exit_to"),
                "dominant_gt": dominant_gt,
                "gt_share_sideways": float(gt_share.get("CLEAR_SIDEWAYS", 0.0)),
                "gt_share_ambiguous": float(gt_share.get("AMBIGUOUS", 0.0)),
                "bars_no_cont_no_hard": r.get("bars_no_cont_no_hard"),
                "bars_gate_ok": r.get("bars_gate_ok"),
                "first_gate_age": r.get("first_gate_age"),
                "max_streak_no_progress": r.get("max_streak_no_progress"),
                "causes": causes,
                "hit_age_gate": hit,
            }
        )

    short_in_chop = sum(1 for s in short_early if s.get("gt_sideways_or_ambiguous"))
    short_by_month = Counter(s["year_month"] for s in short_early)
    long_by_month = Counter(r["year_month"] for r in long_compact)

    # Recommend age: prefer 48 if enough long-run coverage without being as aggressive as 24
    coverage = {t: thresh_eligible_run_hits[t] / max(1, len(long_compact)) for t in AGE_CANDIDATES}
    if coverage.get(48, 0) >= 0.5 or (len(long_compact) and thresh_eligible_run_hits[48] >= max(1, len(long_compact) // 2)):
        recommended_age = 48
        age_reason = "age>=48 covers most remaining long runs while less aggressive than 24"
    elif coverage.get(96, 0) >= 0.4:
        recommended_age = 96
        age_reason = "only late ages reliably gate; 96 safer for trend continuance"
    elif coverage.get(24, 0) >= 0.5:
        recommended_age = 24
        age_reason = "age>=24 needed to catch remaining sticky runs; risk of early neutral"
    else:
        recommended_age = 48
        age_reason = "default middle candidate; limited gate coverage on remaining runs"

    proposal = {
        "config_flag": "turning_neutral_fallback_mode=off|on (default off)",
        "min_age_bars": recommended_age,
        "from_states": ["topping", "bottoming"],
        "to_state": "neutral",
        "gates_required": [
            "no fresh/same-bar/evidence BOS|CHoCH for turn direction",
            "impulse filter not satisfied (closes and indicator)",
            "15m bias in {neutral, unknown}",
            "min_hold already satisfied",
            "no continuation event this bar",
        ],
        "do_not": [
            "no March hardcode",
            "do not force neutral from early_*",
            "do not change policy in same PR",
        ],
    }

    # Serialize case notes counters
    cases_out = {}
    for k, v in case_notes.items():
        cases_out[k] = {
            **{kk: vv for kk, vv in v.items() if kk != "states"},
            "states": dict(v["states"]) if "states" in v else {},
        }

    summary = {
        "phase": "C2B2A_neutral_fallback_root_cause",
        "read_only": True,
        "config": {
            "weakening_multi_bar_mode": "strict",
            "turning_multi_bar_mode": "strict",
            "turning_evidence_window_bars": 24,
            "production_defaults_still_off": True,
        },
        "n_load_bars": int(len(frame)),
        "long_tb_runs_ge96": {
            "n": len(long_compact),
            "by_month": dict(long_by_month),
            "by_state": dict(Counter(r["state"] for r in long_compact)),
            "median_len": float(np.median([r["length_bars"] for r in long_compact])) if long_compact else 0.0,
            "max_len": int(max((r["length_bars"] for r in long_compact), default=0)),
            "share_dominant_sideways_or_ambiguous": (
                sum(
                    1
                    for r in long_compact
                    if r["dominant_gt"] in {"CLEAR_SIDEWAYS", "AMBIGUOUS"}
                )
                / max(1, len(long_compact))
            ),
            "cause_counts": dict(
                Counter(c for r in long_compact for c in r["causes"])
            ),
            "runs": long_compact[:40],  # compact cap
        },
        "short_early_runs_lt6": {
            "n": len(short_early),
            "by_month": dict(short_by_month),
            "by_state": dict(Counter(s["state"] for s in short_early)),
            "share_gt_sideways_or_ambiguous_at_entry": short_in_chop / max(1, len(short_early)),
            "from_tb_share": sum(
                1 for s in short_early if s.get("prev_state") in {"topping", "bottoming"}
            )
            / max(1, len(short_early)),
        },
        "neutral_gate_sensitivity": {
            "eligible_bars_by_min_age": dict(thresh_eligible_bars),
            "long_runs_hit_gate_by_min_age": dict(thresh_eligible_run_hits),
            "long_run_coverage_by_min_age": coverage,
            "recommended_min_age": recommended_age,
            "recommended_min_age_reason": age_reason,
        },
        "suggested_gates": proposal["gates_required"],
        "dangerous_neutral_contexts": {
            "n_flagged_samples": len(dangerous_examples),
            "samples": dangerous_examples[:25],
            "rules_of_thumb": [
                "Avoid neutral while CLEAR trend aligns with expected exit direction still forming",
                "Avoid age<24 even if gate_ok (churn with short early_* already high)",
                "Prefer requiring 15m literally neutral/unknown — not merely non-confirming",
            ],
        },
        "case_windows": cases_out,
        "smallest_implementation_proposal": proposal,
        "c2b2_implementation_warranted": len(long_compact) > 0,
    }

    blob = json.dumps(json_safe(summary), sort_keys=True, separators=(",", ":"))
    summary["deterministic_hash"] = hashlib.sha256(blob.encode()).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "README_results.md").write_text(
        f"""# Phase C2B2A — Neutral fallback root-cause (read-only)

Stack: C1-C strict + C2B-C strict (window 24). No SM/policy changes.

## Headline
- Long topping/bottoming runs ≥96: **{len(long_compact)}**
- Short early_* runs &lt;6: **{len(short_early)}**
- Recommended diagnostic min age: **{recommended_age}**
- Implement C2B2?: **{summary['c2b2_implementation_warranted']}**

## Gate sketch
No turn BOS/CHoCH evidence, no impulse, 15m neutral/unknown, min_hold done, no continuation.

Default remains off until C2B2B implements.
""",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C2B2A neutral fallback root-cause audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--load-start", default=LOAD_START)
    p.add_argument("--load-end", default=LOAD_END)
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    args = p.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        output_dir=args.output_dir,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "long_ge96": summary["long_tb_runs_ge96"]["n"],
                "short_early_lt6": summary["short_early_runs_lt6"]["n"],
                "recommended_min_age": summary["neutral_gate_sensitivity"]["recommended_min_age"],
                "cause_counts": summary["long_tb_runs_ge96"]["cause_counts"],
                "proposal": summary["smallest_implementation_proposal"],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

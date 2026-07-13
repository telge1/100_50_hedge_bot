"""Multi-week / multi-regime validation audit for M3 (= C3).

Research-only. Does not mutate live strategy, productive pipeline CSVs, or
B3/R2 thresholds. Gates remain enabled=False; outcomes are post-hoc only.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.pipeline_counterfactual import simulate_sequence
from research.regime_scanner.pipeline_counterfactual_audit import (
    c0_reproduction_check,
    filter_activated_setups,
    flatten_sequence,
    load_pipeline_tables,
    prepare_b3_map,
    prepare_candles,
    prepare_r2_timeline,
    variant_metrics,
)
from research.regime_scanner.pipeline_counterfactual_multiweek import (
    B3_CONFIG,
    MAIN_VARIANTS,
    MARCH_WEEK_END,
    MARCH_WEEK_START,
    M_TO_C,
    R2_CONFIG,
    QUALITY_AMBIGUOUS,
    QUALITY_GOOD,
    QUALITY_WEAK,
    WeekWindow,
    assert_gate_configs_unchanged,
    assign_week_id,
    block_stage,
    choose_recommendation,
    classify_block_verdict,
    classify_market_phase,
    decision_thresholds_scenarios,
    enrich_forward_outcome,
    leave_one_week_out,
    map_quality_label,
    multi_variant_config,
    no_double_count,
    precision_recall_false_block,
    primary_gate_family,
    slice_weeks,
    timeline_state_shares,
    to_utc,
    weekly_stability,
)
from research.regime_scanner.point_audit import json_safe

DEFAULT_PIPELINE = (
    "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1"
)
DEFAULT_MARCH_PIPELINE = (
    "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
)
DEFAULT_MARCH_B3 = (
    "research/backtests/results/regime_scanner_direction_gate_audit_march_week1/"
    "direction_gate_timeline_15m.csv"
)
DEFAULT_MARCH_R2 = (
    "research/backtests/results/regime_scanner_risk_off_audit_march_week1/risk_off_timeline.csv"
)
DEFAULT_B3 = (
    "research/backtests/results/regime_scanner_pipeline_counterfactual_multiweek/"
    "cache/b3_timeline_15m_jan_apr.csv"
)
DEFAULT_R2 = (
    "research/backtests/results/regime_scanner_pipeline_counterfactual_multiweek/"
    "cache/r2_timeline_jan_apr.csv"
)
DEFAULT_OUT = "research/backtests/results/regime_scanner_pipeline_counterfactual_multiweek"

FOCUS_SETUPS = ("setup_00055", "setup_00056", "setup_00057", "setup_00058", "setup_00059")


def _first_by_setup(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if df is None or df.empty or "setup_id" not in df.columns:
        return out
    for _, row in df.iterrows():
        sid = str(row["setup_id"])
        if sid not in out:
            out[sid] = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
    return out


def _json_list(v: object) -> str:
    return json.dumps(json_safe(v), ensure_ascii=True)


def _truthy(v: object) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).strip().lower() in {"true", "1", "yes"}


def run_variant_window(
    *,
    variant_m: str,
    setups: pd.DataFrame,
    pa_by: dict[str, dict[str, Any]],
    mom_by: dict[str, dict[str, Any]],
    r2: pd.DataFrame | None,
    b3: pd.DataFrame | None,
    candles: pd.DataFrame,
    decision_index: pd.DatetimeIndex,
) -> list[dict[str, Any]]:
    cfg = multi_variant_config(variant_m)
    use_r2 = r2 if cfg.use_r2 else None
    use_b3 = b3 if cfg.use_b3 else None
    seqs: list[dict[str, Any]] = []
    for _, setup_row in setups.iterrows():
        sid = str(setup_row["setup_id"])
        setup_map = {k: (None if pd.isna(v) else v) for k, v in setup_row.to_dict().items()}
        if not setup_map.get("side"):
            setup_map["side"] = setup_map.get("setup_side")
        seq = simulate_sequence(
            setup_row=setup_map,
            pa_row=pa_by.get(sid),
            existing_mom_row=mom_by.get(sid),
            r2_timeline=use_r2,
            b3_timeline=use_b3,
            candles_5m=candles,
            decision_index=decision_index,
            cfg=cfg,
        )
        seq["multi_variant"] = variant_m
        seq["c_variant"] = M_TO_C[variant_m]
        seqs.append(seq)
    return seqs


def march_week_reproduction(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Reproduce March-week C0/C3 checks with the multiweek runner path."""
    setups_raw, pa_raw, mom_raw = load_pipeline_tables(Path(args.march_pipeline_dir))
    setups = filter_activated_setups(setups_raw, str(MARCH_WEEK_START), str(MARCH_WEEK_END))
    setup_ids = set(setups["setup_id"].astype(str))
    pa = pa_raw[pa_raw["setup_id"].astype(str).isin(setup_ids)].copy() if len(pa_raw) else pa_raw
    mom = mom_raw[mom_raw["setup_id"].astype(str).isin(setup_ids)].copy() if len(mom_raw) else mom_raw
    pa_by = _first_by_setup(pa)
    mom_by = _first_by_setup(mom)

    r2 = prepare_r2_timeline(Path(args.march_risk_csv), str(MARCH_WEEK_START), str(MARCH_WEEK_END))
    candles, decision_index = prepare_candles(
        args.symbol, str(MARCH_WEEK_START), str(MARCH_WEEK_END), r2=r2
    )
    all_dec = pd.DatetimeIndex(sorted(candles["decision_time"].unique()))
    b3 = prepare_b3_map(Path(args.march_b3_csv), all_dec)
    warm = MARCH_WEEK_START - pd.Timedelta(days=1)
    b3 = b3[(b3["decision_time"] >= warm) & (b3["decision_time"] < MARCH_WEEK_END + pd.Timedelta(hours=12))]

    by_var: dict[str, list[dict[str, Any]]] = {}
    for mv in MAIN_VARIANTS:
        by_var[mv] = run_variant_window(
            variant_m=mv,
            setups=setups,
            pa_by=pa_by,
            mom_by=mom_by,
            r2=r2,
            b3=b3,
            candles=candles,
            decision_index=decision_index,
        )

    c0 = by_var["M0"]
    c0_check, c0_summary = c0_reproduction_check(c0, setups, pa, mom)
    # Map C-style for metrics helper
    for s in c0:
        s["variant"] = "C0"
    metrics_c0 = variant_metrics("C0", c0, pd.DataFrame(), None)

    def focus_state(mv: str, sid: str) -> dict[str, Any]:
        return next((s for s in by_var[mv] if str(s.get("setup_id")) == sid), {})

    m2 = by_var["M2"]
    m3 = by_var["M3"]
    r2_entry_blocks = sum(
        1
        for s in m2
        if str(s.get("setup_id")) in {str(x.get("setup_id")) for x in c0 if x.get("entry_allowed")}
        and not s.get("entry_allowed")
        and primary_gate_family(s.get("primary_abort_reason")) == "R2"
    )
    r2_all_blocks = sum(
        1
        for s in m2
        if any(str(r).startswith("R2_") for r in (s.get("abort_reasons") or []))
    )
    setup_only_extra = sum(
        1
        for s in m2
        if s.get("final_state") == "BLOCKED_AT_SETUP"
        and not next((x for x in c0 if str(x.get("setup_id")) == str(s.get("setup_id"))), {}).get(
            "entry_allowed"
        )
    )

    checks = {
        "c0_entries": metrics_c0.get("n_entries"),
        "c0_entries_ok": metrics_c0.get("n_entries") == 24,
        "pa_confirmations": metrics_c0.get("n_pa_confirmations"),
        "pa_confirmations_ok": metrics_c0.get("n_pa_confirmations") == 32,
        "r2_entry_blocks": r2_entry_blocks,
        "r2_entry_blocks_ok": r2_entry_blocks == 3,
        "r2_total_abort_prefix_blocks": r2_all_blocks,
        "setup_block_without_entry_path": setup_only_extra,
        "setup_block_without_entry_path_ok": setup_only_extra >= 1,
        "b3_entry_blocks": sum(
            1
            for s in by_var["M1"]
            if str(s.get("setup_id")) in {str(x.get("setup_id")) for x in c0 if x.get("entry_allowed")}
            and not s.get("entry_allowed")
        ),
        "b3_entry_blocks_ok": True,  # filled below
        "00055_m3_pa_block": focus_state("M3", "setup_00055").get("final_state"),
        "00055_ok": focus_state("M3", "setup_00055").get("final_state") == "ABORTED_AT_PA",
        "00056_no_pa": focus_state("M0", "setup_00056").get("final_state"),
        "00057_no_pa": focus_state("M0", "setup_00057").get("final_state"),
        "00059_no_pa": focus_state("M0", "setup_00059").get("final_state"),
        "00056_57_59_ok": all(
            focus_state("M0", sid).get("final_state") == "NO_PA_CONFIRMATION"
            for sid in ("setup_00056", "setup_00057", "setup_00059")
        ),
        "00058_final": focus_state("M0", "setup_00058").get("final_state"),
        "00058_ok": focus_state("M0", "setup_00058").get("final_state") == "EXPIRED",
        "c0_reproduction_all_match": bool(c0_summary.get("all_match")),
    }
    checks["b3_entry_blocks_ok"] = checks["b3_entry_blocks"] == 0
    checks["all_ok"] = all(
        bool(checks[k])
        for k in (
            "c0_entries_ok",
            "pa_confirmations_ok",
            "r2_entry_blocks_ok",
            "setup_block_without_entry_path_ok",
            "b3_entry_blocks_ok",
            "00055_ok",
            "00056_57_59_ok",
            "00058_ok",
            "c0_reproduction_all_match",
        )
    )
    rows = [{"check": k, "value": v, "ok": v if isinstance(v, bool) else None} for k, v in checks.items()]
    # Expand numeric expectations
    rows = [
        {
            "check": "c0_entries",
            "expected": 24,
            "actual": checks["c0_entries"],
            "ok": checks["c0_entries_ok"],
        },
        {
            "check": "pa_confirmations",
            "expected": 32,
            "actual": checks["pa_confirmations"],
            "ok": checks["pa_confirmations_ok"],
        },
        {
            "check": "r2_entry_blocks",
            "expected": 3,
            "actual": checks["r2_entry_blocks"],
            "ok": checks["r2_entry_blocks_ok"],
        },
        {
            "check": "setup_block_without_entry_path",
            "expected": ">=1",
            "actual": checks["setup_block_without_entry_path"],
            "ok": checks["setup_block_without_entry_path_ok"],
        },
        {
            "check": "b3_entry_blocks",
            "expected": 0,
            "actual": checks["b3_entry_blocks"],
            "ok": checks["b3_entry_blocks_ok"],
        },
        {
            "check": "00055_m3_blocked_at_pa",
            "expected": "ABORTED_AT_PA",
            "actual": checks["00055_m3_pa_block"],
            "ok": checks["00055_ok"],
        },
        {
            "check": "00056_57_59_no_pa",
            "expected": "NO_PA_CONFIRMATION",
            "actual": {
                "00056": checks["00056_no_pa"],
                "00057": checks["00057_no_pa"],
                "00059": checks["00059_no_pa"],
            },
            "ok": checks["00056_57_59_ok"],
        },
        {
            "check": "00058_expired",
            "expected": "EXPIRED",
            "actual": checks["00058_final"],
            "ok": checks["00058_ok"],
        },
        {
            "check": "c0_reproduction_all_match",
            "expected": True,
            "actual": checks["c0_reproduction_all_match"],
            "ok": checks["c0_reproduction_all_match"],
        },
        {
            "check": "all_reproduction_gates",
            "expected": True,
            "actual": checks["all_ok"],
            "ok": checks["all_ok"],
        },
    ]
    summary = {
        "all_ok": checks["all_ok"],
        "c0_summary": c0_summary,
        "checks": checks,
        "n_setups": int(len(setups)),
        "m0_entries": int(metrics_c0.get("n_entries") or 0),
        "m2_entries": sum(1 for s in m2 if s.get("entry_allowed")),
        "m3_entries": sum(1 for s in m3 if s.get("entry_allowed")),
    }
    return pd.DataFrame(rows), summary


def build_entry_outcomes_multi(
    sequences: list[dict[str, Any]],
    candles: pd.DataFrame,
    weeks: list[WeekWindow],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    week_by_id = {w.week_id: w for w in weeks}
    for seq in sequences:
        if not seq.get("entry_allowed"):
            continue
        fo = enrich_forward_outcome(
            candles,
            seq.get("entry_timestamp"),
            float(seq.get("entry_price") or 0.0),
            str(seq.get("side") or ""),
        )
        wid = assign_week_id(seq.get("entry_timestamp") or seq.get("setup_activation_timestamp"), weeks)
        w = week_by_id.get(wid or "")
        rows.append(
            {
                "week_id": wid,
                "is_known_march_week": bool(w.is_known_march_week) if w else False,
                "is_out_of_sample": bool(w.is_out_of_sample) if w else False,
                "setup_id": seq.get("setup_id"),
                "multi_variant": seq.get("multi_variant"),
                "c_variant": seq.get("c_variant") or seq.get("variant"),
                "side": seq.get("side"),
                "entry_timestamp": seq.get("entry_timestamp"),
                "entry_price": seq.get("entry_price"),
                "final_state": seq.get("final_state"),
                "required_confirm_candles": seq.get("required_confirm_candles"),
                "entry_quality": fo.get("entry_quality"),
                "entry_quality_raw": fo.get("entry_quality_raw"),
                "outcome_confidence": fo.get("outcome_confidence"),
                "mfe_pct": fo.get("mfe_pct"),
                "mae_pct": fo.get("mae_pct"),
                "reached_plus_025": fo.get("reached_plus_025"),
                "minutes_to_025": fo.get("minutes_to_025"),
                "reached_plus_050": fo.get("reached_plus_050"),
                "minutes_to_050": fo.get("minutes_to_050"),
                "returned_to_entry": fo.get("returned_to_entry"),
                "minutes_to_return": fo.get("minutes_to_return"),
                "max_adverse_before_025": fo.get("max_adverse_before_025"),
                "max_favorable_before_strong_adverse": fo.get("max_favorable_before_strong_adverse"),
                "adverse_15m": fo.get("adverse_15m"),
                "adverse_30m": fo.get("adverse_30m"),
                "adverse_60m": fo.get("adverse_60m"),
                "adverse_120m": fo.get("adverse_120m"),
                "favorable_15m": fo.get("favorable_15m"),
                "favorable_30m": fo.get("favorable_30m"),
                "favorable_60m": fo.get("favorable_60m"),
                "favorable_120m": fo.get("favorable_120m"),
                "evaluable": fo.get("evaluable"),
                "outcome_reason": fo.get("reason"),
            }
        )
    return pd.DataFrame(rows)


def later_setup_info(
    blocked_setup_id: str,
    blocked_side: str,
    blocked_ts: object,
    setups: pd.DataFrame,
    mom_by: dict[str, dict[str, Any]],
    horizon_hours: float = 24.0,
) -> dict[str, Any]:
    if blocked_ts is None or setups.empty:
        return {"later_new_setup": False, "later_entry": False, "minutes_to_new_setup": None}
    try:
        t0 = to_utc(blocked_ts)
    except (TypeError, ValueError):
        return {"later_new_setup": False, "later_entry": False, "minutes_to_new_setup": None}
    s = setups.copy()
    s["setup_activation_timestamp"] = pd.to_datetime(s["setup_activation_timestamp"], utc=True)
    side_col = "setup_side" if "setup_side" in s.columns else "side"
    cand = s[
        (s["setup_activation_timestamp"] > t0)
        & (s["setup_activation_timestamp"] <= t0 + pd.Timedelta(hours=horizon_hours))
        & (s[side_col].astype(str).str.lower() == str(blocked_side).lower())
        & (s["setup_id"].astype(str) != str(blocked_setup_id))
    ].sort_values("setup_activation_timestamp")
    if cand.empty:
        return {"later_new_setup": False, "later_entry": False, "minutes_to_new_setup": None}
    first = cand.iloc[0]
    mins = (to_utc(first["setup_activation_timestamp"]) - t0).total_seconds() / 60.0
    later_entry = str(first["setup_id"]) in mom_by
    return {
        "later_new_setup": True,
        "later_entry": later_entry,
        "minutes_to_new_setup": float(mins),
        "later_setup_id": str(first["setup_id"]),
    }


def build_blocked_entry_rows(
    *,
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    baseline_outcomes: pd.DataFrame,
    weeks: list[WeekWindow],
    setups: pd.DataFrame,
    mom_by: dict[str, dict[str, Any]],
    r2_timeline: pd.DataFrame,
) -> pd.DataFrame:
    m0_by = {str(s["setup_id"]): s for s in sequences_by_variant.get("M0", [])}
    out_by = {
        str(r["setup_id"]): r
        for _, r in baseline_outcomes.iterrows()
        if r.get("multi_variant") == "M0" or r.get("c_variant") == "C0"
    }
    if baseline_outcomes is not None and len(baseline_outcomes) and "setup_id" in baseline_outcomes.columns:
        # Prefer explicit M0 filter
        m0_out = baseline_outcomes[
            baseline_outcomes.get("multi_variant", baseline_outcomes.get("c_variant")) == "M0"
        ] if "multi_variant" in baseline_outcomes.columns else baseline_outcomes
        if "multi_variant" in baseline_outcomes.columns:
            m0_out = baseline_outcomes[baseline_outcomes["multi_variant"] == "M0"]
        elif "c_variant" in baseline_outcomes.columns:
            m0_out = baseline_outcomes[baseline_outcomes["c_variant"] == "C0"]
        else:
            m0_out = baseline_outcomes
        out_by = {str(r["setup_id"]): r for _, r in m0_out.iterrows()}

    rows: list[dict[str, Any]] = []
    for mv in ("M1", "M2", "M3"):
        for seq in sequences_by_variant.get(mv, []):
            sid = str(seq.get("setup_id"))
            base = m0_by.get(sid)
            if not base or not base.get("entry_allowed"):
                continue
            if seq.get("entry_allowed"):
                continue
            bout = out_by.get(sid, {})
            quality = map_quality_label(bout.get("entry_quality"))
            block_ts = None
            path = seq.get("state_path") or []
            for step in reversed(path):
                if str(step.get("state") or "") in {
                    "BLOCKED_AT_SETUP",
                    "ABORTED_AT_PA",
                    "ABORTED_DURING_CONFIRMATION",
                }:
                    block_ts = step.get("timestamp")
                    break
            block_ts = block_ts or seq.get("setup_activation_timestamp")
            later = later_setup_info(
                sid, str(seq.get("side") or ""), block_ts, setups, mom_by
            )
            verdict = classify_block_verdict(
                baseline_quality=quality,
                blocked=True,
                later_new_setup=bool(later.get("later_new_setup")),
                later_recovered_entry=bool(later.get("later_entry")),
            )
            # Prefer quality-based primary labels for FP/TP/Ambiguous files
            if quality == QUALITY_GOOD:
                primary_verdict = "FALSE_POSITIVE_BLOCK"
            elif quality == QUALITY_WEAK:
                primary_verdict = "TRUE_POSITIVE_BLOCK"
            else:
                primary_verdict = "AMBIGUOUS_BLOCK"

            r2_row = {}
            if r2_timeline is not None and not r2_timeline.empty and block_ts is not None:
                rt = r2_timeline.copy()
                rt["decision_time"] = pd.to_datetime(rt["decision_time"], utc=True)
                rt = rt[rt["decision_time"] <= to_utc(block_ts)]
                if len(rt):
                    r2_row = rt.iloc[-1].to_dict()

            wid = assign_week_id(base.get("entry_timestamp") or base.get("setup_activation_timestamp"), weeks)
            rows.append(
                {
                    "week_id": wid,
                    "setup_id": sid,
                    "side": seq.get("side"),
                    "multi_variant": mv,
                    "setup_time": seq.get("setup_activation_timestamp"),
                    "pa_time": seq.get("pa_structure_break_timestamp"),
                    "original_entry_time": base.get("entry_timestamp"),
                    "block_stage": block_stage(seq.get("final_state")),
                    "block_time": block_ts,
                    "block_reason": seq.get("primary_abort_reason"),
                    "abort_reasons": _json_list(seq.get("abort_reasons") or []),
                    "gate_family": primary_gate_family(seq.get("primary_abort_reason")),
                    "b3_state": seq.get("b3_state_at_pa") or seq.get("b3_state_at_setup"),
                    "r2_state": seq.get("risk_state_at_pa") or seq.get("risk_state_at_setup"),
                    "r2_score_long": r2_row.get("risk_score_long", r2_row.get("long_risk_score")),
                    "r2_score_short": r2_row.get("risk_score_short", r2_row.get("short_risk_score")),
                    "r2_criteria": r2_row.get("long_risk_reason")
                    or r2_row.get("short_risk_reason")
                    or r2_row.get("entry_reason"),
                    "confirmation_status": seq.get("final_state"),
                    "original_entry_price": base.get("entry_price"),
                    "outcome_quality": quality,
                    "mfe_pct": bout.get("mfe_pct"),
                    "mae_pct": bout.get("mae_pct"),
                    "reached_plus_025": bout.get("reached_plus_025"),
                    "later_new_setup": later.get("later_new_setup"),
                    "later_entry": later.get("later_entry"),
                    "minutes_to_new_setup": later.get("minutes_to_new_setup"),
                    "later_setup_id": later.get("later_setup_id"),
                    "block_verdict": verdict,
                    "primary_verdict": primary_verdict,
                    "fachliche_bewertung": (
                        "False block of good baseline entry"
                        if primary_verdict == "FALSE_POSITIVE_BLOCK"
                        else "True block of weak baseline entry"
                        if primary_verdict == "TRUE_POSITIVE_BLOCK"
                        else "Ambiguous baseline outcome; block not decisive"
                    ),
                }
            )
    return pd.DataFrame(rows)


def summarize_week(
    *,
    week: WeekWindow,
    phase: dict[str, Any],
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    outcomes: pd.DataFrame,
    blocked: pd.DataFrame,
    candles_week_n: int,
    b3_tl: pd.DataFrame,
    r2_tl: pd.DataFrame,
) -> dict[str, Any]:
    def seqs(mv: str) -> list[dict[str, Any]]:
        return [
            s
            for s in sequences_by_variant.get(mv, [])
            if assign_week_id(s.get("setup_activation_timestamp"), [week]) == week.week_id
        ]

    m0 = seqs("M0")
    m1 = seqs("M1")
    m2 = seqs("M2")
    m3 = seqs("M3")
    m0_entries = [s for s in m0 if s.get("entry_allowed")]
    m0_ids = {str(s["setup_id"]) for s in m0_entries}

    def entry_blocks(mv_seqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [s for s in mv_seqs if str(s.get("setup_id")) in m0_ids and not s.get("entry_allowed")]

    b_m1 = entry_blocks(m1)
    b_m2 = entry_blocks(m2)
    b_m3 = entry_blocks(m3)

    out_m0 = outcomes[(outcomes["multi_variant"] == "M0") & (outcomes["week_id"] == week.week_id)] if len(outcomes) else pd.DataFrame()
    out_m3 = outcomes[(outcomes["multi_variant"] == "M3") & (outcomes["week_id"] == week.week_id)] if len(outcomes) else pd.DataFrame()
    qmap = {str(r["setup_id"]): map_quality_label(r.get("entry_quality")) for _, r in out_m0.iterrows()}

    def count_quality(blocked_seqs: list[dict[str, Any]], q: str) -> int:
        return sum(1 for s in blocked_seqs if qmap.get(str(s.get("setup_id"))) == q)

    good_m0 = int((out_m0["entry_quality"] == QUALITY_GOOD).sum()) if len(out_m0) else 0
    weak_m0 = int((out_m0["entry_quality"] == QUALITY_WEAK).sum()) if len(out_m0) else 0
    amb_m0 = int((out_m0["entry_quality"] == QUALITY_AMBIGUOUS).sum()) if len(out_m0) else 0

    good_blocked_m3 = count_quality(b_m3, QUALITY_GOOD)
    weak_blocked_m3 = count_quality(b_m3, QUALITY_WEAK)
    amb_blocked_m3 = count_quality(b_m3, QUALITY_AMBIGUOUS)
    good_allowed_m3 = int((out_m3["entry_quality"] == QUALITY_GOOD).sum()) if len(out_m3) else 0
    weak_allowed_m3 = int((out_m3["entry_quality"] == QUALITY_WEAK).sum()) if len(out_m3) else 0

    pr = precision_recall_false_block(
        weak_prevented=weak_blocked_m3,
        good_prevented=good_blocked_m3,
        good_allowed=good_allowed_m3,
        n_weak_baseline=weak_m0,
    )

    after2 = sum(1 for s in m3 if s.get("final_state") == "ENTRY_ALLOWED_AFTER_2")
    after3 = sum(1 for s in m3 if s.get("final_state") == "ENTRY_ALLOWED_AFTER_3")

    r2_shares = timeline_state_shares(
        r2_tl,
        state_col="risk_state",
        states={
            "r2_risk_off_long": "long_risk_off",
            "r2_risk_off_short": "short_risk_off",
            "r2_elevated_long": "long_risk_elevated",
            "r2_elevated_short": "short_risk_elevated",
        },
        week_start=week.start,
        week_end=week.end,
    )
    # Combined off / elevated shares
    if r2_tl is not None and not r2_tl.empty:
        rt = r2_tl.copy()
        rt["decision_time"] = pd.to_datetime(rt["decision_time"], utc=True)
        rt = rt[(rt["decision_time"] >= week.start) & (rt["decision_time"] < week.end)]
        share_off = float(rt["risk_state"].isin(["long_risk_off", "short_risk_off"]).mean()) if len(rt) else 0.0
        share_elev = float(
            rt["risk_state"].isin(["long_risk_elevated", "short_risk_elevated"]).mean()
        ) if len(rt) else 0.0
    else:
        share_off = share_elev = 0.0

    b3_shares = timeline_state_shares(
        b3_tl,
        state_col="direction_gate_state",
        states={
            "b3_strong_bearish": "strong_bearish",
            "b3_strong_bullish": "strong_bullish",
        },
        week_start=week.start,
        week_end=week.end,
        time_col="decision_time" if b3_tl is not None and "decision_time" in getattr(b3_tl, "columns", []) else "bar_close_time",
    )

    blk_w = blocked[blocked["week_id"] == week.week_id] if len(blocked) else pd.DataFrame()

    return {
        **week.to_dict(),
        **phase,
        "n_5m_candles_observed": candles_week_n,
        "n_setups": len(m0),
        "n_pa_confirmations": sum(1 for s in m0 if s.get("pa_structure_break_timestamp")),
        "n_m0_entries": len(m0_entries),
        "n_m0_long_entries": sum(1 for s in m0_entries if s.get("side") == "long"),
        "n_m0_short_entries": sum(1 for s in m0_entries if s.get("side") == "short"),
        "n_b3_blocks_on_m0_entries": len(b_m1),
        "n_r2_blocks_on_m0_entries": len(b_m2),
        "n_m3_blocks_on_m0_entries": len(b_m3),
        "n_blocks_at_setup_m3": sum(1 for s in m3 if s.get("final_state") == "BLOCKED_AT_SETUP"),
        "n_blocks_at_pa_m3": sum(1 for s in m3 if s.get("final_state") == "ABORTED_AT_PA"),
        "n_blocks_during_confirm_m3": sum(
            1 for s in m3 if s.get("final_state") == "ABORTED_DURING_CONFIRMATION"
        ),
        "n_entries_after_2_m3": after2,
        "n_entries_after_3_m3": after3,
        "n_good_m0": good_m0,
        "n_weak_m0": weak_m0,
        "n_ambiguous_m0": amb_m0,
        "n_good_blocked_m3": good_blocked_m3,
        "n_weak_blocked_m3": weak_blocked_m3,
        "n_ambiguous_blocked_m3": amb_blocked_m3,
        "n_good_allowed_m3": good_allowed_m3,
        "n_weak_allowed_m3": weak_allowed_m3,
        "precision_m3": pr["precision"],
        "recall_m3": pr["recall"],
        "false_block_rate_m3": pr["false_block_rate"],
        "block_share_m0_entries_m3": (len(b_m3) / len(m0_entries)) if m0_entries else None,
        "r2_time_share_risk_off": share_off,
        "r2_time_share_risk_elevated": share_elev,
        "b3_time_share_strong_bearish": b3_shares.get("share_b3_strong_bearish"),
        "b3_time_share_strong_bullish": b3_shares.get("share_b3_strong_bullish"),
        "b3_n_state_changes": b3_shares.get("n_state_changes"),
        "b3_avg_state_duration_bars": b3_shares.get("avg_state_duration_bars"),
        "r2_n_state_changes": r2_shares.get("n_state_changes"),
        "r2_avg_state_duration_bars": r2_shares.get("avg_state_duration_bars"),
        "n_false_positive_blocks_m3": int(
            ((blk_w["multi_variant"] == "M3") & (blk_w["primary_verdict"] == "FALSE_POSITIVE_BLOCK")).sum()
        )
        if len(blk_w)
        else good_blocked_m3,
        "n_true_positive_blocks_m3": int(
            ((blk_w["multi_variant"] == "M3") & (blk_w["primary_verdict"] == "TRUE_POSITIVE_BLOCK")).sum()
        )
        if len(blk_w)
        else weak_blocked_m3,
    }


def confirmation_diagnostics(
    sequences_by_variant: dict[str, list[dict[str, Any]]],
    outcomes: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    m0 = {str(s["setup_id"]): s for s in sequences_by_variant.get("M0", []) if s.get("entry_allowed")}
    out_m0 = {
        str(r["setup_id"]): r
        for _, r in outcomes[outcomes["multi_variant"] == "M0"].iterrows()
    } if len(outcomes) else {}
    for seq in sequences_by_variant.get("M3", []):
        sid = str(seq.get("setup_id"))
        base = m0.get(sid)
        if base is None:
            continue
        req = int(seq.get("required_confirm_candles") or 2)
        bout = out_m0.get(sid, {})
        delayed = bool(seq.get("entry_allowed") and seq.get("final_state") == "ENTRY_ALLOWED_AFTER_3")
        saved = bool(
            (not seq.get("entry_allowed"))
            and req >= 3
            and map_quality_label(bout.get("entry_quality")) == QUALITY_WEAK
        )
        price_shift = None
        if seq.get("entry_allowed") and base.get("entry_price") and seq.get("entry_price"):
            try:
                price_shift = (
                    (float(seq["entry_price"]) - float(base["entry_price"]))
                    / abs(float(base["entry_price"]))
                    * 100.0
                )
            except (TypeError, ValueError, ZeroDivisionError):
                price_shift = None
        rows.append(
            {
                "setup_id": sid,
                "side": seq.get("side"),
                "required_confirm_candles": req,
                "final_state": seq.get("final_state"),
                "risk_state_at_pa": seq.get("risk_state_at_pa"),
                "entry_allowed_m3": seq.get("entry_allowed"),
                "entry_ts_m0": base.get("entry_timestamp"),
                "entry_ts_m3": seq.get("entry_timestamp"),
                "entry_price_m0": base.get("entry_price"),
                "entry_price_m3": seq.get("entry_price"),
                "price_shift_pct": price_shift,
                "baseline_quality": map_quality_label(bout.get("entry_quality")),
                "baseline_mfe": bout.get("mfe_pct"),
                "baseline_mae": bout.get("mae_pct"),
                "delayed_by_third_candle": delayed,
                "weak_prevented_via_third_path": saved,
                "elevated_at_pa": str(seq.get("risk_state_at_pa") or "").endswith("risk_elevated"),
            }
        )
    return pd.DataFrame(rows)


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    gate_cfg = assert_gate_configs_unchanged()

    print("=== March week reproduction ===", flush=True)
    march_df, march_summary = march_week_reproduction(args)
    march_df.to_csv(out / "march_week_reproduction_check.csv", index=False)
    if not march_summary.get("all_ok"):
        print("March reproduction FAILED — aborting multi-week run.", flush=True)
        summary = {
            "status": "aborted_march_reproduction_failed",
            "march_reproduction": march_summary,
            "gate_configs": gate_cfg,
        }
        (out / "audit_summary.json").write_text(
            json.dumps(json_safe(summary), indent=2), encoding="utf-8"
        )
        return summary

    print("March reproduction OK — continuing multi-week.", flush=True)

    pipeline_dir = Path(args.pipeline_dir)
    b3_path = Path(args.b3_csv)
    r2_path = Path(args.risk_csv)
    if not b3_path.exists() or not r2_path.exists():
        summary = {
            "status": "aborted_missing_timelines",
            "missing_b3": not b3_path.exists(),
            "missing_r2": not r2_path.exists(),
            "b3_csv": str(b3_path),
            "risk_csv": str(r2_path),
            "march_reproduction": march_summary,
            "hint": "Generate cache timelines first (see README).",
        }
        (out / "audit_summary.json").write_text(
            json.dumps(json_safe(summary), indent=2), encoding="utf-8"
        )
        march_df.to_csv(out / "march_week_reproduction_check.csv", index=False)
        return summary

    range_start = to_utc(args.range_start)
    range_end = to_utc(args.range_end)

    raw = load_symbol_candles(args.symbol)
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    available_start = raw["timestamp"].min()
    available_end = raw["timestamp"].max() + pd.Timedelta(minutes=5)

    # Clip requested range to available causal candle span
    eff_start = max(range_start, available_start.floor("D"))
    eff_end = min(range_end, (raw["timestamp"].max() + pd.Timedelta(days=1)).floor("D"))
    weeks = slice_weeks(raw["timestamp"], range_start=eff_start, range_end=eff_end)
    complete_weeks = [w for w in weeks if w.is_complete]
    incomplete_weeks = [w for w in weeks if not w.is_complete]

    coverage_rows = [w.to_dict() for w in weeks]
    coverage_rows.append(
        {
            "week_id": "_DATA_SPAN",
            "week_start": available_start.isoformat(),
            "week_end": available_end.isoformat(),
            "is_complete": None,
            "n_5m_candles": int(len(raw)),
            "expected_5m_candles": None,
            "coverage_ratio": None,
            "is_known_march_week": False,
            "is_out_of_sample": False,
            "skip_reason": None,
            "requested_start": range_start.isoformat(),
            "requested_end": range_end.isoformat(),
            "effective_start": eff_start.isoformat(),
            "effective_end": eff_end.isoformat(),
        }
    )
    pd.DataFrame(coverage_rows).to_csv(out / "multiweek_data_coverage.csv", index=False)

    setups_raw, pa_raw, mom_raw = load_pipeline_tables(pipeline_dir)
    setups = filter_activated_setups(setups_raw, str(eff_start), str(eff_end))
    setup_ids = set(setups["setup_id"].astype(str))
    pa = pa_raw[pa_raw["setup_id"].astype(str).isin(setup_ids)].copy() if len(pa_raw) else pa_raw
    mom = mom_raw[mom_raw["setup_id"].astype(str).isin(setup_ids)].copy() if len(mom_raw) else mom_raw
    # Deduplicate
    assert no_double_count(setups["setup_id"])
    assert no_double_count(pa["setup_id"]) if len(pa) else True
    assert no_double_count(mom["setup_id"]) if len(mom) else True

    pa_by = _first_by_setup(pa)
    mom_by = _first_by_setup(mom)

    print(f"Preparing candles/timelines {eff_start} -> {eff_end} ...", flush=True)
    r2 = prepare_r2_timeline(r2_path, str(eff_start), str(eff_end))
    # Ensure score column aliases
    if len(r2):
        if "risk_score_long" not in r2.columns and "long_risk_score" in r2.columns:
            r2["risk_score_long"] = r2["long_risk_score"]
        if "risk_score_short" not in r2.columns and "short_risk_score" in r2.columns:
            r2["risk_score_short"] = r2["short_risk_score"]

    candles, decision_index = prepare_candles(args.symbol, str(eff_start), str(eff_end), r2=r2)
    all_dec = pd.DatetimeIndex(sorted(candles["decision_time"].unique()))
    b3 = prepare_b3_map(b3_path, all_dec)
    warm = eff_start - pd.Timedelta(days=1)
    b3 = b3[(b3["decision_time"] >= warm) & (b3["decision_time"] < eff_end + pd.Timedelta(hours=12))]

    # Also keep 15m B3 for state-share metrics if available
    b3_15 = pd.read_csv(b3_path)
    if "gate_variant" in b3_15.columns:
        b3_15 = b3_15[b3_15["gate_variant"] == "B3"].copy()
    if "bar_close_time" in b3_15.columns:
        b3_15["decision_time"] = pd.to_datetime(b3_15["bar_close_time"], utc=True)

    sequences_by_variant: dict[str, list[dict[str, Any]]] = {m: [] for m in MAIN_VARIANTS}
    for mv in MAIN_VARIANTS:
        print(f"Running {mv} ({M_TO_C[mv]}) on {len(setups)} setups...", flush=True)
        sequences_by_variant[mv] = run_variant_window(
            variant_m=mv,
            setups=setups,
            pa_by=pa_by,
            mom_by=mom_by,
            r2=r2,
            b3=b3,
            candles=candles,
            decision_index=decision_index,
        )

    all_seqs = [s for mv in MAIN_VARIANTS for s in sequences_by_variant[mv]]
    outcomes = build_entry_outcomes_multi(all_seqs, candles, weeks)
    blocked = build_blocked_entry_rows(
        sequences_by_variant=sequences_by_variant,
        baseline_outcomes=outcomes,
        weeks=weeks,
        setups=setups,
        mom_by=mom_by,
        r2_timeline=r2,
    )

    # Phase + weekly summaries (complete weeks in main; incomplete separate)
    weekly_rows: list[dict[str, Any]] = []
    incomplete_rows: list[dict[str, Any]] = []
    for w in weeks:
        phase = classify_market_phase(candles, w.start, w.end)
        n_cand = int(
            (
                (candles["timestamp"] >= w.start) & (candles["timestamp"] < w.end)
            ).sum()
        ) if "timestamp" in candles.columns else w.n_5m_candles
        row = summarize_week(
            week=w,
            phase=phase,
            sequences_by_variant=sequences_by_variant,
            outcomes=outcomes,
            blocked=blocked,
            candles_week_n=n_cand,
            b3_tl=b3_15 if len(b3_15) else b3,
            r2_tl=r2,
        )
        if w.is_complete:
            weekly_rows.append(row)
        else:
            incomplete_rows.append(row)

    weekly_df = pd.DataFrame(weekly_rows)
    if incomplete_rows:
        pd.DataFrame(incomplete_rows).to_csv(out / "multiweek_incomplete_weeks.csv", index=False)
    weekly_df.to_csv(out / "multiweek_weekly_summary.csv", index=False)

    # Variant comparison overall + OOS
    def variant_compare(subset_weeks: list[WeekWindow] | None, label: str) -> list[dict[str, Any]]:
        week_ids = {w.week_id for w in subset_weeks} if subset_weeks is not None else None

        def in_scope(ts: object) -> bool:
            if week_ids is None:
                return True
            return assign_week_id(ts, weeks) in week_ids

        rows = []
        for mv in MAIN_VARIANTS:
            seqs = sequences_by_variant[mv]
            if week_ids is not None:
                seqs = [s for s in seqs if in_scope(s.get("setup_activation_timestamp"))]
            m0_seqs = sequences_by_variant["M0"]
            if week_ids is not None:
                m0_seqs = [s for s in m0_seqs if in_scope(s.get("setup_activation_timestamp"))]
            m0_entry_ids = {str(s["setup_id"]) for s in m0_seqs if s.get("entry_allowed")}
            entries = [s for s in seqs if s.get("entry_allowed")]
            blocks = [
                s for s in seqs if str(s.get("setup_id")) in m0_entry_ids and not s.get("entry_allowed")
            ]
            out_v = outcomes[outcomes["multi_variant"] == mv]
            if week_ids is not None and len(out_v):
                out_v = out_v[out_v["week_id"].isin(week_ids)]
            out_m0 = outcomes[outcomes["multi_variant"] == "M0"]
            if week_ids is not None and len(out_m0):
                out_m0 = out_m0[out_m0["week_id"].isin(week_ids)]
            qmap = {
                str(r["setup_id"]): map_quality_label(r.get("entry_quality"))
                for _, r in out_m0.iterrows()
            }
            good_b = sum(1 for s in blocks if qmap.get(str(s.get("setup_id"))) == QUALITY_GOOD)
            weak_b = sum(1 for s in blocks if qmap.get(str(s.get("setup_id"))) == QUALITY_WEAK)
            amb_b = sum(1 for s in blocks if qmap.get(str(s.get("setup_id"))) == QUALITY_AMBIGUOUS)
            good_a = int((out_v["entry_quality"] == QUALITY_GOOD).sum()) if len(out_v) else 0
            weak_a = int((out_v["entry_quality"] == QUALITY_WEAK).sum()) if len(out_v) else 0
            pr = precision_recall_false_block(
                weak_prevented=weak_b,
                good_prevented=good_b,
                good_allowed=good_a,
                n_weak_baseline=int((out_m0["entry_quality"] == QUALITY_WEAK).sum()) if len(out_m0) else 0,
            )
            rows.append(
                {
                    "scope": label,
                    "multi_variant": mv,
                    "c_variant": M_TO_C[mv],
                    "n_setups": len(seqs),
                    "n_entries": len(entries),
                    "n_long_entries": sum(1 for s in entries if s.get("side") == "long"),
                    "n_short_entries": sum(1 for s in entries if s.get("side") == "short"),
                    "n_blocks_on_m0_entries": len(blocks),
                    "n_good_blocked": good_b,
                    "n_weak_blocked": weak_b,
                    "n_ambiguous_blocked": amb_b,
                    "n_good_allowed": good_a,
                    "n_weak_allowed": weak_a,
                    "precision": pr["precision"],
                    "recall": pr["recall"],
                    "false_block_rate": pr["false_block_rate"],
                    "n_entries_after_2": sum(1 for s in seqs if s.get("final_state") == "ENTRY_ALLOWED_AFTER_2"),
                    "n_entries_after_3": sum(1 for s in seqs if s.get("final_state") == "ENTRY_ALLOWED_AFTER_3"),
                }
            )
        return rows

    compare_rows = []
    compare_rows.extend(variant_compare(complete_weeks, "all_complete_weeks"))
    oos_weeks = [w for w in complete_weeks if w.is_out_of_sample]
    compare_rows.extend(variant_compare(oos_weeks, "out_of_sample_excluding_march"))
    compare_rows.extend(
        variant_compare([w for w in complete_weeks if w.is_known_march_week], "known_march_week_only")
    )
    compare_df = pd.DataFrame(compare_rows)
    compare_df.to_csv(out / "multiweek_variant_comparison.csv", index=False)

    # Entries / outcomes / blocked
    entry_rows = []
    for mv in MAIN_VARIANTS:
        for s in sequences_by_variant[mv]:
            if not s.get("entry_allowed"):
                continue
            flat = flatten_sequence(s)
            flat["multi_variant"] = mv
            flat["week_id"] = assign_week_id(
                s.get("entry_timestamp") or s.get("setup_activation_timestamp"), weeks
            )
            entry_rows.append(flat)
    pd.DataFrame(json_safe(entry_rows)).to_csv(out / "multiweek_entries.csv", index=False)
    outcomes.to_csv(out / "multiweek_entry_outcomes.csv", index=False)
    blocked.to_csv(out / "multiweek_blocked_entries.csv", index=False)

    fp = blocked[blocked["primary_verdict"] == "FALSE_POSITIVE_BLOCK"] if len(blocked) else pd.DataFrame()
    tp = blocked[blocked["primary_verdict"] == "TRUE_POSITIVE_BLOCK"] if len(blocked) else pd.DataFrame()
    amb = blocked[blocked["primary_verdict"] == "AMBIGUOUS_BLOCK"] if len(blocked) else pd.DataFrame()
    fp.to_csv(out / "multiweek_false_positive_blocks.csv", index=False)
    tp.to_csv(out / "multiweek_true_positive_blocks.csv", index=False)
    amb.to_csv(out / "multiweek_ambiguous_blocks.csv", index=False)

    # Remaining weak leaks under M3
    leak_rows = []
    if len(outcomes):
        weak_m3 = outcomes[(outcomes["multi_variant"] == "M3") & (outcomes["entry_quality"] == QUALITY_WEAK)]
        m3_by = {str(s["setup_id"]): s for s in sequences_by_variant["M3"]}
        for _, r in weak_m3.iterrows():
            seq = m3_by.get(str(r["setup_id"]), {})
            cat = "MOMENTUM_QUALITY_LEAK"
            if not seq.get("pa_structure_break_timestamp"):
                cat = "SETUP_QUALITY_LEAK"
            elif not (seq.get("confirmation_candles") or []):
                cat = "PA_QUALITY_LEAK"
            leak_rows.append(
                {
                    "week_id": r.get("week_id"),
                    "setup_id": r.get("setup_id"),
                    "side": r.get("side"),
                    "entry_timestamp": r.get("entry_timestamp"),
                    "mfe_pct": r.get("mfe_pct"),
                    "mae_pct": r.get("mae_pct"),
                    "leak_category": cat,
                    "risk_state_at_pa": seq.get("risk_state_at_pa"),
                    "b3_state_at_pa": seq.get("b3_state_at_pa"),
                }
            )
    pd.DataFrame(leak_rows).to_csv(out / "multiweek_remaining_weak_entry_leaks.csv", index=False)

    # Market phase summary
    phase_rows = []
    if len(weekly_df):
        for phase, g in weekly_df.groupby("market_phase"):
            phase_rows.append(
                {
                    "market_phase": phase,
                    "n_weeks": int(len(g)),
                    "n_m0_entries": int(g["n_m0_entries"].sum()),
                    "n_m1_blocks": int(g["n_b3_blocks_on_m0_entries"].sum()),
                    "n_m2_blocks": int(g["n_r2_blocks_on_m0_entries"].sum()),
                    "n_m3_blocks": int(g["n_m3_blocks_on_m0_entries"].sum()),
                    "n_weak_blocked_m3": int(g["n_weak_blocked_m3"].sum()),
                    "n_good_blocked_m3": int(g["n_good_blocked_m3"].sum()),
                    "precision_m3": (
                        g["n_weak_blocked_m3"].sum()
                        / (g["n_weak_blocked_m3"].sum() + g["n_good_blocked_m3"].sum())
                        if (g["n_weak_blocked_m3"].sum() + g["n_good_blocked_m3"].sum())
                        else None
                    ),
                    "false_block_rate_m3": (
                        g["n_good_blocked_m3"].sum()
                        / (g["n_good_blocked_m3"].sum() + g["n_good_allowed_m3"].sum())
                        if (g["n_good_blocked_m3"].sum() + g["n_good_allowed_m3"].sum())
                        else None
                    ),
                    "avg_r2_time_share_risk_off": float(g["r2_time_share_risk_off"].mean()),
                    "avg_b3_time_share_strong": float(
                        (
                            g["b3_time_share_strong_bearish"].fillna(0)
                            + g["b3_time_share_strong_bullish"].fillna(0)
                        ).mean()
                    ),
                }
            )
            # blocked MFE/MAE from blocked table
            if len(blocked):
                # map setups in these weeks
                wids = set(g["week_id"])
                bb = blocked[(blocked["multi_variant"] == "M3") & (blocked["week_id"].isin(wids))]
                phase_rows[-1]["avg_mfe_blocked"] = float(bb["mfe_pct"].mean()) if len(bb) else None
                phase_rows[-1]["avg_mae_blocked"] = float(bb["mae_pct"].mean()) if len(bb) else None
    pd.DataFrame(phase_rows).to_csv(out / "multiweek_market_phase_summary.csv", index=False)

    # Long/short summary
    ls_rows = []
    for side in ("long", "short"):
        for scope, wset in (
            ("all_complete", complete_weeks),
            ("oos", oos_weeks),
        ):
            wids = {w.week_id for w in wset}
            out_m0 = outcomes[
                (outcomes["multi_variant"] == "M0")
                & (outcomes["side"] == side)
                & (outcomes["week_id"].isin(wids))
            ] if len(outcomes) else pd.DataFrame()
            out_m3 = outcomes[
                (outcomes["multi_variant"] == "M3")
                & (outcomes["side"] == side)
                & (outcomes["week_id"].isin(wids))
            ] if len(outcomes) else pd.DataFrame()
            blk = blocked[
                (blocked["multi_variant"] == "M3")
                & (blocked["side"] == side)
                & (blocked["week_id"].isin(wids))
            ] if len(blocked) else pd.DataFrame()
            good_b = int((blk["primary_verdict"] == "FALSE_POSITIVE_BLOCK").sum()) if len(blk) else 0
            weak_b = int((blk["primary_verdict"] == "TRUE_POSITIVE_BLOCK").sum()) if len(blk) else 0
            good_a = int((out_m3["entry_quality"] == QUALITY_GOOD).sum()) if len(out_m3) else 0
            pr = precision_recall_false_block(
                weak_prevented=weak_b,
                good_prevented=good_b,
                good_allowed=good_a,
                n_weak_baseline=int((out_m0["entry_quality"] == QUALITY_WEAK).sum()) if len(out_m0) else 0,
            )
            ls_rows.append(
                {
                    "scope": scope,
                    "side": side,
                    "n_m0_entries": int(len(out_m0)),
                    "n_m3_entries": int(len(out_m3)),
                    "n_m3_blocks": int(len(blk)),
                    "n_true_positive_blocks": weak_b,
                    "n_false_positive_blocks": good_b,
                    "precision": pr["precision"],
                    "recall": pr["recall"],
                    "false_block_rate": pr["false_block_rate"],
                    "mean_mfe_m0": float(out_m0["mfe_pct"].mean()) if len(out_m0) else None,
                    "mean_mae_m0": float(out_m0["mae_pct"].mean()) if len(out_m0) else None,
                }
            )
    pd.DataFrame(ls_rows).to_csv(out / "multiweek_long_short_summary.csv", index=False)

    # R2 diagnostics
    r2_blk = blocked[blocked["multi_variant"] == "M2"] if len(blocked) else pd.DataFrame()
    r2_diag = {
        "n_r2_blocks_on_m0_entries": int(len(r2_blk)),
        "n_good": int((r2_blk["primary_verdict"] == "FALSE_POSITIVE_BLOCK").sum()) if len(r2_blk) else 0,
        "n_weak": int((r2_blk["primary_verdict"] == "TRUE_POSITIVE_BLOCK").sum()) if len(r2_blk) else 0,
        "n_ambiguous": int((r2_blk["primary_verdict"] == "AMBIGUOUS_BLOCK").sum()) if len(r2_blk) else 0,
        "n_long_blocks": int((r2_blk["side"] == "long").sum()) if len(r2_blk) else 0,
        "n_short_blocks": int((r2_blk["side"] == "short").sum()) if len(r2_blk) else 0,
        "weeks_with_false_block": int(
            r2_blk[r2_blk["primary_verdict"] == "FALSE_POSITIVE_BLOCK"]["week_id"].nunique()
        )
        if len(r2_blk)
        else 0,
        "weeks_with_any_r2_block": int(r2_blk["week_id"].nunique()) if len(r2_blk) else 0,
        "weeks_with_true_block": int(
            r2_blk[r2_blk["primary_verdict"] == "TRUE_POSITIVE_BLOCK"]["week_id"].nunique()
        )
        if len(r2_blk)
        else 0,
    }
    good_a_m2 = compare_df[
        (compare_df["scope"] == "all_complete_weeks") & (compare_df["multi_variant"] == "M2")
    ]
    r2_diag["false_block_rate"] = (
        float(good_a_m2.iloc[0]["false_block_rate"]) if len(good_a_m2) else None
    )
    # Pattern like 00055: long PA block while baseline good
    like_55 = 0
    if len(r2_blk):
        like_55 = int(
            (
                (r2_blk["side"] == "long")
                & (r2_blk["block_stage"] == "pa")
                & (r2_blk["primary_verdict"] == "FALSE_POSITIVE_BLOCK")
            ).sum()
        )
    r2_diag["n_false_blocks_like_00055_long_pa"] = like_55
    r2_diag_rows = [{"metric": k, "value": v} for k, v in r2_diag.items()]
    if len(r2_blk):
        for reason, cnt in r2_blk["block_reason"].fillna("NA").value_counts().items():
            r2_diag_rows.append({"metric": f"block_reason::{reason}", "value": int(cnt)})
    pd.DataFrame(r2_diag_rows).to_csv(out / "multiweek_r2_diagnostics.csv", index=False)

    # B3 diagnostics
    b3_blk = blocked[blocked["multi_variant"] == "M1"] if len(blocked) else pd.DataFrame()
    b3_diag = {
        "n_b3_blocks_on_m0_entries": int(len(b3_blk)),
        "n_good": int((b3_blk["primary_verdict"] == "FALSE_POSITIVE_BLOCK").sum()) if len(b3_blk) else 0,
        "n_weak": int((b3_blk["primary_verdict"] == "TRUE_POSITIVE_BLOCK").sum()) if len(b3_blk) else 0,
        "n_ambiguous": int((b3_blk["primary_verdict"] == "AMBIGUOUS_BLOCK").sum()) if len(b3_blk) else 0,
        "n_long_blocks": int((b3_blk["side"] == "long").sum()) if len(b3_blk) else 0,
        "n_short_blocks": int((b3_blk["side"] == "short").sum()) if len(b3_blk) else 0,
        "n_blocks_at_setup": int((b3_blk["block_stage"] == "setup").sum()) if len(b3_blk) else 0,
        "n_blocks_at_pa": int((b3_blk["block_stage"] == "pa").sum()) if len(b3_blk) else 0,
        "n_blocks_during_confirm": int((b3_blk["block_stage"] == "confirmation").sum())
        if len(b3_blk)
        else 0,
    }
    good_a_m1 = compare_df[
        (compare_df["scope"] == "all_complete_weeks") & (compare_df["multi_variant"] == "M1")
    ]
    b3_diag["false_block_rate"] = (
        float(good_a_m1.iloc[0]["false_block_rate"]) if len(good_a_m1) else None
    )
    pd.DataFrame([{"metric": k, "value": v} for k, v in b3_diag.items()]).to_csv(
        out / "multiweek_b3_diagnostics.csv", index=False
    )

    conf_df = confirmation_diagnostics(sequences_by_variant, outcomes)
    conf_df.to_csv(out / "multiweek_confirmation_2_vs_3.csv", index=False)
    n_req3 = int((conf_df["required_confirm_candles"] >= 3).sum()) if len(conf_df) else 0
    n_after2 = int(
        sum(
            1
            for s in sequences_by_variant["M3"]
            if s.get("final_state") == "ENTRY_ALLOWED_AFTER_2"
        )
    )
    n_after3 = int(
        sum(
            1
            for s in sequences_by_variant["M3"]
            if s.get("final_state") == "ENTRY_ALLOWED_AFTER_3"
        )
    )
    n_third_saves_weak = int(conf_df["weak_prevented_via_third_path"].sum()) if len(conf_df) else 0
    n_third_delays_good = int(
        (
            (conf_df["delayed_by_third_candle"])
            & (conf_df["baseline_quality"] == QUALITY_GOOD)
        ).sum()
    ) if len(conf_df) else 0
    third_benefit = float(n_third_saves_weak - n_third_delays_good)

    lowo = leave_one_week_out(weekly_rows)
    pd.DataFrame(lowo).to_csv(out / "multiweek_leave_one_week_out.csv", index=False)

    oos_compare = compare_df[compare_df["scope"] == "out_of_sample_excluding_march"]
    oos_summary = {
        "n_oos_weeks": len(oos_weeks),
        "excluded_week_ids": [w.week_id for w in complete_weeks if w.is_known_march_week],
        "excluded_prior_audit_window": f"{MARCH_WEEK_START.isoformat()}->{MARCH_WEEK_END.isoformat()}",
        "note": "Research weeks overlapping 2026-03-01..2026-03-08 are excluded from OOS",
        "variant_rows": oos_compare.to_dict(orient="records"),
    }
    # Flatten key M3 OOS metrics
    m3_oos = oos_compare[oos_compare["multi_variant"] == "M3"]
    if len(m3_oos):
        oos_summary.update({f"m3_{k}": m3_oos.iloc[0][k] for k in m3_oos.columns if k not in {"scope"}})
    pd.DataFrame([oos_summary]).to_csv(out / "multiweek_out_of_sample_summary.csv", index=False)

    # Stability
    stab = [
        weekly_stability(weekly_rows, value_key=k)
        for k in (
            "n_m3_blocks_on_m0_entries",
            "n_good_blocked_m3",
            "n_weak_blocked_m3",
            "false_block_rate_m3",
            "precision_m3",
        )
    ]

    m3_all = compare_df[
        (compare_df["scope"] == "all_complete_weeks") & (compare_df["multi_variant"] == "M3")
    ]
    m1_all = compare_df[
        (compare_df["scope"] == "all_complete_weeks") & (compare_df["multi_variant"] == "M1")
    ]
    m2_all = compare_df[
        (compare_df["scope"] == "all_complete_weeks") & (compare_df["multi_variant"] == "M2")
    ]
    m0_all = compare_df[
        (compare_df["scope"] == "all_complete_weeks") & (compare_df["multi_variant"] == "M0")
    ]

    def _f(df: pd.DataFrame, col: str) -> float | None:
        return float(df.iloc[0][col]) if len(df) and df.iloc[0][col] is not None else None

    n_weeks_weak = int((weekly_df["n_weak_blocked_m3"] > 0).sum()) if len(weekly_df) else 0
    ls_all = pd.DataFrame(ls_rows)
    ls_fbr = ls_all[ls_all["scope"] == "all_complete"]["false_block_rate"].dropna()
    asymmetry = float(ls_fbr.max() - ls_fbr.min()) if len(ls_fbr) >= 2 else None

    scenarios = decision_thresholds_scenarios(
        false_block_rate=_f(m3_all, "false_block_rate"),
        n_weeks_with_weak_prevented=n_weeks_weak,
        n_complete_weeks=len(complete_weeks),
        b3_entry_blocks=int(_f(m1_all, "n_blocks_on_m0_entries") or 0),
        r2_entry_blocks=int(_f(m2_all, "n_blocks_on_m0_entries") or 0),
        third_candle_net_benefit=third_benefit,
        oos_false_block_rate=_f(m3_oos, "false_block_rate") if len(m3_oos) else None,
        long_short_asymmetry=asymmetry,
    )

    # OOS stability without march: compare FBR all vs oos
    fbr_all = _f(m3_all, "false_block_rate")
    fbr_oos = _f(m3_oos, "false_block_rate") if len(m3_oos) else None
    stable_without_march = True
    if fbr_all is not None and fbr_oos is not None:
        stable_without_march = abs(fbr_oos - fbr_all) <= 0.15 and (
            (_f(m3_oos, "n_weak_blocked") or 0) > 0 or (_f(m3_all, "n_weak_blocked") or 0) == 0
        )

    recommendation = choose_recommendation(
        scenarios=scenarios,
        b3_entry_blocks=int(_f(m1_all, "n_blocks_on_m0_entries") or 0),
        r2_entry_blocks=int(_f(m2_all, "n_blocks_on_m0_entries") or 0),
        r2_false_block_rate=_f(m2_all, "false_block_rate"),
        b3_false_block_rate=_f(m1_all, "false_block_rate"),
        third_candle_benefit=third_benefit,
        stable_without_march=stable_without_march,
        m0_reproduced=True,
    )

    answers = {
        "q1_period": f"{eff_start.isoformat()} -> {eff_end.isoformat()} (available candles {available_start} -> {raw['timestamp'].max()})",
        "q2_complete_weeks": str(len(complete_weeks)),
        "q3_m0_entries_total": str(int(_f(m0_all, "n_entries") or 0)),
        "q4_long_short_entries": (
            f"long={int(_f(m0_all, 'n_long_entries') or 0)}, "
            f"short={int(_f(m0_all, 'n_short_entries') or 0)}"
        ),
        "q5_b3_entry_blocks": str(int(_f(m1_all, "n_blocks_on_m0_entries") or 0)),
        "q6_r2_entry_blocks": str(int(_f(m2_all, "n_blocks_on_m0_entries") or 0)),
        "q7_m3_entry_blocks": str(int(_f(m3_all, "n_blocks_on_m0_entries") or 0)),
        "q8_r2_good_weak_amb": (
            f"good={r2_diag['n_good']}, weak={r2_diag['n_weak']}, ambiguous={r2_diag['n_ambiguous']}"
        ),
        "q9_b3_good_weak_amb": (
            f"good={b3_diag['n_good']}, weak={b3_diag['n_weak']}, ambiguous={b3_diag['n_ambiguous']}"
        ),
        "q10_r2_false_block_rate": str(r2_diag.get("false_block_rate")),
        "q11_b3_false_block_rate": str(b3_diag.get("false_block_rate")),
        "q12_00055_pattern_repeats": (
            f"yes_n={like_55}" if like_55 else "no_additional_long_pa_false_blocks"
            if like_55 == 0
            else f"n={like_55}"
        ),
        "q13_r2_works_in_phases": _json_list(
            [
                r
                for r in phase_rows
                if (r.get("n_weak_blocked_m3") or 0) > (r.get("n_good_blocked_m3") or 0)
            ]
        ),
        "q14_r2_hurts_in_phases": _json_list(
            [
                r
                for r in phase_rows
                if (r.get("n_good_blocked_m3") or 0) > (r.get("n_weak_blocked_m3") or 0)
            ]
        ),
        "q15_b3_measurable_on_entries": (
            "yes" if int(_f(m1_all, "n_blocks_on_m0_entries") or 0) > 0 else "no"
        ),
        "q16_b3_primary_role": (
            "setup_blocker"
            if (b3_diag.get("n_blocks_at_setup", 0) or 0)
            > max(b3_diag.get("n_blocks_at_pa", 0) or 0, b3_diag.get("n_blocks_during_confirm", 0) or 0)
            else "pa_abort"
            if (b3_diag.get("n_blocks_at_pa", 0) or 0)
            >= (b3_diag.get("n_blocks_during_confirm", 0) or 0)
            else "confirmation_abort"
            if int(b3_diag.get("n_b3_blocks_on_m0_entries") or 0) > 0
            else "no_material_entry_path_role"
        ),
        "q17_third_candle_benefit": (
            f"saves_weak={n_third_saves_weak}, delays_good={n_third_delays_good}, "
            f"net={third_benefit}, after2={n_after2}, after3={n_after3}, req3_paths={n_req3}"
        ),
        "q18_avg_entry_delay": (
            float(conf_df["price_shift_pct"].mean()) if len(conf_df) and conf_df["price_shift_pct"].notna().any() else None
        ),
        "q19_good_entries_worsened_by_3c": str(n_third_delays_good > 0),
        "q20_stable_across_weeks": _json_list(stab),
        "q21_holds_without_march": str(stable_without_march),
        "q22_long_short_asymmetry": str(asymmetry),
        "q23_remaining_weak_under_m3": str(len(leak_rows)),
        "q24_rest_problem_layer": (
            pd.Series([r["leak_category"] for r in leak_rows]).value_counts().to_dict()
            if leak_rows
            else {}
        ),
        "q25_decision": recommendation,
        "q26_pipeline_integration_test_ok": recommendation["decision"] == "A",
        "q27_more_research_needed": recommendation["decision"] in {"B", "C", "D", "E"},
    }

    summary = {
        "status": "ok",
        "symbol": args.symbol,
        "effective_start": eff_start.isoformat(),
        "effective_end": eff_end.isoformat(),
        "n_complete_weeks": len(complete_weeks),
        "n_incomplete_weeks": len(incomplete_weeks),
        "pipeline_dir": str(pipeline_dir),
        "b3_csv": str(b3_path),
        "risk_csv": str(r2_path),
        "gate_configs": gate_cfg,
        "march_reproduction": march_summary,
        "variant_comparison_all": m0_all.to_dict(orient="records")
        + m1_all.to_dict(orient="records")
        + m2_all.to_dict(orient="records")
        + m3_all.to_dict(orient="records"),
        "r2_diagnostics": r2_diag,
        "b3_diagnostics": b3_diag,
        "confirmation": {
            "n_after_2": n_after2,
            "n_after_3": n_after3,
            "n_req3": n_req3,
            "n_third_saves_weak": n_third_saves_weak,
            "n_third_delays_good": n_third_delays_good,
            "net_benefit": third_benefit,
        },
        "stability": stab,
        "scenarios": scenarios,
        "recommendation": recommendation,
        "answers": answers,
        "safety": {
            "no_live_changes": True,
            "no_pipeline_csv_mutation": True,
            "no_threshold_optimization": True,
            "no_new_filters": True,
            "gates_enabled_false": True,
            "outcomes_post_hoc_only": True,
            "nothing_committed": True,
        },
    }

    (out / "audit_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2), encoding="utf-8"
    )
    write_readme(summary, out / "README.md")
    return summary


def write_readme(summary: Mapping[str, Any], path: Path) -> None:
    rec = summary.get("recommendation") or {}
    answers = summary.get("answers") or {}
    lines = [
        "# Multi-week pipeline counterfactual validation (M0–M3 / C3)",
        "",
        "Research-only validation of B3 Strong-Trend + R2 Failed-Breakout Risk-Off + adaptive 2/3-candle confirmation.",
        "",
        f"- Status: `{summary.get('status')}`",
        f"- Period: `{summary.get('effective_start')}` → `{summary.get('effective_end')}`",
        f"- Complete weeks: `{summary.get('n_complete_weeks')}`",
        f"- Recommendation: **{rec.get('decision')}** — {rec.get('label')}",
        f"- Reason: {rec.get('reason')}",
        "",
        "## Safety",
        "- No live strategy changes",
        "- No productive pipeline integration",
        "- B3/R2 configs unchanged; `enabled=False`",
        "- Outcomes post-hoc only; market phases post-hoc only",
        "- Nothing committed",
        "",
        "## Answers",
    ]
    for k, v in answers.items():
        lines.append(f"- **{k}**: {v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--range-start", default="2026-01-01T00:00:00+00:00")
    p.add_argument("--range-end", default="2026-05-01T00:00:00+00:00")
    p.add_argument("--pipeline-dir", default=DEFAULT_PIPELINE)
    p.add_argument("--b3-csv", default=DEFAULT_B3)
    p.add_argument("--risk-csv", default=DEFAULT_R2)
    p.add_argument("--march-pipeline-dir", default=DEFAULT_MARCH_PIPELINE)
    p.add_argument("--march-b3-csv", default=DEFAULT_MARCH_B3)
    p.add_argument("--march-risk-csv", default=DEFAULT_MARCH_R2)
    p.add_argument("--output-dir", default=DEFAULT_OUT)
    p.add_argument(
        "--march-only",
        action="store_true",
        help="Only run March reproduction check and exit",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.march_only:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        df, summary = march_week_reproduction(args)
        df.to_csv(out / "march_week_reproduction_check.csv", index=False)
        (out / "audit_summary.json").write_text(
            json.dumps(json_safe({"status": "march_only", "march_reproduction": summary}), indent=2),
            encoding="utf-8",
        )
        print(json.dumps(json_safe(summary), indent=2))
        return 0 if summary.get("all_ok") else 2
    summary = run_audit(args)
    print(json.dumps(json_safe(summary.get("recommendation")), indent=2))
    print("status:", summary.get("status"))
    return 0 if summary.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

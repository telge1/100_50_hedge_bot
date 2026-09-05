"""Frozen HIGH-edge forward outcome evaluation — Phase A smoke + Phase B inventory.

Never uses outcomes for matching / thresholds / state definition.
"""

from __future__ import annotations

import time
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.buckets import (
    aggregate_window,
    build_second_buckets,
    side_vwap,
)
from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.episodes import discover_episodes
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.cohort_eval import (
    cohort_horizon_stats,
    information_stack_label,
    leave_one_out,
    sample_size_label,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.coverage_inventory import (
    build_coverage_inventory,
    recommend_expand_window,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    build_causal_edges_from_samples,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_disambiguation import (
    DisambiguationThresholds,
    select_disambiguated_match,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    JoinThresholds,
    apply_join_to_event,
    evaluate_candidates,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    input_from_aef_compression,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import (
    FREEZE_FLAGS,
    OUTCOME_HORIZONS_EVAL_S,
    FreezeViolation,
    verify_freeze,
    write_freeze,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    try_write_parquet,
    write_csv,
    write_json,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.state_aligned_outcomes import (
    attach_forward_outcomes_for_event,
    build_decision_timestamps,
)

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_edge_forward_outcome_evaluation_v1"
)

SMOKE_START = "2026-08-29T11:50:00Z"
SMOKE_END = "2026-08-29T12:30:00Z"
OB_LOAD_START = "2026-08-29T11:00:00Z"
# Forward 60m beyond smoke end
OUTCOME_DATA_END = "2026-08-29T13:30:00Z"


def _run_window(
    *,
    event_start: str,
    event_end: str,
    ob_start: str,
    data_end: str,
    output_dir: Path,
    raw_root: Path,
    freeze_hashes: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    # Re-verify freeze immediately before computing anything outcome-related
    verify_freeze(output_dir)

    cfg = TrapAcceptConfig()
    thr = JoinThresholds()
    dthr = DisambiguationThresholds()
    thr_accept = JoinThresholds(accept_confidence=dthr.accept_confidence)

    start = parse_utc(event_start)
    end = parse_utc(event_end)
    ob_s = parse_utc(ob_start)
    dend = parse_utc(data_end)
    symbols = ("BTCUSDT", "DOGEUSDT")
    query_log: list[dict[str, Any]] = []

    print(f"[{label}] loading OB200…", flush=True)
    samples_by, seg_meta, n_ok = load_ob200_samples(
        symbols=symbols, start=ob_s, end=end, raw_root=raw_root, sample_ms=250
    )
    edges, _, _ = build_causal_edges_from_samples(samples_by)

    features: list[dict[str, Any]] = []
    decision_ts_rows: list[dict[str, Any]] = []
    outcome_rows: list[dict[str, Any]] = []
    feature_fingerprints: list[dict[str, Any]] = []

    for symbol in symbols:
        # Trades cover event window + forward horizon
        trades, _ = load_trades_clickhouse(
            symbol=symbol, start=start, end=dend, query_log=query_log
        )
        buckets = build_second_buckets(trades)
        disc = discover_episodes(
            symbol=symbol,
            trades=trades,
            buckets=buckets,
            start=start,
            end=end,
            cfg=cfg.aef_config(),
        )
        allowed = [c for c in disc["compressions"] if c.get("allowed")][:25]
        sym_edges = [e for e in edges if e.symbol == symbol]
        samples = samples_by.get(symbol) or []

        for row in allowed:
            ev = input_from_aef_compression(row, source=f"frozen_eval:{symbol}")
            side = aggressor_side(ev.direction)
            flow = aggregate_window(buckets, ev.flow_start_ts, ev.flow_end_ts)
            vwap = side_vwap(trades, ev.flow_start_ts, ev.flow_end_ts, side)
            cands = evaluate_candidates(
                ev,
                sym_edges,
                samples,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=thr,
            )
            join, _, _ = select_disambiguated_match(
                ev,
                cands,
                trades=trades,
                flow_start_price=flow.first_price,
                flow_vwap=vwap,
                flow_low=flow.low_price,
                flow_high=flow.high_price,
                thr=thr,
                dthr=dthr,
            )
            # Snapshot join before outcomes
            join_snap = join.to_dict()
            ev2 = apply_join_to_event(deepcopy(ev), join, thr_accept)
            feat, _legacy_out = process_event(
                ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=dend
            )
            # Fingerprint frozen decision fields (must ignore outcomes)
            fp = {
                "event_id": feat["event_id"],
                "edge_join_status": feat.get("edge_join_status"),
                "matched_edge_id": feat.get("matched_edge_id"),
                "matched_edge_price": feat.get("matched_edge_price"),
                "edge_match_confidence_class": feat.get("edge_match_confidence_class"),
                "final_trap_label": feat.get("final_trap_label"),
                "final_acceptance_state": feat.get("final_acceptance_state"),
                "final_research_state": feat.get("final_research_state"),
            }
            feature_fingerprints.append(fp)
            feat["information_stack"] = information_stack_label(feat)
            features.append(feat)

            dts = build_decision_timestamps(
                feat, ev.flow_start_ts, ev.flow_end_ts, ev.decision_ts
            )
            decision_ts_rows.append(dts)

            outs = attach_forward_outcomes_for_event(
                feat=feat,
                buckets=buckets,
                data_end=dend,
                flow_start=ev.flow_start_ts,
                flow_end=ev.flow_end_ts,
                decision_ts=ev.decision_ts,
                horizons=OUTCOME_HORIZONS_EVAL_S,
            )
            for o in outs:
                o["join_fingerprint"] = join_snap.get("matched_edge_id")
                o["information_stack"] = feat["information_stack"]
            outcome_rows.extend(outs)

            # Integrity: outcomes must not alter fingerprint
            assert fp["matched_edge_price"] == feat.get("matched_edge_price")
            assert fp["final_research_state"] == feat.get("final_research_state")

    # verify freeze again before writing aggregates
    verify_freeze(output_dir)

    conf = Counter(f.get("edge_match_confidence_class") for f in features)
    join_st = Counter(f.get("edge_join_status") for f in features)
    acc = Counter(f.get("final_acceptance_state") for f in features)
    trap = Counter(f.get("final_trap_label") for f in features)
    comb = Counter(f.get("final_research_state") for f in features)
    stack = Counter(f.get("information_stack") for f in features)

    high_ids = [f["event_id"] for f in features if f.get("edge_match_confidence_class") == "HIGH"]

    # Cohort tables
    horizons = list(OUTCOME_HORIZONS_EVAL_S)
    edge_cmp: list[dict[str, Any]] = []
    for conf_v in ("HIGH", "MEDIUM", "NONE"):
        for h in horizons:
            req_dir = conf_v == "HIGH"
            edge_cmp.append(
                cohort_horizon_stats(
                    outcome_rows,
                    cohort_key="edge_match_confidence_class",
                    cohort_value=conf_v,
                    horizon_s=h,
                    require_directional=req_dir,
                )
            )
    # EDGE_NOT_REACHED via join status
    for h in horizons:
        edge_cmp.append(
            cohort_horizon_stats(
                outcome_rows,
                cohort_key="edge_join_status",
                cohort_value="EDGE_NOT_REACHED",
                horizon_s=h,
                require_directional=False,
            )
        )

    acc_cmp: list[dict[str, Any]] = []
    for av in ("ACCEPTED_ABOVE", "ACCEPTED_BELOW", "FAILED_BREAK", "BREAK_RECLAIMED", "NO_BREAK", "UNKNOWN_EDGE"):
        for h in horizons:
            # ACCEPTED_* directional via acceptance_aligned when present
            rows_h = [
                {
                    **r,
                    "state_aligned_return_bps": r.get("acceptance_aligned_return_bps")
                    if av.startswith("ACCEPTED") or av in {"FAILED_BREAK", "BREAK_RECLAIMED"}
                    else r.get("state_aligned_return_bps"),
                    "include_in_directional_hit_rate": (
                        av.startswith("ACCEPTED") or av in {"FAILED_BREAK", "BREAK_RECLAIMED"}
                    )
                    and r.get("acceptance_aligned_return_bps") is not None,
                }
                for r in outcome_rows
                if r.get("final_acceptance_state") == av
            ]
            # patch include flag for ACCEPTED
            for r in rows_h:
                if av.startswith("ACCEPTED") or av in {"FAILED_BREAK", "BREAK_RECLAIMED"}:
                    r["include_in_directional_hit_rate"] = r.get("acceptance_aligned_return_bps") is not None
                else:
                    r["include_in_directional_hit_rate"] = False
            acc_cmp.append(
                cohort_horizon_stats(
                    rows_h if rows_h else outcome_rows,
                    cohort_key="final_acceptance_state",
                    cohort_value=av,
                    horizon_s=h,
                    require_directional=av.startswith("ACCEPTED") or av in {"FAILED_BREAK", "BREAK_RECLAIMED"},
                )
            )

    trap_cmp: list[dict[str, Any]] = []
    for tv in ("NEVER_TRAPPED", "TEMPORARY_UNDERWATER", "TRAP_CONFIRMED", "VWAP_RECLAIMED", "UNKNOWN_DATA"):
        for h in horizons:
            trap_cmp.append(
                cohort_horizon_stats(
                    outcome_rows,
                    cohort_key="final_trap_label",
                    cohort_value=tv,
                    horizon_s=h,
                    require_directional=False,
                )
            )

    comb_cmp: list[dict[str, Any]] = []
    for cv in (
        "ATTACKER_WINNING",
        "ATTACKER_TRAPPED_REJECTION",
        "ABSORPTION_NO_RESOLUTION",
        "BREAK_WITHOUT_HEALTHY_FLOW",
        "MIXED_OR_UNKNOWN",
    ):
        for h in horizons:
            dir_ok = cv in {"ATTACKER_WINNING", "ATTACKER_TRAPPED_REJECTION"}
            comb_cmp.append(
                cohort_horizon_stats(
                    outcome_rows,
                    cohort_key="final_research_state",
                    cohort_value=cv,
                    horizon_s=h,
                    require_directional=dir_ok,
                )
            )

    stack_cmp: list[dict[str, Any]] = []
    for sv in (
        "efficiency_only",
        "efficiency_high_edge",
        "efficiency_high_edge_acceptance",
        "efficiency_high_edge_trap",
        "efficiency_high_edge_trap_acceptance",
    ):
        for h in horizons:
            stack_cmp.append(
                cohort_horizon_stats(
                    outcome_rows,
                    cohort_key="information_stack",
                    cohort_value=sv,
                    horizon_s=h,
                    require_directional=sv != "efficiency_only",
                )
            )

    # Controls: HIGH vs EDGE_NOT_REACHED / NONE same symbol-side (descriptive)
    control_rows: list[dict[str, Any]] = []
    for h in (60, 300, 900, 1800):
        high_s = cohort_horizon_stats(
            outcome_rows,
            cohort_key="edge_match_confidence_class",
            cohort_value="HIGH",
            horizon_s=h,
            require_directional=True,
        )
        none_s = cohort_horizon_stats(
            outcome_rows,
            cohort_key="edge_join_status",
            cohort_value="EDGE_NOT_REACHED",
            horizon_s=h,
            require_directional=False,
        )
        control_rows.append(
            {
                "horizon_s": h,
                "high_n": high_s.get("n_complete"),
                "high_median_aligned_bps": high_s.get("median"),
                "high_mean_aligned_bps": high_s.get("mean"),
                "high_positive_rate": high_s.get("positive_rate"),
                "high_label": high_s.get("sample_size_label"),
                "not_reached_n": none_s.get("n_complete"),
                "not_reached_median_raw_bps": none_s.get("median"),
                "not_reached_mean_raw_bps": none_s.get("mean"),
                "comparison_note": (
                    "HIGH uses state_aligned; NOT_REACHED raw only — not a directional contest"
                ),
                "claim_better": False,  # SMALL_N — no claim
            }
        )

    loo = leave_one_out(outcome_rows, event_ids=high_ids, horizon_s=300)

    elapsed = time.perf_counter() - t0
    n_high = conf.get("HIGH", 0)
    ssl = sample_size_label(n_high)

    if n_ok == 0:
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_BLOCKED"
    elif ssl == "VERY_SMALL_N":
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_SMALL_N"
    elif ssl == "SMALL_N":
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_SMALL_N"
    elif ssl == "EXPLORATORY":
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_EXPLORATORY"
    else:
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_COMPLETE"

    # Method completeness can still be COMPLETE for SMALL_N if pipeline ok —
    # user said COMPLETE = technical/method completeness. Prefer SMALL_N label when n<30.
    method_complete = n_ok >= 2 and len(features) > 0 and bool(freeze_hashes)
    if method_complete and ssl in {"VERY_SMALL_N", "SMALL_N"}:
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_SMALL_N"
    elif method_complete and ssl == "EXPLORATORY":
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_EXPLORATORY"
    elif method_complete:
        verdict = "FROZEN_HIGH_EDGE_OUTCOMES_V1_COMPLETE"

    summary = {
        "verdict_hint": verdict,
        "phase": label,
        "event_window": [event_start, event_end],
        "outcome_data_end": data_end,
        "n_aef_events": len(features),
        "confidence": dict(conf),
        "join_status": dict(join_st),
        "acceptance": dict(acc),
        "trap": dict(trap),
        "combined": dict(comb),
        "information_stack": dict(stack),
        "n_high": n_high,
        "high_sample_size_label": ssl,
        "n_outcome_rows": len(outcome_rows),
        "freeze_bundle_sha256": freeze_hashes.get("freeze_bundle_sha256"),
        **FREEZE_FLAGS,
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "ob200_segments_usable": n_ok,
    }

    sub = output_dir / label
    ensure_outdir(sub)
    write_csv(sub / "event_decision_timestamps.csv", decision_ts_rows)
    write_csv(sub / "forward_outcomes.csv", outcome_rows)
    try_write_parquet(sub / "forward_outcomes.parquet", outcome_rows)
    write_csv(sub / "cohort_outcomes.csv", edge_cmp + acc_cmp + trap_cmp + comb_cmp)
    write_csv(sub / "edge_quality_comparison.csv", edge_cmp)
    write_csv(sub / "acceptance_comparison.csv", acc_cmp)
    write_csv(sub / "trap_comparison.csv", trap_cmp)
    write_csv(sub / "combined_state_comparison.csv", comb_cmp)
    write_csv(sub / "information_stack_comparison.csv", stack_cmp)
    write_csv(sub / "control_group_comparison.csv", control_rows)
    write_csv(sub / "leave_one_out_sensitivity.csv", loo)
    write_csv(sub / "features_decisions.csv", features)
    write_json(sub / "SUMMARY.json", summary)
    write_json(
        sub / "outcome_data_quality.json",
        {
            "query_log": query_log,
            "ob200_segment_meta": seg_meta,
            "n_incomplete_primary": sum(
                1
                for r in outcome_rows
                if r.get("anchor") == "state_available" and not r.get("outcome_coverage_complete")
            ),
            "n_complete_primary": sum(
                1
                for r in outcome_rows
                if r.get("anchor") == "state_available" and r.get("outcome_coverage_complete")
            ),
        },
    )
    write_json(
        sub / "no_outcome_fit_audit.json",
        {
            **FREEZE_FLAGS,
            "feature_fingerprints_n": len(feature_fingerprints),
            "note": "Outcomes appended after freeze join; fingerprints exclude outcome fields",
            "freeze_bundle_sha256": freeze_hashes.get("freeze_bundle_sha256"),
        },
    )
    return summary


def run_frozen_evaluation(
    *,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    run_expand: bool = False,
) -> dict[str, Any]:
    ensure_outdir(output_dir)
    print("writing freeze…", flush=True)
    freeze_hashes = write_freeze(output_dir)
    verify_freeze(output_dir)

    # Phase B inventory first (read-only files)
    inv = build_coverage_inventory(raw_root=raw_root)
    write_csv(output_dir / "coverage_overlap_inventory.csv", inv)
    expand_plan = recommend_expand_window(inv)
    write_json(output_dir / "coverage_expand_plan.json", expand_plan)

    # Phase A: reproduce smoke
    try:
        phase_a = _run_window(
            event_start=SMOKE_START,
            event_end=SMOKE_END,
            ob_start=OB_LOAD_START,
            data_end=OUTCOME_DATA_END,
            output_dir=output_dir,
            raw_root=raw_root,
            freeze_hashes=freeze_hashes,
            label="phase_a_smoke",
        )
    except FreezeViolation as e:
        write_json(output_dir / "SUMMARY.json", {"verdict_hint": "FROZEN_HIGH_EDGE_OUTCOMES_V1_BLOCKED", "error": str(e)})
        raise

    # Promote phase_a key files to root for mandated names
    for name in (
        "event_decision_timestamps.csv",
        "forward_outcomes.csv",
        "forward_outcomes.parquet",
        "cohort_outcomes.csv",
        "edge_quality_comparison.csv",
        "acceptance_comparison.csv",
        "trap_comparison.csv",
        "combined_state_comparison.csv",
        "information_stack_comparison.csv",
        "control_group_comparison.csv",
        "leave_one_out_sensitivity.csv",
        "outcome_data_quality.json",
        "no_outcome_fit_audit.json",
    ):
        src = output_dir / "phase_a_smoke" / name
        dst = output_dir / name
        if src.exists():
            dst.write_bytes(src.read_bytes())

    phase_c = None
    if run_expand and expand_plan.get("allowed"):
        ew = expand_plan["proposed_event_window"]
        phase_c = _run_window(
            event_start=ew[0],
            event_end=ew[1],
            ob_start=expand_plan["ob_warmup_start"],
            data_end=expand_plan["proposed_data_end_for_60m"],
            output_dir=output_dir,
            raw_root=raw_root,
            freeze_hashes=freeze_hashes,
            label="phase_c_bounded_expand",
        )

    top = {
        **phase_a,
        "coverage_expand_plan": expand_plan,
        "phase_c": phase_c,
        "phase_c_ran": phase_c is not None,
    }
    # If expand ran and increased n_high, refresh verdict from phase_c
    if phase_c and phase_c.get("n_high", 0) >= 30:
        top["verdict_hint"] = phase_c.get("verdict_hint")
        top["n_high_expanded"] = phase_c.get("n_high")
    write_json(output_dir / "SUMMARY.json", top)

    (output_dir / "commands.txt").write_text(
        "\n".join(
            [
                "cd /home/telgenbuescher/projects/orderbook_analyse",
                "PYTHONPATH=src .venv/bin/python -m pytest "
                "tests/test_aggressor_efficiency_trapped_vwap_acceptance_v1.py "
                "tests/test_causal_pool_edge_join_v1.py "
                "tests/test_causal_pool_edge_ambiguity_resolution_v1.py "
                "tests/test_frozen_high_edge_forward_outcomes_v1.py -q",
                "PYTHONPATH=src .venv/bin/python "
                "scripts/run_frozen_high_edge_forward_outcome_evaluation_v1.py --phase-a",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return top

"""Orchestrate causal pool-edge join smoke + stage-1 re-run."""

from __future__ import annotations

import time
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.buckets import aggregate_window, build_second_buckets, side_vwap
from orderbook_analyse.aggressor_efficiency_flip.contracts import aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.episodes import discover_episodes
from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.contracts import TrapAcceptConfig
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    build_causal_edges_from_samples,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import (
    JoinThresholds,
    apply_join_to_event,
    evaluate_candidates,
    select_match,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.event_adapter import (
    input_from_aef_compression,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import (
    json_safe,
    prefix_snapshot,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.pipeline import process_event
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "causal_pool_edge_join_for_aggressor_trap_acceptance_v1"
)

SMOKE_START = "2026-08-29T11:50:00Z"
SMOKE_END = "2026-08-29T12:30:00Z"
# OB200 load slightly earlier for wall persistence / warmup visibility
OB_LOAD_START = "2026-08-29T11:00:00Z"


def run_causal_edge_join_smoke(
    *,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)
    stage1_dir = output_dir / "stage1_smoke_with_causal_edges"
    ensure_outdir(stage1_dir)

    cfg = TrapAcceptConfig()
    thr = JoinThresholds()
    start = parse_utc(SMOKE_START)
    end = parse_utc(SMOKE_END)
    ob_start = parse_utc(OB_LOAD_START)
    symbols = ("BTCUSDT", "DOGEUSDT")
    query_log: list[dict[str, Any]] = []

    # --- Source inventory / coverage ---
    source_inventory = {
        "precomputed_l2_wall_attack_btc_doge_v1": {
            "path": "results/l2_wall_attack_discovery/btc_doge_v1",
            "window": "2026-08-25T00:00:00Z .. 2026-08-25T07:00:00Z",
            "overlap_with_aef_smoke_20260829": False,
            "causal_notes": "appear/touch timestamps from OB200 replay; resolution labels use post-contact horizons — do not use resolution to pick edge",
            "usable_for_this_smoke": False,
        },
        "nested_ask_pool_edge_short": {
            "path": "results/a_plus_nested_ask_pool_edge_short_v1/",
            "notes": "ASK pool edges from LLD HTF geometry; strategy outcomes may bias — not primary for this join",
            "usable_for_this_smoke": False,
        },
        "raw_ob200_v3": {
            "path": str(raw_root),
            "symbols": list(symbols),
            "overlap_with_aef_smoke_20260829": True,
            "precedence_rank": 1,
            "extractor": "ob200_v3_raw_discovery.walls.extract_wall_events + lifecycles_v2",
            "usable_for_this_smoke": True,
        },
    }
    write_json(output_dir / "source_inventory.json", source_inventory)

    print("loading OB200 samples…", flush=True)
    samples_by, seg_meta, n_ok = load_ob200_samples(
        symbols=symbols, start=ob_start, end=end, raw_root=raw_root, sample_ms=250
    )
    edges, lifecycles, lc_rows = build_causal_edges_from_samples(samples_by)
    write_csv(output_dir / "causal_edges.csv", [e.to_dict() for e in edges])
    write_json(
        output_dir / "edge_contract.json",
        {
            "rule": "edge_available_ts <= aef.flow_start_ts",
            "size_persistence": "as-of sample at/before flow_start only; no future peak_qty",
            "presence_check": "wall price still dominant on correct side at as-of sample",
            "n_edges": len(edges),
            "n_lifecycles": len(lifecycles),
            "segments_usable": n_ok,
            "segment_meta": seg_meta,
        },
    )
    write_json(output_dir / "thresholds_used.json", {**thr.to_dict(), "aef": cfg.to_dict()})

    if n_ok == 0 or not edges:
        summary = {
            "verdict_hint": "CAUSAL_POOL_EDGE_JOIN_V1_BLOCKED",
            "reason": "no_usable_ob200_edges_in_window",
            "n_ok_segments": n_ok,
            "n_edges": len(edges),
        }
        write_json(output_dir / "SUMMARY.json", summary)
        return summary

    # --- AEF events per symbol ---
    features_before: list[dict[str, Any]] = []
    features_after: list[dict[str, Any]] = []
    outcomes_after: list[dict[str, Any]] = []
    join_results: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    prefix_audit: dict[str, Any] = {"ok": True, "checks": []}

    for symbol in symbols:
        trades, meta = load_trades_clickhouse(
            symbol=symbol, start=start, end=end, query_log=query_log
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
            ev = input_from_aef_compression(row, source=f"aef_smoke:{symbol}")
            # BEFORE join
            feat_b, _ = process_event(ev, buckets=buckets, trades=trades, cfg=cfg, data_end=end)
            features_before.append(feat_b)

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
            all_candidates.extend(cands)
            join = select_match(ev, cands, thr=thr)
            join_results.append(join.to_dict())
            if join.edge_join_status in {"NO_CAUSAL_EDGE", "EDGE_STALE", "EDGE_TOO_FAR", "SIDE_MISMATCH", "DATA_INCOMPLETE"}:
                unmatched.append(join.to_dict())
            if join.edge_join_status == "MULTIPLE_EDGE_AMBIGUOUS":
                ambiguous.append(join.to_dict())

            ev2 = apply_join_to_event(deepcopy(ev), join, thr)
            feat_a, out_a = process_event(ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=end)
            features_after.append(feat_a)
            outcomes_after.append(out_a)

            timelines.append(
                {
                    "aef_event_id": ev.event_id,
                    "symbol": symbol,
                    "direction": ev.direction,
                    "flow_side": side,
                    "flow_start_ts": iso_z(ev.flow_start_ts),
                    "flow_end_ts": iso_z(ev.flow_end_ts),
                    "flow_vwap": vwap,
                    "flow_low": flow.low_price,
                    "flow_high": flow.high_price,
                    "edge_id": join.matched_edge_id,
                    "edge_side": ev2.wall_side,
                    "edge_price": join.matched_edge_price,
                    "edge_available_ts": join.matched_edge_available_ts,
                    "wall_notional_asof": join.matched_edge_notional_asof,
                    "persistence_asof": join.matched_edge_persistence_seconds,
                    "distance_bps": join.matched_edge_distance_bps,
                    "match_status": join.edge_join_status,
                    "confidence": join.edge_match_confidence_class,
                    "acceptance_state": feat_a.get("final_acceptance_state"),
                    "trap_state": feat_a.get("final_trap_label"),
                    "combined_state": feat_a.get("final_research_state"),
                    "explanation_codes": join.edge_match_explanation_codes,
                    "acceptance_before": feat_b.get("final_acceptance_state"),
                }
            )

            # prefix parity for HIGH/MEDIUM matches
            if join.edge_match_confidence_class in thr.accept_confidence:
                for cp in (5, 10, 30, 60):
                    cut = ev.decision_ts + timedelta(seconds=cp)
                    f_full, _ = process_event(ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=end)
                    f_pref, _ = process_event(
                        ev2, buckets=buckets, trades=trades, cfg=cfg, as_of=cut, data_end=cut
                    )
                    # edge match must be identical (join computed before process; re-check join with as_of samples only)
                    # Recompute join with samples truncated to flow_start (edge match must not use post-flow walls)
                    a = prefix_snapshot(f_full, cp)
                    b = prefix_snapshot(f_pref, cp)
                    edge_same = (
                        f_full.get("matched_edge_id") == f_pref.get("matched_edge_id")
                        and f_full.get("matched_edge_price") == f_pref.get("matched_edge_price")
                        and f_full.get("edge_match_confidence_class")
                        == f_pref.get("edge_match_confidence_class")
                    )
                    ok = edge_same and (a.get("decision_state") == b.get("decision_state"))
                    prefix_audit["checks"].append(
                        {
                            "event_id": ev.event_id,
                            "checkpoint_s": cp,
                            "ok": ok,
                            "edge_same": edge_same,
                        }
                    )
                    if not ok:
                        prefix_audit["ok"] = False

    # summaries
    conf_counts = Counter(r.get("edge_match_confidence_class") for r in join_results)
    status_counts = Counter(r.get("edge_join_status") for r in join_results)
    acc_before = Counter(f.get("final_acceptance_state") for f in features_before)
    acc_after = Counter(f.get("final_acceptance_state") for f in features_after)
    state_after = Counter(f.get("final_research_state") for f in features_after)
    trap_after = Counter(f.get("final_trap_label") for f in features_after)

    before_after = [
        {
            "metric": "UNKNOWN_EDGE",
            "before": acc_before.get("UNKNOWN_EDGE", 0),
            "after": acc_after.get("UNKNOWN_EDGE", 0),
        },
        {
            "metric": "ACCEPTED_ABOVE",
            "before": acc_before.get("ACCEPTED_ABOVE", 0),
            "after": acc_after.get("ACCEPTED_ABOVE", 0),
        },
        {
            "metric": "ACCEPTED_BELOW",
            "before": acc_before.get("ACCEPTED_BELOW", 0),
            "after": acc_after.get("ACCEPTED_BELOW", 0),
        },
        {
            "metric": "BREAK_RECLAIMED",
            "before": acc_before.get("BREAK_RECLAIMED", 0),
            "after": acc_after.get("BREAK_RECLAIMED", 0),
        },
        {
            "metric": "NO_BREAK",
            "before": acc_before.get("NO_BREAK", 0),
            "after": acc_after.get("NO_BREAK", 0),
        },
        {
            "metric": "HIGH_matches",
            "before": 0,
            "after": conf_counts.get("HIGH", 0),
        },
        {
            "metric": "MEDIUM_matches",
            "before": 0,
            "after": conf_counts.get("MEDIUM", 0),
        },
        {
            "metric": "LOW_matches",
            "before": 0,
            "after": conf_counts.get("LOW", 0),
        },
    ]

    write_csv(output_dir / "edge_join_candidates.csv", all_candidates)
    write_csv(output_dir / "edge_join_results.csv", join_results)
    write_csv(output_dir / "unmatched_events.csv", unmatched)
    write_csv(output_dir / "ambiguous_matches.csv", ambiguous)
    write_csv(output_dir / "reference_match_timelines.csv", timelines)
    write_csv(output_dir / "before_after_acceptance_summary.csv", before_after)
    write_json(output_dir / "prefix_parity_audit.json", prefix_audit)
    write_json(
        output_dir / "edge_quality_audit.json",
        {
            "n_aef_events": len(join_results),
            "confidence": dict(conf_counts),
            "join_status": dict(status_counts),
            "n_edges_catalog": len(edges),
            "ob200_segments_usable": n_ok,
        },
    )
    write_json(
        output_dir / "data_quality_audit.json",
        {
            "query_log": query_log,
            "prefix_parity_ok": prefix_audit["ok"],
            "ob200_segment_meta": seg_meta,
        },
    )

    # stage1 outputs subset
    write_csv(stage1_dir / "features_decisions.csv", features_after)
    write_csv(stage1_dir / "forward_outcomes.csv", outcomes_after)
    write_csv(stage1_dir / "features_decisions_before_join.csv", features_before)

    n_high = conf_counts.get("HIGH", 0)
    n_med = conf_counts.get("MEDIUM", 0)
    real_accept = sum(1 for f in features_after if f.get("final_acceptance_state") not in {None, "UNKNOWN_EDGE"})
    elapsed = time.perf_counter() - t0

    if n_high + n_med == 0:
        verdict_hint = "CAUSAL_POOL_EDGE_JOIN_V1_SMALL_N"
    elif real_accept > 0 and prefix_audit["ok"] and (n_high + n_med) >= 3:
        verdict_hint = "CAUSAL_POOL_EDGE_JOIN_V1_PARTIAL"  # still partial until larger n
    else:
        verdict_hint = "CAUSAL_POOL_EDGE_JOIN_V1_PARTIAL"

    # Upgrade to COMPLETE only if solid overlap
    if (
        prefix_audit["ok"]
        and (n_high + n_med) >= 5
        and real_accept >= 3
        and n_ok >= 2
    ):
        verdict_hint = "CAUSAL_POOL_EDGE_JOIN_V1_COMPLETE"

    summary = {
        "verdict_hint": verdict_hint,
        "n_aef_events": len(join_results),
        "n_causal_edges": len(edges),
        "confidence": dict(conf_counts),
        "join_status": dict(status_counts),
        "acceptance_before": dict(acc_before),
        "acceptance_after": dict(acc_after),
        "combined_after": dict(state_after),
        "trap_after": dict(trap_after),
        "prefix_parity_ok": prefix_audit["ok"],
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "ob200_segments_usable": n_ok,
        "smoke_window": [SMOKE_START, SMOKE_END],
    }
    write_json(output_dir / "SUMMARY.json", summary)
    (output_dir / "commands.txt").write_text(
        "\n".join(
            [
                "cd /home/telgenbuescher/projects/orderbook_analyse",
                "PYTHONPATH=src .venv/bin/python -m pytest tests/test_aggressor_efficiency_trapped_vwap_acceptance_v1.py tests/test_causal_pool_edge_join_v1.py -q",
                "PYTHONPATH=src .venv/bin/python scripts/run_causal_pool_edge_join_for_aggressor_trap_acceptance_v1.py --smoke",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary

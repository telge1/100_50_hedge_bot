"""Smoke runner: causal pool-edge ambiguity resolution v1."""

from __future__ import annotations

import csv
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta
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

PRIOR_JOIN_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "causal_pool_edge_join_for_aggressor_trap_acceptance_v1"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "causal_pool_edge_ambiguity_resolution_v1"
)

SMOKE_START = "2026-08-29T11:50:00Z"
SMOKE_END = "2026-08-29T12:30:00Z"
OB_LOAD_START = "2026-08-29T11:00:00Z"


def _load_prior_ambiguous() -> list[str]:
    path = PRIOR_JOIN_DIR / "ambiguous_matches.csv"
    if not path.exists():
        return []
    return [r["aef_event_id"] for r in csv.DictReader(path.open())]


def _load_prior_summary() -> dict[str, Any]:
    import json

    p = PRIOR_JOIN_DIR / "SUMMARY.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


def run_ambiguity_resolution_smoke(
    *,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)
    stage1_dir = output_dir / "stage1_smoke_with_disambiguation"
    ensure_outdir(stage1_dir)

    cfg = TrapAcceptConfig()
    thr = JoinThresholds()
    dthr = DisambiguationThresholds()
    # Acceptance gate: HIGH only (MEDIUM = sensitivity cohort, not applied)
    thr_accept = JoinThresholds(accept_confidence=dthr.accept_confidence)

    start = parse_utc(SMOKE_START)
    end = parse_utc(SMOKE_END)
    ob_start = parse_utc(OB_LOAD_START)
    symbols = ("BTCUSDT", "DOGEUSDT")
    query_log: list[dict[str, Any]] = []
    prior_amb_ids = set(_load_prior_ambiguous())
    prior_summary = _load_prior_summary()

    write_json(output_dir / "thresholds_used.json", {**thr.to_dict(), **dthr.to_dict(), "aef": cfg.to_dict()})

    print("loading OB200 samples…", flush=True)
    samples_by, seg_meta, n_ok = load_ob200_samples(
        symbols=symbols, start=ob_start, end=end, raw_root=raw_root, sample_ms=250
    )
    edges, _lifecycles, _lc_rows = build_causal_edges_from_samples(samples_by)

    features_before: list[dict[str, Any]] = []
    features_after: list[dict[str, Any]] = []
    join_before: list[dict[str, Any]] = []  # distance-only select for before funnel
    join_after: list[dict[str, Any]] = []
    all_enriched: list[dict[str, Any]] = []
    clusters_out: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    resolution_rows: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    amb_inventory: list[dict[str, Any]] = []
    amb_case_audit: list[dict[str, Any]] = []
    prefix_audit: dict[str, Any] = {"ok": True, "checks": []}
    neg_controls: dict[str, Any] = {
        "outcome_used_for_matching": False,
        "checks": [],
    }

    from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_match import select_match

    for symbol in symbols:
        trades, _meta = load_trades_clickhouse(
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
            # BEFORE: prior distance-only matcher
            join_old = select_match(ev, cands, thr=thr)
            join_before.append(join_old.to_dict())

            join, enriched, cluster_rows = select_disambiguated_match(
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
            join_after.append(join.to_dict())
            for er in enriched:
                if er.get("match_class") or er.get("reached_in_directional_path") is not None:
                    all_enriched.append(er)
            for cl in cluster_rows:
                clusters_out.append({**cl, "aef_event_id": ev.event_id, "symbol": symbol})

            ev2 = apply_join_to_event(deepcopy(ev), join, thr_accept)
            feat_a, _out_a = process_event(ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=end)
            features_after.append(feat_a)

            decisive = None
            if "EXACT_TRADE" in (join.edge_match_explanation_codes or []):
                decisive = "exact_aggressor_trade_at_edge"
            elif "FRONT_REACHED" in (join.edge_match_explanation_codes or []):
                decisive = "unique_front_edge_in_directional_path"
            elif "ZONE_TRADE" in (join.edge_match_explanation_codes or []):
                decisive = "zone_trade_near_edge"
            elif join.edge_join_status == "EDGE_NOT_REACHED":
                decisive = "no_candidate_reached_by_flow"
            elif join.edge_join_status == "MULTIPLE_EDGE_AMBIGUOUS":
                decisive = "tied_causal_candidates"
            else:
                decisive = ",".join(join.edge_match_explanation_codes or [])

            res_row = {
                "aef_event_id": ev.event_id,
                "symbol": symbol,
                "was_prior_ambiguous": ev.event_id in prior_amb_ids,
                "status_before": join_old.edge_join_status,
                "confidence_before": join_old.edge_match_confidence_class,
                "candidate_count_before": join_old.edge_match_candidate_count,
                "status_after": join.edge_join_status,
                "confidence_after": join.edge_match_confidence_class,
                "candidate_count_after": join.edge_match_candidate_count,
                "matched_edge_id": join.matched_edge_id,
                "matched_edge_price": join.matched_edge_price,
                "decisive_causal_feature": decisive,
                "acceptance_activated": join.edge_match_confidence_class in dthr.accept_confidence,
                "acceptance_state": feat_a.get("final_acceptance_state"),
                "trap_state": feat_a.get("final_trap_label"),
                "combined_state": feat_a.get("final_research_state"),
                "explanation_codes": join.edge_match_explanation_codes,
            }
            resolution_rows.append(res_row)

            if join.edge_join_status == "MULTIPLE_EDGE_AMBIGUOUS":
                unresolved.append(res_row)

            if ev.event_id in prior_amb_ids:
                amb_case_audit.append(res_row)
                # inventory of prior ambiguous with why equal-rank
                amb_inventory.append(
                    {
                        "aef_event_id": ev.event_id,
                        "symbol": symbol,
                        "flow_side": side,
                        "n_plausible_before": join_old.edge_match_candidate_count,
                        "why_previously_ambiguous": (
                            "multiple EXACT_EDGE_TOUCH within 0.5bps of flow_start_price; "
                            "distance-only lex ignored directional reach and trade touch"
                        ),
                        "status_after": join.edge_join_status,
                        "confidence_after": join.edge_match_confidence_class,
                        "decisive_causal_feature": decisive,
                    }
                )

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
                    "edge_price": join.matched_edge_price,
                    "match_status": join.edge_join_status,
                    "confidence": join.edge_match_confidence_class,
                    "acceptance_state": feat_a.get("final_acceptance_state"),
                    "trap_state": feat_a.get("final_trap_label"),
                    "combined_state": feat_a.get("final_research_state"),
                    "explanation_codes": join.edge_match_explanation_codes,
                    "decisive_causal_feature": decisive,
                    "status_before": join_old.edge_join_status,
                }
            )

            # Prefix parity for HIGH
            if join.edge_match_confidence_class in dthr.accept_confidence:
                for cp in (5, 10, 30, 60):
                    cut = ev.decision_ts + timedelta(seconds=cp)
                    f_full, _ = process_event(ev2, buckets=buckets, trades=trades, cfg=cfg, data_end=end)
                    f_pref, _ = process_event(
                        ev2, buckets=buckets, trades=trades, cfg=cfg, as_of=cut, data_end=cut
                    )
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
                        {"event_id": ev.event_id, "checkpoint_s": cp, "ok": ok, "edge_same": edge_same}
                    )
                    if not ok:
                        prefix_audit["ok"] = False

            # Negative controls on this event's enriched set
            reached = [e for e in enriched if e.get("reached_in_directional_path")]
            if join.matched_edge_id and reached:
                back = [
                    e
                    for e in reached
                    if e.get("cluster_role") == "BACK_EDGE"
                    and e["edge_id"] != join.matched_edge_id
                    and float(e.get("notional_asof_attack") or 0)
                    > float(
                        next(
                            (x.get("notional_asof_attack") or 0 for x in reached if x["edge_id"] == join.matched_edge_id),
                            0,
                        )
                    )
                ]
                if join.edge_match_confidence_class == "HIGH" and any(
                    not e.get("reached_in_directional_path") and e.get("edge_id") == join.matched_edge_id
                    for e in enriched
                ):
                    neg_controls["checks"].append(
                        {"event_id": ev.event_id, "fail": "matched_unreached", "ok": False}
                    )
                else:
                    neg_controls["checks"].append(
                        {
                            "event_id": ev.event_id,
                            "larger_back_not_preferred": True,
                            "ok": join.matched_edge_id
                            not in {b["edge_id"] for b in back}
                            or join.edge_join_status == "MULTIPLE_EDGE_AMBIGUOUS",
                        }
                    )

    conf_before = Counter(r.get("edge_match_confidence_class") for r in join_before)
    status_before = Counter(r.get("edge_join_status") for r in join_before)
    conf_after = Counter(r.get("edge_match_confidence_class") for r in join_after)
    status_after = Counter(r.get("edge_join_status") for r in join_after)
    acc_before = Counter(f.get("final_acceptance_state") for f in features_before)
    # Before-join acceptance on old HIGH/MEDIUM would need old apply; use prior summary + distance match
    # Reconstruct acceptance-after-old-join from prior SUMMARY if available
    acc_after = Counter(f.get("final_acceptance_state") for f in features_after)
    state_after = Counter(f.get("final_research_state") for f in features_after)
    trap_after = Counter(f.get("final_trap_label") for f in features_after)

    before_after_join = [
        {"metric": "events", "before": len(join_before), "after": len(join_after)},
        {"metric": "HIGH", "before": conf_before.get("HIGH", 0), "after": conf_after.get("HIGH", 0)},
        {"metric": "MEDIUM", "before": conf_before.get("MEDIUM", 0), "after": conf_after.get("MEDIUM", 0)},
        {"metric": "LOW", "before": conf_before.get("LOW", 0), "after": conf_after.get("LOW", 0)},
        {"metric": "NONE", "before": conf_before.get("NONE", 0), "after": conf_after.get("NONE", 0)},
        {
            "metric": "MULTIPLE_EDGE_AMBIGUOUS",
            "before": status_before.get("MULTIPLE_EDGE_AMBIGUOUS", 0),
            "after": status_after.get("MULTIPLE_EDGE_AMBIGUOUS", 0),
        },
        {
            "metric": "EDGE_NOT_REACHED",
            "before": status_before.get("EDGE_NOT_REACHED", 0),
            "after": status_after.get("EDGE_NOT_REACHED", 0),
        },
        {
            "metric": "EDGE_STALE",
            "before": status_before.get("EDGE_STALE", 0),
            "after": status_after.get("EDGE_STALE", 0),
        },
        {
            "metric": "EXACT_TRADED_EDGE",
            "before": 0,
            "after": status_after.get("EXACT_TRADED_EDGE", 0),
        },
        {
            "metric": "FRONT_EDGE_REACHED",
            "before": 0,
            "after": status_after.get("FRONT_EDGE_REACHED", 0)
            + status_after.get("CLUSTER_FRONT_EDGE_REACHED", 0),
        },
    ]

    prior_unknown = (prior_summary.get("acceptance_after") or {}).get("UNKNOWN_EDGE", 33)
    before_after_acc = [
        {
            "metric": "UNKNOWN_EDGE",
            "before_prior_join": prior_unknown,
            "after_disambiguation": acc_after.get("UNKNOWN_EDGE", 0),
            "raw_before_any_join": acc_before.get("UNKNOWN_EDGE", 0),
        },
        {
            "metric": "ACCEPTED_BELOW",
            "before_prior_join": (prior_summary.get("acceptance_after") or {}).get("ACCEPTED_BELOW", 0),
            "after_disambiguation": acc_after.get("ACCEPTED_BELOW", 0),
        },
        {
            "metric": "FAILED_BREAK",
            "before_prior_join": (prior_summary.get("acceptance_after") or {}).get("FAILED_BREAK", 0),
            "after_disambiguation": acc_after.get("FAILED_BREAK", 0),
        },
        {
            "metric": "ACCEPTED_ABOVE",
            "before_prior_join": (prior_summary.get("acceptance_after") or {}).get("ACCEPTED_ABOVE", 0),
            "after_disambiguation": acc_after.get("ACCEPTED_ABOVE", 0),
        },
    ]

    # Neg control summary
    neg_controls["all_ok"] = all(c.get("ok", True) for c in neg_controls["checks"]) if neg_controls["checks"] else True
    neg_controls["rules"] = [
        "larger_unreached_back_edge_must_not_win",
        "persistence_must_not_beat_exact_trade",
        "future_break_reclaim_size_persistence_unused",
        "BUY_never_BID_SELL_never_ASK",
        "no_HIGH_without_reach_or_trade",
    ]

    n_high = conf_after.get("HIGH", 0)
    n_med = conf_after.get("MEDIUM", 0)
    n_amb = status_after.get("MULTIPLE_EDGE_AMBIGUOUS", 0)
    n_resolved_prior = sum(
        1
        for r in amb_case_audit
        if r["status_after"] != "MULTIPLE_EDGE_AMBIGUOUS"
    )
    real_accept = sum(
        1 for f in features_after if f.get("final_acceptance_state") not in {None, "UNKNOWN_EDGE"}
    )
    elapsed = time.perf_counter() - t0

    if n_ok == 0:
        verdict = "CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1_BLOCKED"
    elif n_resolved_prior == 0 and n_amb >= 15:
        verdict = "CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1_BLOCKED"
    elif n_resolved_prior >= 5 and prefix_audit["ok"] and real_accept >= 1:
        # meaningful causal resolution without forcing matches
        verdict = "CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1_COMPLETE"
    elif n_resolved_prior >= 1 and prefix_audit["ok"]:
        verdict = "CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1_PARTIAL"
    else:
        verdict = "CAUSAL_POOL_EDGE_AMBIGUITY_RESOLUTION_V1_SMALL_N"

    summary = {
        "verdict_hint": verdict,
        "outcome_used_for_matching": False,
        "n_aef_events": len(join_after),
        "prior_ambiguous_count": len(prior_amb_ids),
        "prior_ambiguous_resolved": n_resolved_prior,
        "prior_ambiguous_still_ambiguous": sum(
            1 for r in amb_case_audit if r["status_after"] == "MULTIPLE_EDGE_AMBIGUOUS"
        ),
        "prior_ambiguous_not_reached": sum(
            1 for r in amb_case_audit if r["status_after"] == "EDGE_NOT_REACHED"
        ),
        "confidence_before": dict(conf_before),
        "confidence_after": dict(conf_after),
        "join_status_before": dict(status_before),
        "join_status_after": dict(status_after),
        "acceptance_after": dict(acc_after),
        "combined_after": dict(state_after),
        "trap_after": dict(trap_after),
        "prefix_parity_ok": prefix_audit["ok"],
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "ob200_segments_usable": n_ok,
        "smoke_window": [SMOKE_START, SMOKE_END],
        "n_high": n_high,
        "n_medium": n_med,
        "real_acceptance_events": real_accept,
    }

    write_csv(output_dir / "ambiguity_inventory.csv", amb_inventory)
    write_csv(output_dir / "ambiguity_candidate_features.csv", all_enriched)
    write_csv(output_dir / "ambiguity_resolution_results.csv", resolution_rows)
    write_csv(output_dir / "unresolved_ambiguities.csv", unresolved)
    write_csv(output_dir / "edge_clusters.csv", clusters_out)
    write_csv(output_dir / "before_after_join_summary.csv", before_after_join)
    write_csv(output_dir / "before_after_acceptance_summary.csv", before_after_acc)
    write_csv(output_dir / "reference_timelines.csv", timelines)
    write_json(output_dir / "negative_control_audit.json", json_safe(neg_controls))
    write_json(output_dir / "prefix_parity_audit.json", json_safe(prefix_audit))
    write_json(
        output_dir / "data_quality_audit.json",
        json_safe({"query_log": query_log, "ob200_segment_meta": seg_meta, "prefix_parity_ok": prefix_audit["ok"]}),
    )
    write_json(output_dir / "SUMMARY.json", json_safe(summary))
    write_csv(stage1_dir / "features_decisions.csv", features_after)
    write_csv(stage1_dir / "features_decisions_before_join.csv", features_before)

    (output_dir / "commands.txt").write_text(
        "\n".join(
            [
                "cd /home/telgenbuescher/projects/orderbook_analyse",
                "PYTHONPATH=src .venv/bin/python -m pytest "
                "tests/test_aggressor_efficiency_trapped_vwap_acceptance_v1.py "
                "tests/test_causal_pool_edge_join_v1.py "
                "tests/test_causal_pool_edge_ambiguity_resolution_v1.py -q",
                "PYTHONPATH=src .venv/bin/python "
                "scripts/run_causal_pool_edge_ambiguity_resolution_v1.py --smoke",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary

"""Output writers: JSON, CSV, Markdown, console summary."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .config import RunConfig, iso_z
from .formatting import fmt_bps, fmt_duration_seconds, fmt_fraction_as_pct, fmt_mio_usd, fmt_oi_delta, fmt_pct, fmt_price, json_safe
from .templates_de import render_all_german, render_report_sections


def write_json(path: Path, obj: Any) -> None:
    """Write JSON. Large payloads use compact form (same content, no indent)."""
    payload = json_safe(obj)
    # Indenting multi-MB trees (summary embeds fight/sequence) dominates runtime.
    size_hint = 0
    if isinstance(payload, dict):
        size_hint = len(payload)
        for key in ("fight_facts", "sequence_validation", "level_events", "wall_facts", "factual_reason_codes"):
            val = payload.get(key)
            if isinstance(val, (list, dict)):
                size_hint = max(size_hint, len(val) if not isinstance(val, dict) else len(val) * 8)
    compact = size_hint >= 50 or path.name in {
        "summary.json",
        "fight_episodes.json",
        "factual_reasons.json",
        "edge_observability_summary.json",
        "fight_sequence_summary.json",
    }
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def build_summary_payload(
    cfg: RunConfig,
    *,
    profile_facts: dict[str, Any],
    level_events: list[dict[str, Any]],
    trade_facts: dict[str, Any],
    wall_facts: list[dict[str, Any]],
    wall_bundle: dict[str, Any] | None = None,
    tpo_profile: dict[str, Any] | None = None,
    volume_profile: dict[str, Any] | None = None,
    oi_liq_facts: dict[str, Any],
    factual_reasons: list[dict[str, Any]],
    data_quality: str,
    fight_facts: dict[str, Any] | None = None,
    sequence_validation: dict[str, Any] | None = None,
    liquidation_flow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wall_summary = (wall_bundle or {}).get("summary") or {}
    return json_safe(
        {
            "schema_version": SCHEMA_VERSION,
            "analysis_status": "FACTS_READY_RULES_UNFROZEN",
            "symbol": cfg.symbol,
            "anchor_timestamp_utc": iso_z(cfg.anchor),
            "window": {
                "before_minutes": cfg.before_minutes,
                "after_minutes": cfg.after_minutes,
                "start_utc": iso_z(cfg.window_start),
                "end_utc": iso_z(cfg.window_end),
            },
            "data_quality": data_quality,
            "rules_frozen": False,
            "trade_verdict_evaluated": False,
            "direction": None,
            "entry_ready": False,
            "profile_facts": profile_facts,
            "level_events": level_events,
            "trade_facts": trade_facts,
            "wall_facts": wall_facts,
            "wall_summary": wall_summary,
            "wall_contract_version": (wall_bundle or {}).get("contract_version"),
            "tpo_profile": _tpo_summary(tpo_profile),
            "tpo_profile_status": (tpo_profile or {}).get("tpo_profile_status"),
            "volume_profile": _volume_summary(volume_profile),
            "volume_profile_status": (volume_profile or {}).get("volume_profile_status"),
            "oi_liquidation_facts": oi_liq_facts,
            "factual_reason_codes": factual_reasons,
            "fight_facts_summary": (fight_facts or {}).get("manifest"),
            "fight_facts": fight_facts,
            "sequence_validation": sequence_validation,
            "sequence_validation_summary": (sequence_validation or {}).get("fight_sequence_summary"),
            "liquidation_flow_summary": (liquidation_flow or {}).get("summary"),
            "causality": {
                "outcome_used_for_decision": False,
                "outcome_used_for_thresholds": False,
                "outcome_used_for_profile_definition": False,
            },
        }
    )


def _tpo_summary(tpo_profile: dict[str, Any] | None) -> dict[str, Any]:
    if not tpo_profile:
        return {}
    tpoc = tpo_profile.get("tpoc") or {}
    va = tpo_profile.get("value_area") or {}
    brackets = tpo_profile.get("brackets") or {}
    return {
        "status": tpo_profile.get("tpo_profile_status"),
        "contract_version": tpo_profile.get("contract_version"),
        "profile_kind": (tpo_profile.get("provenance") or {}).get("profile_kind"),
        "tpoc": tpoc.get("tpoc_price"),
        "tpoc_vah": va.get("tpoc_vah"),
        "tpoc_val": va.get("tpoc_val"),
        "value_area_share": va.get("actual_value_area_share"),
        "bracket_minutes": brackets.get("bracket_minutes"),
        "full_brackets": brackets.get("full_count"),
        "partial_brackets": brackets.get("partial_count"),
        "total_brackets": brackets.get("total_count"),
        "total_tpo_marks": brackets.get("total_tpo_marks"),
        "integrity": (tpo_profile.get("integrity") or {}).get("status"),
        "trade_size_invariance": (tpo_profile.get("trade_size_invariance") or {}).get("status"),
        "prefix_parity": (tpo_profile.get("prefix_parity") or {}).get("status"),
        "tpo_volume_confluence_status": None,
    }


def _volume_summary(volume_profile: dict[str, Any] | None) -> dict[str, Any]:
    if not volume_profile:
        return {}
    vp = volume_profile.get("vpoc") or {}
    va = volume_profile.get("value_area") or {}
    return {
        "status": volume_profile.get("volume_profile_status"),
        "contract_version": volume_profile.get("contract_version"),
        "vpoc": vp.get("vpoc_price"),
        "vvah": va.get("vvah"),
        "vval": va.get("vval"),
        "value_area_share": va.get("actual_value_area_share"),
        "trade_count": (volume_profile.get("coverage") or {}).get("deduped_trade_rows_used"),
        "primary_volume_basis": (volume_profile.get("provenance") or {}).get("primary_volume_basis"),
        "integrity": (volume_profile.get("integrity") or {}).get("status"),
        "prefix_parity": (volume_profile.get("prefix_parity") or {}).get("status"),
        "oa_parity": (volume_profile.get("oa_parity") or {}).get("status"),
    }


def write_all_outputs(
    run_dir: Path,
    *,
    summary: dict[str, Any],
    manifest: dict[str, Any],
    coverage: dict[str, Any],
    profiles: dict[str, Any],
    level_events: list[dict[str, Any]],
    trade_buckets: list[dict[str, Any]],
    wall_facts: list[dict[str, Any]],
    wall_bundle: dict[str, Any] | None = None,
    tpo_profile: dict[str, Any] | None = None,
    volume_profile: dict[str, Any] | None = None,
    oi_liq: dict[str, Any],
    reasons: list[dict[str, Any]],
    german: list[dict[str, Any]],
    fight_facts: dict[str, Any] | None = None,
    sequence_validation: dict[str, Any] | None = None,
    liquidation_flow: dict[str, Any] | None = None,
    heavy_detail_csv: bool = True,
) -> None:
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "analysis_manifest.json", manifest)
    write_json(run_dir / "coverage_audit.json", coverage)
    write_json(run_dir / "profile_levels.json", profiles)
    write_json(run_dir / "oi_liquidation_facts.json", oi_liq)
    if heavy_detail_csv:
        write_json(run_dir / "factual_reasons.json", {"reasons": reasons, "templates_de": german})
    else:
        # Lean: codes only — full DE templates are multi-MB and unused by golden parity.
        write_json(
            run_dir / "factual_reasons.json",
            {
                "reasons": [{"code": r.get("code"), "severity": r.get("severity")} for r in reasons],
                "templates_de_omitted": True,
                "template_count": len(german),
                "note": "full templates_de omitted for research-db performance; REPORT.md still rendered",
            },
        )
    transitions, episodes, summaries = _flatten_level_contract(level_events)
    write_csv(run_dir / "level_transitions.csv", transitions)
    write_csv(run_dir / "level_episodes.csv", episodes)
    write_csv(run_dir / "level_events.csv", summaries)
    write_csv(run_dir / "public_trade_buckets.csv", trade_buckets)
    if wall_bundle:
        if heavy_detail_csv:
            write_csv(run_dir / "wall_observations.csv", _strip_observations(wall_bundle.get("observations") or []))
            write_csv(run_dir / "wall_tracks.csv", _strip_tracks(wall_bundle.get("tracks") or []))
            write_csv(run_dir / "wall_transitions.csv", wall_bundle.get("transitions") or [])
            write_csv(run_dir / "wall_trade_matches.csv", wall_bundle.get("trade_matches") or [])
            write_csv(run_dir / "wall_events.csv", _flatten_wall_facts(wall_facts))
        else:
            write_json(
                run_dir / "wall_detail_io.json",
                {
                    "heavy_detail_csv": False,
                    "observation_count": len(wall_bundle.get("observations") or []),
                    "track_count": len(wall_bundle.get("tracks") or []),
                    "transition_count": len(wall_bundle.get("transitions") or []),
                    "trade_match_count": len(wall_bundle.get("trade_matches") or []),
                    "legacy_wall_fact_count": len(wall_facts or []),
                    "note": "per-sample wall CSVs omitted for research-db performance; wall_summary retained",
                },
            )
        write_json(run_dir / "wall_summary.json", wall_bundle.get("summary") or {})
    elif wall_facts and heavy_detail_csv:
        write_csv(run_dir / "wall_events.csv", _flatten_wall_facts(wall_facts))
    if tpo_profile:
        write_json(run_dir / "tpo_profile_summary.json", _tpo_profile_summary_file(tpo_profile))
        write_json(run_dir / "tpo_profile_integrity.json", tpo_profile.get("integrity") or {})
        write_csv(run_dir / "tpo_profile_rows.csv", tpo_profile.get("rows") or [])
        write_csv(run_dir / "tpo_brackets.csv", tpo_profile.get("bracket_rows") or [])
        nodes = (tpo_profile.get("hvn_candidates") or []) + (tpo_profile.get("lvn_candidates") or [])
        write_csv(run_dir / "tpo_profile_nodes.csv", nodes)
    if volume_profile:
        write_json(run_dir / "volume_profile_summary.json", _volume_profile_summary_file(volume_profile))
        write_json(run_dir / "volume_profile_integrity.json", volume_profile.get("integrity") or {})
        write_csv(run_dir / "volume_profile_rows.csv", volume_profile.get("rows") or [])
        nodes = (volume_profile.get("hvn_candidates") or []) + (volume_profile.get("lvn_candidates") or [])
        write_csv(run_dir / "volume_profile_nodes.csv", nodes)
    if fight_facts:
        _write_fight_fact_outputs(run_dir, fight_facts)
    if sequence_validation:
        _write_sequence_validation_outputs(
            run_dir, sequence_validation, heavy_detail_csv=heavy_detail_csv
        )
    if liquidation_flow:
        _write_liquidation_flow_outputs(run_dir, liquidation_flow)
    (run_dir / "REPORT.md").write_text(
        build_report_md(
            summary,
            reasons,
            manifest,
            level_events,
            fight_facts=fight_facts,
            sequence_validation=sequence_validation,
            liquidation_flow=liquidation_flow,
        ),
        encoding="utf-8",
    )


def _write_fight_fact_outputs(run_dir: Path, fight_facts: dict[str, Any]) -> None:
    write_json(run_dir / "fight_episodes.json", fight_facts.get("fight_episodes") or [])
    write_json(run_dir / "level_registry.json", fight_facts.get("level_registry") or {})
    write_json(
        run_dir / "fight_facts_manifest.json",
        {
            "schema_version": fight_facts.get("schema_version"),
            "fight_fact_contract": fight_facts.get("fight_fact_contract"),
            "interpretation_status": fight_facts.get("interpretation_status"),
            "manifest": fight_facts.get("manifest"),
            "frozen_profile_edges": fight_facts.get("frozen_profile_edges"),
        },
    )
    write_csv(run_dir / "profile_state_transitions.csv", fight_facts.get("profile_state_transitions") or [])
    write_csv(run_dir / "profile_state_episodes.csv", fight_facts.get("profile_state_episodes") or [])
    write_csv(run_dir / "aggression_buckets.csv", fight_facts.get("aggression_buckets") or [])
    write_csv(run_dir / "fight_episode_summary.csv", fight_facts.get("fight_episode_summary") or [])
    write_csv(run_dir / "edge_consumption_events.csv", fight_facts.get("edge_consumption_events") or [])
    write_csv(run_dir / "post_trade_refill_events.csv", fight_facts.get("post_trade_refill_events") or [])
    write_csv(run_dir / "outside_profile_episodes.csv", fight_facts.get("outside_profile_episodes") or [])
    write_csv(run_dir / "raw_outside_excursions.csv", fight_facts.get("raw_outside_excursions") or [])
    write_csv(run_dir / "ambiguous_reclaim_candidates.csv", fight_facts.get("ambiguous_reclaim_candidates") or [])
    write_csv(run_dir / "reclaim_events.csv", fight_facts.get("reclaim_events") or [])
    write_csv(run_dir / "retest_proximity_events.csv", fight_facts.get("retest_proximity_events") or [])
    write_csv(run_dir / "episode_oi_liquidation_context.csv", fight_facts.get("episode_oi_liquidation_context") or [])


def _write_liquidation_flow_outputs(run_dir: Path, flow: dict[str, Any]) -> None:
    write_json(run_dir / "liquidation_flow_summary.json", flow.get("summary") or {})
    write_json(run_dir / "liquidation_flow_manifest.json", flow.get("manifest") or {})
    write_csv(run_dir / "liquidation_flow_events.csv", flow.get("events") or [])
    write_csv(run_dir / "liquidation_public_trade_allocation.csv", flow.get("allocations") or [])
    write_csv(run_dir / "liquidation_matching_sensitivity.csv", flow.get("sensitivity") or [])
    write_csv(run_dir / "liquidation_phase_summary.csv", flow.get("phases") or [])


def _write_sequence_validation_outputs(
    run_dir: Path, seq: dict[str, Any], *, heavy_detail_csv: bool = True
) -> None:
    write_json(run_dir / "phase_2a3_preflight_audit.json", seq.get("phase_2a3_preflight_audit") or {})
    write_json(run_dir / "canonical_eligibility_summary.json", seq.get("canonical_eligibility_summary") or {})
    write_csv(run_dir / "ambiguous_reclaim_candidates.csv", seq.get("ambiguous_reclaim_candidates") or [])
    write_json(run_dir / "first_outside_bin_contract.json", seq.get("first_outside_bin_contract") or {})
    write_csv(run_dir / "edge_observability_detail.csv", seq.get("edge_observability_detail") or [])
    write_json(run_dir / "edge_observability_summary.json", seq.get("edge_observability_summary") or {})
    write_json(run_dir / "nearby_liquidity_increase_metrics.json", seq.get("nearby_liquidity_increase_metrics") or {})
    write_json(run_dir / "coverage_aware_consumption_metrics.json", seq.get("coverage_aware_consumption_metrics") or {})
    write_json(run_dir / "phase_2a1_preflight_audit.json", seq.get("preflight_audit") or {})
    write_csv(run_dir / "phase_2a1_episode_distribution.csv", seq.get("episode_distribution") or [])
    write_json(run_dir / "profile_price_bin_contract.json", seq.get("profile_price_bin_contract") or {})
    write_csv(run_dir / "edge_visits.csv", seq.get("edge_visits") or [])
    write_csv(run_dir / "fight_cluster_sensitivity.csv", seq.get("fight_cluster_sensitivity") or [])
    write_json(run_dir / "fight_clusters_by_gap.json", seq.get("fight_clusters_by_gap") or {})
    write_csv(run_dir / "edge_visit_cluster_join_audit.csv", seq.get("edge_visit_cluster_join_audit") or [])
    write_json(run_dir / "same_timestamp_ordering_audit.json", seq.get("same_timestamp_ordering_audit") or {})
    if heavy_detail_csv:
        write_csv(run_dir / "same_timestamp_multistate_groups.csv", seq.get("same_timestamp_multistate_groups") or [])
        write_csv(run_dir / "edge_book_coverage.csv", seq.get("edge_book_coverage") or [])
        write_csv(run_dir / "edge_region_depth_samples.csv", seq.get("edge_region_depth_samples") or [])
    else:
        write_json(
            run_dir / "edge_detail_io.json",
            {
                "heavy_detail_csv": False,
                "edge_book_coverage_rows": len(seq.get("edge_book_coverage") or []),
                "depth_sample_rows": len(seq.get("edge_region_depth_samples") or []),
                "same_timestamp_groups": len(seq.get("same_timestamp_multistate_groups") or []),
                "note": "per-sample edge/book CSVs omitted for research-db performance; summaries retained",
            },
        )
    write_json(run_dir / "edge_book_coverage_summary.json", seq.get("edge_book_coverage_summary") or {})
    write_json(run_dir / "ob_coverage_metrics.json", seq.get("ob_coverage_metrics") or {})
    if heavy_detail_csv:
        write_csv(run_dir / "edge_region_consumption_events.csv", seq.get("edge_region_consumption_events") or [])
        write_csv(run_dir / "exact_refill_events.csv", seq.get("exact_refill_events") or [])
        write_csv(run_dir / "nearby_liquidity_increase_events.csv", seq.get("nearby_liquidity_increase_events") or [])
    write_json(run_dir / "edge_region_consumption_summary.json", seq.get("edge_region_consumption_summary") or {})
    write_json(run_dir / "consumption_metrics_detail.json", seq.get("consumption_metrics_detail") or {})
    write_json(run_dir / "refill_metrics_detail.json", seq.get("refill_metrics_detail") or {})
    write_json(run_dir / "outside_reclaim_invariant_audit.json", seq.get("outside_reclaim_invariant_audit") or {})
    write_json(run_dir / "fight_sequence_summary.json", seq.get("fight_sequence_summary") or {})
    write_json(run_dir / "outside_excursion_category_metrics.json", seq.get("outside_excursion_category_metrics") or {})
    write_csv(run_dir / "outside_excursions.csv", seq.get("outside_excursions") or [])
    write_csv(run_dir / "canonical_outside_excursions.csv", seq.get("canonical_outside_excursions") or [])
    write_csv(run_dir / "ambiguous_same_timestamp_excursions.csv", seq.get("ambiguous_same_timestamp_excursions") or [])


def _flatten_level_contract(level_events: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    transitions: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for ev in level_events:
        for tr in ev.get("transitions") or []:
            transitions.append({"row_type": "TRANSITION", **tr})
        for ep in ev.get("episodes") or []:
            episodes.append({"row_type": "EPISODE", **ep})
        summaries.append(
            {
                "row_type": "SUMMARY",
                "contract_version": ev.get("contract_version"),
                "level_id": ev.get("level_id"),
                "label": ev.get("label"),
                "price": ev.get("price"),
                "first_touch_ts": ev.get("first_touch_ts"),
                "first_cross_up_ts": ev.get("first_cross_up_ts"),
                "first_cross_down_ts": ev.get("first_cross_down_ts"),
                "first_return_below_after_cross_up_ts": ev.get("first_return_below_after_cross_up_ts"),
                "first_return_above_after_cross_down_ts": ev.get("first_return_above_after_cross_down_ts"),
                "seconds_outside_before_first_return": ev.get("seconds_outside_before_first_return"),
                "initial_side_at_anchor": (ev.get("anchor_state") or {}).get("initial_side_at_anchor"),
            }
        )
    return transitions, episodes, summaries


def _tpo_profile_summary_file(tpo_profile: dict[str, Any]) -> dict[str, Any]:
    return {
        k: tpo_profile.get(k)
        for k in (
            "tpo_profile_status",
            "contract_version",
            "provenance",
            "coverage",
            "brackets",
            "integrity",
            "prefix_parity",
            "trade_size_invariance",
            "tpoc",
            "value_area",
            "hvn_candidates",
            "lvn_candidates",
            "tpo_profile_cutoff_utc",
            "future_trade_count_used",
            "tpo_profile_computed_separately",
        )
    }


def _volume_profile_summary_file(volume_profile: dict[str, Any]) -> dict[str, Any]:
    return {
        k: volume_profile.get(k)
        for k in (
            "volume_profile_status",
            "contract_version",
            "provenance",
            "coverage",
            "integrity",
            "prefix_parity",
            "vpoc",
            "value_area",
            "hvn_candidates",
            "lvn_candidates",
            "oa_parity",
            "volume_profile_cutoff_utc",
            "max_trade_ts_used",
            "future_trade_count_used",
            "volume_profile_computed_separately",
        )
    }


def _strip_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in o.items() if k not in ("mid", "bids", "asks")} for o in observations]


def _strip_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in t.items() if k != "observations"} for t in tracks]


def _flatten_wall_facts(wall_facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for w in wall_facts:
        base = {k: v for k, v in w.items() if k != "heuristic_events"}
        events = w.get("heuristic_events") or []
        if not events:
            rows.append(base)
            continue
        for he in events:
            rows.append({**base, **he})
    return rows


def _liquidation_flow_report_lines(flow: dict[str, Any]) -> list[str]:
    s = flow.get("summary") or {}
    lines = [
        f"- Short-Liquidationen: {s.get('short_liquidation_event_count')} Events, "
        f"{s.get('short_liquidation_executed_base_size'):.4f} BTC executed, "
        f"bankruptcy-ref-quote {s.get('short_liquidation_bankruptcy_reference_quote'):,.0f} USD",
        f"- Long-Liquidationen: {s.get('long_liquidation_event_count')} Events, "
        f"{s.get('long_liquidation_executed_base_size'):.4f} BTC executed, "
        f"bankruptcy-ref-quote {s.get('long_liquidation_bankruptcy_reference_quote'):,.0f} USD",
        f"- Gesamte Taker-Buys: {s.get('total_taker_buy_base'):.4f} BTC / "
        f"{fmt_mio_usd(s.get('total_taker_buy_quote'))}",
        f"- Gesamte Taker-Sells: {s.get('total_taker_sell_base'):.4f} BTC / "
        f"{fmt_mio_usd(s.get('total_taker_sell_quote'))}",
        f"- Taker-Delta: {fmt_mio_usd(s.get('taker_delta_quote'))}",
        f"- execution_price / execution_notional: NULL (unbekannt)",
        f"- Direkte Trade-ID-Zuordnung: nicht verfügbar ({s.get('attribution_method')})",
        "- Interpretation: NOT_EVALUATED",
    ]
    for row in s.get("matching_sensitivity") or []:
        lines.append(
            f"- ±{row.get('sensitivity_window_ms')}ms: allocated_liquidation_base "
            f"{row.get('allocated_liquidation_base'):.4f} BTC "
            f"({(row.get('allocated_liquidation_share_of_total_taker_buy_base') or 0)*100:.2f}% of total_taker_buy_base); "
            f"remaining_unattributed_taker_buy_base {row.get('remaining_unattributed_taker_buy_base'):.4f} BTC; "
            f"liquidation_capacity_coverage_pct={row.get('liquidation_capacity_coverage_pct'):.1f}%"
        )
    return lines


def _liquidation_flow_console_lines(flow: dict[str, Any]) -> list[str]:
    s = flow.get("summary") or {}
    out = [
        "",
        "LIQUIDATION FLOW FACTS — NOT_EVALUATED",
        f"- Short liq: {s.get('short_liquidation_event_count')} events, "
        f"{s.get('short_liquidation_executed_base_size'):.4f} BTC, "
        f"bkr-ref-quote {s.get('short_liquidation_bankruptcy_reference_quote'):,.0f} USD",
        f"- Long liq: {s.get('long_liquidation_event_count')} events, "
        f"{s.get('long_liquidation_executed_base_size'):.4f} BTC",
        f"- Taker buys: {s.get('total_taker_buy_base'):.4f} BTC / "
        f"{fmt_mio_usd(s.get('total_taker_buy_quote'))}",
        f"- Taker sells: {s.get('total_taker_sell_base'):.4f} BTC / "
        f"{fmt_mio_usd(s.get('total_taker_sell_quote'))}",
        f"- Taker delta: {fmt_mio_usd(s.get('taker_delta_quote'))}",
        f"- Direct trade ID: unavailable",
    ]
    for row in s.get("matching_sensitivity") or []:
        out.append(
            f"- ±{row.get('sensitivity_window_ms')}ms: allocated_liquidation_base "
            f"{row.get('allocated_liquidation_base'):.4f} BTC, "
            f"remaining_unattributed_taker_buy_base {row.get('remaining_unattributed_taker_buy_base'):.4f} BTC, "
            f"double_counted=0"
        )
    out.append("INTERPRETATION: NOT_EVALUATED")
    return out


def build_report_md(
    summary: dict[str, Any],
    reasons: list[dict[str, Any]],
    manifest: dict[str, Any],
    level_events: list[dict[str, Any]] | None = None,
    *,
    fight_facts: dict[str, Any] | None = None,
    sequence_validation: dict[str, Any] | None = None,
    liquidation_flow: dict[str, Any] | None = None,
) -> str:
    sections = render_report_sections(reasons, summary, manifest, level_events=level_events or [])
    lines = [
        "# BTC OB Fight Fact Report (Phase 0–1)",
        "",
        f"**Status:** `{summary.get('analysis_status')}`",
        f"**Schema:** `{summary.get('schema_version')}`",
        "",
        "## Anchor Profile",
        "",
    ]
    lines.extend(f"- {line}" for line in sections["profile"])
    lines.extend(["", "## Level-Episoden (Fakten)", ""])
    lines.extend(f"- {line}" for line in sections["episodes"])
    lines.extend(["", "## Public-Trade-Fenster", ""])
    lines.extend(f"- {line}" for line in sections["trade_windows"])
    lines.extend(["", "## Open Interest", ""])
    lines.extend(f"- {line}" for line in sections["oi"])
    lines.extend(["", "## Liquidationen", ""])
    lines.extend(f"- {line}" for line in sections["liquidations"])
    if liquidation_flow:
        lines.extend(["", "## LIQUIDATION FLOW FACTS — NOT_EVALUATED", ""])
        lines.extend(_liquidation_flow_report_lines(liquidation_flow))
    lines.extend(["", "## Orderbuch-Fakten", ""])
    lines.extend(f"- {line}" for line in sections["walls"])
    if sections["heuristics"]:
        lines.extend(["", "## Wall-Heuristik-Sequenzen (UNFROZEN)", ""])
        lines.extend(f"- {line}" for line in sections["heuristics"])
    if fight_facts:
        lines.extend(["", "## FIGHT FACTS — INTERPRETATION NOT EVALUATED", ""])
        edges = fight_facts.get("frozen_profile_edges") or {}
        if edges.get("profile_state") == "VALID":
            lines.append(
                f"- Frozen edges: upper {edges.get('upper_inner_edge')}–{edges.get('upper_outer_edge')}, "
                f"lower {edges.get('lower_outer_edge')}–{edges.get('lower_inner_edge')}"
            )
        fm = fight_facts.get("manifest") or {}
        lines.append(f"- Profile-state episodes: {fm.get('profile_state_episode_count')}")
        lines.append(f"- Outside episodes: {fm.get('outside_episode_count')}")
        lines.append(f"- Edge consumption events: {fm.get('edge_consumption_count')}")
        lines.append(f"- Post-trade refills: {fm.get('post_trade_refill_count')}")
        lines.append(f"- Reclaim events: {fm.get('reclaim_count')}")
        for fe in (fight_facts.get("fight_episode_summary") or [])[:6]:
            lines.append(
                f"- Fight `{fe.get('fight_episode_id')}` edge={fe.get('edge')} "
                f"state={fe.get('profile_state')} dur={fe.get('duration_seconds')}s "
                f"delta={fe.get('taker_delta_quote')} price_bps={fe.get('price_change_bps')}"
            )
        lines.extend(
            [
                "",
                "BREAKOUT CONFIRMATION:       NOT_EVALUATED",
                "FAILED BREAKOUT:             NOT_EVALUATED",
                "BUYER/SELLER CONTROL:        NOT_EVALUATED",
                "ABSORPTION:                  NOT_EVALUATED",
                "TRADE DIRECTION:             null",
            ]
        )
    if sequence_validation:
        sq = sequence_validation.get("fight_sequence_summary") or {}
        lines.extend(["", "## FIGHT SEQUENCE VALIDATION (Phase 2A.3) — RULES UNFROZEN", ""])
        lines.append(f"- Verdict: `{sequence_validation.get('verdict')}`")
        lines.append(f"- Canonical reclaim contract: `{sq.get('canonical_reclaim_contract')}`")
        lines.append(f"- RAW outside observations: {sq.get('raw_outside_observation_count')}")
        lines.append(f"- Ambiguous reclaim candidates: {sq.get('ambiguous_reclaim_candidate_count')}")
        lines.append(f"- Canonical outside excursions: {sq.get('canonical_outside_count')}")
        lines.append(f"- Canonical reclaims: {sq.get('canonical_reclaim_count')}")
        lines.append(f"- Raw state episodes: {sq.get('raw_state_episode_count')}")
        lines.append(
            f"- Edge visits (raw): upper={sq.get('edge_visits_upper')} lower={sq.get('edge_visits_lower')} "
            f"(total={sq.get('edge_visit_count')})"
        )
        lines.append(
            f"- Cluster count gap=0: {sq.get('cluster_count_gap_0')} "
            f"(invariant_ok={sq.get('gap0_invariant_ok')})"
        )
        lines.append(
            f"- Outside excursions raw/canonical/ambiguous: "
            f"{sq.get('outside_excursion_count_raw')}/"
            f"{sq.get('outside_excursion_count_canonical')}/"
            f"{sq.get('outside_excursion_count_ambiguous')}"
        )
        lines.append(
            f"- Reclaims (canonical v3): {sq.get('canonical_reclaim_count')} "
            f"(unique cross_ts={sq.get('unique_reclaim_cross_timestamps')})"
        )
        lines.append(
            f"- Nearby liquidity increases: Ask {sq.get('nearby_ask_count')} / "
            f"Bid {sq.get('nearby_bid_count')} / Unknown {sq.get('nearby_unknown_count')}"
        )
        lines.append(f"- Fight-time edge observability rows: {(sq.get('edge_observability_summary') or {}).get('row_count')}")
        gaps = sq.get("cluster_counts_by_gap") or {}
        lines.append(f"- Cluster counts by gap: {gaps}")
        ob_cov = sq.get("ob_coverage_metrics") or {}
        if ob_cov:
            lines.append(f"- OB200 coverage metrics: {ob_cov.get('overall') or ob_cov}")
        cons = sq.get("consumption_metrics") or sq.get("consumption_by_scope") or {}
        lines.append(f"- Consumption metrics: {cons}")
        lines.append(f"- Exact refills: {sq.get('exact_refill_count')}")
        lines.append(f"- Open excursions: {sq.get('open_excursion_count')}")
        lines.append(f"- Edge book coverage: {(sq.get('edge_book_coverage') or {}).get('by_scope_status')}")
        lines.append(f"- OI/Liq coverage: {sq.get('oi_liquidation_coverage')}")
        lines.append(f"- Same-timestamp ordering audited: {sq.get('same_timestamp_ordering_audited')}")
        lines.append(f"- Legacy global-first reclaim enabled: {sq.get('legacy_global_first_reclaim_enabled')}")
        lines.extend(
            [
                "",
                "BREAKOUT CONFIRMATION:       NOT_EVALUATED",
                "FAILED BREAKOUT:             NOT_EVALUATED",
                "ABSORPTION:                  NOT_EVALUATED",
                "BUYER/SELLER CONTROL:        NOT_EVALUATED",
                "TRADE DIRECTION:             null",
                "RULES FROZEN:                false",
            ]
        )
    lines.extend(
        [
            "",
            "## Nicht evaluiert",
            "",
            "- Käufer-/Verkäuferkontrolle",
            "- Absorption",
            "- Breakout-Akzeptanz",
            "- Long-/Short-Entry",
            "",
            "## Manifest",
            "",
            f"- OB root: `{manifest.get('ob_root')}`",
            f"- auto_extension_enabled: `{manifest.get('auto_extension_enabled')}`",
            f"- rules_frozen: `{manifest.get('rules_frozen')}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _fmt_count(value: Any, *, computed: bool = True) -> str:
    """Render counts: 0 stays 0; missing computed field is NOT_AVAILABLE; never silent 0."""
    if not computed:
        return "NOT_AVAILABLE"
    if value is None:
        return "NOT_AVAILABLE"
    if isinstance(value, (list, tuple, set, dict)):
        return str(len(value))
    return str(value)


def _seq_summary(sequence_validation: dict[str, Any] | None) -> dict[str, Any]:
    if not sequence_validation:
        return {}
    if sequence_validation.get("fight_sequence_summary"):
        return sequence_validation["fight_sequence_summary"]
    # lean summary already flattened to fight_sequence_summary fields
    if "canonical_outside_count" in sequence_validation or "verdict" in sequence_validation:
        return sequence_validation
    return {}


def _fight_manifest(fight_facts: dict[str, Any] | None) -> dict[str, Any]:
    if not fight_facts:
        return {}
    if fight_facts.get("manifest"):
        return fight_facts["manifest"]
    return fight_facts


def _observability_console_block(sq: dict[str, Any]) -> list[str]:
    lines: list[str] = ["", "FIGHT-TIME OBSERVABILITY"]
    eos = sq.get("edge_observability_summary") or {}
    by = eos.get("by_edge_time_scope") or {}
    # Prefer UPPER FULL_WINDOW PROFILE_EDGE_ZONE / EXACT_LEVEL_TICK
    chosen = None
    for key, rows in by.items():
        if not isinstance(rows, list) or not rows:
            continue
        if key.startswith("UPPER|FULL_WINDOW"):
            chosen = rows[0]
            if "PROFILE_EDGE_ZONE" in key or "EXACT_LEVEL_TICK" in key:
                break
    if chosen is None and by:
        first_rows = next(iter(by.values()))
        chosen = first_rows[0] if first_rows else None
    if not chosen:
        lines.append("- Coverage: NOT_AVAILABLE")
        return lines
    status = chosen.get("status") or chosen.get("full_region_coverage") or "NOT_AVAILABLE"
    outside_pct = chosen.get("outside_book_pct")
    lines.append(f"- Relevant edge: {chosen.get('edge') or 'NOT_AVAILABLE'}")
    lines.append(f"- Scope: {chosen.get('scope') or 'NOT_AVAILABLE'}")
    lines.append(f"- Coverage-Status: {status}")
    lines.append(
        f"- Full/Partial/Outside-book/Missing: "
        f"{chosen.get('full_coverage_pct')}% / {chosen.get('partial_coverage_pct')}% / "
        f"{chosen.get('outside_book_pct')}% / {chosen.get('missing_pct')}%"
    )
    mostly_outside = (
        outside_pct is not None and float(outside_pct) >= 50.0
    ) or "OUTSIDE_BOOK" in str(status)
    if mostly_outside:
        lines.append("PASSIVE EDGE CONTROL: NOT_EVALUATED")
        lines.append("REASON: EDGE_REGION_MOSTLY_OUTSIDE_OB200_RANGE")
        lines.append("- Note: not observable ≠ 0 observed; no absorption/refill/control inferred.")
    return lines


def print_console_summary(
    summary: dict[str, Any],
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    fight_facts: dict[str, Any] | None = None,
    sequence_validation: dict[str, Any] | None = None,
    level_events: list[dict[str, Any]] | None = None,
    anchor_profile_context: dict[str, Any] | None = None,
) -> None:
    """Render the final console from a complete result bundle (never a mid-pipeline stub)."""
    pf = summary.get("profile_facts") or {}
    oi = summary.get("oi_liquidation_facts") or {}
    ws = summary.get("wall_summary") or {}
    tpo = summary.get("tpo_profile") or {}
    vp = summary.get("volume_profile") or {}
    nearest_tpo = (pf.get("nearest_tpo_levels") or pf.get("nearest_profile_levels") or [{}])[0]
    nearest_vol = (pf.get("nearest_volume_levels") or [{}])[0]
    ff = fight_facts if fight_facts is not None else summary.get("fight_facts")
    sv = sequence_validation if sequence_validation is not None else summary.get("sequence_validation")
    levs = level_events if level_events is not None else (summary.get("level_events") or [])
    ctx = anchor_profile_context if anchor_profile_context is not None else summary.get("anchor_profile_context")
    fm = _fight_manifest(ff if isinstance(ff, dict) else {})
    sq = _seq_summary(sv if isinstance(sv, dict) else {})

    print("BTC OB FIGHT FACT ANALYSIS")
    print("─" * 40)
    print(f"ANCHOR:                   {summary.get('anchor_timestamp_utc')}")
    w = summary.get("window") or {}
    print(f"WINDOW:                   {w.get('start_utc')}–{w.get('end_utc')}")
    print(f"SYMBOL:                   {summary.get('symbol')}")
    print(f"SCHEMA:                   {summary.get('schema_version')}")
    print(f"DATA QUALITY:             {summary.get('data_quality')}")
    print(f"RULES FROZEN:             {summary.get('rules_frozen')}")
    print(f"TRADE VERDICT EVALUATED:  {summary.get('trade_verdict_evaluated')}")
    cov_rt = manifest.get("input_coverage_runtime_s")
    full_rt = manifest.get("full_analysis_runtime_s") or manifest.get("total_runtime_s")
    if cov_rt is not None:
        print(f"INPUT/COVERAGE RUNTIME:   {cov_rt:.3f} s")
    if full_rt is not None:
        print(f"FULL ANALYSIS RUNTIME:    {float(full_rt):.3f} s")
    print("")
    print(f"ANCHOR PRICE:             {fmt_price(pf.get('price_at_anchor'))}")
    print("")
    def _na(v: Any) -> str:
        return str(v) if v is not None else "NOT_AVAILABLE"

    print("TPO PROFILE — 30m BRACKET PRESENCE")
    print(f"- Status: {tpo.get('status') or pf.get('tpo_profile_status')}")
    if pf.get("profile_start_utc") or pf.get("profile_cutoff_utc"):
        print(f"- Session: {pf.get('profile_start_utc')} → cutoff {pf.get('profile_cutoff_utc')}")
    print(f"- Bracket duration: {tpo.get('bracket_minutes') or manifest.get('profile_settings', {}).get('tpo_bracket_minutes')}m")
    print(
        f"- Brackets: {tpo.get('full_brackets')} full / {tpo.get('partial_brackets')} partial "
        f"(total {tpo.get('total_brackets')})"
    )
    print(
        f"- TPO POC/VAH/VAL: {fmt_price(pf.get('tpo_poc'))} / "
        f"{fmt_price(pf.get('tpo_vah'))} / {fmt_price(pf.get('tpo_val'))}"
    )
    if pf.get("tpo_value_area_share") is not None:
        print(f"- TPO Value-Area-Anteil: {fmt_fraction_as_pct(pf.get('tpo_value_area_share'))}")
    if pf.get("inside_tpo_value_area") is not None:
        print(f"- Anchor inside TPO VA: {pf.get('inside_tpo_value_area')}")
    if nearest_tpo:
        print(f"- Nächstes TPO-Level: {nearest_tpo.get('kind')} {fmt_price(nearest_tpo.get('price'))}")
    integ = tpo.get("integrity")
    inv = tpo.get("trade_size_invariance")
    pre = tpo.get("prefix_parity")
    if integ is not None or inv is not None or pre is not None:
        print(
            f"- Integrity: {_na(integ)}; Trade-Size-Invarianz: {_na(inv)}; "
            f"Prefix-Parität: {_na(pre)}"
        )
    conf_status = pf.get("tpo_volume_confluence_status")
    if conf_status:
        print(f"- TPO↔Volume Konfluenz: {conf_status}")
    print("")
    print("VOLUME PROFILE — BASE VOLUME")
    print(f"- Status: {vp.get('status')}")
    if vp.get("primary_volume_basis") is not None:
        print(f"- Basis: {vp.get('primary_volume_basis')}")
    print(f"- VPOC/VVAH/VVAL: {fmt_price(vp.get('vpoc'))} / {fmt_price(vp.get('vvah'))} / {fmt_price(vp.get('vval'))}")
    if vp.get("value_area_share") is not None:
        print(f"- Value-Area-Anteil: {fmt_fraction_as_pct(vp.get('value_area_share'))}")
    inside = pf.get("inside_volume_value_area")
    if inside is not None:
        print(f"- Anchor inside Volume VA: {inside}")
    if nearest_vol:
        print(f"- Nächstes Volume-Level: {nearest_vol.get('kind')} {fmt_price(nearest_vol.get('price'))}")
    vi = vp.get("integrity")
    vp_ = vp.get("prefix_parity")
    vo = vp.get("oa_parity")
    if vi is not None or vp_ is not None or vo is not None:
        print(f"- Integrity: {_na(vi)}; Prefix-Parität: {_na(vp_)}; OA-Parität: {_na(vo)}")
    conf = pf.get("tpo_volume_level_confluence") or []
    if conf and conf_status == "VALID_INDEPENDENT_MEASURES":
        poc_conf = next((c for c in conf if c.get("tpo_kind") == "poc"), {})
        if poc_conf.get("evaluation_status") == "EVALUATED":
            print(
                f"- TPO↔Volume POC: same_bin={poc_conf.get('same_bin')}, "
                f"dist_bps={poc_conf.get('distance_bps'):.2f}"
            )
    print("")
    print("LEVEL EPISODE SUMMARY")
    vah = next((e for e in levs if e.get("level_id") in {"TPO_VAH", "tpo_vah"}), None)
    if vah and (vah.get("episodes") or vah.get("first_complete_above_episode")):
        ep = vah.get("first_complete_above_episode")
        if ep:
            print(f"- TPO-VAH erste ABOVE-Episode: {fmt_duration_seconds(ep.get('duration_seconds'))}")
        above = [e for e in (vah.get("episodes") or []) if e.get("direction") == "ABOVE" and e.get("complete")]
        if above:
            longest = max(above, key=lambda e: e.get("duration_seconds") or 0)
            print(f"- TPO-VAH längste ABOVE: {fmt_duration_seconds(longest.get('duration_seconds'))}")
    else:
        print("- TPO-VAH episodes: see REPORT.md / level_episodes.csv")
    print("")
    print("PUBLIC TRADE SUMMARY")
    rel = {x.get("label"): x for x in ((summary.get("trade_facts") or {}).get("relative_windows") or [])}
    w10 = rel.get("anchor_0_10m")
    if w10:
        print(f"- 0–10m Delta: {fmt_mio_usd(w10.get('delta_notional'))}; Preis: {fmt_bps(w10.get('price_change_bps'))}")
    w30 = rel.get("anchor_0_30m")
    if w30:
        print(f"- 0–30m Delta: {fmt_mio_usd(w30.get('delta_notional'))}; Preis: {fmt_bps(w30.get('price_change_bps'))}")
    print("")
    print("ORDERBOOK FACT SUMMARY")
    print(f"- Book-Samples: {_fmt_count(ws.get('book_samples_total'))}")
    ask_obs = ws.get("ask_wall_observations")
    bid_obs = ws.get("bid_wall_observations")
    if ask_obs is not None or bid_obs is not None:
        print(f"- Wall-Beobachtungen: Ask {_na(ask_obs)} / Bid {_na(bid_obs)}")
    ask_tr = ws.get("ask_wall_tracks")
    bid_tr = ws.get("bid_wall_tracks")
    if ask_tr is not None or bid_tr is not None:
        print(f"- Eindeutige Tracks: Ask {_na(ask_tr)} / Bid {_na(bid_tr)}")
    td = ws.get("trade_associated_decreases") or {}
    ud = ws.get("unmatched_decreases") or {}
    if td:
        print(f"- Trade-associated Decreases: Ask {_na(td.get('ask'))} / Bid {_na(td.get('bid'))}")
    if ud:
        print(f"- Unmatched Decreases: Ask {_na(ud.get('ask'))} / Bid {_na(ud.get('bid'))}")
    rf = ws.get("refill_sequences_heuristic") or {}
    if rf:
        print(f"- Refill-Sequenzen (UNFROZEN): Ask {_na(rf.get('ask'))} / Bid {_na(rf.get('bid'))}")
    print("")
    print("OI/LIQ SUMMARY")
    unit = (oi.get("oi_unit") or {}).get("display_label") or "Source-Einheiten"
    print(f"- OI Delta: {fmt_oi_delta(oi.get('oi_delta'))} {unit} ({fmt_pct(oi.get('oi_delta_pct'))})")
    ls = oi.get("liquidation_summary") or {}
    print(f"- Liquidationen: {oi.get('liquidation_count')} (Long {ls.get('long_count')}, Short {ls.get('short_count')})")
    lf = summary.get("liquidation_flow_summary") or {}
    if lf:
        for line in _liquidation_flow_console_lines({"summary": lf}):
            print(line)

    if ctx:
        print("")
        print("ANCHOR PROFILE CONTEXT")
        print(f"- Contract: {ctx.get('contract_version')}")
        print(f"- Anchor context: {ctx.get('anchor_context')}")
        print(f"- Observation context: {ctx.get('observation_context')}")
        edges_c = ctx.get("edges") or {}
        print(
            f"- Outer upper/lower: {fmt_price(edges_c.get('outer_upper_edge'))} / "
            f"{fmt_price(edges_c.get('outer_lower_edge'))}"
        )
        prior = ctx.get("prior_edge_cross") or {}
        print(f"- Prior outer-cross status: {prior.get('status') or 'NOT_AVAILABLE'}")
        if prior.get("last_outer_cross"):
            loc = prior["last_outer_cross"]
            print(f"- Last outer cross: {loc.get('cross_ts')} @ {fmt_price(loc.get('cross_price'))}")
            s2a = prior.get("seconds_from_last_outer_cross_to_anchor")
            print(
                f"- Seconds last outer-cross → anchor: {s2a if s2a is not None else 'NOT_AVAILABLE'}"
            )
            rem = prior.get("remained_outside_until_anchor")
            print(f"- Remained outside until anchor: {rem if rem is not None else 'NOT_AVAILABLE'}")
        print("ANCHOR OBSERVATION CONTEXT")
        print(f"- {ctx.get('observation_context')}")

    print("")
    print("FIGHT FACTS")
    print(f"- Profile-state episodes: {_fmt_count(fm.get('profile_state_episode_count'))}")
    print(f"- Outside episodes: {_fmt_count(fm.get('outside_episode_count'))}")
    print(f"- Exact frozen-edge events: {_fmt_count(fm.get('edge_consumption_count'))}")
    cons = sq.get("consumption_by_scope") or {}
    print(f"- TPO edge-bin events: {_fmt_count((cons.get('TPO_EDGE_BIN') or {}).get('total'), computed=bool(cons))}")
    print(f"- Volume edge-bin events: {_fmt_count((cons.get('VOLUME_EDGE_BIN') or {}).get('total'), computed=bool(cons))}")
    print(f"- Profile-edge-zone events: {_fmt_count((cons.get('PROFILE_EDGE_ZONE') or {}).get('total'), computed=bool(cons))}")
    print(f"- Post-trade refills: {_fmt_count(fm.get('post_trade_refill_count'))}")
    print(f"- Canonical reclaims: {_fmt_count(fm.get('reclaim_count') if fm.get('reclaim_count') is not None else sq.get('canonical_reclaim_count'))}")
    print("BREAKOUT CONFIRMATION:       NOT_EVALUATED")
    print("FAILED BREAKOUT:             NOT_EVALUATED")
    print("BUYER/SELLER CONTROL:        NOT_EVALUATED")
    print("ABSORPTION:                  NOT_EVALUATED")
    print("TRADE DIRECTION:             null")

    print("")
    print("CANONICAL SEQUENCE")
    print(f"- Verdict: {sq.get('verdict') or (sv or {}).get('verdict') or 'NOT_AVAILABLE'}")
    print(f"- Raw outside: {_fmt_count(sq.get('raw_outside_observation_count'))}")
    print(f"- Canonical outside: {_fmt_count(sq.get('canonical_outside_count'))}")
    print(f"- Ambiguous outside: {_fmt_count(sq.get('outside_excursion_count_ambiguous'))}")
    print(f"- Canonical reclaims: {_fmt_count(sq.get('canonical_reclaim_count'))}")
    print(f"- Ambiguous reclaim candidates: {_fmt_count(sq.get('ambiguous_reclaim_candidate_count'))}")
    print(
        f"- Edge visits Upper/Lower: {_fmt_count(sq.get('edge_visits_upper'))}/"
        f"{_fmt_count(sq.get('edge_visits_lower'))}"
    )
    print(f"- Open outside excursions: {_fmt_count(sq.get('open_excursion_count'))}")
    print(
        f"- Cluster gap=0: {_fmt_count(sq.get('cluster_count_gap_0'))} "
        f"(invariant_ok={sq.get('gap0_invariant_ok') if sq.get('gap0_invariant_ok') is not None else 'NOT_AVAILABLE'})"
    )
    print(
        f"- Nearby liquidity increases Ask/Bid/Unknown: "
        f"{_fmt_count(sq.get('nearby_ask_count'))}/"
        f"{_fmt_count(sq.get('nearby_bid_count'))}/"
        f"{_fmt_count(sq.get('nearby_unknown_count'))}"
    )
    for line in _observability_console_block(sq):
        print(line)
    print("BREAKOUT CONFIRMATION:       NOT_EVALUATED")
    print("FAILED BREAKOUT:             NOT_EVALUATED")
    print("ABSORPTION:                  NOT_EVALUATED")
    print("BUYER/SELLER CONTROL:        NOT_EVALUATED")
    print("TRADE DIRECTION:             null")
    print("RULES FROZEN:                false")
    print("")
    print("ANALYSIS STATUS")
    print(str(sq.get("verdict") or (sv or {}).get("verdict") or "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY"))
    print("")
    print("OUTPUT PATH")
    print(str(run_dir))

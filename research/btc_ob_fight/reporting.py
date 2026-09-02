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
    path.write_text(json.dumps(json_safe(obj), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


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
) -> None:
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "analysis_manifest.json", manifest)
    write_json(run_dir / "coverage_audit.json", coverage)
    write_json(run_dir / "profile_levels.json", profiles)
    write_json(run_dir / "oi_liquidation_facts.json", oi_liq)
    write_json(run_dir / "factual_reasons.json", {"reasons": reasons, "templates_de": german})
    transitions, episodes, summaries = _flatten_level_contract(level_events)
    write_csv(run_dir / "level_transitions.csv", transitions)
    write_csv(run_dir / "level_episodes.csv", episodes)
    write_csv(run_dir / "level_events.csv", summaries)
    write_csv(run_dir / "public_trade_buckets.csv", trade_buckets)
    if wall_bundle:
        write_csv(run_dir / "wall_observations.csv", _strip_observations(wall_bundle.get("observations") or []))
        write_csv(run_dir / "wall_tracks.csv", _strip_tracks(wall_bundle.get("tracks") or []))
        write_csv(run_dir / "wall_transitions.csv", wall_bundle.get("transitions") or [])
        write_csv(run_dir / "wall_trade_matches.csv", wall_bundle.get("trade_matches") or [])
        write_json(run_dir / "wall_summary.json", wall_bundle.get("summary") or {})
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
    write_csv(run_dir / "wall_events.csv", _flatten_wall_facts(wall_facts))
    if fight_facts:
        _write_fight_fact_outputs(run_dir, fight_facts)
    if sequence_validation:
        _write_sequence_validation_outputs(run_dir, sequence_validation)
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


def _write_sequence_validation_outputs(run_dir: Path, seq: dict[str, Any]) -> None:
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
    write_csv(run_dir / "same_timestamp_multistate_groups.csv", seq.get("same_timestamp_multistate_groups") or [])
    write_csv(run_dir / "edge_book_coverage.csv", seq.get("edge_book_coverage") or [])
    write_csv(run_dir / "edge_region_depth_samples.csv", seq.get("edge_region_depth_samples") or [])
    write_json(run_dir / "edge_book_coverage_summary.json", seq.get("edge_book_coverage_summary") or {})
    write_json(run_dir / "ob_coverage_metrics.json", seq.get("ob_coverage_metrics") or {})
    write_csv(run_dir / "edge_region_consumption_events.csv", seq.get("edge_region_consumption_events") or [])
    write_json(run_dir / "edge_region_consumption_summary.json", seq.get("edge_region_consumption_summary") or {})
    write_json(run_dir / "consumption_metrics_detail.json", seq.get("consumption_metrics_detail") or {})
    write_csv(run_dir / "exact_refill_events.csv", seq.get("exact_refill_events") or [])
    write_csv(run_dir / "nearby_liquidity_increase_events.csv", seq.get("nearby_liquidity_increase_events") or [])
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


def print_console_summary(summary: dict[str, Any], run_dir: Path, manifest: dict[str, Any]) -> None:
    pf = summary.get("profile_facts") or {}
    oi = summary.get("oi_liquidation_facts") or {}
    ws = summary.get("wall_summary") or {}
    tpo = summary.get("tpo_profile") or {}
    vp = summary.get("volume_profile") or {}
    nearest_tpo = (pf.get("nearest_tpo_levels") or pf.get("nearest_profile_levels") or [{}])[0]
    nearest_vol = (pf.get("nearest_volume_levels") or [{}])[0]
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
    print("")
    print(f"ANCHOR PRICE:             {fmt_price(pf.get('price_at_anchor'))}")
    print("")
    print("TPO PROFILE — 30m BRACKET PRESENCE")
    print(f"- Status: {tpo.get('status') or pf.get('tpo_profile_status')}")
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
    print(
        f"- Integrity: {tpo.get('integrity')}; Trade-Size-Invarianz: {tpo.get('trade_size_invariance')}; "
        f"Prefix-Parität: {tpo.get('prefix_parity')}"
    )
    conf_status = pf.get("tpo_volume_confluence_status")
    if conf_status:
        print(f"- TPO↔Volume Konfluenz: {conf_status}")
    print("")
    print("VOLUME PROFILE — BASE VOLUME")
    print(f"- Status: {vp.get('status')}")
    print(f"- Basis: {vp.get('primary_volume_basis')}")
    print(f"- VPOC/VVAH/VVAL: {fmt_price(vp.get('vpoc'))} / {fmt_price(vp.get('vvah'))} / {fmt_price(vp.get('vval'))}")
    if vp.get("value_area_share") is not None:
        print(f"- Value-Area-Anteil: {fmt_fraction_as_pct(vp.get('value_area_share'))}")
    inside = pf.get("inside_volume_value_area")
    if inside is not None:
        print(f"- Anchor inside Volume VA: {inside}")
    if nearest_vol:
        print(f"- Nächstes Volume-Level: {nearest_vol.get('kind')} {fmt_price(nearest_vol.get('price'))}")
    print(f"- Integrity: {vp.get('integrity')}; Prefix-Parität: {vp.get('prefix_parity')}; OA-Parität: {vp.get('oa_parity')}")
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
    vah = next((e for e in (summary.get("level_events") or []) if e.get("level_id") == "TPO_VAH"), None)
    if vah is None:
        vah = next((e for e in (summary.get("level_events") or []) if e.get("level_id") == "tpo_vah"), None)
    if vah:
        ep = vah.get("first_complete_above_episode")
        if ep:
            print(f"- TPO-VAH erste ABOVE-Episode: {fmt_duration_seconds(ep.get('duration_seconds'))}")
        above = [e for e in (vah.get("episodes") or []) if e.get("direction") == "ABOVE" and e.get("complete")]
        if above:
            longest = max(above, key=lambda e: e.get("duration_seconds") or 0)
            print(f"- TPO-VAH längste ABOVE: {fmt_duration_seconds(longest.get('duration_seconds'))}")
    print("")
    print("PUBLIC TRADE SUMMARY")
    rel = {x.get("label"): x for x in ((summary.get("trade_facts") or {}).get("relative_windows") or [])}
    w10 = rel.get("anchor_0_10m")
    if w10:
        print(f"- 0–10m Delta: {fmt_mio_usd(w10.get('delta_notional'))}; Preis: {fmt_bps(w10.get('price_change_bps'))}")
    print("")
    print("ORDERBOOK FACT SUMMARY")
    print(f"- Book-Samples: {ws.get('book_samples_total')}")
    print(f"- Wall-Beobachtungen: Ask {ws.get('ask_wall_observations')} / Bid {ws.get('bid_wall_observations')}")
    print(f"- Eindeutige Tracks: Ask {ws.get('ask_wall_tracks')} / Bid {ws.get('bid_wall_tracks')}")
    td = ws.get("trade_associated_decreases") or {}
    ud = ws.get("unmatched_decreases") or {}
    print(f"- Trade-associated Decreases: Ask {td.get('ask')} / Bid {td.get('bid')}")
    print(f"- Unmatched Decreases: Ask {ud.get('ask')} / Bid {ud.get('bid')}")
    rf = ws.get("refill_sequences_heuristic") or {}
    print(f"- Refill-Sequenzen (UNFROZEN): Ask {rf.get('ask')} / Bid {rf.get('bid')}")
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
    ff = summary.get("fight_facts") or {}
    if ff:
        fm = ff.get("manifest") or {}
        print("")
        print("FIGHT FACTS — INTERPRETATION NOT EVALUATED")
        print(f"- Profile-state episodes: {fm.get('profile_state_episode_count')}")
        print(f"- Outside episodes: {fm.get('outside_episode_count')}")
        print(f"- Edge consumption: {fm.get('edge_consumption_count')}")
        print(f"- Post-trade refills: {fm.get('post_trade_refill_count')}")
        print(f"- Reclaim events: {fm.get('reclaim_count')}")
        print("BREAKOUT CONFIRMATION:       NOT_EVALUATED")
        print("FAILED BREAKOUT:             NOT_EVALUATED")
        print("BUYER/SELLER CONTROL:        NOT_EVALUATED")
        print("ABSORPTION:                  NOT_EVALUATED")
        print("TRADE DIRECTION:             null")
    sv = summary.get("sequence_validation") or {}
    if sv:
        sq = sv.get("fight_sequence_summary") or {}
        print("")
        print("FIGHT SEQUENCE VALIDATION (Phase 2A.3) — RULES UNFROZEN")
        print(f"- Verdict: {sv.get('verdict')}")
        print(f"- RAW outside: {sq.get('raw_outside_observation_count')}")
        print(f"- Ambiguous candidates: {sq.get('ambiguous_reclaim_candidate_count')}")
        print(f"- Canonical outside: {sq.get('canonical_outside_count')}")
        print(f"- Canonical reclaims: {sq.get('canonical_reclaim_count')}")
        print(f"- Edge visits U/L: {sq.get('edge_visits_upper')}/{sq.get('edge_visits_lower')} (total={sq.get('edge_visit_count')})")
        print(f"- Cluster gap=0: {sq.get('cluster_count_gap_0')} (invariant_ok={sq.get('gap0_invariant_ok')})")
        print(
            f"- Nearby Ask/Bid/Unknown: {sq.get('nearby_ask_count')}/"
            f"{sq.get('nearby_bid_count')}/{sq.get('nearby_unknown_count')}"
        )
        obm = sq.get("ob_coverage_metrics") or {}
        if obm.get("overall"):
            o = obm["overall"]
            print(
                f"- OB200 coverage: full={o.get('full_coverage_pct')}% "
                f"partial={o.get('partial_coverage_pct')}% missing={o.get('missing_sample_pct')}%"
            )
        print("BREAKOUT CONFIRMATION:       NOT_EVALUATED")
        print("FAILED BREAKOUT:             NOT_EVALUATED")
        print("ABSORPTION:                  NOT_EVALUATED")
        print("BUYER/SELLER CONTROL:        NOT_EVALUATED")
        print("TRADE DIRECTION:             null")
        print("RULES FROZEN:                false")
        print("")
        print("ANALYSIS STATUS")
        print(str(sv.get("verdict") or "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY"))
    else:
        print("")
        print("ANALYSIS STATUS")
        print("BTC_OB_FIGHT_CAUSAL_FACT_ENGINE_READY")
    print("")
    print("OUTPUT PATH")
    print(str(run_dir))

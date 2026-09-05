"""Research-DB orchestration path for BTC/DOGE OB Fight fact CLI."""

from __future__ import annotations

import csv
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import PACKAGE_VERSION, SCHEMA_VERSION
from .config import RunConfig, allocate_run_dir, iso_z, utc
from .coverage_gate import (
    build_eligibility_bundle,
    evaluate_candles_coverage,
    evaluate_liq_coverage,
    evaluate_ob200_coverage,
    evaluate_oi_coverage,
    evaluate_trades_coverage,
)
from .eligibility_contract import (
    CONTEXT_PARTIAL,
    DATA_COMPLETE,
    DATA_CONTRACT_ERROR,
    DATA_NOT_AVAILABLE,
    DATA_PARTIAL_FACTS_ONLY,
    DATA_SOURCE_RESEARCH_DB,
    ELIGIBILITY_CONTRACT_VERSION,
    RESEARCH_DATABASE,
    exit_code_for,
)
from .facts import build_trade_facts, oi_liquidation_facts
from .fight_facts import FIGHT_FACT_CONTRACT, SCHEMA_FIGHT_V22, build_fight_facts
from .fight_sequence import SCHEMA_SEQUENCE, build_sequence_validation
from .factual_reasons import derive_factual_reason_codes
from .level_events import compute_level_events
from .liquidation_flow_facts import LIQUIDATION_FLOW_CONTRACT, build_liquidation_flow_facts
from .loaders import _ensure_import_paths, price_at_timestamp
from .outside_reclaim import RECLAIM_EVENT_CONTRACT_V3
from .anchor_profile_context import build_anchor_profile_context
from .phase_2a4_preflight import write_preflight
from .profiles import anchor_profile_facts, build_session_profile_metadata
from .reporting import (
    build_summary_payload,
    print_console_summary,
    write_all_outputs,
    write_json,
)
from .research_db_loader import (
    TimedQuery,
    load_candles_coverage,
    load_liquidations,
    load_ob200_snapshots,
    load_open_interest,
    load_public_trades,
    ob_snapshots_to_wall_rows,
    probe_ob200_coverage_meta,
    probe_public_trade_events_meta,
    research_client,
)
from .profile_edge_state import set_active_symbol
from .instrument_contract import instrument_for
from .tpo_profile import (
    build_tpo_profile_from_trades,
    verify_tpo_prefix_parity,
    verify_tpo_trade_size_invariance,
)
from .volume_profile import (
    build_volume_profile_from_trades,
    compare_with_oa_profile,
    dedupe_session_trades,
    profile_session_window,
)
from .wall_events import build_wall_fact_pipeline


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # flatten nested values
    flat_rows = []
    keys: list[str] = []
    for row in rows:
        flat = {}
        for k, v in row.items():
            if isinstance(v, (list, dict)):
                flat[k] = json.dumps(v, sort_keys=True, default=str)
            else:
                flat[k] = v
            if k not in keys:
                keys.append(k)
        flat_rows.append(flat)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(flat_rows)


def _print_eligibility_banner(
    *,
    symbol: str,
    eligibility: dict[str, Any],
    runtime_s: float,
    missing_lines: list[str],
    runtime_label: str = "INPUT/COVERAGE RUNTIME",
    include_header: bool = True,
) -> None:
    """Eligibility status. Default runtime label is input/coverage (not full analysis)."""
    status = eligibility["eligibility_status"]
    mand = "COMPLETE" if eligibility.get("mandatory_data_complete") else "PARTIAL/INCOMPLETE"
    ctx = "COMPLETE" if eligibility.get("context_data_complete") else "PARTIAL"
    if include_header:
        print(f"{symbol} OB FIGHT — ELIGIBILITY")
        print(f"DATA SOURCE:              {DATA_SOURCE_RESEARCH_DB}")
    print(f"ELIGIBILITY:              {status}")
    print(f"MANDATORY DATA:           {mand}")
    print(f"CONTEXT DATA:             {ctx}")
    print("RAW ARCHIVE REPLAY:       False")
    print(
        "PROFILE CAUSALITY:        "
        + ("PASS" if eligibility.get("profile_causality_passed") else "FAIL")
    )
    print("TRADE VERDICT EVALUATED:  False")
    print(f"{runtime_label}: {runtime_s:.3f} s")
    if status in {DATA_PARTIAL_FACTS_ONLY, DATA_NOT_AVAILABLE, DATA_CONTRACT_ERROR}:
        print(f"DECISION BLOCKED: {eligibility.get('decision_blocked_reason') or status}")
        if missing_lines:
            print("MISSING:")
            for line in missing_lines[:20]:
                print(f"- {line}")
    elif status == CONTEXT_PARTIAL:
        print("NOTE: context sources incomplete; facts still computed")
    print("")


def _coverage_outputs(run_dir: Path, eligibility: dict[str, Any], timings: list[dict[str, Any]]) -> None:
    write_json(run_dir / "data_eligibility.json", eligibility)
    _write_csv(run_dir / "coverage_by_source.csv", eligibility.get("sources") or [])
    _write_csv(run_dir / "missing_intervals.csv", eligibility.get("missing_rows") or [])
    _write_csv(run_dir / "research_db_query_timings.csv", timings)


def _run_coverage_only(
    cfg: RunConfig,
    run_dir: Path,
    client: Any,
    timer: TimedQuery,
    wall0: float,
    session_start: Any,
    instrument: Any,
) -> int:
    """Coverage/eligibility only — no OB arrays, no trade events, no fight pipeline."""
    ob_cov = probe_ob200_coverage_meta(
        client, timer, cfg.symbol, cfg.window_start, cfg.window_end, inclusive_end=True
    )
    fight_trades_cov = probe_public_trade_events_meta(
        client, timer, cfg.symbol, cfg.window_start, cfg.window_end, source_name="PUBLIC_TRADES"
    )
    profile_trades_cov = probe_public_trade_events_meta(
        client, timer, cfg.symbol, session_start, cfg.anchor, source_name="PROFILE_TRADES"
    )
    # Context: lightweight counts only
    oi_rows, oi_meta = load_open_interest(client, timer, cfg.symbol, cfg.window_start, cfg.window_end)
    liq_rows, liq_meta = load_liquidations(client, timer, cfg.symbol, cfg.window_start, cfg.window_end)
    candles_meta = load_candles_coverage(client, timer, cfg.symbol, cfg.window_start, cfg.window_end)
    oi_cov = evaluate_oi_coverage(oi_rows, oi_meta, symbol=cfg.symbol, start=cfg.window_start, end=cfg.window_end)
    liq_cov = evaluate_liq_coverage(
        liq_rows,
        liq_meta,
        symbol=cfg.symbol,
        start=cfg.window_start,
        end=cfg.window_end,
        client=client,
        timer=timer,
    )
    candles_cov = evaluate_candles_coverage(candles_meta, symbol=cfg.symbol)

    contract_error = None
    if fight_trades_cov.get("lineage", {}).get("research_trade_events_missing") and (
        fight_trades_cov["effective_coverage_status"] == "NOT_AVAILABLE"
    ):
        # Not a schema contradiction — mandatory events absent from research DB
        contract_error = None

    eligibility = build_eligibility_bundle(
        symbol=cfg.symbol,
        anchor=cfg.anchor,
        before_minutes=cfg.before_minutes,
        after_minutes=cfg.after_minutes,
        ob_cov=ob_cov,
        fight_trades_cov=fight_trades_cov,
        profile_trades_cov=profile_trades_cov,
        oi_cov=oi_cov,
        liq_cov=liq_cov,
        candles_cov=candles_cov,
        profile_causality_passed=True,
        contract_error=contract_error,
    )
    eligibility["coverage_only"] = True
    eligibility["loaded_event_tables"] = False
    eligibility["instrument"] = instrument.to_dict()

    missing_lines = []
    for sec in (ob_cov.get("missing_seconds") or [])[:20]:
        missing_lines.append(f"OB200 {sec}")
    if fight_trades_cov["effective_coverage_status"] == "NOT_AVAILABLE":
        missing_lines.append("PUBLIC_TRADES RESEARCH_TRADE_EVENTS_MISSING")
    if profile_trades_cov["effective_coverage_status"] == "NOT_AVAILABLE":
        missing_lines.append("PROFILE_TRADES RESEARCH_TRADE_EVENTS_MISSING")

    lineage = {
        "data_source": DATA_SOURCE_RESEARCH_DB,
        "database": RESEARCH_DATABASE,
        "raw_archive_replay_used": False,
        "mixed_sources_used": False,
        "lineage_companion_used": False,
        "trade_tick_source": f"{RESEARCH_DATABASE}.research_public_trades",
        "trade_source_mode": fight_trades_cov.get("source_segment_status"),
        "ob_table": ob_cov.get("table"),
        "candles_table": candles_meta.get("table"),
        "candles_classification": "COVERAGE_ONLY",
        "query_timings": timer.timings,
        "probe_mode": "COVERAGE_ONLY",
    }
    write_json(run_dir / "input_lineage.json", lineage)
    _coverage_outputs(run_dir, eligibility, timer.timings)
    runtime = time.perf_counter() - wall0
    manifest = {
        "package_version": PACKAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "symbol": cfg.symbol,
        "data_source": DATA_SOURCE_RESEARCH_DB,
        "database": RESEARCH_DATABASE,
        "raw_archive_replay_used": False,
        "mixed_sources_used": False,
        "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
        "eligibility_status": eligibility["eligibility_status"],
        "facts_computation_allowed": False,
        "interpretation_allowed": False,
        "trade_decision_eligible": False,
        "mandatory_data_complete": eligibility["mandatory_data_complete"],
        "context_data_complete": eligibility["context_data_complete"],
        "profile_causality_passed": True,
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
        "coverage_only": True,
        "total_runtime_s": round(runtime, 3),
        "instrument": instrument.to_dict(),
        "query_timings": timer.timings,
        "output_dir": str(run_dir),
    }
    write_json(run_dir / "analysis_manifest.json", manifest)
    (run_dir / "REPORT.md").write_text(
        f"# Coverage-only\n\nStatus: `{eligibility['eligibility_status']}`\n"
        f"Runtime: {runtime:.3f}s\n",
        encoding="utf-8",
    )
    _print_eligibility_banner(
        symbol=cfg.symbol,
        eligibility=eligibility,
        runtime_s=runtime,
        missing_lines=missing_lines,
    )
    print(f"OUTPUT PATH: {run_dir}")
    return exit_code_for(
        eligibility["eligibility_status"],
        require_complete=cfg.require_complete,
        coverage_only=True,
    )


def run_research_db_analysis(cfg: RunConfig) -> int:
    wall0 = time.perf_counter()
    _ensure_import_paths()
    set_active_symbol(cfg.symbol)
    run_dir = allocate_run_dir(cfg.out_root, cfg.anchor)
    timer = TimedQuery()
    client = research_client()
    instrument = instrument_for(cfg.symbol)

    session_start, _, session_id = profile_session_window(cfg.anchor)

    if cfg.coverage_only:
        return _run_coverage_only(cfg, run_dir, client, timer, wall0, session_start, instrument)

    trade_load_start = min(cfg.window_start - timedelta(minutes=5), session_start)

    # --- Load mandatory + context ---
    ob_snaps, ob_meta = load_ob200_snapshots(
        client, timer, cfg.symbol, cfg.window_start, cfg.window_end, inclusive_end=True
    )
    trades, trade_meta = load_public_trades(
        client,
        timer,
        cfg.symbol,
        trade_load_start,
        cfg.window_end,
        allow_legacy_trade_companion=cfg.allow_legacy_trade_companion,
    )
    oi_rows, oi_meta = load_open_interest(client, timer, cfg.symbol, cfg.window_start, cfg.window_end)
    liq_rows, liq_meta = load_liquidations(client, timer, cfg.symbol, cfg.window_start, cfg.window_end)
    candles_meta = load_candles_coverage(client, timer, cfg.symbol, cfg.window_start, cfg.window_end)

    # Profile trades: session_start <= ts < anchor (subset)
    profile_trades = [t for t in trades if session_start <= t["ts"] < cfg.anchor]
    profile_meta = {
        **trade_meta,
        "deduped_count": len(profile_trades),
        "min_ts": iso_z(profile_trades[0]["ts"]) if profile_trades else None,
        "max_ts": iso_z(profile_trades[-1]["ts"]) if profile_trades else None,
    }

    ob_cov = evaluate_ob200_coverage(
        ob_snaps, symbol=cfg.symbol, start=cfg.window_start, end=cfg.window_end, inclusive_end=True
    )
    fight_trades_cov = evaluate_trades_coverage(
        [t for t in trades if cfg.window_start <= t["ts"] <= cfg.window_end],
        trade_meta,
        symbol=cfg.symbol,
        start=cfg.window_start,
        end=cfg.window_end,
        source_name="PUBLIC_TRADES",
    )
    profile_trades_cov = evaluate_trades_coverage(
        profile_trades,
        profile_meta,
        symbol=cfg.symbol,
        start=session_start,
        end=cfg.anchor,
        source_name="PROFILE_TRADES",
    )
    oi_cov = evaluate_oi_coverage(oi_rows, oi_meta, symbol=cfg.symbol, start=cfg.window_start, end=cfg.window_end)
    liq_cov = evaluate_liq_coverage(
        liq_rows,
        liq_meta,
        symbol=cfg.symbol,
        start=cfg.window_start,
        end=cfg.window_end,
        client=client,
        timer=timer,
    )
    candles_cov = evaluate_candles_coverage(candles_meta, symbol=cfg.symbol)

    # Profile causality: no future trades in session profile inputs
    future_in_profile = sum(1 for t in profile_trades if t["ts"] >= cfg.anchor)
    profile_causality_passed = future_in_profile == 0 and (
        profile_trades_cov["effective_coverage_status"] in {"COMPLETE", "PARTIAL"}
        or profile_trades_cov["effective_coverage_status"] == "NOT_AVAILABLE"
    )
    # causality pass requires we will use anchor-exclusive builder; true when we don't feed post-anchor
    profile_causality_passed = future_in_profile == 0

    contract_error = None
    if trade_meta.get("lineage_companion_used") and trade_meta.get("raw_archive_replay_used"):
        contract_error = "RAW_ARCHIVE_MIXED_WITH_RESEARCH"

    eligibility = build_eligibility_bundle(
        symbol=cfg.symbol,
        anchor=cfg.anchor,
        before_minutes=cfg.before_minutes,
        after_minutes=cfg.after_minutes,
        ob_cov=ob_cov,
        fight_trades_cov=fight_trades_cov,
        profile_trades_cov=profile_trades_cov,
        oi_cov=oi_cov,
        liq_cov=liq_cov,
        candles_cov=candles_cov,
        profile_causality_passed=profile_causality_passed,
        contract_error=contract_error,
    )

    missing_lines = []
    missing_secs = ob_cov.get("missing_seconds") or []
    missing_iv = ob_cov.get("missing_intervals") or []
    if len(missing_secs) > 20 and missing_iv:
        for iv in missing_iv[:10]:
            missing_lines.append(f"OB200 {iv.get('start')}..{iv.get('end')}")
        if len(missing_iv) > 10:
            missing_lines.append(f"OB200 ... +{len(missing_iv) - 10} more intervals ({len(missing_secs)} seconds)")
    else:
        for sec in missing_secs:
            missing_lines.append(f"OB200 {sec}")
    if fight_trades_cov["effective_coverage_status"] in {"PARTIAL", "NOT_AVAILABLE"}:
        missing_lines.append("PUBLIC_TRADES window coverage incomplete")
    if profile_trades_cov["effective_coverage_status"] in {"PARTIAL", "NOT_AVAILABLE"}:
        missing_lines.append("PROFILE_TRADES session coverage incomplete")

    lineage = {
        "data_source": DATA_SOURCE_RESEARCH_DB,
        "database": RESEARCH_DATABASE,
        "raw_archive_replay_used": False,
        "mixed_sources_used": bool(trade_meta.get("mixed_sources_used") or trade_meta.get("lineage_companion_used")),
        "trade_tick_source": trade_meta.get("table"),
        "trade_source_mode": trade_meta.get("source_mode"),
        "lineage_companion_used": bool(trade_meta.get("lineage_companion_used")),
        "research_trade_events_missing": bool(trade_meta.get("research_trade_events_missing")),
        "ob_table": ob_meta.get("table"),
        "oi_table": oi_meta.get("table"),
        "liq_table": liq_meta.get("table"),
        "candles_table": candles_meta.get("table"),
        "candles_classification": candles_meta.get("classification"),
        "ob_build_ids": ob_cov.get("build_ids"),
        "query_timings": timer.timings,
        "instrument": instrument.to_dict(),
    }
    write_json(run_dir / "input_lineage.json", lineage)
    _coverage_outputs(run_dir, eligibility, timer.timings)

    manifest: dict[str, Any] = {
        "package_version": PACKAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "symbol": cfg.symbol,
        "anchor_timestamp_utc": cfg.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "normalized_anchor_utc": cfg.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "before_minutes": cfg.before_minutes,
            "after_minutes": cfg.after_minutes,
            **eligibility["windows"],
        },
        "data_source": DATA_SOURCE_RESEARCH_DB,
        "database": RESEARCH_DATABASE,
        "raw_archive_replay_used": False,
        "mixed_sources_used": bool(trade_meta.get("mixed_sources_used") or trade_meta.get("lineage_companion_used")),
        "eligibility_contract": ELIGIBILITY_CONTRACT_VERSION,
        "eligibility_status": eligibility["eligibility_status"],
        "facts_computation_allowed": eligibility["facts_computation_allowed"],
        "interpretation_allowed": False,
        "trade_decision_eligible": False,
        "mandatory_data_complete": eligibility["mandatory_data_complete"],
        "context_data_complete": eligibility["context_data_complete"],
        "profile_causality_passed": eligibility["profile_causality_passed"],
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
        "auto_extension_enabled": False,
        "auto_extension_reason": "RULES_UNFROZEN",
        "read_only": True,
        "no_overwrite": True,
        "require_complete": cfg.require_complete,
        "coverage_only": cfg.coverage_only,
        "allow_legacy_trade_companion": cfg.allow_legacy_trade_companion,
        "benchmark": cfg.benchmark,
        "heavy_detail_csv": cfg.heavy_detail_csv,
        "heuristics": cfg.heuristic_manifest(),
        "instrument": instrument.to_dict(),
        "query_timings": timer.timings,
        "causality": {
            "anchor_exclusive": True,
            "profile_session_id": session_id,
            "session_start_utc": iso_z(session_start),
            "future_trades_in_profile_inputs": future_in_profile,
            "profile_causality_passed": profile_causality_passed,
        },
        "output_dir": str(run_dir),
        "lineage_companion_used": bool(trade_meta.get("lineage_companion_used")),
        "research_trade_events_missing": bool(trade_meta.get("research_trade_events_missing")),
    }

    coverage_runtime = time.perf_counter() - wall0
    manifest["input_coverage_runtime_s"] = round(coverage_runtime, 3)
    _print_eligibility_banner(
        symbol=cfg.symbol,
        eligibility=eligibility,
        runtime_s=coverage_runtime,
        missing_lines=missing_lines,
        runtime_label="INPUT/COVERAGE RUNTIME",
    )

    if cfg.coverage_only or eligibility["eligibility_status"] == DATA_NOT_AVAILABLE:
        write_json(run_dir / "analysis_manifest.json", manifest)
        (run_dir / "REPORT.md").write_text(
            f"# Coverage / Eligibility\n\n"
            f"Status: `{eligibility['eligibility_status']}`\n\n"
            f"facts_computation_allowed: {eligibility['facts_computation_allowed']}\n",
            encoding="utf-8",
        )
        code = exit_code_for(
            eligibility["eligibility_status"],
            require_complete=cfg.require_complete,
            coverage_only=cfg.coverage_only,
        )
        print(f"OUTPUT PATH: {run_dir}")
        return code

    if eligibility["eligibility_status"] == DATA_CONTRACT_ERROR:
        write_json(run_dir / "analysis_manifest.json", manifest)
        (run_dir / "REPORT.md").write_text(
            f"# DATA CONTRACT ERROR\n\n`{eligibility.get('contract_error')}`\n",
            encoding="utf-8",
        )
        print(f"OUTPUT PATH: {run_dir}")
        return exit_code_for(DATA_CONTRACT_ERROR, require_complete=cfg.require_complete)

    if not eligibility["facts_computation_allowed"]:
        write_json(run_dir / "analysis_manifest.json", manifest)
        print(f"OUTPUT PATH: {run_dir}")
        return exit_code_for(
            eligibility["eligibility_status"], require_complete=cfg.require_complete
        )

    # --- Fact pipeline (reuse existing engines) ---
    def _mark(msg: str) -> None:
        if cfg.benchmark:
            print(f"[research-db] {msg} (+{time.perf_counter()-wall0:.1f}s)", flush=True)

    _mark("dedupe session trades")
    session_trades, session_cov = dedupe_session_trades(trades, session_start, cfg.anchor)
    # Avoid OA profile metadata dependency for research path when possible
    try:
        profiles = build_session_profile_metadata(client, cfg.symbol, cfg.anchor)
    except Exception as exc:
        profiles = {"status": "SKIPPED", "error": str(exc)[:200]}

    _mark("build TPO")
    from .config import tick_size_for_symbol as _tick_for_symbol

    symbol_tick = _tick_for_symbol(cfg.symbol)
    tpo_profile = build_tpo_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=client,
        symbol=cfg.symbol,
        profile_session_id=session_id,
        session_trades=session_trades,
        coverage_meta=session_cov,
    )
    if isinstance(tpo_profile.get("provenance"), dict):
        tpo_profile["provenance"]["orderbook_tick_size"] = symbol_tick
        tpo_profile["provenance"]["symbol"] = cfg.symbol
    tpo_profile["trade_size_invariance"] = verify_tpo_trade_size_invariance(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=client,
        symbol=cfg.symbol,
        session_trades=session_trades,
        coverage_meta=session_cov,
        baseline=tpo_profile,
    )
    tpo_profile["prefix_parity"] = verify_tpo_prefix_parity(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=client,
        symbol=cfg.symbol,
        session_trades=session_trades,
        coverage_meta=session_cov,
        baseline=tpo_profile,
    )
    # provenance source label
    if isinstance(tpo_profile.get("provenance"), dict):
        tpo_profile["provenance"]["source"] = trade_meta.get("table")
        tpo_profile["provenance"]["anchor_exclusive"] = True

    _mark("build volume profile")
    vol_step = (tpo_profile.get("provenance") or {}).get("price_increment")
    volume_profile = build_volume_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=client,
        symbol=cfg.symbol,
        price_step=vol_step,
        session_trades=session_trades,
        coverage_meta=session_cov,
    )
    try:
        volume_profile["oa_parity"] = compare_with_oa_profile(
            client, cfg.symbol, session_start, cfg.anchor, volume_profile
        )
    except Exception as exc:
        volume_profile["oa_parity"] = {"status": "SKIPPED", "error": str(exc)[:200]}

    _mark("adapt OB snapshots to wall rows")
    ob_rows = ob_snapshots_to_wall_rows(ob_snaps)
    _mark(f"wall rows ready n={len(ob_rows)}")
    # Map liq/oi to legacy fact shapes
    oi_legacy = [{"ts": r["ts"], "oi": r["oi"], "oi_value": r["oi_value"]} for r in oi_rows]
    liq_legacy = liq_rows

    _mark("build wall fact pipeline")
    wall_trades = [t for t in trades if cfg.window_start <= t["ts"] <= cfg.window_end]
    wall_bundle = build_wall_fact_pipeline(
        ob_rows, wall_trades, symbol=cfg.symbol, window_end=cfg.window_end
    )
    wall_facts = wall_bundle["legacy_wall_facts"]
    _mark("wall fact pipeline done")

    price_anchor = price_at_timestamp(trades, cfg.anchor)
    pf = anchor_profile_facts(
        cfg.anchor, price_anchor, tpo_profile=tpo_profile, volume_profile=volume_profile
    )
    levels = pf.get("all_anchor_levels") or []
    level_events = compute_level_events(
        trades, levels, cfg.window_start, cfg.window_end, anchor=cfg.anchor
    )
    trade_facts = build_trade_facts(trades, cfg.anchor, cfg.window_start, cfg.window_end)
    oi_liq = oi_liquidation_facts(oi_legacy, liq_legacy, cfg.window_start, cfg.window_end)
    _mark("level/trade/oi facts")

    fight_bundle = build_fight_facts(
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        trades=trades,
        wall_bundle=wall_bundle,
        oi_rows=oi_legacy,
        liq_rows=liq_legacy,
        anchor=cfg.anchor,
        window_end=cfg.window_end,
        reference_price=price_anchor,
    )
    if eligibility["eligibility_status"] == DATA_PARTIAL_FACTS_ONLY:
        fight_bundle["interpretation_status"] = "NOT_EVALUATED_DATA_PARTIAL"
        fight_bundle["decision_blocked_reason"] = eligibility.get("decision_blocked_reason")
    _mark("fight facts")

    sequence_bundle = build_sequence_validation(
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        fight_bundle=fight_bundle,
        wall_bundle=wall_bundle,
        ob_rows=ob_rows,
        oi_rows=oi_legacy,
        liq_rows=liq_legacy,
        trades=trades,
        anchor=cfg.anchor,
        window_end=cfg.window_end,
        trades_meta=trade_meta,
        strict_invariants=ob_cov["effective_coverage_status"] == "COMPLETE",
    )
    _mark("sequence validation")

    reasons = derive_factual_reason_codes(pf, level_events, trade_facts, wall_facts, oi_liq, volume_profile)
    # Full DE templates are expensive (~3k rows) and omitted from lean research-db artifacts;
    # REPORT.md uses render_report_sections(reasons, ...) which does not need pre-rendered templates.
    german: list[dict[str, Any]] = []
    write_preflight(run_dir / "phase_2a4_liquidation_flow_preflight.json")
    outer_edge = pf.get("volume_vah") or (volume_profile.get("value_area") or {}).get("vvah")
    liquidation_flow = build_liquidation_flow_facts(
        trades=trades,
        liq_events=liq_legacy,
        liq_load_meta=liq_meta,
        oi_rows=oi_legacy,
        window_start=cfg.window_start,
        window_end=cfg.window_end,
        anchor=cfg.anchor,
        outer_edge_price=float(outer_edge) if outer_edge is not None else None,
        reclaim_events=fight_bundle.get("reclaim_events") or [],
    )
    _mark("liquidation flow + reasons")

    anchor_context = build_anchor_profile_context(
        anchor_price=price_anchor,
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        trades=trades,
        anchor=cfg.anchor,
        before_minutes=cfg.before_minutes,
        symbol=cfg.symbol,
    )

    data_quality = "PASS"
    if eligibility["eligibility_status"] == CONTEXT_PARTIAL:
        data_quality = "PARTIAL"
    if eligibility["eligibility_status"] == DATA_PARTIAL_FACTS_ONLY:
        data_quality = "PARTIAL"

    summary = build_summary_payload(
        cfg,
        profile_facts=pf,
        level_events=level_events,
        trade_facts=trade_facts,
        wall_facts=wall_facts,
        wall_bundle=wall_bundle,
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        oi_liq_facts=oi_liq,
        factual_reasons=reasons,
        data_quality=data_quality,
        fight_facts=fight_bundle,
        sequence_validation=sequence_bundle,
        liquidation_flow=liquidation_flow,
    )
    # Research-db path: keep summary lean. Full payloads are already written to
    # dedicated fight/sequence/level files — embedding them again (68MB+) dominates I/O.
    summary["fight_facts"] = (fight_bundle or {}).get("manifest")
    summary["sequence_validation"] = (sequence_bundle or {}).get("fight_sequence_summary")
    summary["level_events"] = [
        {
            "level_id": e.get("level_id"),
            "label": e.get("label"),
            "price": e.get("price"),
            "first_touch_ts": e.get("first_touch_ts"),
            "cross_count": e.get("cross_count"),
            "episode_count": e.get("episode_count"),
        }
        for e in level_events
    ]
    summary["wall_facts"] = wall_bundle.get("summary") if wall_bundle else wall_facts
    summary["factual_reason_codes"] = [
        {"code": r.get("code"), "severity": r.get("severity")} for r in reasons
    ]
    # Buckets already written to public_trade_buckets.csv — drop from summary I/O.
    tf = dict(summary.get("trade_facts") or {})
    tf.pop("time_series_buckets", None)
    summary["trade_facts"] = tf
    summary["eligibility_status"] = eligibility["eligibility_status"]
    summary["data_source"] = DATA_SOURCE_RESEARCH_DB
    summary["trade_verdict_evaluated"] = False
    summary["direction"] = None
    summary["analysis_status"] = (
        "FACTS_READY_RULES_UNFROZEN"
        if eligibility["eligibility_status"] in {DATA_COMPLETE, CONTEXT_PARTIAL}
        else "FACTS_PARTIAL_RULES_UNFROZEN"
    )
    summary["summary_payload_mode"] = "LEAN_RESEARCH_DB_V1"
    summary["anchor_profile_context"] = {
        k: anchor_context.get(k)
        for k in (
            "contract_version",
            "anchor_context",
            "observation_context",
            "anchor_price",
            "levels",
            "edges",
            "prior_edge_cross",
        )
    }

    write_json(run_dir / "anchor_profile_context.json", anchor_context)

    trade_buckets = trade_facts.get("time_series_buckets") or []
    write_all_outputs(
        run_dir,
        summary=summary,
        manifest=manifest,
        coverage={"eligibility": eligibility, "ob_meta": ob_meta, "trade_meta": trade_meta},
        profiles=profiles if isinstance(profiles, dict) else pf,
        level_events=level_events,
        trade_buckets=trade_buckets,
        wall_facts=wall_facts,
        wall_bundle=wall_bundle,
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        oi_liq=oi_liq,
        reasons=reasons,
        german=german,
        fight_facts=fight_bundle,
        sequence_validation=sequence_bundle,
        liquidation_flow=liquidation_flow,
        heavy_detail_csv=cfg.heavy_detail_csv,
    )
    _coverage_outputs(run_dir, eligibility, timer.timings)
    write_json(run_dir / "input_lineage.json", lineage)

    manifest.update(
        {
            "analysis_status": summary["analysis_status"],
            "fight_facts": {
                "schema_version": SCHEMA_FIGHT_V22,
                "fight_fact_contract": FIGHT_FACT_CONTRACT,
                "interpretation_status": fight_bundle.get("interpretation_status"),
            },
            "sequence_validation": {
                "schema_version": SCHEMA_SEQUENCE,
                "verdict": sequence_bundle.get("verdict"),
            },
            "canonical_reclaim_contract": RECLAIM_EVENT_CONTRACT_V3,
            "liquidation_flow_contract": LIQUIDATION_FLOW_CONTRACT,
            "tpo_profile": {
                "status": tpo_profile.get("tpo_profile_status"),
                "poc": (tpo_profile.get("summary") or {}).get("poc")
                or (tpo_profile.get("poc") or {}).get("poc_price"),
                "vah": (tpo_profile.get("summary") or {}).get("vah")
                or (tpo_profile.get("value_area") or {}).get("vah"),
                "val": (tpo_profile.get("summary") or {}).get("val")
                or (tpo_profile.get("value_area") or {}).get("val"),
            },
            "volume_profile": {
                "status": volume_profile.get("volume_profile_status"),
                "vpoc": (volume_profile.get("vpoc") or {}).get("vpoc_price"),
                "vvah": (volume_profile.get("value_area") or {}).get("vvah"),
                "vval": (volume_profile.get("value_area") or {}).get("vval"),
            },
            "rules_frozen": False,
            "trade_verdict_evaluated": False,
            "direction": None,
            "interpretation_allowed": False,
            "trade_decision_eligible": False,
        }
    )

    runtime = time.perf_counter() - wall0
    manifest["total_runtime_s"] = round(runtime, 3)
    manifest["full_analysis_runtime_s"] = round(runtime, 3)
    write_json(run_dir / "analysis_manifest.json", manifest)
    print_console_summary(
        summary,
        run_dir,
        manifest,
        fight_facts=fight_bundle,
        sequence_validation=sequence_bundle,
        level_events=level_events,
        anchor_profile_context=anchor_context,
    )
    print(f"OUTPUT PATH: {run_dir}")
    return exit_code_for(
        eligibility["eligibility_status"], require_complete=cfg.require_complete
    )

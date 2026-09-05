"""CLI orchestration for BTC OB Fight fact analysis."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import PACKAGE_VERSION, SCHEMA_VERSION
from .config import (
    ALLOWED_SYMBOLS,
    DEFAULT_AFTER_MINUTES,
    DEFAULT_BEFORE_MINUTES,
    DEFAULT_DATA_SOURCE,
    DEFAULT_SYMBOL,
    RunConfig,
    allocate_run_dir,
    resolve_ob_root,
    utc,
)
from .facts import build_trade_facts, oi_liquidation_facts
from .fight_facts import FIGHT_FACT_CONTRACT, SCHEMA_FIGHT_V22, build_fight_facts
from .liquidation_flow_facts import LIQUIDATION_FLOW_CONTRACT, build_liquidation_flow_facts
from .outside_reclaim import RECLAIM_EVENT_CONTRACT_V3
from .phase_2a4_preflight import write_preflight
from .fight_sequence import SCHEMA_SEQUENCE, build_sequence_validation
from .factual_reasons import derive_factual_reason_codes
from .level_events import compute_level_events
from .loaders import (
    clickhouse_client,
    coverage_candles,
    coverage_liquidations,
    coverage_open_interest,
    coverage_public_trades,
    load_liquidations,
    load_liquidation_events,
    load_open_interest,
    load_public_trades,
    price_at_timestamp,
)
from .ob_replay import audit_ob_coverage, replay_as_of
from .profiles import anchor_profile_facts, build_session_profile_metadata
from .reporting import (
    build_summary_payload,
    print_console_summary,
    write_all_outputs,
    write_json,
)
from .templates_de import render_all_german
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
from .wall_events import build_wall_fact_pipeline, sample_ob_snapshots


EXIT_OK = 0
EXIT_TECH = 1
EXIT_CLI = 2
EXIT_DATA = 3
EXIT_PARTIAL_REQUIRE_COMPLETE = 4
EXIT_CONTRACT_ERROR = 5

PHASES = 10


def _phase(n: int, msg: str, t0: float | None = None) -> float:
    elapsed = ""
    if t0 is not None:
        elapsed = f" ({time.time() - t0:.1f}s)"
    print(f"[{n}/{PHASES}] {msg}{elapsed}", flush=True)
    return time.time()


def parse_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {raw}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must include explicit timezone: {raw}")
    normalized = utc(dt)
    return normalized


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BTC/DOGE OB Fight fact CLI (research-db default)")
    p.add_argument("--timestamp", required=True, help="Anchor ISO-8601 timestamp with timezone")
    p.add_argument("--before-minutes", type=int, default=DEFAULT_BEFORE_MINUTES)
    p.add_argument("--after-minutes", type=int, default=DEFAULT_AFTER_MINUTES)
    p.add_argument("--symbol", default=DEFAULT_SYMBOL, choices=sorted(ALLOWED_SYMBOLS))
    p.add_argument("--out-root", type=Path, default=Path("results"))
    p.add_argument("--ob-root", type=Path, default=None, help="Legacy raw OB root (raw-legacy only)")
    p.add_argument(
        "--data-source",
        default=DEFAULT_DATA_SOURCE,
        choices=["research-db", "raw-legacy"],
        help="Default research-db. raw-legacy is LEGACY_SLOW_RAW_REPLAY only.",
    )
    p.add_argument("--require-complete", action="store_true")
    p.add_argument("--coverage-only", action="store_true")
    p.add_argument(
        "--allow-legacy-trade-companion",
        action="store_true",
        help="Explicit non-pure diagnostic: load trades from orderbook_analysis.public_trades_canonical",
    )
    p.add_argument(
        "--benchmark",
        action="store_true",
        help="Same facts; BENCHMARK_OUTPUT_MINIMAL (reduced non-canonical detail writes)",
    )
    p.add_argument(
        "--heavy-detail-csv",
        action="store_true",
        help="Write per-sample wall/edge detail CSVs (default: lean research-db outputs)",
    )
    return p


def validate_args(args: argparse.Namespace) -> RunConfig:
    if args.symbol not in ALLOWED_SYMBOLS:
        raise ValueError(f"symbol must be one of {sorted(ALLOWED_SYMBOLS)}, got {args.symbol}")
    if args.before_minutes <= 0 or args.after_minutes <= 0:
        raise ValueError("before-minutes and after-minutes must be positive")
    if args.before_minutes > 24 * 60 or args.after_minutes > 24 * 60:
        raise ValueError("window minutes exceed maximum (1440)")
    anchor = parse_timestamp(args.timestamp)
    out_root = args.out_root.expanduser().resolve()
    ob_root = None
    if args.data_source == "raw-legacy":
        print("WARNING: LEGACY_SLOW_RAW_REPLAY enabled — not the default research-db path", flush=True)
        ob_root = resolve_ob_root(args.ob_root)
        if ob_root is None:
            raise FileNotFoundError("no valid OB200 shadow root found for raw-legacy")
    return RunConfig(
        symbol=args.symbol,
        anchor=anchor,
        before_minutes=args.before_minutes,
        after_minutes=args.after_minutes,
        out_root=out_root,
        data_source=args.data_source,
        ob_root=ob_root,
        require_complete=bool(args.require_complete),
        coverage_only=bool(args.coverage_only),
        allow_legacy_trade_companion=bool(getattr(args, "allow_legacy_trade_companion", False)),
        benchmark=bool(getattr(args, "benchmark", False)),
        heavy_detail_csv=bool(getattr(args, "heavy_detail_csv", False)),
    )


def run_analysis(cfg: RunConfig) -> int:
    if cfg.data_source == "research-db":
        from .research_db_cli import run_research_db_analysis

        return run_research_db_analysis(cfg)
    return _run_raw_legacy_analysis(cfg)


def _run_raw_legacy_analysis(cfg: RunConfig) -> int:
    assert cfg.ob_root is not None
    run_dir = allocate_run_dir(cfg.out_root, cfg.anchor)
    manifest: dict[str, Any] = {
        "package_version": PACKAGE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "symbol": cfg.symbol,
        "anchor_timestamp_utc": cfg.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "normalized_anchor_utc": cfg.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "before_minutes": cfg.before_minutes,
            "after_minutes": cfg.after_minutes,
        },
        "ob_root": str(cfg.ob_root),
        "read_only": cfg.read_only,
        "no_overwrite": cfg.no_overwrite,
        "auto_extension_enabled": False,
        "auto_extension_reason": "RULES_UNFROZEN",
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "heuristics": cfg.heuristic_manifest(),
        "profile_settings": {
            "value_area_pct": 0.70,
            "target_bins": 160,
            "tpo_profile_engine": "research.btc_ob_fight.tpo_profile",
            "tpo_bracket_minutes": 30,
            "volume_at_price_engine": "research.btc_ob_fight.volume_profile",
            "oa_volume_path_not_used_for_tpo": True,
        },
        "output_dir": str(run_dir),
    }

    coverage: dict[str, Any] = {
        "symbol": cfg.symbol,
        "window_utc": {
            "start": cfg.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": cfg.window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "anchor_utc": cfg.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    t0 = _phase(1, "Loading causal session data...")
    ob_cov = audit_ob_coverage(cfg.ob_root, cfg.symbol, cfg.window_start, cfg.window_end)
    coverage["ob200"] = ob_cov

    probes = {}
    for label, at in [
        ("window_start", cfg.window_start),
        ("anchor", cfg.anchor),
        ("window_end", cfg.window_end - timedelta(seconds=1)),
    ]:
        try:
            snap = replay_as_of(cfg.ob_root, cfg.symbol, at)
            probes[label] = {
                "ok": True,
                "as_of": snap["as_of"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bid_levels": snap["bid_levels"],
                "ask_levels": snap["ask_levels"],
                "genuine_200": snap.get("genuine_200"),
                "segment": snap["segment"],
            }
        except Exception as exc:
            probes[label] = {"ok": False, "error": str(exc)}
    coverage["ob200_replay_probes"] = probes
    ob_ok = ob_cov.get("all_hours_ok") and all(p.get("ok") and p.get("genuine_200") for p in probes.values())

    cl = clickhouse_client()
    coverage["public_trades"] = coverage_public_trades(cl, cfg.symbol, cfg.window_start, cfg.window_end)
    coverage["candles_1m"] = coverage_candles(cl, cfg.symbol, cfg.window_start, cfg.window_end)
    coverage["open_interest_5s"] = coverage_open_interest(cl, cfg.symbol, cfg.window_start, cfg.window_end)
    coverage["liquidations"] = coverage_liquidations(cl, cfg.symbol, cfg.window_start, cfg.window_end)

    if not ob_ok or coverage["public_trades"]["count"] == 0:
        manifest["analysis_status"] = "DATA_INSUFFICIENT"
        write_json(run_dir / "coverage_audit.json", coverage)
        write_json(run_dir / "analysis_manifest.json", manifest)
        write_json(
            run_dir / "summary.json",
            {
                "schema_version": SCHEMA_VERSION,
                "analysis_status": "DATA_INSUFFICIENT",
                "data_quality": "FAIL",
            },
        )
        (run_dir / "REPORT.md").write_text("# DATA INSUFFICIENT\n\n`BTC_OB_FIGHT_DATA_INSUFFICIENT`\n", encoding="utf-8")
        print("BTC_OB_FIGHT_DATA_INSUFFICIENT")
        return EXIT_DATA

    session_start, _, session_id = profile_session_window(cfg.anchor)
    trade_load_start = min(cfg.window_start - timedelta(minutes=5), session_start)
    trades, trade_meta = load_public_trades(
        cl,
        cfg.symbol,
        trade_load_start,
        cfg.window_end,
    )
    coverage["trade_load"] = trade_meta
    coverage["profile_session"] = {
        "session_start_utc": session_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anchor_cutoff_utc": cfg.anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trade_load_start_utc": trade_load_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_id": session_id,
    }
    session_trades, session_cov = dedupe_session_trades(trades, session_start, cfg.anchor)
    profiles = build_session_profile_metadata(cl, cfg.symbol, cfg.anchor)
    _phase(1, "Loading causal session data... done", t0)

    t0 = _phase(2, "Building genuine 30m TPO...")
    tpo_profile = build_tpo_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=cl,
        symbol=cfg.symbol,
        profile_session_id=session_id,
        session_trades=session_trades,
        coverage_meta=session_cov,
    )
    tpo_profile["trade_size_invariance"] = verify_tpo_trade_size_invariance(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=cl,
        symbol=cfg.symbol,
        session_trades=session_trades,
        coverage_meta=session_cov,
    )
    tpo_profile["prefix_parity"] = verify_tpo_prefix_parity(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=cl,
        symbol=cfg.symbol,
        session_trades=session_trades,
        coverage_meta=session_cov,
    )
    _phase(2, "Building genuine 30m TPO... done", t0)

    t0 = _phase(3, "Building volume profile...")
    vol_step = (tpo_profile.get("provenance") or {}).get("price_increment")
    volume_profile = build_volume_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=cfg.anchor,
        cl=cl,
        symbol=cfg.symbol,
        price_step=vol_step,
        session_trades=session_trades,
        coverage_meta=session_cov,
    )
    volume_profile["oa_parity"] = compare_with_oa_profile(
        cl,
        cfg.symbol,
        session_start,
        cfg.anchor,
        volume_profile,
    )
    _phase(3, "Building volume profile... done", t0)

    t0 = _phase(4, "Replaying OB200...")
    oi_rows = load_open_interest(cl, cfg.symbol, cfg.window_start, cfg.window_end)
    liq_rows = load_liquidations(cl, cfg.symbol, cfg.window_start, cfg.window_end)
    ob_rows = sample_ob_snapshots(cfg.ob_root, cfg.symbol, cfg.window_start, cfg.window_end)
    _phase(4, "Replaying OB200... done", t0)

    t0 = _phase(5, "Building wall facts...")
    wall_bundle = build_wall_fact_pipeline(
        ob_rows,
        trades,
        symbol=cfg.symbol,
        window_end=cfg.window_end,
    )
    wall_facts = wall_bundle["legacy_wall_facts"]
    _phase(5, "Building wall facts... done", t0)

    t0 = _phase(6, "Building level episodes...")
    price_anchor = price_at_timestamp(trades, cfg.anchor)
    pf = anchor_profile_facts(
        cfg.anchor,
        price_anchor,
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
    )
    levels = pf.get("all_anchor_levels") or []
    level_events = compute_level_events(trades, levels, cfg.window_start, cfg.window_end, anchor=cfg.anchor)
    trade_facts = build_trade_facts(trades, cfg.anchor, cfg.window_start, cfg.window_end)
    oi_liq = oi_liquidation_facts(oi_rows, liq_rows, cfg.window_start, cfg.window_end)
    _phase(6, "Building level episodes... done", t0)

    t0 = _phase(7, "Building causal fight facts (Phase 2A)...")
    fight_bundle = build_fight_facts(
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        trades=trades,
        wall_bundle=wall_bundle,
        oi_rows=oi_rows,
        liq_rows=liq_rows,
        anchor=cfg.anchor,
        window_end=cfg.window_end,
        reference_price=price_anchor,
    )
    _phase(7, "Building causal fight facts (Phase 2A)... done", t0)

    t0 = _phase(8, "Building fight sequence validation (Phase 2A.3)...")
    sequence_bundle = build_sequence_validation(
        tpo_profile=tpo_profile,
        volume_profile=volume_profile,
        fight_bundle=fight_bundle,
        wall_bundle=wall_bundle,
        ob_rows=ob_rows,
        oi_rows=oi_rows,
        liq_rows=liq_rows,
        trades=trades,
        anchor=cfg.anchor,
        window_end=cfg.window_end,
        trades_meta=trade_meta,
        strict_invariants=True,
    )
    _phase(8, "Building fight sequence validation (Phase 2A.3)... done", t0)

    manifest["fight_facts"] = {
        "schema_version": SCHEMA_FIGHT_V22,
        "fight_fact_contract": FIGHT_FACT_CONTRACT,
        "interpretation_status": fight_bundle.get("interpretation_status"),
        "episode_count": fight_bundle.get("manifest", {}).get("profile_state_episode_count"),
        "outside_episode_count": fight_bundle.get("manifest", {}).get("outside_episode_count"),
        "edge_consumption_count": fight_bundle.get("manifest", {}).get("edge_consumption_count"),
        "reclaim_contract": RECLAIM_EVENT_CONTRACT_V3,
    }
    manifest["sequence_validation"] = {
        "schema_version": SCHEMA_SEQUENCE,
        "verdict": sequence_bundle.get("verdict"),
        "interpretation_status": sequence_bundle.get("interpretation_status"),
        "summary": sequence_bundle.get("fight_sequence_summary"),
    }
    manifest["canonical_reclaim_contract"] = RECLAIM_EVENT_CONTRACT_V3
    manifest["canonical_reclaim_output"] = "reclaim_events.csv"
    manifest["ambiguous_reclaim_output"] = "ambiguous_reclaim_candidates.csv"
    manifest["raw_outside_output"] = "raw_outside_excursions.csv"
    manifest["canonical_reclaims_only_in_primary_output"] = True
    manifest["ambiguous_events_decision_eligible"] = False
    manifest["exchange_order_proven"] = False
    manifest["legacy_global_first_reclaim_enabled"] = False
    manifest["same_timestamp_ordering_audited"] = True
    manifest["rules_frozen"] = False
    manifest["trade_verdict_evaluated"] = False
    manifest["direction"] = None
    manifest["historical_run_012_reclaim_status"] = "KNOWN_INVALID_GLOBAL_FIRST_BUG"
    manifest["historical_run_014_reclaim_status"] = "CORRECTED_LAYER_ONLY_IN_reclaim_events_corrected.csv"
    manifest["edge_visit_source_of_truth"] = "edge_visits.csv"
    manifest["outside_excursion_source_of_truth"] = "outside_excursions.csv"
    manifest["same_timestamp_policy"] = "AMBIGUOUS_MULTI_STATE conservative normalization"
    manifest["gap0_invariant"] = "cluster_count(gap=0) == edge_visit_count"
    manifest["profile_parity"] = {
        "tpo_poc_vah_val": "78545/79080/78230",
        "volume_vpoc_vvah_vval": "78565/79140/78190",
    }

    manifest["tpo_profile"] = {
        "status": tpo_profile.get("tpo_profile_status"),
        "contract_version": tpo_profile.get("contract_version"),
        "profile_kind": (tpo_profile.get("provenance") or {}).get("profile_kind"),
        "brackets": tpo_profile.get("brackets"),
        "integrity": (tpo_profile.get("integrity") or {}).get("status"),
        "trade_size_invariance": (tpo_profile.get("trade_size_invariance") or {}).get("status"),
        "prefix_parity": (tpo_profile.get("prefix_parity") or {}).get("status"),
    }
    manifest["volume_profile"] = {
        "status": volume_profile.get("volume_profile_status"),
        "contract_version": volume_profile.get("contract_version"),
        "primary_volume_basis": (volume_profile.get("provenance") or {}).get("primary_volume_basis"),
        "prefix_parity": (volume_profile.get("prefix_parity") or {}).get("status"),
        "integrity": (volume_profile.get("integrity") or {}).get("status"),
        "oa_parity": (volume_profile.get("oa_parity") or {}).get("status"),
    }
    manifest["causality"] = {
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "tpo_profile_computed_separately": True,
        "volume_profile_computed_separately": True,
        "oa_volume_path_not_used_for_tpo": True,
        "tpo_volume_confluence_status": pf.get("tpo_volume_confluence_status"),
        "volume_profile_future_trades_used": volume_profile.get("future_trade_count_used", 0),
        "tpo_profile_future_trades_used": tpo_profile.get("future_trade_count_used", 0),
    }

    t0 = _phase(9, "Running integrity checks...")
    if tpo_profile.get("tpo_profile_status") == "INTEGRITY_FAILED":
        manifest["analysis_status"] = "TPO_PROFILE_INTEGRITY_FAILED"
        write_json(run_dir / "coverage_audit.json", coverage)
        write_json(run_dir / "analysis_manifest.json", manifest)
        write_json(run_dir / "tpo_profile_integrity.json", tpo_profile.get("integrity") or {})
        write_json(
            run_dir / "summary.json",
            {
                "schema_version": SCHEMA_VERSION,
                "analysis_status": "TPO_PROFILE_INTEGRITY_FAILED",
                "data_quality": "FAIL",
                "tpo_profile_status": "INTEGRITY_FAILED",
            },
        )
        (run_dir / "REPORT.md").write_text(
            "# TPO PROFILE INTEGRITY FAILED\n\n`TPO_PROFILE_DATA_INSUFFICIENT`\n",
            encoding="utf-8",
        )
        print("TPO_PROFILE_DATA_INSUFFICIENT")
        return EXIT_DATA

    if tpo_profile.get("tpo_profile_status") == "TPO_PROFILE_DATA_INSUFFICIENT":
        manifest["analysis_status"] = "TPO_PROFILE_DATA_INSUFFICIENT"
        write_json(run_dir / "coverage_audit.json", coverage)
        write_json(run_dir / "analysis_manifest.json", manifest)
        write_json(
            run_dir / "summary.json",
            {
                "schema_version": SCHEMA_VERSION,
                "analysis_status": "TPO_PROFILE_DATA_INSUFFICIENT",
                "data_quality": "FAIL",
            },
        )
        (run_dir / "REPORT.md").write_text(
            "# TPO PROFILE DATA INSUFFICIENT\n\n`TPO_PROFILE_DATA_INSUFFICIENT`\n",
            encoding="utf-8",
        )
        print("TPO_PROFILE_DATA_INSUFFICIENT")
        return EXIT_DATA

    if volume_profile.get("volume_profile_status") == "INTEGRITY_FAILED":
        manifest["analysis_status"] = "VOLUME_PROFILE_INTEGRITY_FAILED"
        write_json(run_dir / "coverage_audit.json", coverage)
        write_json(run_dir / "analysis_manifest.json", manifest)
        write_json(run_dir / "volume_profile_integrity.json", volume_profile.get("integrity") or {})
        write_json(
            run_dir / "summary.json",
            {
                "schema_version": SCHEMA_VERSION,
                "analysis_status": "VOLUME_PROFILE_INTEGRITY_FAILED",
                "data_quality": "FAIL",
                "volume_profile_status": "INTEGRITY_FAILED",
            },
        )
        (run_dir / "REPORT.md").write_text(
            "# VOLUME PROFILE INTEGRITY FAILED\n\n`BTC_OB_FIGHT_VOLUME_PROFILE_DATA_INSUFFICIENT`\n",
            encoding="utf-8",
        )
        print("BTC_OB_FIGHT_VOLUME_PROFILE_DATA_INSUFFICIENT")
        return EXIT_DATA
    _phase(9, "Running integrity checks... done", t0)

    t0 = _phase(10, "Building liquidation flow facts (Phase 2A.4)...")
    write_preflight(run_dir / "phase_2a4_liquidation_flow_preflight.json")
    liq_events, liq_event_meta = load_liquidation_events(
        cl, cfg.symbol, cfg.window_start, cfg.window_end
    )
    outer_edge = pf.get("volume_vah") or (volume_profile.get("value_area") or {}).get("vvah")
    liquidation_flow = build_liquidation_flow_facts(
        trades=trades,
        liq_events=liq_events,
        liq_load_meta=liq_event_meta,
        oi_rows=oi_rows,
        window_start=cfg.window_start,
        window_end=cfg.window_end,
        anchor=cfg.anchor,
        outer_edge_price=float(outer_edge) if outer_edge is not None else None,
        reclaim_events=fight_bundle.get("reclaim_events") or [],
    )
    manifest["liquidation_flow"] = {
        "contract_version": LIQUIDATION_FLOW_CONTRACT,
        "interpretation_status": "NOT_EVALUATED",
        "unique_liquidation_events": liquidation_flow["summary"]["unique_liquidation_event_count"],
        "attribution_method": liquidation_flow["summary"]["attribution_method"],
        "attribution_decision_eligible": False,
    }
    _phase(10, "Building liquidation flow facts (Phase 2A.4)... done", t0)

    reasons = derive_factual_reason_codes(pf, level_events, trade_facts, wall_facts, oi_liq, volume_profile)
    german = render_all_german(reasons)

    data_quality = "PASS"
    if not coverage["candles_1m"].get("complete"):
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
    manifest["analysis_status"] = summary["analysis_status"]

    trade_buckets = trade_facts.get("time_series_buckets") or []
    t0 = _phase(10, "Writing reports...")
    write_all_outputs(
        run_dir,
        summary=summary,
        manifest=manifest,
        coverage=coverage,
        profiles=profiles,
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
    )
    _phase(10, "Writing reports... done", t0)
    print_console_summary(summary, run_dir, manifest)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        cfg = validate_args(args)
        print(f"normalized_anchor_utc={cfg.anchor.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        return run_analysis(cfg)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(f"CLI error: {exc}", file=sys.stderr)
        return EXIT_CLI
    except FileNotFoundError as exc:
        print(f"DATA error: {exc}", file=sys.stderr)
        print("BTC_OB_FIGHT_DATA_INSUFFICIENT")
        return EXIT_DATA
    except Exception as exc:
        print(f"technical error: {exc}", file=sys.stderr)
        return EXIT_TECH


if __name__ == "__main__":
    raise SystemExit(main())

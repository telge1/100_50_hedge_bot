"""Pilot orchestration: dry-run then six-event canonical QDH integration."""

from __future__ import annotations

import csv
import json
import logging
import resource
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_text,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.timeparse import format_utc_z

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    AUDIT_ID,
    BAND_TICKS,
    BATCH_RUN_REL,
    CONTRACT_VERSION,
    PACKAGE_NAME,
    QDH_SOURCE_MODULES,
    SCHEMA_VERSION,
    SILVER_DATABASE,
    TICK_SIZE,
)
from .event_load import resolve_pilot_events
from .legacy import classify_legacy_vs_canonical, load_legacy_features
from .pilot_cases import PILOT_CASES, pilot_cases_as_dicts
from .reports import (
    render_canonical_report,
    render_dry_run_report,
    render_pair_report,
    write_pair_comparison_csv,
)
from .run_one import run_one_event

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore


def _repo_root() -> Path:
    # .../obfull_research_engine/src/obfull_research_engine/mp_qdh... → repo
    return Path(__file__).resolve().parents[4]


def _peak_ram_mb() -> float:
    # Linux: ru_maxrss in KB
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        if pd is not None:
            pd.DataFrame([]).to_parquet(path)
        else:
            path.write_bytes(b"")
        return
    if pd is None:
        raise RuntimeError("pandas required for parquet output")
    pd.DataFrame(rows).to_parquet(path, index=False)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
            w.writerow(flat)


def build_qdh_source_contract() -> dict[str, Any]:
    import importlib
    import inspect

    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1 import (
        BAND_TICKS_DEFAULT,
        BUCKET_MS,
        EPSILON,
        M_LIQ,
        M_OI,
        QDH_HALF_LIFE_MS,
    )
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (
        update_qdh,
    )
    from obfull_research_engine.ob_forschungsengine_v1.qdh_engine import run_qdh_base

    import_ok: dict[str, bool] = {}
    for name, mod in QDH_SOURCE_MODULES.items():
        try:
            importlib.import_module(mod)
            import_ok[name] = True
        except Exception:  # noqa: BLE001
            import_ok[name] = False

    return {
        "canonical_engine": "ob_forschungsengine_v1.qdh_engine.run_qdh_base",
        "qdh_formula_module": "level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard.update_qdh",
        "attribution_module": "level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution.attribute_intervals",
        "public_trades_source": "orderbook_analysis.public_trades_canonical",
        "defended_band": {
            "definition": "wall_price ± band_ticks * tick_size",
            "band_ticks": BAND_TICKS_DEFAULT,
            "tick_size": TICK_SIZE,
            "source": "wall_flow_attribution.band_bounds",
        },
        "bucket_ms": BUCKET_MS,
        "qdh_half_life_ms": QDH_HALF_LIFE_MS,
        "epsilon": EPSILON,
        "M_OI": M_OI,
        "M_LIQ": M_LIQ,
        "qdh_reimplemented": False,
        "update_qdh_signature": str(inspect.signature(update_qdh)),
        "run_qdh_base_signature": str(inspect.signature(run_qdh_base)),
        "modules": QDH_SOURCE_MODULES,
        "import_check": import_ok,
    }


def run_pilot(
    *,
    out_dir: Path,
    repo_root: Path | None = None,
    dry_run_only: bool = False,
    skip_dry_run: bool = False,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    repo_root = repo_root or _repo_root()
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty run dir: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "pilot.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
        force=True,
    )
    log = logging.getLogger("mp_qdh_canonical_pilot")

    batch_dir = repo_root / BATCH_RUN_REL
    resolved, input_audit = resolve_pilot_events(batch_dir=batch_dir)
    atomic_write_json(out_dir / "input_audit.json", {**input_audit, "cases": pilot_cases_as_dicts()})
    contract = build_qdh_source_contract()
    atomic_write_json(out_dir / "qdh_source_contract.json", contract)

    if not input_audit.get("ok"):
        verdict = "CANONICAL_QDH_PILOT_BLOCKED_IMPLEMENTATION"
        if any(b.get("reason") == "AMBIGUOUS_EVENT_ID" for b in input_audit.get("blockers") or []):
            verdict = "CANONICAL_QDH_SOURCE_MISSING"  # shouldn't happen; keep distinct
        # Prefer clear event-resolution blocker
        verdict = "CANONICAL_QDH_PILOT_BLOCKED_IMPLEMENTATION"
        atomic_write_json(
            out_dir / "run_manifest.json",
            {"ok": False, "verdict": verdict, "input_audit": input_audit},
        )
        return {"ok": False, "verdict": verdict, "out_dir": str(out_dir)}

    # Verify QDH source modules importable
    try:
        from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (  # noqa: F401
            update_qdh,
        )
        from obfull_research_engine.ob_forschungsengine_v1.qdh_engine import run_qdh_base  # noqa: F401
    except ImportError as exc:
        atomic_write_json(
            out_dir / "run_manifest.json",
            {"ok": False, "verdict": "CANONICAL_QDH_SOURCE_MISSING", "error": str(exc)},
        )
        return {"ok": False, "verdict": "CANONICAL_QDH_SOURCE_MISSING", "out_dir": str(out_dir)}

    client = get_clickhouse_client(role="mp_qdh_canonical_pilot")
    dry_results: list[dict[str, Any]] = []
    try:
        if not skip_dry_run:
            log.info("DRY-RUN start n=%s", len(resolved))
            for item in resolved:
                case = item["case"]
                r = run_one_event(
                    event=item["event"],
                    window=item["window"],
                    case_meta={
                        "pair_id": case.pair_id,
                        "pair_label": case.pair_label,
                        "outcome_class": case.outcome_class,
                    },
                    client=client,
                    dry_run=True,
                    persist_qdh=False,
                )
                dry_results.append(r)
                log.info(
                    "dry %s wall_ok=%s blocker=%s",
                    case.event_id,
                    r.get("ok"),
                    (r.get("link") or {}).get("blocker_reason") or r.get("blocker_reason"),
                )
            atomic_write_text(out_dir / "dry_run_report.md", render_dry_run_report(dry_results))
            if dry_run_only:
                elapsed = time.monotonic() - t0
                manifest = {
                    "ok": True,
                    "verdict": "DRY_RUN_ONLY",
                    "elapsed_s": round(elapsed, 3),
                    "peak_ram_mb": round(_peak_ram_mb(), 1),
                }
                atomic_write_json(out_dir / "run_manifest.json", manifest)
                return {"ok": True, "verdict": "DRY_RUN_ONLY", "out_dir": str(out_dir), "dry": dry_results}

            dry_blockers = [r for r in dry_results if not r.get("ok")]
            if dry_blockers:
                log.warning("Dry-run had %s blocked events; continuing pilot for successful walls", len(dry_blockers))

        # Full pilot
        results: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        all_buckets: list[dict[str, Any]] = []
        snaps: list[dict[str, Any]] = []
        wall_audit_rows: list[dict[str, Any]] = []
        trade_audit_rows: list[dict[str, Any]] = []

        for item in resolved:
            case = item["case"]
            log.info("PILOT %s %s/%s", case.event_id, case.pair_id, case.outcome_class)
            try:
                r = run_one_event(
                    event=item["event"],
                    window=item["window"],
                    case_meta={
                        "pair_id": case.pair_id,
                        "pair_label": case.pair_label,
                        "outcome_class": case.outcome_class,
                    },
                    out_dir=out_dir,
                    client=client,
                    dry_run=False,
                    persist_qdh=True,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("event failed: %s\n%s", exc, traceback.format_exc())
                r = {
                    "ok": False,
                    "event_id": case.event_id,
                    "coverage_ok": False,
                    "blocker_reason": f"EXCEPTION:{type(exc).__name__}:{exc}",
                    "case": {
                        "pair_id": case.pair_id,
                        "pair_label": case.pair_label,
                        "outcome_class": case.outcome_class,
                    },
                }
            results.append(r)
            link = r.get("link") or {
                "event_id": case.event_id,
                "coverage_ok": False,
                "blocker_reason": r.get("blocker_reason"),
            }
            links.append(link)
            wall_audit_rows.append(
                {
                    "event_id": case.event_id,
                    "pair_id": case.pair_id,
                    "outcome_class": case.outcome_class,
                    "wall_id": link.get("wall_id"),
                    "wall_side": link.get("wall_side"),
                    "wall_price": link.get("wall_price"),
                    "wall_size_at_zone_touch": link.get("wall_size_at_zone_touch"),
                    "wall_visible_at_zone_touch": link.get("wall_visible_at_zone_touch"),
                    "wall_selection_reason": link.get("wall_selection_reason"),
                    "wall_selection_confidence": link.get("wall_selection_confidence"),
                    "canonical_band_low": link.get("canonical_band_low"),
                    "canonical_band_high": link.get("canonical_band_high"),
                    "coverage_ok": link.get("coverage_ok"),
                    "blocker_reason": link.get("blocker_reason") or r.get("blocker_reason"),
                    "candidates_json": json.dumps(link.get("candidates_top") or [])[:4000],
                }
            )
            if r.get("ok"):
                all_buckets.extend(r.get("buckets") or [])
                snaps.append(r.get("decision_snapshot") or {})
                td = r.get("trade_dedup") or {}
                trade_audit_rows.append(
                    {
                        "event_id": case.event_id,
                        "raw_count": td.get("raw_count"),
                        "unique_count": td.get("unique_count"),
                        "duplicate_count": td.get("duplicate_count"),
                        "rejected_missing_trade_id": td.get("rejected_missing_trade_id"),
                        "identity_rule": td.get("identity_rule"),
                        "band_attributed_hit_qty": (r.get("band_stats") or {}).get("attributed_hit_qty_sum")
                        or (r.get("band_stats") or {}).get("sum_attributed_hit_qty"),
                        "mass_balance_violations": (r.get("band_stats") or {}).get("mass_balance_violations"),
                    }
                )

        # Legacy compare
        legacy = load_legacy_features(repo_root, [c.event_id for c in PILOT_CASES])
        legacy_rows: list[dict[str, Any]] = []
        for r in results:
            eid = r["event_id"]
            leg = legacy.get(eid) or {}
            snap = r.get("decision_snapshot") or {}
            # Aggregate decision-window fills from pre buckets
            pre = [b for b in (r.get("buckets") or []) if not b.get("post_decision")]
            can_fill = sum(float(b.get("attributed_fill_qty") or 0) for b in pre)
            can_pull = sum(float(b.get("residual_pull_qty") or 0) for b in pre)
            can_refill = sum(float(b.get("refill_qty") or 0) for b in pre)
            row = {
                "event_id": eid,
                "legacy_tag": leg.get("_tag"),
                "legacy_hit_qty": leg.get("legacy_hit_qty"),
                "legacy_pull_qty": leg.get("legacy_pull_qty"),
                "legacy_add_qty": leg.get("legacy_add_qty"),
                "legacy_refill_ratio": leg.get("legacy_refill_ratio"),
                "legacy_wall_persistence": leg.get("legacy_wall_persistence"),
                "legacy_wall_moved_with_price": leg.get("legacy_wall_moved_with_price"),
                "canonical_qdh_attributed_fill_qty_sum": can_fill,
                "canonical_qdh_residual_pull_qty_sum": can_pull,
                "canonical_qdh_refill_qty_sum": can_refill,
                "canonical_qdh_base_at_cutoff": snap.get("canonical_qdh_base"),
                "cmp_hit": classify_legacy_vs_canonical(
                    legacy_val=leg.get("legacy_hit_qty"),
                    canonical_val=can_fill,
                    comparable=True,
                ),
                "cmp_pull": classify_legacy_vs_canonical(
                    legacy_val=leg.get("legacy_pull_qty"),
                    canonical_val=can_pull,
                    comparable=True,
                ),
                "cmp_add_vs_refill": classify_legacy_vs_canonical(
                    legacy_val=leg.get("legacy_add_qty"),
                    canonical_val=can_refill,
                    comparable=False,  # add ≠ refill semantically
                ),
                "cmp_refill_ratio": "SEMANTICALLY_NOT_COMPARABLE",
                "cmp_wall_persistence": "SEMANTICALLY_NOT_COMPARABLE",
                "cmp_wall_moved": "SEMANTICALLY_NOT_COMPARABLE",
            }
            legacy_rows.append(row)

        _write_parquet(out_dir / "canonical_mp_wall_links.parquet", links)
        _write_parquet(out_dir / "canonical_wall_flow_buckets.parquet", all_buckets)
        _write_parquet(out_dir / "canonical_event_decision_snapshots.parquet", snaps)
        _write_csv(out_dir / "wall_selection_audit.csv", wall_audit_rows)
        _write_csv(out_dir / "public_trade_attribution_audit.csv", trade_audit_rows)
        _write_csv(out_dir / "legacy_vs_canonical.csv", legacy_rows)

        pair_rows = write_pair_comparison_csv(out_dir / "matched_pair_comparison.csv", results)
        summary_rows = []
        for r in results:
            link = r.get("link") or {}
            snap = r.get("decision_snapshot") or {}
            case = r.get("case") or {}
            summary_rows.append(
                {
                    "event_id": r.get("event_id"),
                    "pair_id": case.get("pair_id"),
                    "outcome_class": case.get("outcome_class"),
                    "ok": r.get("ok"),
                    "wall_id": link.get("wall_id"),
                    "wall_side": link.get("wall_side"),
                    "wall_price": link.get("wall_price"),
                    "wall_size_at_zone_touch": link.get("wall_size_at_zone_touch"),
                    "canonical_qdh_base": snap.get("canonical_qdh_base"),
                    "queue_remaining": snap.get("canonical_qdh_queue_remaining"),
                    "qdh_valid": snap.get("qdh_valid"),
                    "attribution_confidence": snap.get("attribution_confidence"),
                    "coverage_ok": r.get("coverage_ok"),
                    "blocker_reason": r.get("blocker_reason") or link.get("blocker_reason"),
                    "leakage_check_passed": snap.get("leakage_check_passed"),
                }
            )
        _write_csv(out_dir / "six_event_summary.csv", summary_rows)

        # Pair reports
        for pair_id, label, fname in (
            ("PAIR1", "ABSORB_LONG", "PAIR_1_ABSORB_REPORT.md"),
            ("PAIR2", "FAILED_BREAK_SHORT", "PAIR_2_FAILED_BREAK_REPORT.md"),
            ("PAIR3", "TRUE_BREAK_SHORT", "PAIR_3_TRUE_BREAK_REPORT.md"),
        ):
            atomic_write_text(
                out_dir / fname,
                render_pair_report(pair_id=pair_id, pair_label=label, results=results),
            )

        # Verdict
        n_ok = sum(1 for r in results if r.get("ok"))
        n_wall = sum(1 for r in results if (r.get("link") or {}).get("wall_id"))
        n_blocked = sum(1 for r in results if not r.get("ok"))
        blockers = sorted(
            {
                str(r.get("blocker_reason") or (r.get("link") or {}).get("blocker_reason") or "UNKNOWN")
                for r in results
                if not r.get("ok")
            }
        )
        if n_ok == 6:
            # Check if evidence thin
            thin = sum(
                1
                for r in results
                if r.get("ok")
                and (
                    (r.get("decision_snapshot") or {}).get("canonical_qdh_base") in (None, 0, 0.0)
                    or not (r.get("buckets") or [])
                )
            )
            verdict = (
                "CANONICAL_QDH_PILOT_SUCCESS_INSUFFICIENT_EVIDENCE"
                if thin >= 3
                else "CANONICAL_QDH_PILOT_SUCCESS"
            )
        elif n_ok > 0:
            verdict = "CANONICAL_QDH_PILOT_PARTIAL"
            if all("WALL" in (b or "") for b in blockers):
                verdict = "CANONICAL_QDH_PILOT_BLOCKED_WALL_SELECTION"
            elif any("COVERAGE" in (b or "") or "CHUNK" in (b or "") for b in blockers) and n_ok == 0:
                verdict = "CANONICAL_QDH_PILOT_BLOCKED_COVERAGE"
        else:
            if any("WALL" in (b or "") for b in blockers):
                verdict = "CANONICAL_QDH_PILOT_BLOCKED_WALL_SELECTION"
            elif any("ATTRIB" in (b or "") for b in blockers):
                verdict = "CANONICAL_QDH_PILOT_BLOCKED_ATTRIBUTION"
            elif any("CHUNK" in (b or "") or "COVERAGE" in (b or "") for b in blockers):
                verdict = "CANONICAL_QDH_PILOT_BLOCKED_COVERAGE"
            else:
                verdict = "CANONICAL_QDH_PILOT_BLOCKED_IMPLEMENTATION"

        # Aggregate metrics
        fill_sum = sum(
            sum(float(b.get("attributed_fill_qty") or 0) for b in (r.get("buckets") or []) if not b.get("post_decision"))
            for r in results
            if r.get("ok")
        )
        pull_sum = sum(
            sum(float(b.get("residual_pull_qty") or 0) for b in (r.get("buckets") or []) if not b.get("post_decision"))
            for r in results
            if r.get("ok")
        )
        refill_sum = sum(
            sum(float(b.get("refill_qty") or 0) for b in (r.get("buckets") or []) if not b.get("post_decision"))
            for r in results
            if r.get("ok")
        )
        unk_sum = sum(
            sum(float(b.get("attribution_unknown_qty") or 0) for b in (r.get("buckets") or []) if not b.get("post_decision"))
            for r in results
            if r.get("ok")
        )
        uniq_trades = sum(int((r.get("trade_dedup") or {}).get("unique_count") or 0) for r in results)
        dup_removed = sum(int((r.get("trade_dedup") or {}).get("duplicate_count") or 0) for r in results)
        qdh_valid_events = sum(1 for s in snaps if s.get("qdh_valid"))
        exhausted = sum(1 for s in snaps if s.get("queue_state") == "QUEUE_EXHAUSTED")
        near_zero = sum(1 for s in snaps if s.get("near_zero_queue_warning"))
        leak_viol = sum(1 for s in snaps if s.get("leakage_check_passed") is False)
        mass_viol = sum(
            int((r.get("band_stats") or {}).get("mass_balance_violations") or 0)
            + int((r.get("exact_stats") or {}).get("mass_balance_violations") or 0)
            for r in results
        )

        elapsed = time.monotonic() - t0
        runtime = {
            "elapsed_s": round(elapsed, 3),
            "peak_ram_mb": round(_peak_ram_mb(), 1),
            "n_events": 6,
            "n_ok": n_ok,
            "n_blocked": n_blocked,
            "created_at": format_utc_z(datetime.now(timezone.utc)),
        }
        atomic_write_json(out_dir / "runtime_metrics.json", runtime)

        summary_stats = {
            "verdict": verdict,
            "qdh_source_module": contract["canonical_engine"],
            "qdh_reimplemented": False,
            "pilot_events": 6,
            "events_with_unique_wall": n_wall,
            "events_with_full_attribution": n_ok,
            "events_blocked": n_blocked,
            "blocker_reasons": blockers,
            "defended_band_contract": contract["defended_band"],
            "public_trade_source": contract["public_trades_source"],
            "unique_trade_ids": uniq_trades,
            "duplicates_removed": dup_removed,
            "attributed_fill_sum": fill_sum,
            "residual_pull_sum": pull_sum,
            "refill_sum": refill_sum,
            "unknown_sum": unk_sum,
            "qdh_valid_events": qdh_valid_events,
            "queue_exhausted_events": exhausted,
            "near_zero_queue_warnings": near_zero,
            "causality_violations": leak_viol,
            "mass_balance_violations": mass_viol,
            "legacy_material_diffs": sum(
                1 for row in legacy_rows if row.get("cmp_hit") == "MATERIAL_DIFFERENCE" or row.get("cmp_pull") == "MATERIAL_DIFFERENCE"
            ),
            "band_ticks": BAND_TICKS,
            "package": PACKAGE_NAME,
            "audit_id": AUDIT_ID,
            "schema_version": SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "silver_database": SILVER_DATABASE,
        }
        atomic_write_text(
            out_dir / "CANONICAL_QDH_PILOT_REPORT.md",
            render_canonical_report(summary_stats, results, legacy_rows, pair_rows, runtime),
        )
        atomic_write_json(
            out_dir / "run_manifest.json",
            {
                "ok": n_ok == 6,
                "verdict": verdict,
                "summary": summary_stats,
                "runtime": runtime,
                "out_dir": str(out_dir),
            },
        )
        log.info("DONE verdict=%s ok=%s/%s", verdict, n_ok, 6)
        return {
            "ok": n_ok == 6,
            "verdict": verdict,
            "out_dir": str(out_dir),
            "summary": summary_stats,
            "runtime": runtime,
            "results": results,
        }
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

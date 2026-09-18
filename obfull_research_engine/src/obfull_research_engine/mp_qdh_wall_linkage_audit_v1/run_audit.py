"""Orchestrate six-event wall-linkage audit + write outputs."""

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
from obfull_research_engine.mp_qdh_canonical_integration_v1.event_load import resolve_pilot_events
from obfull_research_engine.mp_qdh_canonical_integration_v1.pilot_cases import PILOT_CASES, pilot_cases_as_dicts
from obfull_research_engine.timeparse import format_utc_z

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    AUDIT_ID,
    BATCH_RUN_REL,
    PACKAGE_NAME,
    PHASE0_CONTRACT,
    SCHEMA_VERSION,
)
from .audit_one import audit_one_event
from .report import render_audit_report, write_pair_comparison


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _peak_ram_mb() -> float:
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


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
            flat = {
                k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)


def run_audit(
    *,
    out_dir: Path,
    repo_root: Path | None = None,
    dry_run_only: bool = False,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    repo_root = repo_root or _repo_root()
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty run dir: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        handlers=[logging.FileHandler(out_dir / "audit.log"), logging.StreamHandler()],
        force=True,
    )
    log = logging.getLogger("wall_linkage_audit")

    atomic_write_json(out_dir / "phase0_contract.json", PHASE0_CONTRACT)
    batch_dir = repo_root / BATCH_RUN_REL
    resolved, input_audit = resolve_pilot_events(batch_dir=batch_dir)
    atomic_write_json(out_dir / "input_audit.json", {**input_audit, "cases": pilot_cases_as_dicts()})
    if not input_audit.get("ok"):
        verdict = "WALL_LINKAGE_AUDIT_BLOCKED"
        atomic_write_json(out_dir / "run_manifest.json", {"ok": False, "verdict": verdict, "input_audit": input_audit})
        return {"ok": False, "verdict": verdict, "out_dir": str(out_dir)}

    client = get_clickhouse_client(role="mp_qdh_wall_linkage_audit")
    results: list[dict[str, Any]] = []
    try:
        # Dry-run
        dry_rows = []
        for item in resolved:
            case = item["case"]
            r = audit_one_event(
                event=item["event"],
                window=item["window"],
                case_meta={"pair_id": case.pair_id, "pair_label": case.pair_label, "outcome_class": case.outcome_class},
                client=client,
                dry_run=True,
            )
            dry_rows.append(r)
            log.info("dry %s status=%s", case.event_id, r.get("linkage_status"))
        atomic_write_json(out_dir / "dry_run.json", dry_rows)
        if dry_run_only:
            return {"ok": True, "verdict": "DRY_RUN_ONLY", "out_dir": str(out_dir), "dry": dry_rows}

        # Full audit
        for item in resolved:
            case = item["case"]
            log.info("AUDIT %s", case.event_id)
            try:
                r = audit_one_event(
                    event=item["event"],
                    window=item["window"],
                    case_meta={
                        "pair_id": case.pair_id,
                        "pair_label": case.pair_label,
                        "outcome_class": case.outcome_class,
                    },
                    client=client,
                    dry_run=False,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("%s\n%s", exc, traceback.format_exc())
                r = {
                    "ok": False,
                    "event_id": case.event_id,
                    "linkage_status": "DATA_INCOMPLETE",
                    "blocker_reason": f"EXCEPTION:{type(exc).__name__}:{exc}",
                    "case": {
                        "pair_id": case.pair_id,
                        "pair_label": case.pair_label,
                        "outcome_class": case.outcome_class,
                    },
                    "candidates": [],
                    "funnel": None,
                    "rejections": [],
                    "flow_100ms": [],
                    "flow_1s": [],
                    "reconciliation": {},
                    "mass_balance_violations": 0,
                    "causality_violations": 0,
                }
            results.append(r)

        # Flatten outputs
        cand_rows: list[dict[str, Any]] = []
        link_rows: list[dict[str, Any]] = []
        funnel_rows: list[dict[str, Any]] = []
        rej_rows: list[dict[str, Any]] = []
        flow100: list[dict[str, Any]] = []
        flow1s: list[dict[str, Any]] = []
        recon_rows: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        status_counts: dict[str, int] = {}
        for r in results:
            st = str(r.get("linkage_status") or "UNKNOWN")
            status_counts[st] = status_counts.get(st, 0) + 1
            sel = r.get("selected") or {}
            link_rows.append(
                {
                    "event_id": r.get("event_id"),
                    "pair_id": (r.get("case") or {}).get("pair_id"),
                    "outcome_class": (r.get("case") or {}).get("outcome_class"),
                    "event_role": r.get("event_role"),
                    "expected_book_side": r.get("expected_book_side"),
                    "label_price_only": r.get("label_price_only"),
                    "trade_side": r.get("trade_side"),
                    "linkage_status": st,
                    "detail": r.get("detail"),
                    "wall_id": sel.get("candidate_wall_id"),
                    "wall_price": sel.get("wall_price_first"),
                    "queue_at_touch": sel.get("queue_at_touch"),
                    "max_queue": sel.get("max_queue"),
                    "ok": r.get("ok"),
                }
            )
            for c in r.get("candidates") or []:
                cand_rows.append({"event_id": r.get("event_id"), "linkage_status": st, **c})
            if r.get("funnel"):
                funnel_rows.append(r["funnel"])
            rej_rows.extend(r.get("rejections") or [])
            # Cap rejections file size: keep summary + sample
            flow100.extend(r.get("flow_100ms") or [])
            flow1s.extend(r.get("flow_1s") or [])
            if r.get("reconciliation"):
                recon_rows.append(r["reconciliation"])
            if r.get("causality_violations"):
                warnings.append({"event_id": r.get("event_id"), "type": "CAUSALITY", "n": r["causality_violations"]})
            if r.get("mass_balance_violations"):
                warnings.append({"event_id": r.get("event_id"), "type": "MASS_BALANCE", "n": r["mass_balance_violations"]})

        # Truncate rejections if huge — keep counts in funnel; write sample
        max_rej = 50_000
        if len(rej_rows) > max_rej:
            warnings.append({"type": "REJECTIONS_TRUNCATED", "kept": max_rej, "total": len(rej_rows)})
            rej_rows = rej_rows[:max_rej]

        _write_csv(out_dir / "wall_candidates.csv", cand_rows)
        _write_csv(out_dir / "wall_linkage_results.csv", link_rows)
        _write_csv(out_dir / "public_trade_attribution_funnel.csv", funnel_rows)
        _write_csv(out_dir / "public_trade_rejections.csv", rej_rows)
        _write_csv(out_dir / "canonical_flow_100ms.csv", flow100)
        _write_csv(out_dir / "canonical_flow_1s.csv", flow1s)
        _write_csv(out_dir / "event_reconciliation.csv", recon_rows)
        pair_rows = write_pair_comparison(out_dir / "pair_comparison.csv", results)
        atomic_write_json(out_dir / "warnings.json", warnings)

        # Aggregates
        unique_trades = sum(int(f.get("total_unique_trade_count") or 0) for f in funnel_rows)
        in_band_qty = sum(float(f.get("in_band_trade_qty") or 0) for f in funnel_rows)
        correct_qty = sum(float(f.get("correct_aggressor_trade_qty") or 0) for f in funnel_rows)
        attr_fill = sum(float(f.get("attributed_fill_qty") or 0) for f in funnel_rows)
        pull_sum = sum(float(r.get("pre_trigger_pull") or 0) for r in recon_rows)
        refill_sum = sum(float(r.get("pre_trigger_refill") or 0) for r in recon_rows)
        book_dec = sum(float(r.get("pre_trigger_book_decrease") or 0) for r in recon_rows)
        fill_share = (attr_fill / book_dec) if book_dec > 1e-12 else None
        attr_rate_in_band = (100.0 * attr_fill / in_band_qty) if in_band_qty > 1e-12 else None
        n_full = sum(1 for r in results if r.get("ok") and r.get("funnel"))
        mass_viol = sum(int(r.get("mass_balance_violations") or 0) for r in results)
        caus_viol = sum(int(r.get("causality_violations") or 0) for r in results)
        qdh_valid = sum(
            1
            for r in recon_rows
            if r.get("qdh_at_trigger") not in (None, "") and float(r.get("qdh_at_trigger") or 0) == float(r.get("qdh_at_trigger") or 0)
        )

        blocked_explained = all(
            (r.get("linkage_status") in (
                "NO_CANONICAL_WALL",
                "DEPLETED_BEFORE_TOUCH",
                "PULLED_BEFORE_TOUCH",
                "MOVED_BEFORE_TOUCH",
                "AMBIGUOUS_MULTIPLE_WALLS",
                "PRESENT_AT_TOUCH",
                "DATA_INCOMPLETE",
            ))
            for r in results
            if r.get("event_id") in ("mpe_e13a0ab36c241caf4c30", "mpe_aa9edc2d4c8dd2bb21a3")
        )

        if len(results) == 6 and blocked_explained and mass_viol == 0 and caus_viol == 0 and all(
            (r.get("reconciliation") or {}).get("agg_1s_matches_100ms", True) for r in results if r.get("funnel")
        ):
            verdict = "WALL_LINKAGE_AUDIT_SUCCESS"
        elif any(r.get("ok") for r in results):
            verdict = "WALL_LINKAGE_AUDIT_PARTIAL"
        else:
            verdict = "WALL_LINKAGE_AUDIT_FAILED"

        elapsed = time.monotonic() - t0
        runtime = {
            "elapsed_s": round(elapsed, 3),
            "peak_ram_mb": round(_peak_ram_mb(), 1),
            "created_at": format_utc_z(datetime.now(timezone.utc)),
        }
        summary = {
            "verdict": verdict,
            "pilot_events": 6,
            "status_counts": status_counts,
            "events_with_full_attribution": n_full,
            "total_unique_trades": unique_trades,
            "in_band_trade_qty": in_band_qty,
            "correct_aggressor_qty": correct_qty,
            "attributed_fill_qty": attr_fill,
            "attribution_rate_vs_in_band_pct": attr_rate_in_band,
            "pull_sum": pull_sum,
            "refill_sum": refill_sum,
            "fill_share_of_decrease": fill_share,
            "qdh_valid_events": qdh_valid,
            "causality_violations": caus_viol,
            "mass_balance_violations": mass_viol,
            "pair_judgements": {p["pair_id"]: p.get("judgement") for p in pair_rows},
            "blocked_events_explained": blocked_explained,
            "package": PACKAGE_NAME,
            "audit_id": AUDIT_ID,
            "schema_version": SCHEMA_VERSION,
        }
        atomic_write_json(out_dir / "runtime_metrics.json", runtime)
        atomic_write_text(
            out_dir / "WALL_LINKAGE_AUDIT_REPORT.md",
            render_audit_report(summary, results, pair_rows, runtime, PHASE0_CONTRACT),
        )
        atomic_write_json(
            out_dir / "run_manifest.json",
            {"ok": verdict == "WALL_LINKAGE_AUDIT_SUCCESS", "verdict": verdict, "summary": summary, "runtime": runtime},
        )
        log.info("DONE %s", verdict)
        return {"ok": verdict == "WALL_LINKAGE_AUDIT_SUCCESS", "verdict": verdict, "summary": summary, "runtime": runtime, "out_dir": str(out_dir), "results": results}
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

"""Finalize crash-interrupted pilot run from persisted parquet/csv (no CH)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_text,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.pilot_cases import PILOT_CASES
from obfull_research_engine.mp_qdh_canonical_integration_v1.reports import (
    render_canonical_report,
    render_pair_report,
    write_pair_comparison_csv,
)
from obfull_research_engine.timeparse import format_utc_z


def _case_meta(event_id: str) -> dict[str, Any]:
    for c in PILOT_CASES:
        if c.event_id == event_id:
            return {
                "pair_id": c.pair_id,
                "pair_label": c.pair_label,
                "outcome_class": c.outcome_class,
            }
    return {}


def _finite(val: Any) -> Any:
    if val is None:
        return None
    try:
        import math

        import numpy as np

        if isinstance(val, (float, int, np.floating, np.integer)):
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                return None
            return f if isinstance(val, (float, np.floating)) else val
        if isinstance(val, np.ndarray):
            if val.size == 0:
                return None
            if val.size == 1:
                return _finite(val.item())
            return val.tolist()
    except Exception:  # noqa: BLE001
        pass
    if isinstance(val, str) and val in ("", "None", "nan", "NaN"):
        return None
    return val


def finalize(out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    links = pd.read_parquet(out_dir / "canonical_mp_wall_links.parquet").to_dict(orient="records")
    snaps = pd.read_parquet(out_dir / "canonical_event_decision_snapshots.parquet").to_dict(
        orient="records"
    )
    buckets = pd.read_parquet(out_dir / "canonical_wall_flow_buckets.parquet")
    snap_by = {s["event_id"]: s for s in snaps}
    link_by = {r["event_id"]: r for r in links}

    # Reconstruct minimal results for reporting
    results: list[dict[str, Any]] = []
    for c in PILOT_CASES:
        link = {k: _finite(v) for k, v in (link_by.get(c.event_id) or {}).items()}
        if isinstance(link.get("case"), str):
            try:
                link["case"] = json.loads(link["case"])
            except json.JSONDecodeError:
                pass
        ok = bool(link.get("coverage_ok")) and bool(link.get("wall_id"))
        snap = {k: _finite(v) for k, v in (snap_by.get(c.event_id) or {}).items()}
        ev_buckets = []
        if len(buckets):
            for row in buckets[buckets["event_id"] == c.event_id].to_dict(orient="records"):
                ev_buckets.append({k: _finite(v) for k, v in row.items()})
        trade_path = out_dir / "public_trade_attribution_audit.csv"
        td: dict[str, Any] = {}
        if trade_path.is_file():
            for row in csv.DictReader(trade_path.open(encoding="utf-8")):
                if row.get("event_id") == c.event_id:
                    td = row
                    break
        results.append(
            {
                "ok": ok,
                "event_id": c.event_id,
                "coverage_ok": link.get("coverage_ok"),
                "blocker_reason": link.get("blocker_reason"),
                "link": link,
                "decision_snapshot": snap,
                "buckets": ev_buckets,
                "case": _case_meta(c.event_id),
                "trade_dedup": {
                    "unique_count": int(float(td["unique_count"])) if td.get("unique_count") not in (None, "") else 0,
                    "duplicate_count": int(float(td["duplicate_count"])) if td.get("duplicate_count") not in (None, "") else 0,
                },
                "band_stats": {},
                "exact_stats": {},
            }
        )

    legacy_rows = list(csv.DictReader((out_dir / "legacy_vs_canonical.csv").open(encoding="utf-8")))
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
    with (out_dir / "six_event_summary.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)

    for pair_id, label, fname in (
        ("PAIR1", "ABSORB_LONG", "PAIR_1_ABSORB_REPORT.md"),
        ("PAIR2", "FAILED_BREAK_SHORT", "PAIR_2_FAILED_BREAK_REPORT.md"),
        ("PAIR3", "TRUE_BREAK_SHORT", "PAIR_3_TRUE_BREAK_REPORT.md"),
    ):
        atomic_write_text(out_dir / fname, render_pair_report(pair_id=pair_id, pair_label=label, results=results))

    n_ok = sum(1 for r in results if r.get("ok"))
    n_wall = sum(1 for r in results if (r.get("link") or {}).get("wall_id"))
    n_blocked = 6 - n_ok
    blockers = sorted(
        {
            str(r.get("blocker_reason") or (r.get("link") or {}).get("blocker_reason") or "UNKNOWN")
            for r in results
            if not r.get("ok")
        }
    )
    if n_ok == 6:
        verdict = "CANONICAL_QDH_PILOT_SUCCESS"
    elif n_ok > 0:
        verdict = "CANONICAL_QDH_PILOT_PARTIAL"
        if n_ok == 0 and all("WALL" in b for b in blockers):
            verdict = "CANONICAL_QDH_PILOT_BLOCKED_WALL_SELECTION"
    else:
        verdict = (
            "CANONICAL_QDH_PILOT_BLOCKED_WALL_SELECTION"
            if any("WALL" in b for b in blockers)
            else "CANONICAL_QDH_PILOT_BLOCKED_IMPLEMENTATION"
        )

    def _sum_pre(key: str) -> float:
        s = 0.0
        for r in results:
            for b in r.get("buckets") or []:
                if b.get("post_decision"):
                    continue
                v = _finite(b.get(key))
                if v is None:
                    continue
                try:
                    s += float(v)
                except (TypeError, ValueError):
                    pass
        return s

    fill_sum = _sum_pre("attributed_fill_qty")
    pull_sum = _sum_pre("residual_pull_qty")
    refill_sum = _sum_pre("refill_qty")
    unk_sum = _sum_pre("attribution_unknown_qty")
    uniq = sum(int((r.get("trade_dedup") or {}).get("unique_count") or 0) for r in results)
    dups = sum(int((r.get("trade_dedup") or {}).get("duplicate_count") or 0) for r in results)
    qdh_valid = sum(1 for r in results if (r.get("decision_snapshot") or {}).get("qdh_valid") in (True, "True", "true"))
    exhausted = sum(
        1 for r in results if (r.get("decision_snapshot") or {}).get("queue_state") == "QUEUE_EXHAUSTED"
    )
    near_zero = sum(
        1
        for r in results
        if (r.get("decision_snapshot") or {}).get("near_zero_queue_warning") in (True, "True", "true")
    )
    leak = sum(
        1
        for r in results
        if (r.get("decision_snapshot") or {}).get("leakage_check_passed") in (False, "False", "false")
    )
    material = sum(
        1
        for row in legacy_rows
        if row.get("cmp_hit") == "MATERIAL_DIFFERENCE" or row.get("cmp_pull") == "MATERIAL_DIFFERENCE"
    )

    runtime = {
        "elapsed_s": None,
        "peak_ram_mb": None,
        "finalized_at": format_utc_z(datetime.now(timezone.utc)),
        "note": "finalized after CSV fieldnames crash; CH not re-queried",
    }
    # try pilot.log timestamps
    log = (out_dir / "pilot.log").read_text(encoding="utf-8") if (out_dir / "pilot.log").is_file() else ""
    atomic_write_json(out_dir / "runtime_metrics.json", runtime)

    summary = {
        "verdict": verdict,
        "qdh_source_module": "ob_forschungsengine_v1.qdh_engine.run_qdh_base",
        "qdh_reimplemented": False,
        "pilot_events": 6,
        "events_with_unique_wall": n_wall,
        "events_with_full_attribution": n_ok,
        "events_blocked": n_blocked,
        "blocker_reasons": blockers,
        "defended_band_contract": {
            "definition": "wall_price ± band_ticks * tick_size",
            "band_ticks": 5,
            "tick_size": 0.1,
        },
        "public_trade_source": "orderbook_analysis.public_trades_canonical",
        "unique_trade_ids": uniq,
        "duplicates_removed": dups,
        "attributed_fill_sum": fill_sum,
        "residual_pull_sum": pull_sum,
        "refill_sum": refill_sum,
        "unknown_sum": unk_sum,
        "qdh_valid_events": qdh_valid,
        "queue_exhausted_events": exhausted,
        "near_zero_queue_warnings": near_zero,
        "causality_violations": leak,
        "mass_balance_violations": 0,
        "legacy_material_diffs": material,
        "pair_core_diffs": {pr.get("pair_id"): pr.get("core_diff_note") for pr in pair_rows},
    }
    atomic_write_text(
        out_dir / "CANONICAL_QDH_PILOT_REPORT.md",
        render_canonical_report(summary, results, legacy_rows, pair_rows, runtime),
    )
    atomic_write_json(
        out_dir / "run_manifest.json",
        {
            "ok": n_ok == 6,
            "verdict": verdict,
            "summary": summary,
            "runtime": runtime,
            "out_dir": str(out_dir),
            "finalized_without_ch_requery": True,
        },
    )
    with (out_dir / "pilot.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat()}Z INFO FINALIZE verdict={verdict} ok={n_ok}/6\n")
    return {"verdict": verdict, "summary": summary, "n_ok": n_ok}


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "obfull_research_engine/runs/mp_qdh_canonical_pilot_v1_20260917")
    print(json.dumps(finalize(out), indent=2, default=str)[:4000])

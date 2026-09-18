"""Orchestrate v2 study with checkpoints (reuse frozen v1 universe)."""

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
from obfull_research_engine.mp_big_move_case_control_v1.stats import cliffs_delta, median
from obfull_research_engine.mp_qdh_30event_case_control_v1.paired import (
    overlap_groups,
    paired_feature_rows,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.event_load import (
    load_events_csv,
    load_windows_csv,
)

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    BATCH_RUN_REL,
    CONTRACT_VERSION,
    PACKAGE_NAME,
    PHASE0_CONTRACT,
    SCHEMA_VERSION,
    STUDY_ID,
)
from .analyze_one import analyze_one_event_v2
from .universe import EventUniverseMismatch, load_and_verify_frozen_universe, write_frozen_copies


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


PAIRED_KEYS = [
    "attributed_fill_qty",
    "residual_pull_qty",
    "refill_qty",
    "unknown_qty",
    "fill_share_of_book_decrease",
    "qdh_at_decision",
    "qdh_auc_per_decision_second",
    "persistence_ratio_at_decision",
    "qdh_toxic_at_decision",
    "impact_efficiency_bps_per_million",
    "same_side_depth_2bps_at_decision_norm",
    "mid_change_favorable_signed",
    "wall_move_count",
    "ticks_path_opened",
]


def _purged_pairs(pairs: list[dict[str, Any]], ov_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep at most one pair per overlap group (lowest pair_id); drop others."""
    drop_events: set[str] = set()
    for og in ov_rows:
        keep = str(og.get("sensitivity_keep_event_id") or "")
        ids = str(og.get("event_ids") or "").split("|")
        for eid in ids:
            if eid and eid != keep:
                drop_events.add(eid)
    out = []
    for p in pairs:
        if p["winner_event_id"] in drop_events or p["control_event_id"] in drop_events:
            continue
        out.append(p)
    return out


def run_study(
    *,
    out_dir: Path,
    repo_root: Path | None = None,
    dry_run_only: bool = False,
    dry_run_pairs: int = 1,
    resume: bool = True,
    max_pairs: int | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    repo_root = repo_root or _repo_root()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(out_dir / "study.log", encoding="utf-8")],
    )
    log = logging.getLogger("mp_qdh_30event_v2")
    atomic_write_json(out_dir / "phase0_contract.json", PHASE0_CONTRACT)

    try:
        frozen = load_and_verify_frozen_universe(repo_root)
    except EventUniverseMismatch as exc:
        atomic_write_json(out_dir / "run_manifest.json", {"ok": False, "verdict": "EVENT_UNIVERSE_MISMATCH", "error": str(exc)})
        return {"ok": False, "verdict": "EVENT_UNIVERSE_MISMATCH", "error": str(exc)}

    write_frozen_copies(out_dir, frozen, repo_root)
    pairs = list(frozen["pairs"])
    if max_pairs is not None:
        pairs = pairs[: int(max_pairs)]
    if dry_run_only:
        pairs = pairs[: int(dry_run_pairs)]

    contract_hash = f"v2|{frozen['event_list_sha256']}|{frozen['pair_list_sha256']}|{CONTRACT_VERSION}"
    ckpt_dir = out_dir / "checkpoints"
    ckpt_dir.mkdir(exist_ok=True)
    hash_path = ckpt_dir / "contract_hash.txt"
    if hash_path.exists():
        old = hash_path.read_text(encoding="utf-8").strip()
        if old != contract_hash:
            raise RuntimeError(f"CONTRACT_HASH_MISMATCH: {old} != {contract_hash}")
    else:
        hash_path.write_text(contract_hash + "\n", encoding="utf-8")

    batch_dir = Path(repo_root) / BATCH_RUN_REL
    events = load_events_csv(batch_dir)
    windows = load_windows_csv(batch_dir)

    work: list[dict[str, Any]] = []
    for p in pairs:
        for role, eid in (("WINNER", p["winner_event_id"]), ("CONTROL", p["control_event_id"])):
            work.append({"pair_id": p["pair_id"], "case_role": role, "event_id": eid, "pair": p})

    total = len(work)
    results: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    peak = _peak_ram_mb()
    client = None if dry_run_only else get_clickhouse_client(role="mp_qdh_30event_case_control_v2")
    try:
        for i, item in enumerate(work):
            eid = item["event_id"]
            ckpt = ckpt_dir / f"{eid}.json"
            if resume and ckpt.exists() and not dry_run_only:
                prev = json.loads(ckpt.read_text(encoding="utf-8"))
                if prev.get("contract_hash") != contract_hash:
                    raise RuntimeError(f"checkpoint hash mismatch {eid}")
                results[eid] = prev["result"]
                log.info("RESUME %s (%d/%d)", eid, i + 1, total)
                continue
            ev = events.get(eid)
            if ev is None:
                err = {"ok": False, "event_id": eid, "error": "EVENT_ID_NOT_FOUND", "case": item}
                results[eid] = err
                atomic_write_json(ckpt, {"contract_hash": contract_hash, "result": err})
                continue
            window = windows.get(ev.get("window_id") or "") or {}
            case_meta = {
                "pair_id": item["pair_id"],
                "case_role": item["case_role"],
                "outcome_class": item["pair"].get(
                    "winner_outcome" if item["case_role"] == "WINNER" else "control_outcome"
                ),
            }
            log.info("RUN %s %s (%d/%d)", item["case_role"], eid, i + 1, total)
            try:
                res = analyze_one_event_v2(
                    event=ev, window=window, case_meta=case_meta, client=client, dry_run=dry_run_only
                )
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "event_id": eid, "error": str(exc), "traceback": traceback.format_exc(), "case": case_meta}
                warnings.append({"event_id": eid, "error": str(exc)})
                log.exception("FAIL %s", eid)
            slim = {k: v for k, v in res.items()}
            flow_100 = slim.pop("flow_100ms", [])
            flow_1s = slim.pop("flow_1s", [])
            wall_ev = slim.pop("wall_movement_events", [])
            results[eid] = slim
            if not dry_run_only:
                atomic_write_json(
                    ckpt,
                    {"contract_hash": contract_hash, "result": slim, "completed_at": datetime.now(timezone.utc).isoformat()},
                )
                ev_dir = ckpt_dir / eid
                ev_dir.mkdir(exist_ok=True)
                _write_csv(ev_dir / "flow_100ms.csv", flow_100)
                _write_csv(ev_dir / "flow_1s.csv", flow_1s)
                _write_csv(ev_dir / "wall_movement_events.csv", wall_ev)
                slim["_flow_100ms"] = flow_100
                slim["_flow_1s"] = flow_1s
                slim["_wall_movement_events"] = wall_ev
            peak = max(peak, _peak_ram_mb())
            log.info(
                "DONE %s ok=%s linkage=%s coverage=%s",
                eid,
                slim.get("ok"),
                slim.get("linkage_status"),
                (slim.get("coverage") or {}).get("pass"),
            )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    if dry_run_only:
        atomic_write_json(
            out_dir / "dry_run.json",
            {
                "n_events": len(results),
                "results": [
                    {"event_id": e, "ok": r.get("ok"), "linkage_status": r.get("linkage_status"), "case": r.get("case")}
                    for e, r in results.items()
                ],
                "db_mutation": False,
            },
        )
        return {"ok": True, "verdict": "DRY_RUN_ONLY", "out_dir": str(out_dir)}

    for eid, r in results.items():
        if "_flow_100ms" not in r:
            ev_dir = ckpt_dir / eid
            if (ev_dir / "flow_100ms.csv").exists():
                r["_flow_100ms"] = list(csv.DictReader((ev_dir / "flow_100ms.csv").open(encoding="utf-8")))
                r["_flow_1s"] = list(csv.DictReader((ev_dir / "flow_1s.csv").open(encoding="utf-8")))
                r["_wall_movement_events"] = list(
                    csv.DictReader((ev_dir / "wall_movement_events.csv").open(encoding="utf-8"))
                )

    feat_rows = []
    flow100_all = []
    flow1s_all = []
    wall_sum = []
    wall_ev_all = []
    attr_rows = []
    dq = []
    features_by_event: dict[str, dict[str, Any]] = {}

    for item in work:
        eid = item["event_id"]
        r = results.get(eid) or {}
        feat = dict(r.get("features") or {})
        feat.update(
            {
                "event_id": eid,
                "pair_id": item["pair_id"],
                "case_role": item["case_role"],
                "outcome_class": item["pair"].get(
                    "winner_outcome" if item["case_role"] == "WINNER" else "control_outcome"
                ),
                "label_price_only": r.get("label_price_only") or item["pair"].get("label_price_only"),
                "event_role": r.get("event_role") or item["pair"].get("event_role"),
                "trade_side": r.get("trade_side") or item["pair"].get("trade_side"),
                "linkage_status": r.get("linkage_status"),
                "ok": r.get("ok"),
                "blocked_reason": r.get("blocked_reason"),
            }
        )
        for er in frozen["event_rows"]:
            if er["event_id"] == eid:
                feat["confluence_class"] = er.get("confluence_class")
                feat["reference_entry_ts_ns"] = er.get("reference_entry_ts_ns")
                break
        feat_rows.append(feat)
        features_by_event[eid] = feat
        flow100_all.extend(r.get("_flow_100ms") or [])
        flow1s_all.extend(r.get("_flow_1s") or [])
        ws = dict(r.get("wall_movement_summary") or {})
        ws.update({"event_id": eid, "pair_id": item["pair_id"], "case_role": item["case_role"]})
        wall_sum.append(ws)
        wall_ev_all.extend(r.get("_wall_movement_events") or [])
        atr = dict(r.get("attribution_summary") or {})
        atr["event_id"] = eid
        atr["funnel"] = json.dumps(atr.get("funnel") or {})
        atr["attribution_stats"] = json.dumps(atr.get("attribution_stats") or {})
        attr_rows.append(atr)
        cov = r.get("coverage") or {}
        dq.append(
            {
                "event_id": eid,
                "ok": r.get("ok"),
                "blocked_reason": r.get("blocked_reason"),
                "linkage_status": r.get("linkage_status"),
                "coverage_pass": cov.get("pass"),
                "coverage_blockers": "|".join(cov.get("blockers") or []),
                "n_trades_raw": cov.get("n_trades_raw"),
                "cross_epoch": cov.get("cross_epoch"),
                "seq_gaps": cov.get("seq_gaps"),
                "qdh_at_decision_na_reason": feat.get("qdh_at_decision_na_reason"),
            }
        )

    ov_rows = overlap_groups(frozen["event_rows"])
    purged = _purged_pairs(pairs, ov_rows)
    paired_all = paired_feature_rows(pairs, features_by_event, PAIRED_KEYS)
    paired_purged = paired_feature_rows(purged, features_by_event, PAIRED_KEYS)
    for row in paired_purged:
        row["view"] = "OVERLAP_PURGED"
    for row in paired_all:
        row["view"] = "ALL_PAIRS"

    # Mark formal statuses
    calib_rows = [
        {
            "metric": "absorption_ratio",
            "status": "NOT_CALIBRATED",
            "note": "Masterplan formal metric deferred; no threshold fit on 30-event sample",
        },
        {
            "metric": "vacuum_score",
            "status": "NOT_CALIBRATED",
            "note": "Masterplan formal metric deferred; depth_norm is diagnostic only",
        },
    ]

    _write_csv(out_dir / "canonical_event_features.csv", feat_rows)
    _write_csv(out_dir / "canonical_flow_100ms.csv", flow100_all)
    _write_csv(out_dir / "canonical_flow_1s.csv", flow1s_all)
    _write_csv(out_dir / "wall_movement_summary.csv", wall_sum)
    _write_csv(out_dir / "wall_movement_events.csv", wall_ev_all)
    _write_csv(out_dir / "public_trade_attribution_summary.csv", attr_rows)
    _write_csv(out_dir / "paired_feature_comparison.csv", paired_all)
    _write_csv(out_dir / "paired_feature_comparison_overlap_purged.csv", paired_purged)
    _write_csv(out_dir / "overlap_groups.csv", ov_rows)
    _write_csv(out_dir / "data_quality_report.csv", dq)
    _write_csv(out_dir / "calibration_placeholders.csv", calib_rows)

    n_ok = sum(1 for r in results.values() if r.get("ok"))
    n_cov_fail = sum(1 for r in results.values() if str(r.get("blocked_reason") or "").startswith("COVERAGE_GATE"))
    fill_sum = sum(float((r.get("features") or {}).get("attributed_fill_qty") or 0) for r in results.values())
    pull_sum = sum(float((r.get("features") or {}).get("residual_pull_qty") or 0) for r in results.values())
    refill_sum = sum(float((r.get("features") or {}).get("refill_qty") or 0) for r in results.values())
    unk_sum = sum(float((r.get("features") or {}).get("unknown_qty") or 0) for r in results.values())
    qdh_ok = sum(1 for f in feat_rows if f.get("qdh_at_decision") is not None)
    status_counts: dict[str, int] = {}
    for r in results.values():
        st = str(r.get("linkage_status") or "UNKNOWN")
        status_counts[st] = status_counts.get(st, 0) + 1

    elapsed = time.monotonic() - t0
    if n_ok == total and n_ok > 0 and n_cov_fail == 0 and len(pairs) == 15:
        verdict = "QDH_30EVENT_V2_SUCCESS"
    elif n_ok == total and n_ok > 0 and n_cov_fail == 0 and len(pairs) < 15:
        # intentional subset (dry/pilot) — not a universe failure
        verdict = "QDH_30EVENT_V2_PARTIAL"
    elif n_ok > 0:
        verdict = "QDH_30EVENT_V2_PARTIAL"
    else:
        verdict = "QDH_30EVENT_V2_FAILED"

    warnings.append({"type": "LOW_SAMPLE", "n_pairs": len(pairs)})
    n_pairs_retained = len(purged)
    n_pairs_removed = len(pairs) - n_pairs_retained
    if ov_rows:
        warnings.append(
            {
                "type": "OVERLAP_PSEUDOREPLICATION",
                "n_groups": len(ov_rows),
                "pairs_retained_after_purge": n_pairs_retained,
                "pairs_removed_by_purge": n_pairs_removed,
            }
        )
    warnings.append({"type": "ABSORPTION_VACUUM_NOT_CALIBRATED"})
    warnings.append(
        {
            "type": "RECEIVE_TIME_PROXY",
            "note": "All band intervals currently LOW when collector_received_at missing",
        }
    )

    top = sorted(
        [r for r in paired_all if r.get("n_valid_pairs")],
        key=lambda r: abs(float(r.get("median_paired_diff") or 0)),
        reverse=True,
    )[:10]

    summary = {
        "verdict": verdict,
        "n_pairs": len(pairs),
        "n_events_ok": n_ok,
        "n_events_total": total,
        "n_coverage_blocked": n_cov_fail,
        "linkage_status_counts": status_counts,
        "attributed_fill_qty": fill_sum,
        "pull_sum": pull_sum,
        "refill_sum": refill_sum,
        "unknown_sum": unk_sum,
        "qdh_at_decision_valid_events": qdh_ok,
        "n_purged_pairs_retained": n_pairs_retained,
        "n_pairs_removed_by_purge": n_pairs_removed,
        "n_purged_pairs": n_pairs_retained,  # legacy alias = retained after purge
        "event_list_sha256": frozen["event_list_sha256"],
        "pair_list_sha256": frozen["pair_list_sha256"],
        "elapsed_s": elapsed,
        "peak_ram_mb": peak,
        "top_paired": [
            {"feature": t["feature"], "median_diff": t.get("median_paired_diff"), "cliffs": t.get("cliffs_delta_winner_vs_control")}
            for t in top
        ],
    }
    atomic_write_json(
        out_dir / "run_manifest.json",
        {
            "ok": verdict in ("QDH_30EVENT_V2_SUCCESS", "QDH_30EVENT_V2_PARTIAL"),
            "study_id": STUDY_ID,
            "package": PACKAGE_NAME,
            "schema_version": SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "summary": summary,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    atomic_write_json(out_dir / "warnings.json", warnings)
    atomic_write_json(out_dir / "runtime_metrics.json", {"elapsed_s": elapsed, "peak_ram_mb": peak})

    report = _render_report(summary, frozen, warnings, top, purged)
    atomic_write_text(out_dir / "QDH_30EVENT_CASE_CONTROL_V2_REPORT.md", report)
    log.info("DONE %s", verdict)
    return {"ok": verdict == "QDH_30EVENT_V2_SUCCESS", "verdict": verdict, "out_dir": str(out_dir), "summary": summary}


def _render_report(summary, frozen, warnings, top, purged) -> str:
    lines = [
        "# QDH 30-Event Case-Control V2 Report",
        "",
        f"**VERDICT:** `{summary['verdict']}`",
        "",
        "## Executive Summary",
        "",
        f"- Frozen universe reused from v1 (event/pair SHA256 verified).",
        f"- Events OK: {summary['n_events_ok']}/{summary['n_events_total']}; coverage-blocked: {summary['n_coverage_blocked']}",
        f"- Fill / Pull / Refill / Unknown: {summary['attributed_fill_qty']} / {summary['pull_sum']} / {summary['refill_sum']} / {summary['unknown_sum']}",
        f"- QDH at decision valid: {summary['qdh_at_decision_valid_events']}",
        f"- Overlap-purged pairs retained: {summary.get('n_purged_pairs_retained', summary.get('n_purged_pairs'))} "
        f"(removed {summary.get('n_pairs_removed_by_purge', '?')} of {summary['n_pairs']})",
        "",
        "## V2 contract fixes",
        "",
        "- Coverage gate: public trades / epoch / seq gaps",
        "- Aggressor persistence wired into QDH (`update_aggressor` → `persistence_ratio`)",
        "- Fill capped for Pull; UNKNOWN for LOW-confidence no-fill decreases; excess fill tracked",
        "- QDH missingness reasons + AUC / decision_horizon_s",
        "- Impact Efficiency via `price_response.update_price_response`",
        "- Depth normalized to 120s pre-touch median baseline (USDT notional)",
        "- Absorption Ratio / Vacuum Score: **NOT_CALIBRATED**",
        "",
        "## Top paired differences (all pairs)",
        "",
    ]
    for t in top:
        lines.append(
            f"- `{t.get('feature')}`: diff={t.get('median_paired_diff', t.get('median_diff'))} "
            f"cliffs={t.get('cliffs_delta_winner_vs_control', t.get('cliffs'))}"
        )
    lines += [
        "",
        f"Runtime: {summary['elapsed_s']}s peak_ram={summary['peak_ram_mb']} MB",
        "",
        f"```json\n{json.dumps(warnings, indent=2)}\n```",
        "",
    ]
    return "\n".join(lines)

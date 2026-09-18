"""Orchestrate freeze → analyze 30 events → paired outputs (checkpoint/resume)."""

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
from .analyze_one import analyze_one_event
from .paired import (
    ABLATION_GROUPS,
    ablation_summary,
    overlap_groups,
    paired_feature_rows,
    subgroup_rows,
)
from .report import render_report
from .universe import build_frozen_universe, write_frozen_universe


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


PAIRED_FEATURE_KEYS = [
    "attributed_fill_qty",
    "residual_pull_qty",
    "refill_qty",
    "fill_share_of_book_decrease",
    "pull_share_of_book_decrease",
    "gross_book_churn",
    "queue_recovery_fraction",
    "queue_min_fraction",
    "qdh_at_touch",
    "qdh_at_decision",
    "qdh_max",
    "qdh_slope",
    "qdh_auc",
    "wall_move_net_ticks",
    "wall_move_count",
    "ticks_path_opened",
    "ticks_path_closed",
    "wall_opens_path_in_trade_direction",
    "wall_blocks_trade_direction",
    "mid_change_favorable_signed",
    "favorable_price_response_after_aggression",
    "same_side_depth_inside_2bps_at_decision",
    "spread_change_bps",
]


def run_study(
    *,
    out_dir: Path,
    repo_root: Path | None = None,
    dry_run_only: bool = False,
    dry_run_pairs: int = 2,
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
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(out_dir / "study.log", encoding="utf-8"),
        ],
    )
    log = logging.getLogger("mp_qdh_30event")

    atomic_write_json(out_dir / "phase0_contract.json", PHASE0_CONTRACT)

    frozen = build_frozen_universe(repo_root)
    write_frozen_universe(out_dir, frozen)
    if not frozen["ok"] and not frozen["pairs"]:
        return {
            "ok": False,
            "verdict": "QDH_30EVENT_COMPARISON_BLOCKED",
            "reason": "EVENT_UNIVERSE_MISMATCH_OR_NO_PAIRS",
            "manifest": frozen["manifest"],
        }

    pairs = list(frozen["pairs"])
    if max_pairs is not None:
        pairs = pairs[: int(max_pairs)]
    if dry_run_only:
        pairs = pairs[: int(dry_run_pairs)]

    contract_hash = frozen["contract_hash_sha256"]
    ckpt_dir = out_dir / "checkpoints"
    ckpt_dir.mkdir(exist_ok=True)
    hash_path = ckpt_dir / "contract_hash.txt"
    if hash_path.exists():
        old = hash_path.read_text(encoding="utf-8").strip()
        if old != contract_hash:
            raise RuntimeError(
                f"CONTRACT_HASH_MISMATCH: checkpoint hash {old} != {contract_hash}; refuse to mix"
            )
    else:
        hash_path.write_text(contract_hash + "\n", encoding="utf-8")

    batch_dir = Path(repo_root) / BATCH_RUN_REL
    events = load_events_csv(batch_dir)
    windows = load_windows_csv(batch_dir)

    # Build work list: winner then control per pair
    work: list[dict[str, Any]] = []
    for p in pairs:
        for role, eid in (("WINNER", p["winner_event_id"]), ("CONTROL", p["control_event_id"])):
            work.append({"pair_id": p["pair_id"], "case_role": role, "event_id": eid, "pair": p})

    total = len(work)
    results: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    peak = _peak_ram_mb()

    client = None if dry_run_only else get_clickhouse_client(role="mp_qdh_30event_case_control")
    try:
        for i, item in enumerate(work):
            eid = item["event_id"]
            ckpt = ckpt_dir / f"{eid}.json"
            if resume and ckpt.exists() and not dry_run_only:
                prev = json.loads(ckpt.read_text(encoding="utf-8"))
                if prev.get("contract_hash") != contract_hash:
                    raise RuntimeError(f"checkpoint contract hash mismatch for {eid}")
                results[eid] = prev["result"]
                log.info("RESUME %s (%d/%d)", eid, i + 1, total)
                continue

            ev = events.get(eid)
            if ev is None:
                err = {"ok": False, "event_id": eid, "error": "EVENT_ID_NOT_FOUND"}
                results[eid] = err
                warnings.append(err)
                atomic_write_json(
                    ckpt,
                    {"contract_hash": contract_hash, "result": err, "completed_at": datetime.now(timezone.utc).isoformat()},
                )
                continue
            wid = ev.get("window_id") or ""
            window = windows.get(wid) or {}
            case_meta = {
                "pair_id": item["pair_id"],
                "case_role": item["case_role"],
                "outcome_class": item["pair"].get(
                    "winner_outcome" if item["case_role"] == "WINNER" else "control_outcome"
                ),
            }
            log.info("RUN %s %s (%d/%d)", item["case_role"], eid, i + 1, total)
            try:
                res = analyze_one_event(
                    event=ev,
                    window=window,
                    case_meta=case_meta,
                    client=client,
                    dry_run=dry_run_only,
                )
            except Exception as exc:  # noqa: BLE001
                res = {
                    "ok": False,
                    "event_id": eid,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "case": case_meta,
                }
                warnings.append({"event_id": eid, "error": str(exc)})
                log.exception("FAIL %s", eid)
            # Strip heavy internals before checkpoint; keep features + summaries
            slim = {k: v for k, v in res.items() if k not in ("candidates",)}
            # flow series kept but large — store separately
            flow_100 = slim.pop("flow_100ms", [])
            flow_1s = slim.pop("flow_1s", [])
            qdh_traj = slim.pop("qdh_trajectory", [])
            wall_ev = slim.pop("wall_movement_events", [])
            results[eid] = slim
            if not dry_run_only:
                atomic_write_json(
                    ckpt,
                    {
                        "contract_hash": contract_hash,
                        "result": slim,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                # side artifacts per event
                ev_dir = ckpt_dir / eid
                ev_dir.mkdir(exist_ok=True)
                _write_csv(ev_dir / "flow_100ms.csv", flow_100)
                _write_csv(ev_dir / "flow_1s.csv", flow_1s)
                _write_csv(ev_dir / "qdh_trajectory.csv", qdh_traj)
                _write_csv(ev_dir / "wall_movement_events.csv", wall_ev)
                # stash back for final concat
                slim["_flow_100ms"] = flow_100
                slim["_flow_1s"] = flow_1s
                slim["_qdh_trajectory"] = qdh_traj
                slim["_wall_movement_events"] = wall_ev
            peak = max(peak, _peak_ram_mb())
            log.info("DONE %s ok=%s linkage=%s", eid, slim.get("ok"), slim.get("linkage_status"))
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
                    {
                        "event_id": eid,
                        "ok": r.get("ok"),
                        "linkage_status": r.get("linkage_status"),
                        "case": r.get("case"),
                    }
                    for eid, r in results.items()
                ],
                "db_mutation": False,
            },
        )
        return {
            "ok": True,
            "verdict": "DRY_RUN_ONLY",
            "out_dir": str(out_dir),
            "n_events": len(results),
        }

    # Reload flows from checkpoints if resumed without in-memory series
    for eid, r in results.items():
        if "_flow_100ms" not in r:
            ev_dir = ckpt_dir / eid
            if (ev_dir / "flow_100ms.csv").exists():
                r["_flow_100ms"] = list(csv.DictReader((ev_dir / "flow_100ms.csv").open(encoding="utf-8")))
                r["_flow_1s"] = list(csv.DictReader((ev_dir / "flow_1s.csv").open(encoding="utf-8")))
                r["_qdh_trajectory"] = list(csv.DictReader((ev_dir / "qdh_trajectory.csv").open(encoding="utf-8")))
                r["_wall_movement_events"] = list(
                    csv.DictReader((ev_dir / "wall_movement_events.csv").open(encoding="utf-8"))
                )

    # Assemble outputs
    feat_rows = []
    flow100_all = []
    flow1s_all = []
    wall_sum_rows = []
    wall_ev_all = []
    attr_rows = []
    qdh_all = []
    vac_rows = []
    micro_rows = []
    dq_rows = []
    features_by_event: dict[str, dict[str, Any]] = {}
    meta_by_event: dict[str, dict[str, Any]] = {}

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
                "mass_balance_violations": r.get("mass_balance_violations"),
                "causality_violations": r.get("causality_violations"),
            }
        )
        # confluence from frozen universe
        for er in frozen["event_rows"]:
            if er["event_id"] == eid:
                feat["confluence_class"] = er.get("confluence_class")
                break
        feat_rows.append(feat)
        features_by_event[eid] = feat
        meta_by_event[eid] = feat
        flow100_all.extend(r.get("_flow_100ms") or [])
        flow1s_all.extend(r.get("_flow_1s") or [])
        ws = dict(r.get("wall_movement_summary") or {})
        ws["event_id"] = eid
        ws["pair_id"] = item["pair_id"]
        ws["case_role"] = item["case_role"]
        wall_sum_rows.append(ws)
        wall_ev_all.extend(r.get("_wall_movement_events") or [])
        atr = dict(r.get("attribution_summary") or {})
        atr["event_id"] = eid
        # drop sample detail from csv summary — keep counts
        sample = atr.pop("rejection_sample", None)
        atr["rejection_sample_n"] = atr.get("rejection_sample_n")
        atr["rejection_counts"] = json.dumps(atr.get("rejection_counts") or {})
        atr["rejection_qty"] = json.dumps(atr.get("rejection_qty") or {})
        atr["funnel"] = json.dumps(atr.get("funnel") or {})
        attr_rows.append(atr)
        if sample:
            # write sample once per event into a small sidecar collected later
            pass
        qdh_all.extend(r.get("_qdh_trajectory") or [])
        vac = dict(r.get("vacuum") or {})
        vac["event_id"] = eid
        vac_rows.append(vac)
        micro = dict(r.get("microprice") or {})
        micro["event_id"] = eid
        micro_rows.append(micro)
        dq_rows.append(
            {
                "event_id": eid,
                "ok": r.get("ok"),
                "linkage_status": r.get("linkage_status"),
                "mass_balance_violations": r.get("mass_balance_violations"),
                "causality_violations": r.get("causality_violations"),
                "error": r.get("error") or r.get("blocked_reason"),
                "n_flow_100ms": len(r.get("_flow_100ms") or []),
                "has_wall": bool(r.get("selected") or (r.get("features") or {}).get("wall_id")),
            }
        )

    paired_rows = paired_feature_rows(pairs, features_by_event, PAIRED_FEATURE_KEYS)
    sub_rows = subgroup_rows(pairs, features_by_event, meta_by_event, PAIRED_FEATURE_KEYS[:12])
    ov_rows = overlap_groups(frozen["event_rows"])
    abl_rows = ablation_summary(pairs, features_by_event)

    _write_csv(out_dir / "canonical_event_features.csv", feat_rows)
    _write_csv(out_dir / "canonical_flow_100ms.csv", flow100_all)
    _write_csv(out_dir / "canonical_flow_1s.csv", flow1s_all)
    _write_csv(out_dir / "wall_movement_events.csv", wall_ev_all)
    _write_csv(out_dir / "wall_movement_summary.csv", wall_sum_rows)
    _write_csv(out_dir / "public_trade_attribution_summary.csv", attr_rows)
    _write_csv(out_dir / "queue_qdh_trajectory.csv", qdh_all)
    _write_csv(out_dir / "vacuum_depth_features.csv", vac_rows)
    _write_csv(out_dir / "microprice_response.csv", micro_rows)
    _write_csv(out_dir / "paired_feature_comparison.csv", paired_rows)
    _write_csv(out_dir / "subgroup_comparison.csv", sub_rows)
    _write_csv(out_dir / "overlap_groups.csv", ov_rows)
    _write_csv(out_dir / "ablation_summary.csv", abl_rows)
    _write_csv(out_dir / "data_quality_report.csv", dq_rows)

    # Summary metrics
    n_ok = sum(1 for r in results.values() if r.get("ok"))
    caus_v = sum(int(r.get("causality_violations") or 0) for r in results.values())
    mass_v = sum(int(r.get("mass_balance_violations") or 0) for r in results.values())
    status_counts: dict[str, int] = {}
    move_states: dict[str, int] = {}
    fill_sum = pull_sum = refill_sum = 0.0
    dec_sum = 0.0
    for eid, r in results.items():
        st = str(r.get("linkage_status") or "UNKNOWN")
        status_counts[st] = status_counts.get(st, 0) + 1
        ms = str((r.get("wall_movement_summary") or {}).get("movement_state") or "NA")
        move_states[ms] = move_states.get(ms, 0) + 1
        f = r.get("features") or {}
        fill_sum += float(f.get("attributed_fill_qty") or 0)
        pull_sum += float(f.get("residual_pull_qty") or 0)
        refill_sum += float(f.get("refill_qty") or 0)
        dec_sum += float(f.get("book_decrease_qty") or 0)

    all_processed = n_ok == total and len(pairs) == 15 and not frozen["gaps"]
    if caus_v > 0 or mass_v > 0:
        verdict = "QDH_30EVENT_COMPARISON_FAILED"
    elif not frozen["ok"] or len(pairs) < 15:
        verdict = "QDH_30EVENT_COMPARISON_PARTIAL"
    elif all_processed and caus_v == 0 and mass_v == 0:
        verdict = "QDH_30EVENT_COMPARISON_SUCCESS"
    elif n_ok > 0:
        verdict = "QDH_30EVENT_COMPARISON_PARTIAL"
    else:
        verdict = "QDH_30EVENT_COMPARISON_FAILED"

    if len(pairs) < 15 and frozen["gaps"]:
        warnings.append({"type": "MATCH_GAPS", "gaps": frozen["gaps"]})
    warnings.append({"type": "LOW_SAMPLE", "n_pairs": len(pairs), "note": "n=15 pairs; no strategy claim"})
    if ov_rows:
        warnings.append({"type": "OVERLAP_PSEUDOREPLICATION", "n_overlap_groups": len(ov_rows)})

    elapsed = time.monotonic() - t0
    summary = {
        "verdict": verdict,
        "n_winners": frozen["manifest"]["n_winners"],
        "n_controls": frozen["manifest"]["n_controls_matched"],
        "n_pairs": len(pairs),
        "n_events_processed_ok": n_ok,
        "n_events_total": total,
        "linkage_status_counts": status_counts,
        "wall_movement_states": move_states,
        "attributed_fill_qty": fill_sum,
        "pull_sum": pull_sum,
        "refill_sum": refill_sum,
        "fill_share_of_decrease": (fill_sum / dec_sum) if dec_sum > 1e-12 else None,
        "causality_violations": caus_v,
        "mass_balance_violations": mass_v,
        "event_list_sha256": frozen["event_list_sha256"],
        "pair_list_sha256": frozen["pair_list_sha256"],
        "contract_hash_sha256": contract_hash,
        "low_sample": True,
        "no_confirmed_edge": True,
        "elapsed_s": elapsed,
        "peak_ram_mb": peak,
    }
    atomic_write_json(out_dir / "run_manifest.json", {
        "ok": verdict.endswith("SUCCESS") or verdict.endswith("PARTIAL"),
        "study_id": STUDY_ID,
        "package": PACKAGE_NAME,
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "summary": summary,
        "ablation_groups": {k: v for k, v in ABLATION_GROUPS.items()},
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    atomic_write_json(out_dir / "warnings.json", warnings)
    atomic_write_json(out_dir / "runtime_metrics.json", {"elapsed_s": elapsed, "peak_ram_mb": peak})

    report = render_report(
        summary=summary,
        frozen=frozen,
        paired_rows=paired_rows,
        abl_rows=abl_rows,
        status_counts=status_counts,
        move_states=move_states,
        ov_rows=ov_rows,
        warnings=warnings,
    )
    atomic_write_text(out_dir / "QDH_30EVENT_CASE_CONTROL_REPORT.md", report)
    log.info("DONE %s", verdict)
    return {"ok": verdict == "QDH_30EVENT_COMPARISON_SUCCESS", "verdict": verdict, "out_dir": str(out_dir), "summary": summary}

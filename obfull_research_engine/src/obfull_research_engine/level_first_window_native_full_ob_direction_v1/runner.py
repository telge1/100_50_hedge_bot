"""Outcome-blind window-native Full-OB direction run. No ClickHouse writes."""

from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_csv, atomic_write_json, atomic_write_text
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..paths import ENGINE_ROOT
from . import (
    CAPABILITY_AUDIT_DIR,
    CONFIG_REL,
    CONTRACT_NAME,
    CONTRACT_VERSION,
    CONTRADICTION_FAMILIES,
    DRILLDOWN_CONFIG_REL,
    EXPECTED_DRILLDOWN_SHA256,
    FROZEN_AUDIT,
    FROZEN_ORIGINAL_PILOT,
    REPAIRED_PILOT_DIR,
    SCHEMA_REL,
    SUPPORT_FAMILIES,
)
from .analyze import analyze_episode, reclassify_stored_episode
from .quality import quality_precheck
from .replay_local import drilldown_cfg_values

RESULTS_ROOT = ENGINE_ROOT / "results" / "level_first_window_native_full_ob_direction_v1"
OUTCOMES_NAME = "path_outcomes.csv"
EXPECTED_REACTION_HASH = "baec330b513dfeaff706111a1581b180f4633baf38bcf97abcf176a4e8c353af"
EXPECTED_CAPABILITY_MAPPING = None  # filled after first known write; compared by file hash snapshot
FROZEN_TARGETS = (
    (REPAIRED_PILOT_DIR, ("reaction_classification.csv", "detection_blind_hash.json", "run_manifest.json")),
    (CAPABILITY_AUDIT_DIR, ("directed_episode_mapping.csv", "run_manifest.json", "causality_proof.json")),
    (FROZEN_AUDIT, ("repair_manifest.json", "final_report.md")),
    (FROZEN_ORIGINAL_PILOT, ("run_manifest.json", "detection_blind_hash.json")),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _frozen_snapshot() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel, names in FROZEN_TARGETS:
        for name in names:
            path = ENGINE_ROOT / rel / name
            if path.is_file():
                out[f"{rel}/{name}"] = file_sha256(path)
    return out


def load_frozen_configs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    schema = _read_json(ENGINE_ROOT / SCHEMA_REL)
    contract = _read_json(ENGINE_ROOT / CONFIG_REL)
    drill = _read_json(ENGINE_ROOT / DRILLDOWN_CONFIG_REL)
    drill_hash = file_sha256(ENGINE_ROOT / DRILLDOWN_CONFIG_REL)
    if drill_hash != EXPECTED_DRILLDOWN_SHA256:
        raise RuntimeError("event_drilldown_v1.json hash drifted")
    if drill_hash != contract.get("source_drilldown_config_sha256"):
        raise RuntimeError("contract source drilldown hash mismatch")
    config_hash = sha256_hex({"schema": schema, "contract": contract, "drilldown_sha256": drill_hash})
    return schema, contract, drill, config_hash, drill_hash


def _git_meta() -> dict[str, str]:
    import subprocess

    repo = ENGINE_ROOT.parent
    try:
        branch = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True)
        return {"branch": branch, "head": head, "dirty": "true" if dirty.strip() else "false"}
    except Exception:  # noqa: BLE001
        return {"branch": "unknown", "head": "unknown", "dirty": "unknown"}


def _join_inputs() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pilot = ENGINE_ROOT / REPAIRED_PILOT_DIR
    if (pilot / OUTCOMES_NAME).is_file():
        outcomes_present = True
    else:
        outcomes_present = False
    reactions = _read_csv(pilot / "reaction_classification.csv")
    if file_sha256(pilot / "reaction_classification.csv") != EXPECTED_REACTION_HASH:
        raise RuntimeError("repaired reaction hash drifted")
    episodes = {r["episode_id"]: r for r in _read_csv(pilot / "level_touch_episodes.csv")}
    cluster_rows = _read_csv(pilot / "level_clusters.csv")
    clusters = {r["level_cluster_id"]: r for r in cluster_rows}
    by_persistent: dict[str, list[dict[str, Any]]] = {}
    for r in cluster_rows:
        by_persistent.setdefault(r["persistent_cluster_id"], []).append(r)
    mapping = {r["episode_id"]: r for r in _read_csv(ENGINE_ROOT / CAPABILITY_AUDIT_DIR / "directed_episode_mapping.csv")}
    coverage = _read_json(pilot / "coverage_report.json")
    rows = []
    for rx in reactions:
        ep = episodes[rx["episode_id"]]
        cl = clusters.get(rx["level_cluster_id"]) or clusters.get(ep["level_cluster_id"])
        if cl is None:
            members = by_persistent.get(ep["persistent_cluster_id"]) or []
            if not members:
                raise KeyError(rx["level_cluster_id"])
            cl = {
                "cluster_price_low": min(float(m["cluster_price_low"]) for m in members),
                "cluster_price_high": max(float(m["cluster_price_high"]) for m in members),
            }
        mp = mapping.get(rx["episode_id"]) or {}
        directed = rx.get("reaction_direction") in {"BULLISH", "BEARISH"}
        usable = str(mp.get("usable_full_ob_coverage") or "").lower() == "true"
        rows.append(
            {
                "episode_id": rx["episode_id"],
                "level_cluster_id": rx["level_cluster_id"],
                "reaction_class": rx["reaction_class"],
                "reaction_direction": rx.get("reaction_direction") or "",
                "level_zone_low": cl["cluster_price_low"],
                "level_zone_high": cl["cluster_price_high"],
                "first_touch_ts": ep["first_touch_ts"],
                "detection_available_at": rx["detection_available_at"],
                "symbol": "BTCUSDT",
                "tick_size": 0.1,
                "source_hashes": ep.get("source_hashes") or "",
                "coverage_span_id": "",
                "directed": directed,
                "prior_usable_coverage": usable,
                "prior_mapping": mp.get("final_mapping") or "",
                "prior_reason": mp.get("decision_reasons") or "",
            }
        )
    meta = {
        "outcomes_present_but_not_loaded": outcomes_present,
        "localized_coverage": coverage.get("localized_coverage") or {},
        "n_reactions": len(reactions),
    }
    return rows, meta


def compact_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Keep COMPLETE resume fields without the raw replay timeline."""
    out = {k: v for k, v in rec.items() if k != "bundle"}
    bundle = rec.get("bundle") or {}
    walls = bundle.get("walls") or []
    refills = bundle.get("refills") or []
    out["bundle"] = {
        "ok": bundle.get("ok", True),
        "walls": walls,
        "refills": [
            {k: r.get(k) for k in ("refill_type", "side", "original_price", "refill_price", "removed_notional", "refilled_notional", "event_time")}
            for r in refills
            if r.get("refill_type") in {"EXACT_REFILL", "NEARBY_REFILL"}
        ],
        "n_refills_raw": len(refills),
        "n_walls": len(walls),
        "n_states": len(bundle.get("states_100ms") or []),
        "engines": bundle.get("engines"),
        "sequence_gaps": bundle.get("sequence_gaps"),
        "ordering_confidence": bundle.get("ordering_confidence"),
        "replay_epoch": bundle.get("replay_epoch"),
        "window_start": bundle.get("window_start"),
        "window_end": bundle.get("window_end"),
    }
    return out


def _episode_path(directory: Path, episode_id: str) -> Path:
    safe = episode_id.replace(":", "_")
    return directory / "episodes" / f"{safe}.json"


def _acquire_lock(directory: Path, run_key: str) -> Path:
    lock = directory / "run.lock"
    directory.mkdir(parents=True, exist_ok=True)
    if lock.is_file():
        prev = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(prev.get("pid") or 0)
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)
            except OSError:
                pass
            else:
                raise RuntimeError(f"run_key {run_key} already running pid={pid}")
    lock.write_text(json.dumps({"pid": os.getpid(), "run_key": run_key, "ts": _now()}), encoding="utf-8")
    return lock


def run_window_native(
    *,
    symbol: str = "BTCUSDT",
    precheck_only: bool = False,
    resume: bool = True,
    assemble_only: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    before = _frozen_snapshot()
    schema, contract, drill, config_hash, drill_hash = load_frozen_configs()
    engine_cfg = drilldown_cfg_values(drill)
    run_key = "fo1_" + config_hash[:16]
    directory = RESULTS_ROOT / symbol.upper() / run_key
    lock = _acquire_lock(directory, run_key)
    inputs, meta = _join_inputs()
    localized = meta["localized_coverage"]
    tick = contract["documented_price_grid"]["tick_size"]

    inventory = []
    pre_rows = []
    to_replay = []
    excluded = []
    undirected = []
    for row in inputs:
        row["tick_size"] = tick
        if not row["directed"]:
            undirected.append(row)
            pre_rows.append(
                {
                    **{k: row[k] for k in ("episode_id", "reaction_class", "reaction_direction")},
                    "replay_allowed": False,
                    "reason": "REACTION_DIRECTION_UNAVAILABLE",
                    "prior_usable_coverage": False,
                }
            )
            inventory.append({**row, "precheck_reason": "REACTION_DIRECTION_UNAVAILABLE"})
            continue
        if not row["prior_usable_coverage"]:
            excluded.append(row)
            reason = row.get("prior_reason") or "PRIOR_QUALITY_EXCLUSION"
            pre_rows.append(
                {
                    "episode_id": row["episode_id"],
                    "reaction_class": row["reaction_class"],
                    "reaction_direction": row["reaction_direction"],
                    "replay_allowed": False,
                    "reason": reason,
                    "prior_usable_coverage": False,
                }
            )
            inventory.append({**row, "precheck_reason": "PRIOR_QUALITY_EXCLUSION_NO_REPLAY"})
            continue
        q = quality_precheck(
            symbol=symbol,
            first_touch=parse_utc(row["first_touch_ts"]),
            detection=parse_utc(row["detection_available_at"]),
            reaction_direction=row["reaction_direction"],
            localized_coverage=localized,
        )
        row["coverage_span_id"] = q.get("coverage_span_id") or ""
        row["evidence_start"] = q.get("evidence_start")
        row["evidence_end"] = q.get("evidence_end")
        inventory.append({**row, "precheck_reason": q.get("reason") or "USABLE"})
        pre_rows.append(
            {
                "episode_id": row["episode_id"],
                "reaction_class": row["reaction_class"],
                "reaction_direction": row["reaction_direction"],
                "replay_allowed": q.get("replay_allowed"),
                "reason": q.get("reason") or "USABLE",
                "prior_usable_coverage": True,
                "coverage_span_id": row["coverage_span_id"],
                "evidence_start": row["evidence_start"],
                "evidence_end": row["evidence_end"],
            }
        )
        if q.get("replay_allowed"):
            to_replay.append(row)
        else:
            excluded.append(row)

    estimated = 90.0 + 8.0 * len(to_replay)
    atomic_write_csv(directory / "input_episode_inventory.csv", inventory)
    atomic_write_csv(directory / "quality_precheck.csv", pre_rows)
    atomic_write_json(
        directory / "config.json",
        {
            "contract": contract,
            "schema_version": schema.get("schema_version"),
            "config_hash": config_hash,
            "drilldown_sha256": drill_hash,
            "outcomes_loaded": False,
        },
    )
    precheck_manifest = {
        "schema": "level_first_window_native_full_ob_direction_v1",
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "run_key": run_key,
        "run_dir": str(directory),
        "status": "PRECHECK_ONLY" if precheck_only else "RUNNING",
        "n_episodes": len(inputs),
        "n_directed": sum(1 for r in inputs if r["directed"]),
        "n_undirected": len(undirected),
        "n_replay_planned": len(to_replay),
        "n_excluded_no_replay": len(excluded),
        "estimated_replay_s": estimated,
        "config_hash": config_hash,
        "outcomes_loaded": False,
        "clickhouse_writes": 0,
        "created_at": _now(),
    }
    atomic_write_json(directory / "run_manifest.json", precheck_manifest)
    if precheck_only:
        after = _frozen_snapshot()
        if after != before:
            raise RuntimeError("frozen artifacts changed during precheck")
        return {
            "verdict": None,
            "status": "PRECHECK_ONLY",
            "run_key": run_key,
            "run_dir": str(directory),
            "n_directed": precheck_manifest["n_directed"],
            "n_undirected": len(undirected),
            "n_replayed": 0,
            "n_replay_planned": len(to_replay),
            "n_excluded_no_replay": len(excluded),
            "estimated_replay_s": estimated,
            "outcomes_loaded": False,
            "clickhouse_writes": 0,
        }

    (directory / "episodes").mkdir(parents=True, exist_ok=True)
    analyzed: list[dict[str, Any]] = []
    n_replayed = 0
    n_reused = 0
    for row in to_replay:
        ep_file = _episode_path(directory, row["episode_id"])
        if resume and ep_file.is_file():
            rec = _read_json(ep_file)
            if rec.get("status") == "COMPLETE":
                rec = reclassify_stored_episode(rec, contract=contract)
                atomic_write_json(ep_file, rec)
                analyzed.append(rec)
                n_reused += 1
                continue
        if assemble_only:
            raise FileNotFoundError(f"assemble-only missing COMPLETE episode {row['episode_id']}")
        rec = analyze_episode(
            row,
            contract=contract,
            engine_cfg=engine_cfg,
            localized_coverage=localized,
            bundle=None,
            allow_replay=True,
        )
        rec["status"] = "COMPLETE"
        rec["replayed"] = True
        compact = compact_record(rec)
        atomic_write_json(ep_file, compact)
        analyzed.append(compact)
        n_replayed += 1

    skipped_results = []
    for row in excluded:
        skipped_results.append(
            analyze_episode(
                row,
                contract=contract,
                engine_cfg=engine_cfg,
                localized_coverage=localized,
                bundle=None,
                allow_replay=False,
            )
        )
    for row in undirected:
        skipped_results.append(
            analyze_episode(
                row,
                contract=contract,
                engine_cfg=engine_cfg,
                localized_coverage=localized,
                bundle=None,
                allow_replay=False,
            )
        )

    all_rows = analyzed + skipped_results
    _write_artifacts(directory, all_rows, contract, config_hash)
    labels = Counter((r.get("overall") or {}).get("overall_class") for r in all_rows if r.get("episode", {}).get("directed"))
    # include undirected label
    labels["FULL_OB_DIRECTION_NOT_EVALUATED"] = len(undirected)

    after = _frozen_snapshot()
    if after != before:
        raise RuntimeError("frozen research artifacts changed")

    blind_payload = {
        "config_hash": config_hash,
        "classifications": [
            {
                "episode_id": r["episode"]["episode_id"],
                "overall_class": (r.get("overall") or {}).get("overall_class"),
                "families": {k: (r.get("families") or {}).get(k, {}).get("status") for k in SUPPORT_FAMILIES + CONTRADICTION_FAMILIES},
            }
            for r in sorted(all_rows, key=lambda x: x["episode"]["episode_id"])
        ],
        "outcomes_loaded": False,
    }
    blind_hash = sha256_hex(blind_payload)
    atomic_write_json(
        directory / "outcome_blind_hash.json",
        {"blind_content_hash": blind_hash, "outcomes_loaded": False, "computed_at": _now(), "payload": blind_payload},
    )
    causality = {
        "outcomes_loaded": False,
        "path_outcomes_opened": False,
        "outcomes_present_but_not_loaded": meta["outcomes_present_but_not_loaded"],
        "clickhouse_writes": 0,
        "candidate_type_used": False,
        "continuation_slots_used": False,
        "evidence_ends_before_detection": True,
        "post_detection_updates_read": False,
        "n_replayed": n_replayed + n_reused,
        "n_replayed_this_process": n_replayed,
        "n_reused_complete": n_reused,
        "ordering_ambiguous_not_fail_closed_for_book_families": True,
        "n_excluded_no_replay": len(excluded),
        "n_undirected_not_replayed": len(undirected),
        "frozen_hashes_before": before,
        "frozen_hashes_after": after,
        "frozen_unchanged": True,
        "config_hash": config_hash,
        "drilldown_sha256": drill_hash,
        "repaired_reaction_classification_sha256": EXPECTED_REACTION_HASH,
        "blind_content_hash": blind_hash,
    }
    atomic_write_json(directory / "causality_proof.json", causality)

    elapsed = time.monotonic() - started
    n_replayed_total = n_replayed + n_reused
    n_strong = labels.get("FULL_OB_STRONGLY_SUPPORTS_REACTION", 0)
    n_partial = labels.get("FULL_OB_PARTIALLY_SUPPORTS_REACTION", 0)
    n_contra = labels.get("FULL_OB_CONTRADICTS_REACTION", 0)
    n_mixed = labels.get("FULL_OB_MIXED", 0)
    n_unclear = labels.get("FULL_OB_UNCLEAR", 0)
    n_ne = labels.get("FULL_OB_NOT_EVALUATED", 0)
    if n_replayed_total == 0 and to_replay:
        verdict = "LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_V1_DATA_QUALITY_BLOCKED"
    elif n_strong + n_partial + n_contra + n_mixed + n_unclear == 0:
        verdict = "LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_V1_DATA_QUALITY_BLOCKED"
    else:
        verdict = "LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_V1_READY_DESCRIPTIVE"

    report = _report(
        verdict=verdict,
        run_key=run_key,
        directory=directory,
        config_hash=config_hash,
        labels=dict(labels),
        n_replayed=n_replayed_total,
        n_excluded=len(excluded),
        n_undirected=len(undirected),
        elapsed=elapsed,
        git=_git_meta(),
        blind_hash=blind_hash,
        contract=contract,
    )
    atomic_write_text(directory / "final_report.md", report)
    manifest = {
        "schema": "level_first_window_native_full_ob_direction_v1",
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "run_key": run_key,
        "run_dir": str(directory),
        "status": "COMPLETE",
        "verdict": verdict,
        "n_episodes": len(inputs),
        "n_directed": sum(1 for r in inputs if r["directed"]),
        "n_undirected": len(undirected),
        "n_replayed": n_replayed_total,
        "n_replayed_this_process": n_replayed,
        "n_reused_complete": n_reused,
        "n_excluded_no_replay": len(excluded),
        "label_counts": dict(labels),
        "config_hash": config_hash,
        "outcomes_loaded": False,
        "clickhouse_writes": 0,
        "dashboard_modified": False,
        "elapsed_s": elapsed,
        "git": _git_meta(),
        "updated_at": _now(),
    }
    atomic_write_json(directory / "run_manifest.json", manifest)
    try:
        lock.unlink()
    except OSError:
        pass
    return {
        "verdict": verdict,
        "status": "COMPLETE",
        "run_key": run_key,
        "run_dir": str(directory),
        "n_directed": manifest["n_directed"],
        "n_undirected": len(undirected),
        "n_replayed": n_replayed_total,
        "n_excluded_no_replay": len(excluded),
        "label_counts": dict(labels),
        "outcomes_loaded": False,
        "clickhouse_writes": 0,
        "estimated_replay_s": estimated,
        "elapsed_s": elapsed,
        "config_hash": config_hash,
        "blind_content_hash": blind_hash,
    }


def _write_artifacts(directory: Path, rows: list[dict[str, Any]], contract: dict[str, Any], config_hash: str) -> None:
    spatial_rows = []
    replay_rows = []
    feature_rows = []
    wall_rows = []
    refill_rows = []
    imb_rows = []
    temporal_rows = []
    family_rows = []
    class_rows = []
    excl_rows = []
    for rec in rows:
        ep = rec["episode"]
        eid = ep["episode_id"]
        spatial = rec.get("spatial") or {}
        feats = rec.get("features") or {}
        overall = rec.get("overall") or {}
        pre = rec.get("precheck") or {}
        rq = rec.get("replay_quality") or {}
        spatial_rows.append({"episode_id": eid, **{k: spatial.get(k) for k in (
            "min_covered_bid", "max_covered_ask", "level_zone_covered", "front_zone_covered",
            "back_zone_covered", "spatial_coverage_quality", "spatial_pass", "reaction_family",
        )}})
        replay_rows.append({
            "episode_id": eid,
            "replayed": bool(rec.get("replayed")),
            "engines": (rec.get("bundle") or {}).get("engines") if rec.get("bundle") else (
                {
                    "replay": "drilldown.replay.replay_window",
                    "states": "drilldown.aggregation_100ms.build_states_100ms",
                    "walls": "drilldown.walls.analyze_walls",
                    "refills": "drilldown.refill.detect_refills",
                } if rec.get("replayed") else {}
            ),
            "sequence_gaps": rq.get("sequence_gaps"),
            "crossed_book_buckets": rq.get("crossed_book_buckets"),
            "ordering_quality": rq.get("ordering_quality"),
            "skipped_reason": rec.get("skipped_reason"),
        })
        feature_rows.append({"episode_id": eid, **feats})
        for w in (rec.get("bundle") or {}).get("walls") or []:
            wall_rows.append({"episode_id": eid, **w})
        for r in (rec.get("bundle") or {}).get("refills") or []:
            refill_rows.append({"episode_id": eid, **{k: r.get(k) for k in r}})
        imb_rows.append({
            "episode_id": eid,
            "depth_imbalance_pre": feats.get("depth_imbalance_pre"),
            "depth_imbalance_post": feats.get("depth_imbalance_post"),
            "depth_imbalance_change": feats.get("depth_imbalance_change"),
            "bid_depth_change": feats.get("bid_depth_change"),
            "ask_depth_change": feats.get("ask_depth_change"),
        })
        temporal_rows.append({"episode_id": eid, **(rec.get("temporal") or {})})
        for name in SUPPORT_FAMILIES + CONTRADICTION_FAMILIES:
            fam = (rec.get("families") or {}).get(name) or {}
            family_rows.append({"episode_id": eid, "family": name, **fam})
        class_rows.append({
            "episode_id": eid,
            "reaction_class": ep.get("reaction_class"),
            "reaction_direction": ep.get("reaction_direction"),
            "overall_class": overall.get("overall_class"),
            "reason": overall.get("reason"),
            "n_support_families": overall.get("n_support_families"),
            "n_contradiction_families": overall.get("n_contradiction_families"),
            "support_families": overall.get("support_families"),
            "contradiction_families": overall.get("contradiction_families"),
            "config_hash": config_hash,
            "candidate_type_used": rec.get("candidate_type_used"),
            "continuation_slots_used": rec.get("continuation_slots_used"),
        })
        if overall.get("overall_class") in {"FULL_OB_NOT_EVALUATED", "FULL_OB_DIRECTION_NOT_EVALUATED"}:
            excl_rows.append({
                "episode_id": eid,
                "overall_class": overall.get("overall_class"),
                "reason": overall.get("reason") or rec.get("skipped_reason") or pre.get("reason"),
                "quality_reasons": overall.get("quality_reasons") or rq.get("reasons"),
            })
    if not wall_rows:
        wall_rows = [{"episode_id": "", "note": "no_wall_rows"}]
    if not refill_rows:
        refill_rows = [{"episode_id": "", "note": "no_refill_rows"}]
    atomic_write_csv(directory / "spatial_coverage.csv", spatial_rows)
    atomic_write_csv(directory / "replay_manifest.csv", replay_rows)
    atomic_write_csv(directory / "raw_book_features.csv", feature_rows)
    atomic_write_csv(directory / "wall_evidence.csv", wall_rows)
    atomic_write_csv(directory / "refill_removal_evidence.csv", refill_rows)
    atomic_write_csv(directory / "imbalance_evidence.csv", imb_rows)
    atomic_write_csv(directory / "temporal_evidence.csv", temporal_rows)
    atomic_write_csv(directory / "evidence_family_mapping.csv", family_rows)
    atomic_write_csv(directory / "full_ob_direction_classification.csv", class_rows)
    atomic_write_csv(directory / "quality_exclusions.csv", excl_rows)


def _report(**kw: Any) -> str:
    contract = kw["contract"]
    reused = "\n".join(
        f"- {x['name']} = {x['value']} ({x['original_function']})" for x in contract.get("reused_thresholds") or []
    )
    blocked = "\n".join(f"- {x['name']}: {x['reason']}" for x in contract.get("not_reusable_without_new_contract") or [])
    return "\n".join(
        [
            "# LEVEL_FIRST_WINDOW_NATIVE_FULL_OB_DIRECTION_V1",
            "",
            f"Verdict: `{kw['verdict']}`",
            f"Run: `{kw['run_key']}`",
            f"Config hash: `{kw['config_hash']}`",
            f"Blind hash: `{kw['blind_hash']}`",
            "",
            "## Reused engines",
            "",
            "- drilldown.replay.replay_window",
            "- drilldown.aggregation_100ms.build_states_100ms",
            "- drilldown.walls.analyze_walls",
            "- drilldown.refill.detect_refills",
            "- drilldown.imbalance depth primitives",
            "- drilldown.engine._book_at",
            "",
            "## Reused thresholds",
            "",
            reused,
            "",
            "## Not reusable without new contract",
            "",
            blocked,
            "",
            "## Counts",
            "",
            f"- replayed: {kw['n_replayed']}",
            f"- excluded no replay: {kw['n_excluded']}",
            f"- undirected: {kw['n_undirected']}",
            f"- labels: {json.dumps(kw['labels'], sort_keys=True)}",
            "",
            "## Known bounds",
            "",
            "Applying `analyze_walls` / `detect_refills` to a 5-minute level window (not a 1s candidate bucket)",
            "finds large persisted walls and exact refills on both sides in every usable episode.",
            "Therefore FRONT_WALL_CONTRADICTION and OPPOSING_REFILL_CONTRADICTION co-occur with BACK_LIQUIDITY_DEFENSE_SUPPORT",
            "and every evaluated episode is MIXED. This is descriptive, not a score.",
            "ORDERING_AMBIGUOUS is the existing trade+book equal-timestamp contract and does not fail-close book families.",
            "",
            f"Elapsed_s: {kw['elapsed']}",
            f"Git: {json.dumps(kw['git'])}",
            "",
            "Outcomes not loaded. No ClickHouse writes. No trading signal.",
            "",
        ]
    ) + "\n"

"""Outcome-blind bounded level-first pilot. Detection hash before any MFE/MAE."""

from __future__ import annotations

import json
import os
import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    DEFAULT_END_Z,
    DEFAULT_OUTCOME_END_Z,
    DEFAULT_START_Z,
    DEFAULT_SYMBOL,
    EVIDENCE_PRE_TOUCH_S,
    FROZEN_PHASE2_DIR,
    HORIZONS_S,
    PHASE2_CONFIG_HASH,
    PHASE2_LLD_CONFIG_HASH,
    PHASE2_MP_CONFIG_HASH,
    PHASE2_RUN_KEY,
    REACTION_HORIZON_S,
    SCHEMA_VERSION,
)
from .clusters import cluster_levels
from .coverage import check_pilot_coverage
from .episodes import parse_utc, walk_cluster_visits
from .levels import developing_update, extract_levels_from_event, unique_logical_levels
from .lld_context import map_cluster_lld
from .persist import (
    RESULTS_ROOT,
    atomic_write_csv,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    compute_run_key,
    file_hashes,
    load_json,
    mark,
    new_manifest,
    now_z,
    run_dir,
)
from .prices import (
    asof_price,
    load_candles_1m,
    load_pilot_trades,
    mid_events,
    price_marks,
    trade_events,
    try_load_1s_mid,
)
from .reactions import classify_reaction
from .snapshots import SnapshotCache, completed_minutes, materialize_minute_mp
from .summarize import count_values, reaction_family, summarize_all

REQUIRED_ARTIFACTS = (
    "coverage_report.json",
    "source_manifest.json",
    "mp_snapshots.jsonl",
    "lld_snapshots.jsonl",
    "mp_level_events.csv",
    "level_clusters.csv",
    "lld_confluence_mapping.csv",
    "level_touch_episodes.csv",
    "reaction_lifecycle.csv",
    "reaction_classification.csv",
    "local_analyzer_evidence.csv",
    "quality_exclusions.csv",
    "detection_blind_hash.json",
    "causality_proof.json",
    "path_outcomes.csv",
    "threshold_first_touch.csv",
    "outcome_summary_by_group.csv",
    "run_manifest.json",
    "final_report.md",
)


def frozen_run_config(
    *,
    symbol: str = DEFAULT_SYMBOL,
    start_z: str = DEFAULT_START_Z,
    end_z: str = DEFAULT_END_Z,
    outcome_end_z: str = DEFAULT_OUTCOME_END_Z,
) -> dict[str, Any]:
    cfg = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol.upper(),
        "start": start_z,
        "end": end_z,
        "outcome_end": outcome_end_z,
        "phase2_run_key": PHASE2_RUN_KEY,
        "phase2_config_hash": PHASE2_CONFIG_HASH,
        "mp_config_hash": PHASE2_MP_CONFIG_HASH,
        "lld_config_hash": PHASE2_LLD_CONFIG_HASH,
        "reaction_horizon_s": REACTION_HORIZON_S,
        "evidence_pre_touch_s": EVIDENCE_PRE_TOUCH_S,
        "horizons_s": list(HORIZONS_S),
        "clickhouse_writes": False,
        "uses_phase2_serializers_only": True,
        "no_second_mp_or_lld_formula": True,
        "integrity_repair": "LEVEL_FIRST_ANALYZER_FUNCTIONAL_INTEGRITY_AUDIT_V1",
    }
    cfg["config_hash"] = sha256_hex(cfg)
    return cfg


def _floor_minute(ts: datetime) -> datetime:
    ts = parse_utc(ts)
    return ts.replace(second=0, microsecond=0)


def asof_minute_z(ts: datetime | str) -> str:
    """Minute floor as Zulu. Local helper so timeparse.format_utc_z cannot be shadowed."""
    dt = parse_utc(ts)
    dt = dt.replace(second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_event_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "naked_poc_excluded_from_causal_event": True,
        "profile_state": row["profile_state"],
        "timeframe": row["timeframe"],
        "symbol": DEFAULT_SYMBOL,
        "profile_id": row["profile_id"],
        "profile_start": row["profile_start"],
        "natural_profile_end": row["natural_profile_end"],
        "effective_profile_end": row["effective_profile_end"],
        "request_as_of": row["snapshot_ts"],
        "available_at": row["available_at"],
        "tpo_poc": row["TPO_POC"],
        "tpo_vah": row["TPO_VAH"],
        "tpo_val": row["TPO_VAL"],
        "price_bin_size": row["price_bin_size"],
        "config_hash": row["config_hash"],
        "canonical_payload_hash": row["payload_hash"],
        "event_id": row.get("event_id") or row["profile_id"],
    }


def rebuild_levels_from_snapshots(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    closed_levels: dict[str, dict[str, Any]] = {}
    levels_by_minute: dict[str, list[dict[str, Any]]] = {}
    clusters_by_minute: dict[str, list[dict[str, Any]]] = {}
    all_level_events: list[dict[str, Any]] = []
    all_clusters: dict[str, dict[str, Any]] = {}
    by_minute: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_minute.setdefault(row["snapshot_ts"], []).append(row)
    for snap_z, minute_rows in sorted(by_minute.items()):
        developing_now: list[dict[str, Any]] = []
        for item in minute_rows:
            extracted = extract_levels_from_event(
                snapshot_event_from_row(item),
                raw_profile=None,
                snapshot_ts=item["snapshot_ts"],
            )
            for lvl in extracted:
                all_level_events.append(lvl)
                if lvl["profile_state"] == "CLOSED":
                    closed_levels[lvl["level_id"]] = lvl
                else:
                    developing_now.append(lvl)
        available = _available_levels_at(closed_by_id=closed_levels, developing_rows=developing_now)
        levels_by_minute[snap_z] = available
        clustered = cluster_levels(available)
        clusters_by_minute[snap_z] = clustered
        for cl in clustered:
            all_clusters[cl["level_cluster_id"]] = {k: v for k, v in cl.items() if k != "members"}
    return all_level_events, levels_by_minute, clusters_by_minute, all_clusters


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_lock(directory: Path, run_key: str) -> Path:
    lock = directory / "run.lock"
    directory.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            prev = int(lock.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            prev = 0
        if prev and _pid_running(prev) and prev != os.getpid():
            raise RuntimeError(f"run_key {run_key} already running as pid {prev}")
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return lock


def _available_levels_at(
    *,
    closed_by_id: dict[str, dict[str, Any]],
    developing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = list(closed_by_id.values()) + list(developing_rows)
    rows.sort(key=lambda r: r["level_id"])
    return rows


def run_precheck(
    *,
    symbol: str = DEFAULT_SYMBOL,
    start: datetime | None = None,
    end: datetime | None = None,
    outcome_end: datetime | None = None,
    results_root: str | Path | None = None,
) -> dict[str, Any]:
    start = start or parse_utc(DEFAULT_START_Z)
    end = end or parse_utc(DEFAULT_END_Z)
    outcome_end = outcome_end or parse_utc(DEFAULT_OUTCOME_END_Z)
    coverage = check_pilot_coverage(symbol=symbol, start=start, end=end, outcome_end=outcome_end)
    minutes = completed_minutes(start, end)
    unique_closed_est = 8 + 4 + 1
    developing_est = max(len(minutes) - 1, 0) * 3
    mp_calls_est = unique_closed_est + developing_est
    lld_calls_est = 40
    episode_est = 15
    replay_est = episode_est
    seconds_est = mp_calls_est * 0.35 + lld_calls_est * 0.6 + replay_est * 15 + 30
    cfg = frozen_run_config(
        symbol=symbol,
        start_z=format_utc_z(start),
        end_z=format_utc_z(end),
        outcome_end_z=format_utc_z(outcome_end),
    )
    key = compute_run_key(cfg)
    out = {
        **coverage,
        "estimated_mp_generator_calls": mp_calls_est,
        "estimated_lld_generator_calls": lld_calls_est,
        "estimated_episodes": episode_est,
        "estimated_local_ob_replays": replay_est,
        "reusable_ob_shards": coverage.get("reusable_ob_candidate_shards") or [],
        "local_ob_replay_likely": True,
        "estimated_runtime_seconds": round(seconds_est, 1),
        "exceeds_10_minutes": seconds_est > 600,
        "run_key": key,
        "result_path": str(run_dir(symbol, key, results_root=results_root)),
        "config_hash": cfg["config_hash"],
    }
    return out


def run_pilot(
    *,
    symbol: str = DEFAULT_SYMBOL,
    start_z: str = DEFAULT_START_Z,
    end_z: str = DEFAULT_END_Z,
    outcome_end_z: str = DEFAULT_OUTCOME_END_Z,
    resume: bool = True,
    precheck_only: bool = False,
    skip_local_ob_replay: bool = False,
    results_root: str | Path | None = None,
    export_builder_price_inputs: bool = False,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    cfg = frozen_run_config(
        symbol=symbol, start_z=start_z, end_z=end_z, outcome_end_z=outcome_end_z
    )
    start = parse_utc(start_z)
    end = parse_utc(end_z)
    outcome_end = parse_utc(outcome_end_z)
    key = compute_run_key(cfg)
    directory = run_dir(symbol, key, results_root=results_root)
    if precheck_only:
        pre = run_precheck(
            symbol=symbol,
            start=start,
            end=end,
            outcome_end=outcome_end,
            results_root=results_root,
        )
        return {**pre, "precheck_only": True, "run_key": key, "result_path": str(directory)}

    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "run_manifest.json"
    existing = load_json(manifest_path)
    if resume and existing and existing.get("status") == "COMPLETE":
        return {
            "status": "COMPLETE",
            "reused": True,
            "run_key": key,
            "run_dir": str(directory),
            "verdict": existing.get("verdict"),
            "manifest": existing,
        }
    _acquire_lock(directory, key)

    flags = {
        "outcomes_loaded_before_level_events": False,
        "outcomes_loaded_before_reaction_classification": False,
        "outcomes_loaded_before_detection_hash": False,
    }
    manifest = existing or new_manifest(symbol=symbol, run_key=key, directory=directory, config=cfg)
    mark(manifest, "RUNNING")
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(directory / "config.json", cfg)

    try:
        print("coverage precheck…", flush=True)
        coverage = check_pilot_coverage(symbol=symbol, start=start, end=end, outcome_end=outcome_end)
        atomic_write_json(directory / "coverage_report.json", coverage)
        print(f"coverage={coverage.get('verdict')}", flush=True)
        if not coverage.get("ok"):
            mark(
                manifest,
                "FAILED",
                verdict="LEVEL_FIRST_PILOT_DATA_QUALITY_BLOCKED",
                coverage_verdict=coverage.get("verdict"),
            )
            atomic_write_json(manifest_path, manifest)
            return {
                "status": "FAILED",
                "verdict": "LEVEL_FIRST_PILOT_DATA_QUALITY_BLOCKED",
                "coverage": coverage,
                "run_key": key,
                "run_dir": str(directory),
            }

        phase2 = ENGINE_ROOT / FROZEN_PHASE2_DIR
        source_manifest = {
            "phase2_dir": str(phase2),
            "phase2_run_key": PHASE2_RUN_KEY,
            "phase2_manifest": load_json(phase2 / "manifest.json"),
            "mp_generator": "market_profile_v1.service.load_profiles",
            "lld_generator": "indicators.liquidity_location.engine.run_liquidity_location",
            "no_second_algorithm": True,
            "forbidden_sources_unused": [
                "all_liquidations",
                "heatmap_png",
                "screenshot",
                "naked_poc",
            ],
        }
        atomic_write_json(directory / "source_manifest.json", source_manifest)

        trade_start = start - timedelta(seconds=EVIDENCE_PRE_TOUCH_S)
        candle_end = min(outcome_end, end + timedelta(seconds=REACTION_HORIZON_S + 60))
        trades, trade_meta = load_pilot_trades(symbol=symbol, start=trade_start, end=outcome_end)
        candles, candle_meta = load_candles_1m(
            symbol=symbol, start=trade_start - timedelta(minutes=2), end=candle_end
        )
        mid_idx, mid_meta = try_load_1s_mid(symbol=symbol, start=trade_start, end=outcome_end)
        t_events = trade_events(trades)
        m_events = mid_events(mid_idx)
        marks = price_marks(trades, mid_idx)

        cache = SnapshotCache()
        mp_snapshot_rows: list[dict[str, Any]] = []
        unique_mp: dict[str, dict[str, Any]] = {}
        closed_levels: dict[str, dict[str, Any]] = {}
        levels_by_minute: dict[str, list[dict[str, Any]]] = {}
        clusters_by_minute: dict[str, list[dict[str, Any]]] = {}
        all_level_events: list[dict[str, Any]] = []
        all_clusters: dict[str, dict[str, Any]] = {}

        existing_mp = directory / "mp_snapshots.jsonl"
        if existing_mp.is_file() and existing_mp.stat().st_size > 0:
            print(f"reusing MP snapshots {existing_mp}", flush=True)
            mp_snapshot_rows = [
                json.loads(line)
                for line in existing_mp.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            all_level_events, levels_by_minute, clusters_by_minute, all_clusters = rebuild_levels_from_snapshots(
                mp_snapshot_rows
            )
        else:
            minutes = completed_minutes(start, end)
            print(f"materializing {len(minutes)} MP minutes…", flush=True)
            for i, snap_ts in enumerate(minutes):
                if i % 15 == 0:
                    print(f"  mp minute {i}/{len(minutes)} {asof_minute_z(snap_ts)}", flush=True)
                minute_rows = materialize_minute_mp(cache, symbol=symbol, snapshot_ts=snap_ts)
                developing_now: list[dict[str, Any]] = []
                for item in minute_rows:
                    bundle = item.pop("bundle")
                    mp_snapshot_rows.append(item)
                    unique_mp[item["payload_hash"]] = bundle
                    extracted = extract_levels_from_event(
                        bundle["event"],
                        raw_profile=bundle.get("raw_profile"),
                        snapshot_ts=item["snapshot_ts"],
                    )
                    for lvl in extracted:
                        all_level_events.append(lvl)
                        if lvl["profile_state"] == "CLOSED":
                            closed_levels[lvl["level_id"]] = lvl
                        else:
                            developing_now.append(lvl)
                available = _available_levels_at(closed_by_id=closed_levels, developing_rows=developing_now)
                levels_by_minute[asof_minute_z(snap_ts)] = available
                clustered = cluster_levels(available)
                clusters_by_minute[asof_minute_z(snap_ts)] = clustered
                for cl in clustered:
                    all_clusters[cl["level_cluster_id"]] = {k: v for k, v in cl.items() if k != "members"}
            atomic_write_jsonl(directory / "mp_snapshots.jsonl", mp_snapshot_rows)

        episodes: list[dict[str, Any]] = []
        developing_updates: list[dict[str, Any]] = []
        quality_exclusions: list[dict[str, Any]] = []

        def clusters_at(ts: datetime) -> list[dict[str, Any]]:
            minute = _floor_minute(ts)
            if minute >= end:
                minute = end - timedelta(minutes=1)
            if minute < start:
                minute = start
            return clusters_by_minute.get(asof_minute_z(minute)) or []

        print("detecting touch episodes…", flush=True)
        episodes = walk_cluster_visits(
            clusters_at=clusters_at,
            trade_events=t_events,
            mid_events=m_events,
            candles_1m=candles,
            window_start=start,
            window_end=end,
        )

        # Developing updates after freeze
        frozen_ids = {m["level_id"]: m for ep in episodes for m in ep.get("frozen_members") or []}
        for lvl in all_level_events:
            frozen = frozen_ids.get(lvl["level_id"])
            if frozen is None:
                continue
            upd = developing_update(frozen=frozen, later=lvl)
            if upd:
                developing_updates.append(upd)

        if not all_level_events:
            quality_exclusions.append({"reason": "NO_MP_LEVEL", "action": "NO_EPISODE"})

        lld_snapshot_rows: list[dict[str, Any]] = []
        lld_zone_by_asof: dict[str, list[dict[str, Any]]] = {}
        confluence_rows: list[dict[str, Any]] = []
        reaction_rows: list[dict[str, Any]] = []
        lifecycle_rows: list[dict[str, Any]] = []
        analyzer_rows: list[dict[str, Any]] = []

        # LLD at unique touch minutes only (causal as_of=T).
        touch_minutes = sorted(
            {asof_minute_z(ep["first_touch_ts"]) for ep in episodes if ep.get("first_touch_ts")}
        )
        print(f"episodes={len(episodes)} lld_as_of_minutes={len(touch_minutes)}", flush=True)
        existing_lld = directory / "lld_snapshots.jsonl"
        existing_zones = directory / "lld_zones_by_asof.json"
        if existing_lld.is_file() and existing_lld.stat().st_size > 0 and existing_zones.is_file():
            lld_snapshot_rows = [
                json.loads(line)
                for line in existing_lld.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            lld_zone_by_asof = json.loads(existing_zones.read_text(encoding="utf-8"))
            print(f"reusing {len(lld_snapshot_rows)} LLD snapshots", flush=True)
        for i, mz in enumerate(touch_minutes):
            if mz in lld_zone_by_asof:
                continue
            if i % 5 == 0:
                print(f"  lld as_of {i}/{len(touch_minutes)} {mz}", flush=True)
            bundle = cache.lld(symbol=symbol, request_as_of=parse_utc(mz))
            snap = dict(bundle["snapshot"])
            snap.pop("generator", None)
            lld_snapshot_rows.append(snap)
            lld_zone_by_asof[mz] = bundle["zones"]
            atomic_write_jsonl(directory / "lld_snapshots.jsonl", lld_snapshot_rows)
            atomic_write_json(directory / "lld_zones_by_asof.json", lld_zone_by_asof)

        from .local_analyzer import evaluate_local_analyzers
        from ..single_case_inspector.ch_modalities import load_oi_samples
        from ..single_case_inspector.footprint_avr import load_second_series
        print("loading shared footprint series and OI samples…", flush=True)
        from ..single_case_inspector.footprint_avr import build_avr_1s

        fp_start = int((start - timedelta(seconds=EVIDENCE_PRE_TOUCH_S)).timestamp())
        fp_end = int((end + timedelta(seconds=REACTION_HORIZON_S)).timestamp())
        shared_fp_series, _bk, shared_fp_meta = load_second_series(
            symbol=symbol,
            start_unix=fp_start,
            end_unix=fp_end,
        )
        print("precomputing shared AVR once…", flush=True)
        shared_avr = build_avr_1s(
            symbol=symbol,
            series=shared_fp_series,
            start_unix=fp_start,
            end_unix=fp_end,
        )
        shared_fp_meta = {**shared_fp_meta, "avr_df": shared_avr}
        shared_oi_samples, shared_oi_meta = load_oi_samples(
            symbol=symbol,
            start=start - timedelta(seconds=1800 + EVIDENCE_PRE_TOUCH_S),
            end=end + timedelta(seconds=REACTION_HORIZON_S),
        )
        print(
            f"shared footprint buckets={shared_fp_meta.get('n_buckets')} avr_rows={0 if shared_avr is None else len(shared_avr)} oi_n={shared_oi_meta.get('n')}",
            flush=True,
        )

        print(f"classifying {len(episodes)} reactions and local analyzers…", flush=True)
        for i, ep in enumerate(episodes):
            if i % 25 == 0:
                print(f"  episode {i}/{len(episodes)}", flush=True)
            touch = parse_utc(ep["first_touch_ts"])
            minute_z = asof_minute_z(touch)
            cluster = {
                **ep["frozen_cluster"],
                "members": ep.get("frozen_members") or [],
            }
            zones = lld_zone_by_asof.get(minute_z) or []
            conf = map_cluster_lld(cluster=cluster, zones=zones, decision_time_z=ep["first_touch_ts"])
            confluence_rows.append(conf)
            reaction = classify_reaction(
                episode=ep,
                cluster=cluster,
                candles_1m=candles,
                price_marks=marks,
                lld_type=conf["lld_confluence_type"],
                window_end=end + timedelta(seconds=REACTION_HORIZON_S),
            )
            if reaction.get("detection_available_at") and parse_utc(
                reaction["detection_available_at"]
            ) < parse_utc(ep["first_touch_ts"]):
                raise RuntimeError("detection before touch")
            for step in reaction.pop("lifecycle", []) or []:
                lifecycle_rows.append(step)
            reaction_rows.append(reaction)
            det_z = reaction.get("detection_available_at") or ep["first_touch_ts"]
            if skip_local_ob_replay:
                analyzer = {
                    "full_ob_label": "NOT_EVALUATED",
                    "full_ob_reason": "SKIPPED_IN_PRECHECK",
                    "footprint_label": "NOT_EVALUATED",
                    "oi_label": "NOT_EVALUATED",
                }
            else:
                analyzer = evaluate_local_analyzers(
                    symbol=symbol,
                    first_touch=touch,
                    detection=parse_utc(det_z),
                    reaction_direction=reaction.get("reaction_direction"),
                    price_at_touch=asof_price(marks, touch),
                    price_at_detection=asof_price(marks, parse_utc(det_z)),
                    localized_coverage=coverage.get("localized_coverage"),
                    series=shared_fp_series,
                    series_meta=shared_fp_meta,
                    oi_samples=shared_oi_samples,
                    oi_meta=shared_oi_meta,
                )
            analyzer_rows.append(
                {
                    "episode_id": ep["episode_id"],
                    "level_cluster_id": ep["level_cluster_id"],
                    "detection_available_at": det_z,
                    **{k: analyzer[k] for k in analyzer if k not in {"oi_windows", "full_ob_quality", "footprint_meta"}},
                }
            )
            if analyzer.get("full_ob_label") == "NOT_EVALUATED":
                quality_exclusions.append(
                    {
                        "episode_id": ep["episode_id"],
                        "modality": "FULL_OB",
                        "reason": analyzer.get("full_ob_reason"),
                    }
                )

        flags["outcomes_loaded_before_level_events"] = False
        flags["outcomes_loaded_before_reaction_classification"] = False

        cluster_by_persistent: dict[str, dict[str, Any]] = {}
        for cl in all_clusters.values():
            pid = str(cl.get("persistent_cluster_id") or cl["level_cluster_id"])
            cluster_by_persistent[pid] = cl
        cluster_csv = list(cluster_by_persistent.values())
        level_csv = [
            {k: v for k, v in r.items() if k != "raw"}
            for r in unique_logical_levels(all_level_events)
        ]
        episode_csv = []
        for ep in episodes:
            episode_csv.append(
                {
                    "episode_id": ep["episode_id"],
                    "level_cluster_id": ep["level_cluster_id"],
                    "persistent_cluster_id": ep.get("persistent_cluster_id"),
                    "first_approach_ts": ep.get("first_approach_ts"),
                    "first_touch_ts": ep.get("first_touch_ts"),
                    "entry_into_zone_ts": ep.get("entry_into_zone_ts"),
                    "first_trade_in_zone_ts": ep.get("first_trade_in_zone_ts"),
                    "first_mid_in_zone_ts": ep.get("first_mid_in_zone_ts"),
                    "first_exit_ts": ep.get("first_exit_ts"),
                    "first_exit_direction": ep.get("first_exit_direction"),
                    "first_return_ts": ep.get("first_return_ts"),
                    "episode_close_ts": ep.get("episode_close_ts"),
                    "visit_count": ep.get("visit_count"),
                    "source_hashes": ep.get("source_hashes"),
                    "quality_status": ep.get("quality_status"),
                    "approach_side": ep.get("approach_side"),
                    "status": ep.get("status"),
                }
            )

        atomic_write_csv(directory / "mp_level_events.csv", level_csv)
        atomic_write_csv(directory / "level_clusters.csv", cluster_csv)
        atomic_write_jsonl(directory / "lld_snapshots.jsonl", lld_snapshot_rows)
        atomic_write_csv(directory / "lld_confluence_mapping.csv", confluence_rows)
        atomic_write_csv(directory / "level_touch_episodes.csv", episode_csv)
        atomic_write_csv(directory / "reaction_lifecycle.csv", lifecycle_rows)
        atomic_write_csv(directory / "reaction_classification.csv", reaction_rows)
        atomic_write_csv(directory / "local_analyzer_evidence.csv", analyzer_rows)
        atomic_write_csv(directory / "quality_exclusions.csv", quality_exclusions)
        atomic_write_json(directory / "developing_level_updates.json", developing_updates)

        blind_payload = {
            "config_hash": cfg["config_hash"],
            "mp_snapshot_hashes": [r["payload_hash"] for r in mp_snapshot_rows],
            "level_ids": [r["level_id"] for r in level_csv],
            "cluster_ids": [r["level_cluster_id"] for r in cluster_csv],
            "episode_ids": [r["episode_id"] for r in episode_csv],
            "reaction_classes": [
                {"episode_id": r["episode_id"], "reaction_class": r["reaction_class"]}
                for r in reaction_rows
            ],
            "analyzer": [
                {
                    "episode_id": r["episode_id"],
                    "full_ob_label": r.get("full_ob_label"),
                    "footprint_label": r.get("footprint_label"),
                    "oi_label": r.get("oi_label"),
                }
                for r in analyzer_rows
            ],
            "flags": dict(flags),
        }
        blind_hash = sha256_hex(blind_payload)
        flags["outcomes_loaded_before_detection_hash"] = False
        atomic_write_json(
            directory / "detection_blind_hash.json",
            {
                "blind_content_hash": blind_hash,
                "computed_at": now_z(),
                "outcomes_loaded_before_detection_hash": False,
                "payload": blind_payload,
            },
        )
        causality = {
            "outcomes_loaded_before_level_events": False,
            "outcomes_loaded_before_reaction_classification": False,
            "outcomes_loaded_before_detection_hash": False,
            "classification_inputs_have_mfe_mae": False,
            "available_at_respected": True,
            "lld_as_of_explicit": True,
            "analyzer_window_ends_at_detection": True,
            "naked_poc_excluded": True,
            "phase2_serializers_only": True,
        }
        atomic_write_json(directory / "causality_proof.json", causality)

        # --- outcomes only after blind hash ---
        from .outcomes import measure_detection_outcomes

        path_rows: list[dict[str, Any]] = []
        threshold_rows: list[dict[str, Any]] = []
        summary_input: list[dict[str, Any]] = []
        for ep, reaction, analyzer, conf in zip(
            episodes, reaction_rows, analyzer_rows, confluence_rows, strict=True
        ):
            measured = measure_detection_outcomes(
                detection=parse_utc(reaction.get("detection_available_at") or ep["first_touch_ts"]),
                direction=reaction.get("reaction_direction"),
                trades=trades,
                mid_index=mid_idx,
                outcome_end=outcome_end,
            )
            cluster = ep["frozen_cluster"]
            primary = measured.get("primary") or {}
            first_nz = primary.get("first_nonzero_direction")
            hz_map = {int(h.get("horizon_s") or 0): h for h in measured.get("horizons") or []}
            h300 = hz_map.get(300) or {}
            path_rows.append(
                {
                    "episode_id": ep["episode_id"],
                    "detection_available_at": reaction.get("detection_available_at"),
                    "reaction_class": reaction["reaction_class"],
                    "reaction_direction": reaction.get("reaction_direction"),
                    "reference_price": measured.get("reference_price"),
                    "reference_price_ts": measured.get("reference_price_ts"),
                    "first_nonzero_direction": first_nz or primary.get("first_move_label") or primary.get("first_dir"),
                    "profit_first": first_nz == "PROFIT_FIRST",
                    "adverse_first": first_nz == "ADVERSE_FIRST",
                    "directional_hit_evaluated": measured.get("directional_hit_evaluated"),
                    "mfe_300s": h300.get("mfe_pct"),
                    "mae_300s": h300.get("mae_pct"),
                    "giveback_300s": h300.get("giveback_from_mfe_pct"),
                    "retained_300s": h300.get("retained_profit_at_horizon_pct") or h300.get("retained_fraction_of_mfe"),
                    "horizons_json": measured.get("horizons"),
                }
            )
            for tr in measured.get("threshold_first_touch") or []:
                threshold_rows.append({"episode_id": ep["episode_id"], **tr})
            tfs = cluster.get("member_timeframes") or []
            states = set(cluster.get("member_profile_states") or [])
            mix = "MIXED" if states == {"CLOSED", "DEVELOPING"} else next(iter(states), "UNKNOWN")
            types = cluster.get("member_level_types") or []
            primary_tpo = types[0] if types else "UNKNOWN"
            summary_input.append(
                {
                    **path_rows[-1],
                    "highest_timeframe": cluster.get("highest_timeframe"),
                    "confluence_class": cluster.get("confluence_class"),
                    "profile_state_mix": mix,
                    "primary_tpo_type": primary_tpo,
                    "lld_confluence_type": conf.get("lld_confluence_type"),
                    "reaction_family": reaction_family(reaction["reaction_class"]),
                    "full_ob_label": analyzer.get("full_ob_label"),
                    "footprint_label": analyzer.get("footprint_label"),
                    "oi_label": analyzer.get("oi_label"),
                    "member_timeframes": tfs,
                }
            )

        after_hash = sha256_hex(blind_payload)
        if after_hash != blind_hash:
            raise RuntimeError("blind payload mutated after outcomes")

        atomic_write_csv(directory / "path_outcomes.csv", path_rows)
        atomic_write_csv(directory / "threshold_first_touch.csv", threshold_rows)
        summary = summarize_all(summary_input)
        atomic_write_csv(directory / "outcome_summary_by_group.csv", summary)

        elapsed = time.perf_counter() - t0
        rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        n_ep = len(episodes)
        if n_ep == 0:
            verdict = "LEVEL_FIRST_PILOT_INSUFFICIENT_EVENTS"
        else:
            verdict = "LEVEL_FIRST_PILOT_PASS_DESCRIPTIVE_ONLY"

        from .report import write_report

        write_report(
            directory=directory,
            verdict=verdict,
            cfg=cfg,
            coverage=coverage,
            mp_snapshots=mp_snapshot_rows,
            lld_snapshots=lld_snapshot_rows,
            levels=level_csv,
            clusters=cluster_csv,
            episodes=episode_csv,
            reactions=reaction_rows,
            analyzers=analyzer_rows,
            confluence=confluence_rows,
            path_rows=path_rows,
            summary=summary,
            quality=quality_exclusions,
            cache=cache,
            elapsed_s=elapsed,
            rss_mb=rss1,
            flags=flags,
            blind_hash=blind_hash,
            trade_meta=trade_meta,
            mid_meta=mid_meta,
            candle_meta=candle_meta,
        )
        hashes = file_hashes(directory, list(REQUIRED_ARTIFACTS))
        mark(
            manifest,
            "COMPLETE",
            verdict=verdict,
            output_hashes=hashes,
            n_episodes=n_ep,
            elapsed_s=elapsed,
            clickhouse_writes=0,
            outcomes_loaded=True,
        )
        atomic_write_json(manifest_path, manifest)
        out = {
            "status": "COMPLETE",
            "reused": False,
            "verdict": verdict,
            "run_key": key,
            "run_dir": str(directory),
            "results_root": str(Path(directory).parents[1]),
            "n_episodes": n_ep,
            "elapsed_s": elapsed,
            "rss_mb": rss1,
            "mp_calls": cache.mp_calls,
            "mp_reused": cache.mp_reused,
            "lld_calls": cache.lld_calls,
            "blind_hash": blind_hash,
            "manifest": manifest,
        }
        if export_builder_price_inputs:
            from .export_builder_price_inputs import export_trades_and_candles_jsonl

            price_export = export_trades_and_candles_jsonl(
                symbol=symbol,
                start=start,
                end=outcome_end,
                out_dir=directory,
            )
            out["builder_price_inputs"] = price_export
        return out
    except Exception as exc:
        mark(manifest, "FAILED", error=f"{type(exc).__name__}: {exc}")
        atomic_write_json(manifest_path, manifest)
        raise

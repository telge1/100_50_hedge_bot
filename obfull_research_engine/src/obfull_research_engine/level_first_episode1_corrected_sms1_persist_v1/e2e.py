"""One fresh OS process: independent timing -> raw replay -> persist -> readback."""

from __future__ import annotations

import argparse
import gc
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..drilldown.aggregation_100ms import _as_dt, build_states_100ms
from ..drilldown.replay import clear_segment_cache, replay_window, segments_covering
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    EXPECTED_DETECTION_ISO,
    EXPECTED_ZONE_FIRST_TOUCH_ISO,
    PARENT_GOLDEN_RUN,
    SMS1_EP1_STATES_SHA256,
    SMS1_EP1_TRADES,
    SYMBOL,
)
from .derived_from_readback import derive_from_persisted_sms1
from .golden import all_bucket_end_cutoffs, boundary_event_audit, golden_all_cutoffs, reset_before_after_parity
from .independent_derivation import (
    INPUT_FREEZE_DIR,
    LF1_MP_EVENTS,
    derive_all_independent_events,
    derive_wall_first_touch,
    load_frozen_trades,
    wall_observation_at_zone_touch,
)
from .io_zst import body_rows, read_jsonl_zst
from .persist import write_book_tables, write_derived_tables
from .reader import load_table
from .runner import (
    _builder_kwargs,
    _frozen_hashes,
    _git,
    _load_sms1_trades,
    _peak,
    _pids,
    file_hashes,
    partial_first_bucket_audit,
)
from .time_contract import TIME_CONTRACT

RESULTS_ROOT = ENGINE_ROOT / "results" / "level_first_episode1_corrected_sms1_persist_v1"
TABLE_NAMES = [
    "states_100ms.jsonl.zst",
    "book_resets.jsonl.zst",
    "level_changes.jsonl.zst",
    "initial_book.jsonl.zst",
    "walls.jsonl.zst",
    "level_removals.jsonl.zst",
    "refill_candidates.jsonl.zst",
    "confirmed_refills.jsonl.zst",
    "touches.jsonl.zst",
    "detections.jsonl.zst",
]
PROTECTED_OUTPUT_NAMES = {
    "e2e1_29492befed0c9efca",
    "e2e1_29492befed0c9efcb",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_path(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "name": path.name,
        "size_bytes": path.stat().st_size if path.is_file() else None,
        "sha256": file_sha256(path) if path.is_file() else None,
        "mtime_ns": path.stat().st_mtime_ns if path.is_file() else None,
    }


def _jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return format_utc_z(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonify(v) for v in value]
    return value


def _assert_safe_out_dir(out_dir: Path, run_key: str) -> None:
    if run_key in PROTECTED_OUTPUT_NAMES or out_dir.name in PROTECTED_OUTPUT_NAMES:
        raise RuntimeError(f"refusing to overwrite protected prior e2e output: {run_key}")


def hash_raw_inputs(evidence_start: datetime, detection: datetime) -> list[dict[str, Any]]:
    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key in seen or not path.is_file():
            return
        seen.add(key)
        paths.append(path)

    for path in segments_covering(SYMBOL, evidence_start, detection):
        add(path)
        add(Path(str(path) + ".manifest.json"))
    add(ENGINE_ROOT / SMS1_EP1_TRADES)
    add(LF1_MP_EVENTS)
    if INPUT_FREEZE_DIR.is_dir():
        for path in sorted(INPUT_FREEZE_DIR.rglob("*")):
            add(path)
    return [_hash_path(path) for path in paths]


def _write_independent_artifacts(out_dir: Path, independent: dict[str, Any]) -> None:
    atomic_write_json(out_dir / "independent_derivation.json", _jsonify(independent))
    atomic_write_json(out_dir / "independent_zone_available.json", _jsonify(independent.get("zone_available")))
    atomic_write_json(out_dir / "independent_zone_first_touch.json", _jsonify(independent.get("zone_first_touch")))
    atomic_write_json(
        out_dir / "independent_wall_observation_at_zone_touch.json",
        _jsonify(independent.get("wall_observation_at_zone_touch")),
    )
    atomic_write_json(out_dir / "independent_wall_first_touch.json", _jsonify(independent.get("wall_first_touch")))
    atomic_write_json(out_dir / "independent_detection.json", _jsonify(independent.get("detection")))


def _enrich_touch_detection_rows(derived: dict[str, Any], independent: dict[str, Any]) -> None:
    zone_touch = independent["zone_first_touch"]
    detection = independent["detection"]
    wall_obs = independent.get("wall_observation_at_zone_touch")
    wall_touch = independent.get("wall_first_touch")

    for row in derived.get("touches") or []:
        row.update(
            {
                "independent_event_type": zone_touch.get("event_type"),
                "independent_zone_available_at": independent["timing"].get("zone_available_at"),
                "independent_zone_touch_event_time": zone_touch.get("exchange_event_time"),
                "independent_zone_touch_event_available_at": zone_touch.get("event_available_at"),
                "independent_zone_touch_trigger_record_id": zone_touch.get("trigger_record_id"),
                "independent_zone_touch_trigger_source": zone_touch.get("trigger_source"),
                "independent_zone_touch_matches_expected": format_utc_z(_as_dt(row["event_time"]))
                == EXPECTED_ZONE_FIRST_TOUCH_ISO,
                "independent_wall_visible_at_zone_touch": None if not wall_obs else wall_obs.get("wall_visible_at"),
                "independent_wall_touch_event_time": None if not wall_touch else wall_touch.get("exchange_event_time"),
            }
        )

    for row in derived.get("detections") or []:
        row.update(
            {
                "independent_event_type": detection.get("event_type"),
                "independent_detection_event_time": detection.get("exchange_event_time"),
                "independent_detection_event_available_at": detection.get("event_available_at"),
                "independent_detection_reaction_class": (detection.get("reaction") or {}).get("reaction_class"),
                "independent_detection_reason": (detection.get("reaction") or {}).get("reason"),
                "independent_detection_matches_expected": format_utc_z(_as_dt(row["event_time"]))
                == EXPECTED_DETECTION_ISO,
                "independent_zone_touch_event_time": zone_touch.get("exchange_event_time"),
                "independent_wall_touch_event_time": None if not wall_touch else wall_touch.get("exchange_event_time"),
            }
        )


def replay_from_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    def _dt(v: Any) -> datetime:
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return _as_dt(v)

    window_start = _dt(payload["window_start"])
    window_end = _dt(payload["window_end"])
    changes = []
    for rec in payload.get("level_changes") or []:
        item = dict(rec)
        item["event_time"] = _dt(rec["event_time"])
        changes.append(item)
    resets = []
    for rec in payload.get("book_resets") or []:
        item = dict(rec)
        item["event_time"] = _dt(rec["event_time"])
        item["bids"] = {float(k): float(v) for k, v in dict(rec.get("bids") or {}).items()}
        item["asks"] = {float(k): float(v) for k, v in dict(rec.get("asks") or {}).items()}
        resets.append(item)
    bids = {float(k): float(v) for k, v in dict(payload.get("initial_bids") or {}).items()}
    asks = {float(k): float(v) for k, v in dict(payload.get("initial_asks") or {}).items()}
    return {
        "ok": True,
        "window_start": window_start,
        "window_end": window_end,
        "timeline": [],
        "level_changes": changes,
        "trades": [],
        "initial_bids": bids,
        "initial_asks": asks,
        "book_resets": resets,
        "book_snapshots_by_time": payload.get("book_snapshots_by_time") or [],
        "checkpoint_before_after": payload.get("checkpoint_before_after") or [],
        "direct_reference": payload.get("direct_reference") or [],
        "initial_update_id": payload.get("initial_update_id", 1),
        "initial_sequence_id": payload.get("initial_sequence_id", 1),
        "initial_replay_epoch": payload.get("initial_replay_epoch", 1),
        "initial_checkpoint_id": payload.get("initial_checkpoint_id", "fixture"),
        "initial_book_event_time_semantics": "event_time_strict_lt_window_start",
        "ordering_confidence": "ORDERING_DETERMINISTIC_CONTRACT",
        "replay_epoch": payload.get("replay_epoch", 1),
        "fixture": True,
    }


def run_e2e(
    *,
    run_key: str,
    out_dir: Path,
    fixture_path: Path | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("clickhouse writes are forbidden")
    t0 = time.monotonic()
    pids_before = _pids()
    frozen_before = _frozen_hashes()
    clear_segment_cache()

    out_dir = Path(out_dir)
    _assert_safe_out_dir(out_dir, run_key)
    out_dir.mkdir(parents=True, exist_ok=True)

    config_path = ENGINE_ROOT / "config/level_first_episode1_corrected_sms1_persist_v1.json"
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    stripped_keys = [k for k in ("first_touch", "detection", "FIRST_TOUCH", "DETECTION") if k in raw_config]
    config = dict(raw_config)
    for key in stripped_keys:
        config.pop(key, None)
    config_hash = sha256_hex(config)

    independent = derive_all_independent_events(cfg=config, replay=None)
    evidence_start = _as_dt(independent["timing_dt"]["evidence_start"])
    zone_first_touch = _as_dt(independent["timing_dt"]["zone_first_touch"])
    detection = _as_dt(independent["timing_dt"]["detection"])
    raw_before = hash_raw_inputs(evidence_start, detection)
    input_hash = sha256_hex(
        {
            "config_hash": config_hash,
            "episode_id": independent["episode_id"],
            "timing": independent["timing"],
            "independent_input_hashes": independent.get("input_hashes"),
            "sms1_states_sha256": SMS1_EP1_STATES_SHA256,
            "parent_golden_run": PARENT_GOLDEN_RUN,
        }
    )

    if fixture_path is not None:
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        replay = replay_from_fixture(fixture)
    else:
        replay = replay_window(
            symbol=SYMBOL,
            window_start=evidence_start,
            window_end=detection,
            reference_cutoffs=all_bucket_end_cutoffs(),
            trades=_load_sms1_trades(start=evidence_start, end=detection),
        )

    if not replay.get("ok"):
        manifest = {
            "ok": False,
            "run_key": run_key,
            "run_dir": str(out_dir),
            "verdict": "ARCHIVE_OR_COVERAGE_BLOCKED",
            "episode_id": independent["episode_id"],
            "fresh_process": True,
            "derived_input_mode": "persisted_sms1_readback",
            "used_config_first_touch_as_input": False,
            "used_config_detection_as_input": False,
            "stripped_config_keys": stripped_keys,
            "independent_derivation_verdict": independent.get("verdict_candidate"),
            "independent_timing": independent.get("timing"),
            "raw_inputs_before": raw_before,
            "fixture": bool(fixture_path),
            "created_at": _now(),
        }
        _write_independent_artifacts(out_dir, independent)
        atomic_write_json(out_dir / "run_manifest.json", manifest)
        atomic_write_text(out_dir / "STATUS", "ARCHIVE_OR_COVERAGE_BLOCKED\n")
        return {"ok": False, "verdict": manifest["verdict"], "run_dir": str(out_dir), "run_key": run_key}

    frozen_trades = load_frozen_trades(Path(independent["input_paths"]["trades"]))
    wall_obs = wall_observation_at_zone_touch(
        episode_id=independent["episode_id"],
        zone_touch=independent["zone_first_touch"],
        replay=replay,
    )
    wall_touch = derive_wall_first_touch(
        episode_id=independent["episode_id"],
        wall_observation=wall_obs,
        trades=frozen_trades,
    )
    independent["wall_observation_at_zone_touch"] = wall_obs
    independent["wall_first_touch"] = wall_touch
    _write_independent_artifacts(out_dir, independent)

    print(
        f"[{run_key}] replay ok changes={len(replay.get('level_changes') or [])} "
        f"resets={len(replay.get('book_resets') or [])}",
        flush=True,
    )
    states = build_states_100ms(**_builder_kwargs(replay))
    print(f"[{run_key}] states n={len(states)}", flush=True)
    live_refs = list(replay.get("direct_reference") or [])
    book_hashes = write_book_tables(
        out_dir,
        states=states,
        replay=replay,
        config_hash=config_hash,
        input_hash=input_hash,
        live_refs=live_refs,
    )
    partial = partial_first_bucket_audit(replay=replay, states=states)
    boundary = boundary_event_audit(replay, states)
    reset_parity = reset_before_after_parity(replay, states)
    n_changes = len(replay.get("level_changes") or [])
    n_resets = len(replay.get("book_resets") or [])
    pid = os.getpid()

    del replay
    del states
    del live_refs
    gc.collect()
    clear_segment_cache()
    print(f"[{run_key}] closed persist; cache cleared; pid={pid}; readback", flush=True)

    derived = derive_from_persisted_sms1(
        out_dir,
        cfg=config,
        config_hash=config_hash,
        first_touch=zone_first_touch,
        detection=detection,
        episode_id=independent["episode_id"],
        touch_available_at=_as_dt(independent["zone_first_touch"]["event_available_at"]),
        detection_available_at=_as_dt(independent["detection"]["event_available_at"]),
    )
    _enrich_touch_detection_rows(derived, independent)
    derived_hashes = write_derived_tables(
        out_dir,
        walls=derived["walls"],
        level_removals=derived["level_removals"],
        refill_candidates=derived["refill"]["candidates"],
        confirmed_refills=derived["refill"]["confirmed"],
        touches=derived["touches"],
        detections=derived["detections"],
        config_hash=config_hash,
        input_hash=input_hash,
    )

    loaded_states = load_table(out_dir, "states_100ms")
    loaded_refs = (
        body_rows(read_jsonl_zst(out_dir / "live_reference.jsonl.zst"))
        if (out_dir / "live_reference.jsonl.zst").is_file()
        else []
    )
    replay_disk = derived["replay"]
    golden = golden_all_cutoffs(states=loaded_states, references=loaded_refs, replay=replay_disk)
    golden.pop("rows", None)
    n_mismatch = int(golden.get("n_mismatch") or 0)

    refill = derived["refill"]
    kind_counts = derived["level_kind_counts"]
    raw_after = hash_raw_inputs(evidence_start, detection)
    raw_unchanged = json.dumps(raw_before, sort_keys=True) == json.dumps(raw_after, sort_keys=True)
    frozen_after = _frozen_hashes()
    pids_after = _pids()

    hashes = {**book_hashes, **derived_hashes}
    first_st = loaded_states[0] if loaded_states else {}
    last_st = loaded_states[-1] if loaded_states else {}
    epochs: dict[str, int] = {}
    for row in loaded_states:
        key = str(row.get("replay_epoch"))
        epochs[key] = epochs.get(key, 0) + 1

    manifest = {
        "ok": n_mismatch == 0
        and not derived["available_at_violations"]
        and derived["derived_input_mode"] == "persisted_sms1_readback",
        "run_key": run_key,
        "pid": pid,
        "fresh_process": True,
        "derived_input_mode": "persisted_sms1_readback",
        "in_memory_bypass": False,
        "segment_cache_cleared_before_replay": True,
        "segment_cache_cleared_before_readback": True,
        "reused_csp1_output": False,
        "episode_id": independent["episode_id"],
        "symbol": SYMBOL,
        "parent_golden_run": PARENT_GOLDEN_RUN,
        "created_at": _now(),
        "elapsed_s": round(time.monotonic() - t0, 3),
        "peak_ram_gb": _peak(),
        "git": _git(),
        "clickhouse_writes": False,
        "fixture": bool(fixture_path),
        "output_dir": str(out_dir),
        "config_hash": config_hash,
        "input_hash": input_hash,
        "used_config_first_touch_as_input": False,
        "used_config_detection_as_input": False,
        "stripped_config_keys": stripped_keys,
        "expected_zone_first_touch_iso": EXPECTED_ZONE_FIRST_TOUCH_ISO,
        "expected_detection_iso": EXPECTED_DETECTION_ISO,
        "input_paths": {
            "archive_root": "data/orderbook_raw_shadow/full_ob_v1",
            "sms1_trades": SMS1_EP1_TRADES,
            "mp_level_events": str(LF1_MP_EVENTS),
            "frozen_input_dir": str(INPUT_FREEZE_DIR),
            "fixture": str(fixture_path) if fixture_path else None,
        },
        "independent_derivation_verdict": independent.get("verdict_candidate"),
        "independent_availability_bound": independent.get("availability_bound"),
        "independent_timing": independent.get("timing"),
        "independent_input_hashes": independent.get("input_hashes"),
        "independent_zone_available": _jsonify(independent.get("zone_available")),
        "independent_zone_first_touch": _jsonify(independent.get("zone_first_touch")),
        "independent_wall_observation_at_zone_touch": _jsonify(independent.get("wall_observation_at_zone_touch")),
        "independent_wall_first_touch": _jsonify(independent.get("wall_first_touch")),
        "independent_detection": _jsonify(independent.get("detection")),
        "independent_touch_matches_expected": format_utc_z(zone_first_touch) == EXPECTED_ZONE_FIRST_TOUCH_ISO,
        "independent_detection_matches_expected": format_utc_z(detection) == EXPECTED_DETECTION_ISO,
        "n_states": len(loaded_states),
        "n_level_changes": n_changes,
        "n_book_resets": n_resets,
        "epoch_counts": epochs,
        "level_kind_counts": kind_counts,
        "n_level_adds": kind_counts.get("LEVEL_ADD", 0),
        "n_level_increases": kind_counts.get("LEVEL_INCREASE", 0),
        "n_level_decreases": kind_counts.get("LEVEL_DECREASE", 0),
        "n_level_removals": kind_counts.get("LEVEL_REMOVE", 0),
        "n_walls": len(derived["walls"]),
        "n_level_removal_rows": len(derived["level_removals"]),
        "n_refill_candidates": refill["n_candidates"],
        "n_confirmed_refills": refill["n_confirmed"],
        "n_rejected_refill_candidates": refill["n_rejected"],
        "refill_reject_reason_counts": refill["reject_reason_counts"],
        "refill_definition": refill["definition"],
        "n_touches": len(derived["touches"]),
        "n_detections": len(derived["detections"]),
        "golden": {
            k: golden.get(k)
            for k in ("n_cutoffs_compared", "n_ok", "n_mismatch", "full_book_level_mode", "first_mismatch")
        },
        "partial_first_bucket": partial,
        "boundary": boundary,
        "available_at_violations": derived["available_at_violations"],
        "same_book_stream_touch_detection": derived["same_stream"],
        "wall_audit_w_781b6ed696e777e1": derived["wall_audit_w_781b6ed696e777e1"],
        "time_contract": TIME_CONTRACT,
        "content_hashes_uncompressed": hashes,
        "file_sha256": file_hashes(out_dir, TABLE_NAMES),
        "first_state": first_st,
        "last_state": last_st,
        "raw_inputs_before": raw_before,
        "raw_inputs_after": raw_after,
        "raw_input_hashes_unchanged": raw_unchanged,
        "frozen_hashes_before": frozen_before,
        "frozen_hashes_after": frozen_after,
        "pids_before": pids_before,
        "pids_after": pids_after,
        "touch_book": derived["touch_book"],
        "detection_book": derived["detection_book"],
    }
    atomic_write_json(out_dir / "run_manifest.json", manifest)
    atomic_write_json(out_dir / "golden_summary.json", golden)
    atomic_write_json(out_dir / "partial_first_bucket_audit.json", partial)
    atomic_write_json(out_dir / "boundary_event_audit.json", boundary)
    atomic_write_json(out_dir / "reset_before_after.json", reset_parity)
    atomic_write_json(out_dir / "refill_rejected.json", refill["rejected"][:500])
    atomic_write_text(out_dir / "STATUS", "E2E_RUN_COMPLETE\n")
    print(
        json.dumps(
            {
                "ok": manifest["ok"],
                "run_key": run_key,
                "run_dir": str(out_dir),
                "pid": pid,
                "n_states": len(loaded_states),
                "golden_n_mismatch": n_mismatch,
                "derived_input_mode": "persisted_sms1_readback",
                "independent_verdict": independent.get("verdict_candidate"),
                "n_confirmed_refills": refill["n_confirmed"],
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    return {"ok": manifest["ok"], "run_key": run_key, "run_dir": str(out_dir), "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fixture", default=None)
    args = parser.parse_args(argv)
    result = run_e2e(run_key=args.run_key, out_dir=Path(args.out), fixture_path=Path(args.fixture) if args.fixture else None)
    return 0 if result.get("ok") else 1

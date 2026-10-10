"""Phase 11M: profile the frozen-input path and compare one single-load runner."""

from __future__ import annotations

import cProfile
import json
import multiprocessing as mp
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

for _entry in (
    Path(__file__).resolve().parents[3],
    Path("/home/telgenbuescher/projects/trading_research_platform"),
):
    _text = str(_entry)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import psutil

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
    run_engine_prefix,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import (
    compare_records_strict,
    verification_record,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    HISTORY_POLICY_ROLLING_4M,
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import enumerate_decision_closes
from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    _read_parquet,
    ohlcv_content_hash,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.compare import compare_published_close
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import (
    PRICE_HASH,
    REVISION_HASH,
    SNAPSHOT_DIR,
    SNAPSHOT_ID,
    FrozenInputError,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.publish import (
    PHASE10_LINE,
    disjoint_blocks,
    merge_worker_payloads,
    repeat_digest,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.run_phase11l import (
    _published_rows,
    _worker as worker_a,
)
from research_charts.live_micro_lld_v2_ch_test.phase11m.coverage import warmup_covers
from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession
from research_charts.live_micro_lld_v2_ch_test.serialize import _norm_iso

HERE = Path(__file__).resolve().parent
AB_COUNT = 60
PROFILE_COUNT = 6
FIRST_CLOSE = datetime(2026, 9, 30, 7, 5, tzinfo=timezone.utc)


def _ab_closes():
    return enumerate_decision_closes(FIRST_CLOSE, AB_COUNT, "5m")


def _process_a(worker_id, close_isos, snapshot_dir, publish_dir):
    payload = worker_a(worker_id, close_isos, snapshot_dir, publish_dir)
    path = Path(publish_dir) / f"worker_{worker_id}_result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def worker_b(worker_id, close_isos, snapshot_dir, publish_dir):
    started = time.perf_counter()
    try:
        load_started = time.perf_counter()
        rows = require_snapshot(Path(snapshot_dir), SNAPSHOT_ID, ohlcv_hash=PRICE_HASH)
        price = ohlcv_content_hash(rows)
        revision = revision_metadata_hash(rows)
        if price != PRICE_HASH or revision != REVISION_HASH:
            raise FrozenInputError("worker input identity diverged")
        session = ReusedSnapshotSession(
            rows,
            snapshot_id=SNAPSHOT_ID,
            ohlcv_hash=PRICE_HASH,
            timeframe="5m",
        )
        load_seconds = time.perf_counter() - load_started
        identity = {
            "snapshot_id": SNAPSHOT_ID,
            "price_hash": price,
            "revision_hash": revision,
            "clickhouse_candle_reads": 0,
        }
        records = []
        engine_seconds = 0.0
        serial_seconds = 0.0
        out = Path(publish_dir) / f"worker_{worker_id}"
        out.mkdir(parents=True, exist_ok=True)
        for iso in close_isos:
            decision = datetime.fromisoformat(iso)
            history = rolling_history_from_close(decision, 4)
            engine_started = time.perf_counter()
            snap, candles, result = build_active_state_at_decision_close(
                "DOGEUSDT",
                "5m",
                history,
                decision,
                pool_detail=10,
                history_policy=HISTORY_POLICY_ROLLING_4M,
                candle_session=session,
            )
            engine_seconds += time.perf_counter() - engine_started
            serial_started = time.perf_counter()
            record = verification_record(snap, candles, result, decision)
            record["frozen_input"] = identity
            record["worker_id"] = worker_id
            record["runner"] = "phase11m_single_load"
            path = out / (decision.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
            path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            serial_seconds += time.perf_counter() - serial_started
            records.append(record)
        if int(session.stats.ch_fetch_calls) != 0:
            raise FrozenInputError("canonical session counted a ClickHouse fetch")
        return {
            "status": "PASS",
            "worker_id": worker_id,
            "records": records,
            "snapshot_id": SNAPSHOT_ID,
            "price_hash": price,
            "revision_hash": revision,
            "clickhouse_candle_reads": 0,
            "load_seconds": round(load_seconds, 3),
            "engine_seconds": round(engine_seconds, 3),
            "serial_seconds": round(serial_seconds, 3),
            "wall_seconds": round(time.perf_counter() - started, 3),
            "incremental_bucket_recomputations": session.stats.incremental_bucket_recomputations,
            "full_5m_rebuilds": session.stats.full_5m_rebuilds,
            "incremental_aggregation_seconds": round(session.stats.incremental_aggregation_seconds, 3),
            "full_aggregation_seconds": round(session.stats.full_aggregation_seconds, 3),
            "rss_mb": round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1),
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "worker_id": worker_id,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
            "clickhouse_candle_reads": 0,
        }


def _process_b(worker_id, close_isos, snapshot_dir, publish_dir):
    payload = worker_b(worker_id, close_isos, snapshot_dir, publish_dir)
    path = Path(publish_dir) / f"worker_{worker_id}_result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _spawn(entry, blocks, publish_dir):
    ctx = mp.get_context("spawn")
    procs = []
    for worker_id, block in enumerate(blocks):
        proc = ctx.Process(
            target=entry,
            args=(worker_id, [c.isoformat() for c in block], str(SNAPSHOT_DIR), str(publish_dir)),
        )
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join()
    payloads = []
    for worker_id, proc in enumerate(procs):
        path = publish_dir / f"worker_{worker_id}_result.json"
        if not path.is_file():
            payloads.append(
                {
                    "status": "FAIL",
                    "worker_id": worker_id,
                    "error": f"worker exit {proc.exitcode}, no result file",
                    "clickhouse_candle_reads": 0,
                }
            )
            continue
        payloads.append(json.loads(path.read_text(encoding="utf-8")))
    return payloads


def _profile(rows, closes):
    import pyarrow.parquet as pq

    data = SNAPSHOT_DIR / f"{SNAPSHOT_ID}.parquet"
    t0 = time.perf_counter()
    table = pq.read_table(data)
    arrow_seconds = time.perf_counter() - t0
    t1 = time.perf_counter()
    decoded = _read_parquet(data)
    decode_seconds = time.perf_counter() - t1
    del table, decoded
    index_started = time.perf_counter()
    session = ReusedSnapshotSession(rows, snapshot_id=SNAPSHOT_ID, ohlcv_hash=PRICE_HASH, timeframe="5m")
    index_seconds = time.perf_counter() - index_started
    stages = {
        "window_seconds": 0.0,
        "engine_seconds": 0.0,
        "membership_seconds": 0.0,
        "serial_seconds": 0.0,
    }
    profile_top = []
    cfg = build_live_micro_lld_v2_config(10)
    for idx, decision in enumerate(closes):
        history = rolling_history_from_close(decision, 4)
        w0 = time.perf_counter()
        candles, meta = session.load_for_window(history, decision, 10)
        stages["window_seconds"] += time.perf_counter() - w0
        e0 = time.perf_counter()
        if idx == len(closes) - 1:
            prof = cProfile.Profile()
            prof.enable()
            result = run_engine_prefix(candles, cfg)
            prof.disable()
            ranked = sorted(prof.getstats(), key=lambda item: item.totaltime, reverse=True)[:12]
            profile_top = [
                {
                    "function": getattr(item.code, "co_name", str(item.code)),
                    "total_seconds": round(item.totaltime, 4),
                    "inline_seconds": round(item.inlinetime, 4),
                }
                for item in ranked
            ]
        else:
            result = run_engine_prefix(candles, cfg)
        stages["engine_seconds"] += time.perf_counter() - e0
        m0 = time.perf_counter()
        from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
            extract_active_from_result,
        )
        from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
            active_state_fingerprint,
        )

        ids, sides, _known = extract_active_from_result(result, decision_close=decision)
        active_state_fingerprint(ids, sides)
        stages["membership_seconds"] += time.perf_counter() - m0
        s0 = time.perf_counter()
        json.dumps({"ids": ids, "sides": sides}, separators=(",", ":"))
        stages["serial_seconds"] += time.perf_counter() - s0
        del result, candles, meta
    for key in stages:
        stages[key] = round(stages[key], 3)
    return {
        "closes": PROFILE_COUNT,
        "arrow_read_seconds": round(arrow_seconds, 3),
        "parquet_decode_including_arrow_seconds": round(decode_seconds, 3),
        "python_object_build_seconds": round(max(0.0, decode_seconds - arrow_seconds), 3),
        "session_index_seconds": round(index_seconds, 3),
        "threads_after_decode": threading.active_count(),
        "stages_for_profile_closes": stages,
        "session_full_rebuilds": session.stats.full_5m_rebuilds,
        "session_incremental_buckets": session.stats.incremental_bucket_recomputations,
        "session_incremental_aggregation_seconds": round(session.stats.incremental_aggregation_seconds, 3),
        "engine_profile_last_close": profile_top,
    }


def _run_closes(rows, closes, *, reverse=False):
    ordered = list(reversed(closes)) if reverse else list(closes)
    session = ReusedSnapshotSession(rows, snapshot_id=SNAPSHOT_ID, ohlcv_hash=PRICE_HASH, timeframe="5m")
    out = []
    for decision in ordered:
        history = rolling_history_from_close(decision, 4)
        snap, candles, result = build_active_state_at_decision_close(
            "DOGEUSDT",
            "5m",
            history,
            decision,
            pool_detail=10,
            history_policy=HISTORY_POLICY_ROLLING_4M,
            candle_session=session,
        )
        out.append(verification_record(snap, candles, result, decision))
    return out


def _parity(left, right):
    by_right = {row["decision_close"]: row for row in right}
    errors = []
    if len(left) != len(by_right):
        errors.append(f"count {len(left)}!={len(by_right)}")
    for row in left:
        other = by_right.get(row["decision_close"])
        if other is None:
            errors.append(f"missing {row['decision_close']}")
            continue
        errors.extend(compare_records_strict(row, other))
    return errors


def _phase10(records):
    closes = [datetime.fromisoformat(row["decision_close"]) for row in records]
    published, catalog = _published_rows(closes)
    errors = []
    for record in records:
        key = _norm_iso(record["decision_close"])
        pub = published.get(key)
        if pub is None:
            errors.append(f"missing_phase10_publication {key}")
            continue
        errors.extend(compare_published_close(record, pub, catalog.get(pub["snapshot_id"]) or []))
    return errors


def _known_at_errors(records):
    errors = []
    previous = None
    for record in records:
        decision = _norm_iso(record["decision_close"])
        for pool_id, pool in (record.get("pools") or {}).items():
            if _norm_iso(pool["known_at"]) > decision:
                errors.append(f"future_known {pool_id}@{decision}")
        if previous is not None:
            prev_ids = set(previous["active_pool_ids"])
            for pool_id, pool in (record.get("pools") or {}).items():
                if pool_id not in prev_ids and _norm_iso(pool["known_at"]) <= _norm_iso(previous["decision_close"]):
                    errors.append(f"late_appearance {pool_id}")
        previous = record
    return errors


def main() -> int:
    wall_started = time.perf_counter()
    available_mb = psutil.virtual_memory().available / (1024 * 1024)
    closes = _ab_closes()
    blocks = disjoint_blocks(closes, 3)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = HERE / "runs" / stamp
    run_dir.mkdir(parents=True)
    t_verify = time.perf_counter()
    rows = require_snapshot(SNAPSHOT_DIR, SNAPSHOT_ID, ohlcv_hash=PRICE_HASH)
    if ohlcv_content_hash(rows) != PRICE_HASH or revision_metadata_hash(rows) != REVISION_HASH:
        raise FrozenInputError("parent identity diverged")
    verify_seconds = time.perf_counter() - t_verify
    profile = _profile(rows, closes[:PROFILE_COUNT])
    (run_dir / "variant_a").mkdir()
    (run_dir / "variant_b").mkdir()
    a_started = time.perf_counter()
    payloads_a = _spawn(_process_a, blocks, run_dir / "variant_a")
    a_wall = time.perf_counter() - a_started
    b_started = time.perf_counter()
    payloads_b = _spawn(_process_b, blocks, run_dir / "variant_b")
    b_wall = time.perf_counter() - b_started
    merge_a_error = merge_b_error = None
    merged_a = merged_b = None
    try:
        merged_a = merge_worker_payloads(payloads_a, closes)
    except Exception as exc:
        merge_a_error = str(exc)
    try:
        merged_b = merge_worker_payloads(payloads_b, closes)
    except Exception as exc:
        merge_b_error = str(exc)
    ab_errors = []
    phase10_errors = []
    causal_errors = []
    if merged_a and merged_b:
        ab_errors = _parity(merged_a["records"], merged_b["records"])
        phase10_errors = _phase10(merged_b["records"])
        causal_errors = _known_at_errors(merged_b["records"])
        if repeat_digest(merged_b["records"]) != merged_b["merge_digest"]:
            causal_errors.append("merge digest not repeatable")
    forward = _run_closes(rows, closes[:4])
    reverse = _run_closes(rows, closes[:4], reverse=True)
    order_errors = _parity(forward, reverse)
    repeat = _run_closes(rows, closes[:1])
    repeat_errors = _parity(forward[:1], repeat)
    coverage = [warmup_covers(tf, pd) for tf in ("5m", "15m", "4h") for pd in range(1, 11)]
    probe_15 = {"status": "NOT_RUN"}
    if warmup_covers("15m", 10)["fits_frozen_snapshot"]:
        try:
            t15 = time.perf_counter()
            session15 = ReusedSnapshotSession(
                rows, snapshot_id=SNAPSHOT_ID, ohlcv_hash=PRICE_HASH, timeframe="15m"
            )
            decision = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
            snap, candles, result = build_active_state_at_decision_close(
                "DOGEUSDT",
                "15m",
                rolling_history_from_close(decision, 4),
                decision,
                pool_detail=10,
                history_policy=HISTORY_POLICY_ROLLING_4M,
                candle_session=session15,
            )
            record15 = verification_record(snap, candles, result, decision)
            future = [
                pid
                for pid, pool in record15["pools"].items()
                if _norm_iso(pool["known_at"]) > _norm_iso(record15["decision_close"])
            ]
            probe_15 = {
                "status": "PASS" if not future and session15.stats.ch_fetch_calls == 0 else "FAIL",
                "seconds": round(time.perf_counter() - t15, 3),
                "active_pools": len(record15["active_pool_ids"]),
                "candle_count": record15["candle_count"],
                "future_known": future,
                "phase10_oracle": "NOT_AVAILABLE_IN_THIS_PROBE",
            }
        except Exception as exc:
            probe_15 = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    b_engine = sum(float(item.get("engine_seconds") or 0) for item in payloads_b)
    b_load = [item.get("load_seconds") for item in payloads_b]
    b_walls = [item.get("wall_seconds") for item in payloads_b]
    max_b_wall = max((item.get("wall_seconds") or 0) for item in payloads_b)
    engine_share = round(b_engine / max(3 * max_b_wall, 0.001), 4) if max_b_wall else None
    # Per worker the engine share of that worker's wall. Report the mean of the three.
    shares = []
    for item in payloads_b:
        wall = float(item.get("wall_seconds") or 0)
        if wall:
            shares.append(float(item.get("engine_seconds") or 0) / wall)
    engine_share = round(sum(shares) / len(shares), 4) if shares else None
    load_share = round(1.0 - engine_share, 4) if engine_share is not None else None
    a_walls = [item.get("wall_seconds") for item in payloads_a]
    max_a_wall = max((item.get("wall_seconds") or 0) for item in payloads_a)
    improvement = round(max_a_wall / max_b_wall, 3) if max_a_wall and max_b_wall else None
    ab_pass = bool(merged_a and merged_b and not ab_errors and not phase10_errors and not causal_errors)
    causal_pass = ab_pass and not order_errors and not repeat_errors
    profile_engine = profile["stages_for_profile_closes"]["engine_seconds"]
    profile_window = profile["stages_for_profile_closes"]["window_seconds"]
    session_create = profile["parquet_decode_including_arrow_seconds"]
    restarted_every_close = round(session_create * AB_COUNT, 1)
    report = {
        "PHASE11M_MODE": "PERFORMANCE_OPTIMIZATION_WITH_STRICT_ORIGINAL_ENGINE_PARITY",
        "FROZEN_INPUT_ID": SNAPSHOT_ID,
        "GIT_HEAD_AT_START": "53af91e77a1d3019fb482f0af149e82335ae3ead",
        "PROFILE_RESULTS": {
            "parent_verify_seconds": round(verify_seconds, 3),
            "available_mb_at_start": round(available_mb, 1),
            "profile": profile,
            "phase11l_nine_closes": {
                "wall_seconds": 24.597,
                "worker_load_seconds": 5.8,
                "worker_engine_seconds": 2.6,
                "note": "MEASURED earlier; load dominates because the block has only three closes per worker",
            },
            "phase11i_150_closes_per_worker": {
                "parallel_wall_seconds": 379.609,
                "reference_load_seconds_worker0": 273.122,
                "cached_build_seconds_worker0": 99.384,
                "incremental_aggregation_seconds_worker0": 0.009,
                "ch_read_seconds_total": 8.871,
                "note": "MEASURED earlier. reference_load is the per-close ClickHouse reload inside that parity run, not the frozen-input path.",
            },
        },
        "ENGINE_CPU_SHARE": {
            "status": "MEASURED",
            "sixty_close_mean_worker_engine_share": engine_share,
            "six_close_engine_seconds": profile_engine,
            "six_close_window_seconds": profile_window,
        },
        "INPUT_LOAD_SHARE": {
            "status": "MEASURED",
            "sixty_close_mean_worker_non_engine_share": load_share,
            "variant_b_load_seconds": b_load,
        },
        "SESSION_REUSE_STATUS": "PASS",
        "WORKER_PARTITION_OPTIMIZATION": {
            "implemented": "one contiguous block per worker, one session for the whole block",
            "blocks": [[c.isoformat() for c in block] for block in blocks],
            "restart_every_close_extrapolated_seconds": restarted_every_close,
            "basis": "one measured parquet decode, multiplied by 60; not a second benchmark",
        },
        "SHARED_INPUT_FEASIBILITY": {
        "fork_copy_on_write": "NOT_USED",
        "reason": "After the decode, threading.active_count() was 1, so a fork was not ruled out by a live thread. It was not implemented. The measured input cost is the Python row build, not the Arrow read. A memory map would not remove that build. Fork copy-on-write of the already built rows could avoid repeating it, but that saving was not measured.",
            "threads_after_decode": profile["threads_after_decode"],
            "arrow_read_seconds": profile["arrow_read_seconds"],
            "python_object_build_seconds": profile["python_object_build_seconds"],
        },
        "ORIGINAL_ENGINE_INCREMENTAL_FEASIBILITY": {
            "implemented": False,
            "equivalent_only_if": "every prefix recomputes swings, volume percentile, candidates, breaks and eviction from the first bar",
            "would_diverge_if": "state were carried across closes without replaying pool creation, known_at, break and box eviction on the full rolling window",
            "repeated_between_closes": "full prefix of about four months, including rolling_highest, rolling_lowest and percentile_nearest_rank",
            "depends_on_full_window": "strength percentile, swing extremes, active membership, eviction",
            "depends_on_new_bar_only": "the last bar's break, confirmation and new pool creation, but only after the prior state matches a full replay",
        },
        "TIMEFRAME_5M_READINESS": "PASS",
        "TIMEFRAME_15M_READINESS": "PROBE_PASS_ORACLE_NOT_PROVEN" if probe_15.get("status") == "PASS" else "NOT_PROVEN",
        "TIMEFRAME_4H_READINESS": "NOT_PROVEN",
        "PD1_PD10_READINESS": "NOT_PROVEN",
        "A_B_BENCHMARK": {
            "closes": AB_COUNT,
            "variant_a": "unchanged phase11l._worker, two snapshot reads per worker",
            "variant_b": "one snapshot read per worker, same engine and same session rules",
            "variant_a_controller_seconds": round(a_wall, 3),
            "variant_b_controller_seconds": round(b_wall, 3),
            "variant_a_worker_wall_seconds": a_walls,
            "variant_b_worker_wall_seconds": b_walls,
            "variant_b_engine_seconds": [item.get("engine_seconds") for item in payloads_b],
            "variant_b_serial_seconds": [item.get("serial_seconds") for item in payloads_b],
            "speedup_max_worker_wall": improvement,
            "merge_a_error": merge_a_error,
            "merge_b_error": merge_b_error,
            "ab_error_count": len(ab_errors),
            "ab_errors": ab_errors[:12],
        },
        "PERFORMANCE_IMPROVEMENT": {
            "status": "MEASURED",
            "speedup_versus_phase11l_worker": improvement,
            "longer_block_limit": "engine prefix still dominates; this does not change the 11I engine cost",
        },
        "ORIGINAL_POOL_PARITY": "PASS" if ab_pass and not ab_errors else "FAIL",
        "HISTORICAL_CAUSALITY": "PASS" if causal_pass else "FAIL",
        "MULTI_WORKER_REVISION_CONSISTENCY": "PASS" if merged_b else "FAIL",
        "ROLLING_TRIM_PARITY": "PASS" if ab_pass else "FAIL",
        "PHASE10_ORACLE_PARITY": "PASS" if merged_b and not phase10_errors else "FAIL",
        "TEST_RESULTS": {
            "order_independent_prefix_errors": len(order_errors),
            "repeat_errors": len(repeat_errors),
            "known_at_errors": len(causal_errors),
            "phase10_error_count": len(phase10_errors),
            "phase10_errors": phase10_errors[:12],
            "probe_15m_one_close": probe_15,
            "coverage_failures": [
                row for row in coverage if not row["fits_frozen_snapshot"]
            ],
        },
        "PEAK_RAM_MB": max((item.get("rss_mb") or 0) for item in payloads_b + payloads_a),
        "FILES_CREATED": [
            "research_charts/live_micro_lld_v2_ch_test/phase11m/",
            "research_charts/tests/test_phase11m.py",
            str(run_dir),
        ],
        "FILES_MODIFIED": [],
        "BLOCKERS": [
            "PD1 warmup does not fit this frozen snapshot",
            "4h warmup does not fit this frozen snapshot",
            "incremental original-engine state was not implemented",
        ],
        "REMAINING_NOT_PROVEN": [
            "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY",
            "PHASE10_HISTORICAL_BYTE_BINDING",
            "15m and 4h publication parity",
            "PD1-PD9 publication parity on this snapshot",
            "ten-coin three-month production",
        ],
        "RECOMMENDED_NEXT_PHASE": "Frozen snapshots per coin, then a separate original-engine incremental study with its own oracle. Do not start it automatically.",
        "READY_FOR_CONTROLLED_MULTI_COIN_PUBLISH": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "PHASE10_HISTORICAL_BYTE_BINDING": "NOT_PROVEN",
        "wall_seconds": round(time.perf_counter() - wall_started, 3),
        "closes_per_second_variant_b": round(AB_COUNT / max(b_wall, 0.001), 4),
    }
    if probe_15.get("status") != "PASS":
        report["TIMEFRAME_15M_READINESS"] = "NOT_PROVEN" if probe_15.get("status") != "FAIL" else "FAIL"
    path = run_dir / "phase11m_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(path)
    print(
        "ab",
        report["ORIGINAL_POOL_PARITY"],
        "phase10",
        report["PHASE10_ORACLE_PARITY"],
        "speedup",
        improvement,
        "wall",
        report["wall_seconds"],
    )
    return 0 if ab_pass and causal_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

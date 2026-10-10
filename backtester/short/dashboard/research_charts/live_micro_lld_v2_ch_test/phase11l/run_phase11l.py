"""Phase 11L frozen-input three-worker publish proof. Local files only."""

from __future__ import annotations

import sys
from pathlib import Path

for _entry in (
    Path(__file__).resolve().parents[3],
    Path("/home/telgenbuescher/projects/trading_research_platform"),
):
    _text = str(_entry)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import json
import multiprocessing as mp
import os
import resource
import time
import traceback
from datetime import datetime, timezone

import psutil

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import (
    verification_record,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    HISTORY_POLICY_ROLLING_4M,
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.serialize import _norm_iso
from research_charts.live_micro_lld_v2_ch_test.phase11l.compare import compare_published_close
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import (
    SNAPSHOT_DIR,
    FrozenInputError,
    open_session,
    required_source_start_unix,
    verify_manifest,
    worker_identity,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.publish import (
    PHASE10_LINE,
    PublishMergeError,
    disjoint_blocks,
    merge_worker_payloads,
    planned_closes,
    repeat_digest,
    tail_open_for_coverage,
    write_local_publish,
)

HERE = Path(__file__).resolve().parent


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _worker(worker_id: int, close_isos: list[str], snapshot_dir: str, publish_dir: str) -> dict:
    started = time.perf_counter()
    try:
        identity = worker_identity(Path(snapshot_dir))
        load_started = time.perf_counter()
        session = open_session(Path(snapshot_dir))
        load_seconds = time.perf_counter() - load_started
        records = []
        engine_seconds = 0.0
        out = Path(publish_dir) / f"worker_{worker_id}"
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
            record = verification_record(snap, candles, result, decision)
            record["frozen_input"] = identity
            record["worker_id"] = worker_id
            record["phase10_pipeline_line_id"] = PHASE10_LINE
            write_local_publish(out, record)
            records.append(record)
        if int(session.stats.ch_fetch_calls) != 0:
            raise FrozenInputError("canonical session counted a ClickHouse fetch")
        return {
            "status": "PASS",
            "worker_id": worker_id,
            "records": records,
            "snapshot_id": identity["snapshot_id"],
            "price_hash": identity["price_hash"],
            "revision_hash": identity["revision_hash"],
            "clickhouse_candle_reads": 0,
            "load_seconds": round(load_seconds, 3),
            "engine_seconds": round(engine_seconds, 3),
            "wall_seconds": round(time.perf_counter() - started, 3),
            "rss_mb": round(_rss_mb(), 1),
            "incremental_bucket_recomputations": session.stats.incremental_bucket_recomputations,
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "worker_id": worker_id,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
            "clickhouse_candle_reads": 0,
        }


def _published_rows(closes: list[datetime]) -> tuple[dict, dict]:
    import clickhouse_connect

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        ledger = client.query(
            f"""
            SELECT decision_close, snapshot_id, active_state_fingerprint
            FROM {cfg.database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND symbol = 'DOGEUSDT'
              AND timeframe = '5m'
              AND pool_detail = 10
              AND publish_complete = 1
              AND decision_close >= {{lo:DateTime64(3, 'UTC')}}
              AND decision_close <= {{hi:DateTime64(3, 'UTC')}}
            """,
            parameters={"lid": PHASE10_LINE, "lo": min(closes), "hi": max(closes)},
        ).result_rows
        published = {}
        for decision_close, snapshot_id, fingerprint in ledger:
            if decision_close.tzinfo is None:
                decision_close = decision_close.replace(tzinfo=timezone.utc)
            key = _norm_iso(decision_close)
            if key in published:
                raise RuntimeError(f"duplicate phase10 publication {key}")
            published[key] = {
                "decision_close": decision_close,
                "snapshot_id": str(snapshot_id),
                "active_state_fingerprint": str(fingerprint),
            }
        ids = sorted({row["snapshot_id"] for row in published.values()})
        catalog: dict[str, list[dict]] = {snapshot_id: [] for snapshot_id in ids}
        if ids:
            rows = client.query(
                f"""
                SELECT snapshot_id, pool_id, side, top_price, bottom_price, strength,
                       strength_class, known_at, row_fingerprint
                FROM {cfg.database}.research_lmv2_snapshot_active_pool_v1
                WHERE snapshot_id IN {{ids:Array(String)}}
                """,
                parameters={"ids": ids},
            ).result_rows
            for snapshot_id, pool_id, side, top, bottom, strength, strength_class, known_at, row_fp in rows:
                catalog.setdefault(str(snapshot_id), []).append(
                    {
                        "pool_id": pool_id,
                        "side": side,
                        "top_price": top,
                        "bottom_price": bottom,
                        "strength": strength,
                        "strength_class": strength_class,
                        "known_at": known_at,
                        "row_fingerprint": row_fp,
                    }
                )
        return published, catalog
    finally:
        client.close()


def _process_entry(worker_id: int, close_isos: list[str], snapshot_dir: str, publish_dir: str) -> None:
    payload = _worker(worker_id, close_isos, snapshot_dir, publish_dir)
    path = Path(publish_dir) / f"worker_{worker_id}_result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    wall_started = time.perf_counter()
    closes = planned_closes()
    blocks = disjoint_blocks(closes)
    need_from = datetime.fromtimestamp(
        required_source_start_unix(min(closes), 10, "5m"), tz=timezone.utc
    )
    identity = verify_manifest(SNAPSHOT_DIR, need_from=need_from, need_to=tail_open_for_coverage(closes))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = HERE / "runs" / stamp
    publish_dir = run_dir / "local_publish"
    publish_dir.mkdir(parents=True)
    vm = psutil.virtual_memory()
    ctx = mp.get_context("spawn")
    procs = []
    for worker_id, block in enumerate(blocks):
        proc = ctx.Process(
            target=_process_entry,
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
    merge_started = time.perf_counter()
    merge_error = None
    merged = None
    try:
        merged = merge_worker_payloads(payloads, closes)
        if repeat_digest(merged["records"]) != merged["merge_digest"]:
            raise PublishMergeError("merge digest is not repeatable")
    except PublishMergeError as exc:
        merge_error = str(exc)
    merge_seconds = time.perf_counter() - merge_started
    parity = "NOT_RUN"
    parity_errors: list[str] = []
    check_seconds = None
    if merged is not None:
        check_started = time.perf_counter()
        published, catalog = _published_rows(closes)
        for record in merged["records"]:
            key = _norm_iso(record["decision_close"])
            pub = published.get(key)
            if pub is None:
                parity_errors.append(f"missing_phase10_publication {key}")
                continue
            rows = catalog.get(pub["snapshot_id"]) or []
            parity_errors.extend(compare_published_close(record, pub, rows))
            record["phase10_snapshot_id"] = pub["snapshot_id"]
        check_seconds = round(time.perf_counter() - check_started, 3)
        parity = "PASS" if not parity_errors else "FAIL"
        if parity == "FAIL":
            merge_error = merge_error or "publication parity failed"
    statuses = {
        "FROZEN_INPUT_VALIDATION": "PASS",
        "THREE_WORKER_REVISION_CONSISTENCY": "PASS" if merged else "FAIL",
        "PHASE10_PUBLICATION_PARITY": parity,
        "DETERMINISTIC_MERGE": "PASS" if merged else "FAIL",
        "ISOLATED_PUBLISH_POC": "PASS" if parity == "PASS" else "FAIL",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "PHASE10_HISTORICAL_BYTE_BINDING": "NOT_PROVEN",
        "READY_FOR_CONTROLLED_MULTI_COIN_PUBLISH": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
    }
    engine_seconds = [item.get("engine_seconds") for item in payloads]
    report = {
        "PHASE": "11L",
        "RUN_ID": stamp,
        "GIT_NOTE": "no commit",
        "closes": [c.isoformat() for c in closes],
        "worker_blocks": [[c.isoformat() for c in block] for block in blocks],
        "frozen_input": identity,
        "worker_status": [
            {k: item.get(k) for k in ("worker_id", "status", "error", "load_seconds", "engine_seconds", "wall_seconds", "rss_mb", "snapshot_id", "price_hash", "revision_hash", "clickhouse_candle_reads", "incremental_bucket_recomputations")}
            for item in payloads
        ],
        "merge_digest": None if merged is None else merged["merge_digest"],
        "merge_error": merge_error,
        "parity_errors": parity_errors[:20],
        "parity_error_count": len(parity_errors),
        "active_counts": None
        if merged is None
        else [len(row["active_pool_ids"]) for row in merged["records"]],
        "performance": {
            "input_verify_included_in_wall": True,
            "worker_load_seconds": [item.get("load_seconds") for item in payloads],
            "worker_engine_seconds": engine_seconds,
            "merge_seconds": round(merge_seconds, 3),
            "publication_check_seconds": check_seconds,
            "wall_seconds": round(time.perf_counter() - wall_started, 3),
            "peak_rss_mb_max_worker": max((item.get("rss_mb") or 0) for item in payloads),
            "parent_rss_mb": round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1),
            "host_available_mb_at_start": round(vm.available / (1024 * 1024), 1),
            "closes_per_second": None
            if merged is None
            else round(len(closes) / max(time.perf_counter() - wall_started, 0.001), 4),
            "phase11e_cached_three_workers_closes_per_sec": 3.0556,
            "phase11i_parallel_closes_per_sec": 1.1854,
            "ten_coin_three_month_estimate": "NOT_MEASURED",
        },
        **statuses,
    }
    path = run_dir / "phase11l_report.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(path)
    print(parity, "errors", len(parity_errors), "wall", report["performance"]["wall_seconds"])
    return 0 if parity == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

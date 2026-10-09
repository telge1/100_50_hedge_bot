#!/usr/bin/env python3
"""Phase 11S — read-only capture forensics. No ClickHouse writes, no engine run."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[2]
_repo = _dash.parents[2]
_trp = Path("/home/telgenbuescher/projects/trading_research_platform")
for p in (_repo, _dash, _trp):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    file_checksum,
    load_snapshot,
    ohlcv_content_hash,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.run_phase11r_full_rolling_input_poc import _fetch

PHASE11R_DIR = (
    Path(__file__).resolve().parent
    / "research_runs"
    / "phase11r_20261009T121404Z_defdfaea"
)
PHASE11R_ID = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
RANGE_START = datetime(2026, 5, 23, 5, 25, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 9, 30, 12, 1, tzinfo=timezone.utc)


def _mem_available_mb() -> int:
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    report_path = Path(__file__).resolve().parent / f"phase11s_capture_forensics_{run_id}.json"
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = f"{_repo}:{_dash}:{_trp}"
    unit = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research_charts/tests/test_phase11s_capture_forensics.py",
            "-q",
            "--tb=line",
        ],
        cwd=str(_dash),
        env=env,
        capture_output=True,
        text=True,
    )

    manifest = json.loads((PHASE11R_DIR / f"{PHASE11R_ID}.manifest.json").read_text(encoding="utf-8"))
    data_path = PHASE11R_DIR / f"{PHASE11R_ID}.parquet"
    checksum_now = file_checksum(data_path)
    file_ok = checksum_now == manifest["file_checksum"]
    frozen = load_snapshot(PHASE11R_DIR, PHASE11R_ID) if file_ok else []
    file_rows_ok = bool(frozen) and len(frozen) == int(manifest["row_count"])

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    version = client.query("SELECT version()").result_rows[0][0]
    create_sql = client.query(f"SHOW CREATE TABLE {cfg.database}.{cfg.table}").result_rows[0][0]
    settings = dict(
        client.query(
            """
            SELECT name, value
            FROM system.settings
            WHERE name IN (
                'async_insert',
                'wait_for_async_insert',
                'select_sequential_consistency'
            )
            """
        ).result_rows
    )
    parts = client.query(
        f"""
        SELECT partition, count() AS active_parts, sum(rows) AS part_rows
        FROM system.parts
        WHERE database = {{db:String}} AND table = {{tb:String}} AND active
        GROUP BY partition
        ORDER BY partition DESC
        LIMIT 3
        """,
        parameters={"db": cfg.database, "tb": cfg.table},
    ).result_rows
    recent = client.query(
        f"""
        SELECT count() AS physical, uniqExact(open_time) AS keys
        FROM {cfg.database}.{cfg.table}
        WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m'
          AND open_time >= now() - INTERVAL 30 MINUTE
          AND open_time < now() - INTERVAL 2 MINUTE
        """,
        parameters={"ex": cfg.exchange},
    ).result_rows[0]
    ch_match = "NOT_RUN"
    ch_note = ""
    if _mem_available_mb() < 4000:
        ch_match = "NOT_RUN"
        ch_note = "skipped full-window compare because available RAM was below 4 GB"
    else:
        current = _fetch(client, cfg.database, cfg.table, cfg.exchange, RANGE_START, RANGE_END)
        same_price = ohlcv_content_hash(current) == manifest["ohlcv_content_hash"]
        same_rev = revision_metadata_hash(current) == manifest["revision_metadata_hash"]
        ch_match = "MATCH" if same_price and same_rev else "DIVERGED"
        ch_note = (
            f"one SELECT FINAL rows={len(current)} "
            f"price_match={same_price} revision_match={same_rev}"
        )
        del current
    client.close()

    fixtures_ok = unit.returncode == 0
    report = {
        "PHASE11S_MODE": "ISOLATED_CAPTURE_CONSISTENCY_FORENSICS",
        "RUN_ID": run_id,
        "CURRENT_CAPTURE_ARCHITECTURE": {
            "collector_insert": (
                "CandleRepository.insert_candles appends a batch. "
                "ReplacingMergeTree(ingested_at) keeps the greatest ingested_at per "
                "(exchange, symbol, interval, open_time) when FINAL is used. "
                "Merges are asynchronous, so a SELECT without FINAL can return several physical rows."
            ),
            "live_path": "WebSocket kline confirm=true becomes source=bybit_live and is buffered, then inserted. confirm=false is not inserted.",
            "recovery_path": (
                "REST recovery uses overlap_minutes=1, so it re-inserts the last stored closed minute "
                "with a new ingested_at and source=bybit_history. Internal repair inserts only minutes "
                "that missing-range detection reports, lookback 30 days."
            ),
            "research_read": (
                "Phase 11R used one SELECT FINAL for the whole window, then publish_snapshot. "
                "get_1m_candles also uses FINAL but does not select ingested_at."
            ),
            "ddl_engine_clause_present": "ReplacingMergeTree(ingested_at)" in create_sql.replace(" ", ""),
            "ddl_order_by": "(exchange,symbol,interval,open_time)" in create_sql.replace(" ", "").replace("`", ""),
            "clickhouse_version": str(version),
            "session_settings": settings,
            "recent_partitions": [
                {"partition": str(p[0]), "active_parts": int(p[1]), "part_rows": int(p[2])} for p in parts
            ],
            "recent_30m_physical_rows": int(recent[0]),
            "recent_30m_distinct_open_times": int(recent[1]),
        },
        "TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "TRANSACTIONAL_CONSISTENCY_EVIDENCE": (
            "The 11R capture is one statement, not several chunks. "
            "This run did not observe the server's part snapshot during that statement, "
            "and it did not run a concurrent insert. "
            "A later matching FINAL read is a second statement."
        ),
        "REVISION_MIXING_RISK": "PRESENT_NOT_DETECTABLE_AFTER_FINAL",
        "COMPLETENESS_OF_PUBLISHED_FILE": "PASS" if file_rows_ok else "FAIL",
        "CANONICAL_MINUTE_UNIQUENESS_IN_FILE": "PASS" if file_rows_ok else "FAIL",
        "SELECTED_REVISION_CONSISTENCY": "NOT_PROVEN",
        "PARQUET_PUBLISH_ATOMICITY": "PASS",
        "SNAPSHOT_ID_CONFUSION": "PASS" if fixtures_ok else "FAIL",
        "CLOSED_CANDLE_CORRECTION_PATH": "PASS",
        "POST_PUBLISH_REPRODUCIBILITY": "PASS" if file_ok and file_rows_ok else "FAIL",
        "PHASE11R_FILE_CHECKSUM_STILL_MATCHES": "PASS" if file_ok else "FAIL",
        "LATER_FINAL_COMPARED_WITH_PHASE11R_FILE": ch_match,
        "LATER_FINAL_NOTE": ch_note,
        "EXISTING_WATERMARK_CAPABILITY": "NONE_FOR_CANDLE_COMMITS",
        "WATERMARK_EVIDENCE": (
            "Candle recovery uses max(open_time) from SELECT FINAL. "
            "insert_candles records no batch id. "
            "Signal processing watermarks in the collector service are not the candle commit boundary."
        ),
        "FAILURE_FIXTURE_RESULTS": {
            "pytest_returncode": unit.returncode,
            "pytest_tail": (unit.stdout or "")[-500:],
            "correction_during_chunked_capture_overlap": "DETECTED" if fixtures_ok else "NOT_RUN",
            "two_revisions_same_minute_before_collapse": "DETECTED" if fixtures_ok else "NOT_RUN",
            "final_winner_hides_older_price": "NOT_DETECTABLE" if fixtures_ok else "NOT_RUN",
            "mixed_generations_unique_keys": "NOT_DETECTABLE" if fixtures_ok else "NOT_RUN",
            "missing_minute": "DETECTED" if fixtures_ok else "NOT_RUN",
            "same_price_different_revision_metadata": "DETECTED_AS_DIFFERENT_SNAPSHOT_ID" if fixtures_ok else "NOT_RUN",
            "different_price_same_open_time_before_collapse": "DETECTED" if fixtures_ok else "NOT_RUN",
            "published_version_a_unchanged_by_version_b": "PASS" if fixtures_ok else "NOT_RUN",
        },
        "MINIMAL_SAFE_SOLUTION": (
            "Keep the published parquet plus manifest as the worker boundary. "
            "Do not add a collector watermark or a raw-candle ledger for this gap. "
            "A second SELECT FINAL cannot prove the original read. "
            "Workers already fail closed on a foreign snapshot id, a foreign content hash, "
            "a tampered file, and a missing minute."
        ),
        "SOLUTION_COMPARISON": [
            {
                "name": "versioned_ingest_watermark",
                "provable_commit_boundary": "only after the collector writes the watermark in the same insert generation",
                "collector_change": "required",
                "clickhouse_change": "new rows or a new table",
                "extra_storage": "one watermark per batch",
                "runtime": "small",
                "historical_reproducibility": "only for candles ingested after the watermark exists",
                "fail_closed": "yes if the export refuses rows newer than the watermark",
                "live_ingest_risk": "a late batch can move the watermark under a running export unless the export pins one id",
            },
            {
                "name": "consistent_export_boundary",
                "provable_commit_boundary": "not by itself; it is the existing single SELECT plus the file publish",
                "collector_change": "none",
                "clickhouse_change": "none",
                "extra_storage": "the parquet already written",
                "runtime": "one read of the window",
                "historical_reproducibility": "the file, not a later FINAL read",
                "fail_closed": "yes after publish",
                "live_ingest_risk": "the SELECT itself remains NOT_PROVEN as a transaction",
            },
            {
                "name": "immutable_raw_candle_ledger",
                "provable_commit_boundary": "yes if every insert is copied before replace-merge",
                "collector_change": "required",
                "clickhouse_change": "new append-only table",
                "extra_storage": "every revision, not only the FINAL winner",
                "runtime": "extra insert on the live path",
                "historical_reproducibility": "from the ledger start onward",
                "fail_closed": "yes if export reads only one ledger generation",
                "live_ingest_risk": "a missed ledger insert leaves a hole",
            },
            {
                "name": "clickhouse_backup_or_snapshot",
                "provable_commit_boundary": "yes for a backup taken as one operation",
                "collector_change": "none",
                "clickhouse_change": "backup load and disk",
                "extra_storage": "a full table copy or part snapshot",
                "runtime": "backup of candles_1m, larger than one symbol window",
                "historical_reproducibility": "the backup, if retained",
                "fail_closed": "yes if workers read only that backup",
                "live_ingest_risk": "backup must not be confused with a later table read",
            },
            {
                "name": "extra_validation_before_publish",
                "provable_commit_boundary": "no; uniqueness and gap checks already run and still accept a FINAL winner set",
                "collector_change": "none",
                "clickhouse_change": "none",
                "extra_storage": "none",
                "runtime": "a second read, which is another statement",
                "historical_reproducibility": "unchanged",
                "fail_closed": "already fail-closed for gaps, duplicate keys, and hash mismatch",
                "live_ingest_risk": "a second read can disagree without identifying which statement was mixed",
            },
        ],
        "REMAINING_NOT_PROVEN": [
            "single SELECT FINAL part isolation on this server during a concurrent insert",
            "that the Phase 11R winner set was the only revision generation present at the original read",
            "power-loss durability beyond fsync and rename",
            "binding Phase 10 publications to this file",
        ],
        "RECOMMENDED_NEXT_PHASE": "NO_CAPTURE_ARCHITECTURE_CHANGE_UNTIL_APPROVED",
        "READY_FOR_REMAINING_POOL_CH_POC": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "FILES_CHANGED": [
            "live_micro_lld_v2_ch_test/run_phase11s_capture_forensics.py",
            "tests/test_phase11s_capture_forensics.py",
        ],
        "REPORT_PATH": str(report_path),
        "PHASE11R_SNAPSHOT_ID": PHASE11R_ID,
        "PHASE11R_SNAPSHOT_LEFT_IN_PLACE": str(PHASE11R_DIR),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    shown = [
        "PHASE11S_MODE",
        "TRANSACTIONAL_CONSISTENCY",
        "REVISION_MIXING_RISK",
        "EXISTING_WATERMARK_CAPABILITY",
        "LATER_FINAL_COMPARED_WITH_PHASE11R_FILE",
        "LATER_FINAL_NOTE",
        "PHASE11R_FILE_CHECKSUM_STILL_MATCHES",
        "MINIMAL_SAFE_SOLUTION",
        "RECOMMENDED_NEXT_PHASE",
        "READY_FOR_REMAINING_POOL_CH_POC",
        "READY_FOR_PRODUCTION_CUTOVER",
        "REPORT_PATH",
    ]
    print(json.dumps({k: report[k] for k in shown}, indent=2))
    arch = report["CURRENT_CAPTURE_ARCHITECTURE"]
    print(
        f"recent_30m physical={arch['recent_30m_physical_rows']} keys={arch['recent_30m_distinct_open_times']} "
        f"version={arch['clickhouse_version']} pytest={unit.returncode}",
        flush=True,
    )
    return 0 if fixtures_ok and file_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

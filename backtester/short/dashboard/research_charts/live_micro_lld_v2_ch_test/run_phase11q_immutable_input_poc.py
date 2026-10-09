#!/usr/bin/env python3
"""Phase 11Q — freeze a small DOGE 1m window locally. Read-only ClickHouse."""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_dash = Path(__file__).resolve().parents[2]
_repo = _dash.parents[2]
_trp = Path("/home/telgenbuescher/projects/trading_research_platform")
for p in (_repo, _dash, _trp):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect

from data.models import Candle
from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    FORMAT_VERSION,
    REFERENCE_CONTRACT,
    SOURCE_QUERY_SEMANTICS,
    CandleInputRow,
    load_snapshot,
    ohlcv_content_hash,
    publish_snapshot,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.trp_import import load_trp

RANGE_START = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch(client, db: str, table: str, exchange: str) -> list[CandleInputRow]:
    q = f"""
    SELECT exchange, symbol, interval, open_time, close_time, open, high, low, close,
           volume, turnover, is_closed, source, source_event_time, ingested_at
    FROM {db}.{table} FINAL
    WHERE exchange = {{ex:String}}
      AND symbol = 'DOGEUSDT'
      AND interval = '1m'
      AND is_closed = 1
      AND open_time >= {{a:DateTime64(3, 'UTC')}}
      AND open_time < {{b:DateTime64(3, 'UTC')}}
    ORDER BY open_time
    """
    rows = client.query(q, parameters={"ex": exchange, "a": RANGE_START, "b": RANGE_END}).result_rows
    out = []
    for r in rows:
        out.append(
            CandleInputRow(
                exchange=str(r[0]),
                symbol=str(r[1]),
                interval=str(r[2]),
                open_time=_utc(r[3]),
                close_time=_utc(r[4]),
                open=Decimal(r[5]),
                high=Decimal(r[6]),
                low=Decimal(r[7]),
                close=Decimal(r[8]),
                volume=Decimal(r[9]),
                turnover=Decimal(r[10]),
                is_closed=int(r[11]),
                source=str(r[12]),
                source_event_time=_utc(r[13]) if r[13] is not None else None,
                ingested_at=_utc(r[14]),
            )
        )
    return out


def _to_candles(rows: list[CandleInputRow]) -> list[Candle]:
    return [
        Candle(
            timestamp=r.open_time,
            open=float(r.open),
            high=float(r.high),
            low=float(r.low),
            close=float(r.close),
            volume=float(r.volume),
            symbol=r.symbol,
            timeframe="1m",
        )
        for r in rows
    ]


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    out_dir = Path(__file__).resolve().parent / "research_runs" / f"phase11q_{run_id}"
    report_path = Path(__file__).resolve().parent / f"phase11q_immutable_input_{run_id}.json"
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = f"{_repo}:{_dash}:{_trp}"
    unit = subprocess.run(
        [sys.executable, "-m", "pytest", "research_charts/tests/test_phase11q_immutable_candle_snapshot.py", "-q", "--tb=line"],
        cwd=str(_dash),
        env=env,
        capture_output=True,
        text=True,
    )

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    t0 = time.perf_counter()
    primary = _fetch(client, cfg.database, cfg.table, cfg.exchange)
    probe = _fetch(client, cfg.database, cfg.table, cfg.exchange)
    client.close()
    fetch_seconds = time.perf_counter() - t0
    probe_match = ohlcv_content_hash(primary) == ohlcv_content_hash(probe) and revision_metadata_hash(primary) == revision_metadata_hash(probe)

    t_w = time.perf_counter()
    manifest = publish_snapshot(
        primary,
        out_dir,
        range_start=RANGE_START,
        range_end_exclusive=RANGE_END,
        source_table=f"{cfg.database}.{cfg.table}",
    )
    write_seconds = time.perf_counter() - t_w
    data_path = out_dir / f"{manifest['snapshot_id']}.parquet"
    file_bytes = data_path.stat().st_size
    raw_bytes = sum(len(str(r).encode()) for r in primary)

    t_r = time.perf_counter()
    loaded = load_snapshot(out_dir, manifest["snapshot_id"])
    read_seconds = time.perf_counter() - t_r
    t_h = time.perf_counter()
    workers = [
        require_snapshot(out_dir, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"])
        for _ in range(3)
    ]
    hash_seconds = time.perf_counter() - t_h
    readback_ok = ohlcv_content_hash(loaded) == ohlcv_content_hash(primary) and revision_metadata_hash(loaded) == revision_metadata_hash(primary)
    workers_ok = all(ohlcv_content_hash(w) == manifest["ohlcv_content_hash"] for w in workers)

    trp = load_trp()
    five_src = trp["aggregate"](_to_candles(primary), "5m", strict_complete_buckets=True)
    five_snap = trp["aggregate"](_to_candles(loaded), "5m", strict_complete_buckets=True)
    five_ok = len(five_src) == len(five_snap) == 72 and all(
        (a.open, a.high, a.low, a.close, a.volume) == (b.open, b.high, b.low, b.close, b.volume)
        for a, b in zip(five_src, five_snap)
    )

    ru = resource.getrusage(resource.RUSAGE_SELF)
    passed = unit.returncode == 0 and readback_ok and workers_ok and five_ok and probe_match and len(primary) == 360
    report = {
        "PHASE11Q_MODE": "ISOLATED_IMMUTABLE_INPUT_POC",
        "RUN_ID": run_id,
        "CLICKHOUSE_REVISION_SEMANTICS": (
            "ReplacingMergeTree(ingested_at) ORDER BY (exchange, symbol, interval, open_time). "
            "FINAL returns the row with the greatest ingested_at per key. "
            "Research get_1m_candles uses FINAL but selects only open_time, open, high, low, close, volume. "
            "It does not read ingested_at, source, or source_event_time."
        ),
        "ENGINE_INPUT_HASH_LIMIT": (
            "engine_input_hash includes candle_sequence_fingerprint of count, first/last open, and first/last close only. "
            "It does not prove the full OHLCV series. Phase-10 publications are not retroactively bound to this file."
        ),
        "CAPTURE_ISOLATION": "NOT_PROVEN",
        "CAPTURE_NOTE": (
            "The file was built from one SELECT FINAL. A second read matched at probe time. "
            "That is not a transactional ClickHouse snapshot. Two reads can diverge if a replace lands between them."
        ),
        "PROBE_READS_MATCHED": probe_match,
        "SNAPSHOT_FORMAT": "parquet+zstd with decimal fields stored as canonical scale-8 strings",
        "SNAPSHOT_SCHEMA": FORMAT_VERSION,
        "SOURCE_QUERY_SEMANTICS": SOURCE_QUERY_SEMANTICS,
        "REFERENCE_CONTRACT": REFERENCE_CONTRACT,
        "SNAPSHOT_ID_DETERMINISTIC": "PASS" if unit.returncode == 0 else "FAIL",
        "OHLCV_CONTENT_HASH": "PASS" if readback_ok else "FAIL",
        "REVISION_METADATA_HASH": "PASS" if readback_ok else "FAIL",
        "ATOMIC_PUBLISH": "PASS" if unit.returncode == 0 else "FAIL",
        "CRASH_RECOVERY": "PASS" if unit.returncode == 0 else "FAIL",
        "CRASH_RECOVERY_SCOPE": "Simulated process crash before manifest publish, with fsync+rename. Power-loss durability was not separately proven.",
        "REAL_DOGE_INPUT_SNAPSHOT": "PASS" if readback_ok and len(primary) == 360 else "FAIL",
        "REAL_DOGE_1M_COMPLETENESS": "PASS" if len(primary) == 360 else "FAIL",
        "REAL_DOGE_RANGE": [RANGE_START.isoformat(), RANGE_END.isoformat()],
        "TRP_5M_PARITY": "PASS" if five_ok else "FAIL",
        "IMMUTABLE_READBACK": "PASS" if readback_ok else "FAIL",
        "SIMULATED_HISTORICAL_CORRECTION": "PASS" if unit.returncode == 0 else "FAIL",
        "MULTI_WORKER_INPUT_CONSISTENCY": "PASS" if workers_ok else "FAIL",
        "MIXED_REVISION_FAIL_CLOSED": "PASS" if unit.returncode == 0 else "FAIL",
        "ENGINE_INPUT_COMPLETENESS": "NOT_TESTED",
        "ENGINE_INPUT_NOTE": "The 6h file does not contain rolling-4m history or PD10 warmup. It is not an engine prefix.",
        "ORIGINAL_ENGINE_ORACLE": "NOT_RUN",
        "REMAINING_PROJECTION_PARITY": "PASS" if unit.returncode == 0 else "NOT_RUN",
        "PHASE10_RETROACTIVE_INPUT_PROOF": "NO",
        "SNAPSHOT_BUILD_SECONDS": round(fetch_seconds + write_seconds, 4),
        "SNAPSHOT_READ_SECONDS": round(read_seconds, 4),
        "HASH_VERIFY_SECONDS": round(hash_seconds, 4),
        "SNAPSHOT_FILE_BYTES": file_bytes,
        "SNAPSHOT_ID": manifest["snapshot_id"],
        "OHLCV_HASH": manifest["ohlcv_content_hash"],
        "REVISION_HASH": manifest["revision_metadata_hash"],
        "ROW_COUNT": len(primary),
        "RAM_PEAK_MB": round(ru.ru_maxrss / 1024.0, 1),
        "TEST_RESULTS": {"pytest_returncode": unit.returncode, "pytest_tail": (unit.stdout or "")[-800:], "pytest_err": (unit.stderr or "")[-800:]},
        "FILES_CHANGED": [
            "live_micro_lld_v2_ch_test/immutable_candle_snapshot_v1.py",
            "live_micro_lld_v2_ch_test/run_phase11q_immutable_input_poc.py",
            "tests/test_phase11q_immutable_candle_snapshot.py",
        ],
        "SNAPSHOT_DIR": str(out_dir),
        "REPORT_PATH": str(report_path),
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "HISTORICAL_CORRECTION_SCOPE": (
            "Post-publish immutability of one local snapshot passed. "
            "ClickHouse capture isolation and Phase-10 retroactive binding did not. "
            "This is not production-wide safety."
        ),
        "READY_FOR_IMMUTABLE_INPUT_PIPELINE_POC": "YES" if passed else "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k not in {"REFERENCE_CONTRACT", "CLICKHOUSE_REVISION_SEMANTICS", "ENGINE_INPUT_HASH_LIMIT", "CAPTURE_NOTE", "SOURCE_QUERY_SEMANTICS"}}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

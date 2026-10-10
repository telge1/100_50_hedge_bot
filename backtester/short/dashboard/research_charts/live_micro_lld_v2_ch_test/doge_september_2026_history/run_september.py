#!/usr/bin/env python3
"""DOGEUSDT September 2026 historical LLD pool production.

Read-only ClickHouse capture, then local frozen-input workers. No publications.
"""

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

import gc
import json
import os
import signal
import subprocess
import time
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    SnapshotConflictError,
    ohlcv_content_hash,
    publish_snapshot,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.compare import compare_published_close
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import required_source_start_unix
from research_charts.live_micro_lld_v2_ch_test.phase11l.publish import PHASE10_LINE
from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession
from research_charts.live_micro_lld_v2_ch_test.serialize import _norm_iso
from research_charts.service import _timeframe_seconds
from research_charts.trp_import import ensure_trp_on_path

HERE = Path(__file__).resolve().parent
SEARCH_ROOT = HERE.parent
INPUTS = HERE / "inputs"
MONTH_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
MONTH_LIMIT = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 10, 1, tzinfo=timezone.utc)
HISTORY_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
SYMBOL = "DOGEUSDT"
COMBOS = (
    ("5m", 1),
    ("5m", 5),
    ("5m", 10),
    ("15m", 1),
    ("15m", 5),
    ("15m", 10),
    ("4h", 1),
    ("4h", 5),
    ("4h", 10),
)
# Measured mean seconds/close, 30 closes ending 2026-09-30T12:00Z (phase 11O).
RATES = {
    ("5m", 1): 1.0219,
    ("5m", 5): 0.8029,
    ("5m", 10): 0.6080,
    ("15m", 1): 0.3945,
    ("15m", 5): 0.2853,
    ("15m", 10): 0.1920,
    ("4h", 10): 0.0486,
}
PARITY_CLOSES = [
    datetime(2026, 9, 30, 11, 20, tzinfo=timezone.utc) + timedelta(minutes=5 * i) for i in range(9)
]


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _month_closes(timeframe: str) -> list[datetime]:
    step = timedelta(seconds=_timeframe_seconds(timeframe))
    cursor = MONTH_START
    closes: list[datetime] = []
    while cursor <= MONTH_LIMIT:
        closes.append(cursor)
        cursor += step
    return closes


def _combo_key(timeframe: str, pool_detail: int) -> str:
    return f"{timeframe}_PD{int(pool_detail)}"


def _warmup_matrix(closes_by: dict[tuple[str, int], list[datetime]]) -> list[dict]:
    rows = []
    for timeframe, pool_detail in COMBOS:
        closes = closes_by[(timeframe, pool_detail)]
        need_first = datetime.fromtimestamp(
            required_source_start_unix(closes[0], pool_detail, timeframe), tz=timezone.utc
        )
        need_last = datetime.fromtimestamp(
            required_source_start_unix(closes[-1], pool_detail, timeframe), tz=timezone.utc
        )
        covered = need_first >= HISTORY_START
        rows.append(
            {
                "timeframe": timeframe,
                "pool_detail": pool_detail,
                "calendar_closes": len(closes),
                "first_decision_close": closes[0].isoformat(),
                "last_decision_close": closes[-1].isoformat(),
                "earliest_required_1m": need_first.isoformat(),
                "latest_decision_required_1m": need_last.isoformat(),
                "history_floor": HISTORY_START.isoformat(),
                "available": "YES" if covered else "NO",
                "status": "WARMUP_COMPLETE" if covered else "BLOCKED_INCOMPLETE_WARMUP",
            }
        )
    return rows


def _client():
    import clickhouse_connect

    cfg = load_clickhouse_config()
    return cfg, clickhouse_connect.get_client(**cfg.connect_kwargs())


def _coverage(range_start: datetime) -> dict:
    cfg, client = _client()
    database, table, exchange = cfg.database, cfg.table, cfg.exchange
    where = "exchange={ex:String} AND symbol='DOGEUSDT' AND interval='1m' AND is_closed=1"
    try:
        logical = client.query(
            f"SELECT min(open_time), max(open_time), count(), uniqExact(open_time) "
            f"FROM {database}.{table} FINAL WHERE {where}",
            parameters={"ex": exchange},
        ).result_rows[0]
        physical = client.query(
            f"SELECT count(), uniqExact(open_time) FROM {database}.{table} WHERE {where}",
            parameters={"ex": exchange},
        ).result_rows[0]
        expected = int((RANGE_END - range_start).total_seconds() // 60)
        window = client.query(
            f"SELECT count(), uniqExact(open_time), min(open_time), max(open_time) "
            f"FROM {database}.{table} FINAL WHERE {where} "
            f"AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}",
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0]
        physical_window = client.query(
            f"SELECT count(), uniqExact(open_time) FROM {database}.{table} WHERE {where} "
            f"AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}",
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0]
        gaps = client.query(
            f"""
            SELECT count() FROM (
              SELECT open_time, lead(open_time) OVER (ORDER BY open_time) AS nxt
              FROM {database}.{table} FINAL
              WHERE {where}
                AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
            )
            WHERE nxt > toDateTime64('2000-01-01 00:00:00', 3, 'UTC')
              AND dateDiff('second', open_time, nxt) != 60
            """,
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0][0]
        price_revisions = client.query(
            f"""
            SELECT count() FROM (
              SELECT open_time FROM {database}.{table}
              WHERE {where}
                AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
              GROUP BY open_time
              HAVING uniqExact(cityHash64(toString(open), toString(high), toString(low), toString(close), toString(volume))) > 1
            )
            """,
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0][0]
        ingest_revisions = client.query(
            f"""
            SELECT open_time, groupArray(ingested_at)
            FROM {database}.{table}
            WHERE {where}
              AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
            GROUP BY open_time
            HAVING uniqExact(ingested_at) > 1
            """,
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows
    finally:
        client.close()
    unique = int(window[1])
    complete = (
        int(window[0]) == expected
        and unique == expected
        and int(gaps) == 0
        and _utc(window[2]) == range_start
        and _utc(window[3]) == RANGE_END - timedelta(minutes=1)
        and int(price_revisions) == 0
    )
    return {
        "exchange": exchange,
        "symbol": SYMBOL,
        "interval": "1m",
        "table": f"{database}.{table}",
        "earliest_closed_1m": _utc(logical[0]).isoformat(),
        "latest_closed_1m": _utc(logical[1]).isoformat(),
        "history_floor_confirmed": _utc(logical[0]) == HISTORY_START,
        "logical_rows": int(logical[2]),
        "logical_unique_minutes": int(logical[3]),
        "physical_rows": int(physical[0]),
        "physical_duplicate_rows": int(physical[0]) - int(physical[1]),
        "required_range_start": range_start.isoformat(),
        "required_range_end_exclusive": RANGE_END.isoformat(),
        "expected_minutes": expected,
        "window_final_rows": int(window[0]),
        "window_unique_minutes": unique,
        "window_first": _utc(window[2]).isoformat(),
        "window_last": _utc(window[3]).isoformat(),
        "missing_minutes": expected - unique,
        "neighbor_gaps_excluding_terminal_null": int(gaps),
        "physical_rows_in_window": int(physical_window[0]),
        "physical_duplicate_rows_in_window": int(physical_window[0]) - int(physical_window[1]),
        "minutes_with_differing_ohlcv": int(price_revisions),
        "minutes_with_multiple_ingested_at": [
            {"open_time": _utc(row[0]).isoformat(), "ingested_at": [_utc(item).isoformat() for item in row[1]]}
            for row in ingest_revisions
        ],
        "completeness_basis": "uniqExact(open_time) == expected span, exact endpoints, and zero non-60s lead steps",
        "september_plus_warmup_complete": "YES" if complete else "NO",
    }


def _existing_snapshots(range_start: datetime) -> list[dict]:
    found = []
    for path in sorted(SEARCH_ROOT.rglob("*.manifest.json")):
        if HERE in path.parents:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("snapshot_format_version") != "immutable_candle_input_v1":
            continue
        if manifest.get("symbol") != SYMBOL:
            continue
        start = datetime.fromisoformat(str(manifest["range_start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(manifest["range_end_exclusive"]).replace("Z", "+00:00"))
        covers = start <= range_start and end >= RANGE_END
        found.append(
            {
                "snapshot_id": manifest.get("snapshot_id"),
                "path": str(path),
                "range_start": start.isoformat(),
                "range_end_exclusive": end.isoformat(),
                "ohlcv_content_hash": manifest.get("ohlcv_content_hash"),
                "revision_metadata_hash": manifest.get("revision_metadata_hash"),
                "covers_september_and_warmup": covers,
            }
        )
    return found


def _fetch_rows(range_start: datetime) -> list[CandleInputRow]:
    cfg, client = _client()
    query = f"""
    SELECT exchange, symbol, interval, open_time, close_time, open, high, low, close,
           volume, turnover, is_closed, source, source_event_time, ingested_at
    FROM {cfg.database}.{cfg.table} FINAL
    WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m'
      AND is_closed = 1
      AND open_time >= {{a:DateTime64(3, 'UTC')}}
      AND open_time < {{b:DateTime64(3, 'UTC')}}
    ORDER BY open_time
    """
    try:
        rows = client.query(
            query, parameters={"ex": cfg.exchange, "a": range_start, "b": RANGE_END}
        ).result_rows
    finally:
        client.close()
    out = []
    for row in rows:
        out.append(
            CandleInputRow(
                exchange=str(row[0]),
                symbol=str(row[1]),
                interval=str(row[2]),
                open_time=_utc(row[3]),
                close_time=_utc(row[4]),
                open=Decimal(row[5]),
                high=Decimal(row[6]),
                low=Decimal(row[7]),
                close=Decimal(row[8]),
                volume=Decimal(row[9]),
                turnover=Decimal(row[10]),
                is_closed=int(row[11]),
                source=str(row[12]),
                source_event_time=_utc(row[13]) if row[13] is not None else None,
                ingested_at=_utc(row[14]),
            )
        )
    return out


def _reuse_or_capture(range_start: datetime, inventory: list[dict]) -> dict:
    covers = [item for item in inventory if item["covers_september_and_warmup"]]
    hashes = {(item["ohlcv_content_hash"], item["revision_metadata_hash"]) for item in covers}
    if len(covers) == 1 or (covers and len(hashes) == 1):
        chosen = covers[0]
        directory = Path(chosen["path"]).parent
        rows = require_snapshot(directory, chosen["snapshot_id"], ohlcv_hash=chosen["ohlcv_content_hash"])
        if revision_metadata_hash(rows) != chosen["revision_metadata_hash"]:
            raise RuntimeError("reused snapshot revision hash diverged")
        return {
            "snapshot_id": chosen["snapshot_id"],
            "price_hash": chosen["ohlcv_content_hash"],
            "revision_hash": chosen["revision_metadata_hash"],
            "directory": str(directory),
            "row_count": len(rows),
            "source": "REUSED_EXISTING_IMMUTABLE_FILE",
        }
    if len(hashes) > 1:
        raise RuntimeError("existing covering snapshots disagree on revision")
    rows = _fetch_rows(range_start)
    manifest = publish_snapshot(
        rows,
        INPUTS,
        range_start=range_start,
        range_end_exclusive=RANGE_END,
        source_table="signal_generator.candles_1m",
    )
    del rows
    gc.collect()
    loaded = require_snapshot(INPUTS, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"])
    if revision_metadata_hash(loaded) != manifest["revision_metadata_hash"]:
        raise RuntimeError("captured revision hash diverged")
    if len(loaded) != int((RANGE_END - range_start).total_seconds() // 60):
        raise RuntimeError("captured snapshot is not a complete minute span")
    first = min(row.open_time for row in loaded)
    last = max(row.open_time for row in loaded)
    if first != range_start or last != RANGE_END - timedelta(minutes=1):
        raise RuntimeError("captured snapshot endpoints do not match the required window")
    result = {
        "snapshot_id": manifest["snapshot_id"],
        "price_hash": manifest["ohlcv_content_hash"],
        "revision_hash": manifest["revision_metadata_hash"],
        "directory": str(INPUTS),
        "row_count": len(loaded),
        "file_checksum": manifest["file_checksum"],
        "source": "NEW_SINGLE_SELECT_FINAL",
    }
    del loaded
    gc.collect()
    return result


def _resources() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    collectors = []
    research = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = proc.info["name"] or ""
        command = " ".join(proc.info["cmdline"] or [])
        if "run_bybit_live_1m_collector.py" in command:
            collectors.append({"pid": proc.pid, "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1)})
        if "python" not in name:
            continue
        if "run_september.py" in command and "--worker" in command:
            research.append(proc.pid)
        if any(token in command for token in ("run_phase11l.py", "run_phase11m.py", "run_benchmark.py")):
            research.append(proc.pid)
    sample = 205_396
    scheduled = 8640 * 3 + 2880 * 3 + 180
    return {
        "ram_available_mb": round(memory.available / (1024 * 1024)),
        "ram_total_mb": round(memory.total / (1024 * 1024)),
        "swap_used_mb": round(psutil.swap_memory().used / (1024 * 1024)),
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "load_average": [round(value, 2) for value in os.getloadavg()],
        "cpu_count": os.cpu_count(),
        "collector_processes": collectors,
        "foreign_research_pids": sorted(set(research)),
        "assumed_worker_rss_mb": 916,
        "three_worker_reserve_mb": 916 * 3 + 2048,
        "result_disk_estimate_gb": round(scheduled * sample * 2 / (1024 ** 3), 2),
        "nproc_soft": None,
    }


def _schedule(closes_by: dict[tuple[str, int], list[datetime]]) -> list[list[dict]]:
    jobs = []
    for timeframe, pool_detail in COMBOS:
        if (timeframe, pool_detail) not in RATES:
            continue
        closes = closes_by[(timeframe, pool_detail)]
        jobs.append(
            {
                "timeframe": timeframe,
                "pool_detail": pool_detail,
                "closes": [item.isoformat() for item in closes],
                "estimated_seconds": round(len(closes) * RATES[(timeframe, pool_detail)], 1),
            }
        )
    workers: list[list[dict]] = [[], [], []]
    load = [0.0, 0.0, 0.0]
    for job in sorted(jobs, key=lambda item: -item["estimated_seconds"]):
        slot = min(range(3), key=lambda index: (load[index], index))
        workers[slot].append(job)
        load[slot] += job["estimated_seconds"]
    for _ in range(4):
        heavy = max(range(3), key=lambda index: load[index])
        light = min(range(3), key=lambda index: (load[index], index))
        if load[heavy] - load[light] < 600 or heavy == light:
            break
        index = max(range(len(workers[heavy])), key=lambda item: workers[heavy][item]["estimated_seconds"])
        task = workers[heavy][index]
        rate = RATES[(task["timeframe"], task["pool_detail"])]
        move = int((load[heavy] - (load[heavy] + load[light]) / 2) / rate)
        if move < 1 or move >= len(task["closes"]) - 1:
            break
        suffix = task["closes"][-move:]
        task["closes"] = task["closes"][:-move]
        task["estimated_seconds"] = round(len(task["closes"]) * rate, 1)
        workers[light].append(
            {
                "timeframe": task["timeframe"],
                "pool_detail": task["pool_detail"],
                "closes": suffix,
                "estimated_seconds": round(len(suffix) * rate, 1),
                "contiguous_suffix": True,
            }
        )
        load[heavy] -= move * rate
        load[light] += move * rate
    return workers


def _published_rows(closes: list[datetime]) -> tuple[dict, dict]:
    cfg, client = _client()
    try:
        ledger = client.query(
            f"""
            SELECT decision_close, snapshot_id, active_state_fingerprint
            FROM {cfg.database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND symbol = 'DOGEUSDT' AND timeframe = '5m' AND pool_detail = 10
              AND publish_complete = 1
              AND decision_close >= {{lo:DateTime64(3, 'UTC')}}
              AND decision_close <= {{hi:DateTime64(3, 'UTC')}}
            """,
            parameters={"lid": PHASE10_LINE, "lo": min(closes), "hi": max(closes)},
        ).result_rows
        published = {}
        for decision_close, snapshot_id, fingerprint in ledger:
            key = _norm_iso(_utc(decision_close))
            if key in published:
                raise RuntimeError(f"duplicate phase10 publication {key}")
            published[key] = {
                "decision_close": _utc(decision_close),
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


def _stamp(iso: str) -> str:
    return _utc(datetime.fromisoformat(iso)).strftime("%Y%m%dT%H%M%SZ")


def _result_path(root: Path, timeframe: str, pool_detail: int, iso: str) -> Path:
    return root / "results" / _combo_key(timeframe, pool_detail) / f"{_stamp(iso)}.json"


def _identity_ok(path: Path, snapshot_id: str, price: str, revision: str) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frozen = payload.get("frozen_input") or {}
    if frozen.get("snapshot_id") != snapshot_id:
        raise RuntimeError(f"snapshot identity conflict {path}")
    if frozen.get("price_hash") != price or frozen.get("revision_hash") != revision:
        raise RuntimeError(f"hash identity conflict {path}")
    return True


def _future_candle(candles, decision: datetime, timeframe: str) -> None:
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.availability import bar_close

    limit = _utc(decision)
    for candle in candles:
        if bar_close(candle.timestamp, timeframe) > limit:
            raise RuntimeError(f"future candle {candle.timestamp.isoformat()} at {limit.isoformat()}")


def _future_known(record: dict, decision: datetime) -> None:
    limit = _norm_iso(decision)
    for pool_id, pool in (record.get("pools") or {}).items():
        known = _norm_iso(pool.get("known_at"))
        if known and limit and known > limit:
            raise RuntimeError(f"future known_at {pool_id}")


def run_worker(worker_id: int, plan_path: Path) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    root = Path(plan["output_root"])
    status_path = root / "workers" / f"worker_{worker_id}_status.json"
    assignment = next(item for item in plan["workers"] if int(item["worker_id"]) == worker_id)
    status = {
        "worker_id": worker_id,
        "pid": os.getpid(),
        "status": "STARTING",
        "snapshot_id": plan["snapshot_id"],
        "completed": 0,
        "expected": sum(len(task["closes"]) for task in assignment["tasks"]),
        "errors": [],
    }
    _atomic(status_path, status)
    try:
        rows = require_snapshot(Path(plan["snapshot_dir"]), plan["snapshot_id"], ohlcv_hash=plan["price_hash"])
        price = ohlcv_content_hash(rows)
        revision = revision_metadata_hash(rows)
        if price != plan["price_hash"] or revision != plan["revision_hash"]:
            raise RuntimeError("worker rejected frozen input identity")
        identity = {
            "snapshot_id": plan["snapshot_id"],
            "price_hash": price,
            "revision_hash": revision,
            "clickhouse_candle_reads": 0,
        }
        status["status"] = "RUNNING"
        _atomic(status_path, status)
        for task in assignment["tasks"]:
            timeframe = task["timeframe"]
            pool_detail = int(task["pool_detail"])
            session = ReusedSnapshotSession(
                rows,
                snapshot_id=plan["snapshot_id"],
                ohlcv_hash=price,
                timeframe=timeframe,
            )
            session.max_1m_bars = 420_000
            for iso in task["closes"]:
                dest = _result_path(root, timeframe, pool_detail, iso)
                if dest.is_file():
                    _identity_ok(dest, plan["snapshot_id"], price, revision)
                    status["completed"] += 1
                    status["last_close"] = iso
                    status["current_combo"] = _combo_key(timeframe, pool_detail)
                    _atomic(status_path, status)
                    continue
                decision = _utc(datetime.fromisoformat(iso))
                history = rolling_history_from_close(decision, 4)
                snap, candles, result = build_active_state_at_decision_close(
                    SYMBOL,
                    timeframe,
                    history,
                    decision,
                    pool_detail=pool_detail,
                    history_policy=HISTORY_POLICY_ROLLING_4M,
                    candle_session=session,
                )
                _future_candle(candles, decision, timeframe)
                record = verification_record(snap, candles, result, decision)
                _future_known(record, decision)
                record["frozen_input"] = identity
                record["worker_id"] = worker_id
                record["timeframe"] = timeframe
                record["pool_detail"] = pool_detail
                record["symbol"] = SYMBOL
                if dest.exists():
                    raise RuntimeError(f"refusing to overwrite {dest}")
                _atomic(dest, record)
                status["completed"] += 1
                status["last_close"] = iso
                status["current_combo"] = _combo_key(timeframe, pool_detail)
                status["rss_mb"] = round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
                _atomic(status_path, status)
                print(f"{status['current_combo']} {_stamp(iso)} {status['completed']}/{status['expected']}", flush=True)
            if int(session.stats.ch_fetch_calls) != 0:
                raise RuntimeError("engine worker performed a ClickHouse candle read")
            del session
            gc.collect()
        status["status"] = "COMPLETE"
        _atomic(status_path, status)
        return 0
    except Exception as exc:
        status["status"] = "FAILED"
        status["errors"].append(f"{type(exc).__name__}: {exc}")
        status["trace"] = traceback.format_exc()
        _atomic(status_path, status)
        print(status["trace"], flush=True)
        return 1


def _spawn(plan_path: Path, log_dir: Path, *, wait: bool) -> list[subprocess.Popen]:
    log_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for worker_id in range(3):
        log = (log_dir / f"worker_{worker_id}.log").open("ab", buffering=0)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(worker_id), "--plan", str(plan_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(HERE),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        log.close()
        procs.append(proc)
    if wait:
        for proc in procs:
            proc.wait()
    return procs


def _wait_running(root: Path, timeout: float) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = []
        for worker_id in range(3):
            path = root / "workers" / f"worker_{worker_id}_status.json"
            if not path.is_file():
                break
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        if len(rows) == 3 and all(row.get("status") in {"RUNNING", "COMPLETE", "FAILED"} for row in rows):
            return rows
        time.sleep(1)
    return []


def _parity(frozen: dict) -> dict:
    root = HERE / "preflight_parity"
    blocks = [PARITY_CLOSES[index : index + 3] for index in range(0, 9, 3)]
    plan = {
        "role": "prestart_parity",
        "snapshot_dir": frozen["directory"],
        "snapshot_id": frozen["snapshot_id"],
        "price_hash": frozen["price_hash"],
        "revision_hash": frozen["revision_hash"],
        "output_root": str(root),
        "workers": [
            {
                "worker_id": index,
                "tasks": [
                    {
                        "timeframe": "5m",
                        "pool_detail": 10,
                        "closes": [item.isoformat() for item in block],
                    }
                ],
            }
            for index, block in enumerate(blocks)
        ],
    }
    plan_path = root / "plan.json"
    _atomic(plan_path, plan)
    _spawn(plan_path, root / "workers", wait=True)
    published, catalog = _published_rows(PARITY_CLOSES)
    errors = []
    identities = set()
    future = 0
    for close in PARITY_CLOSES:
        path = _result_path(root, "5m", 10, close.isoformat())
        if not path.is_file():
            errors.append(f"missing parity close {close.isoformat()}")
            continue
        local = json.loads(path.read_text(encoding="utf-8"))
        frozen_input = local.get("frozen_input") or {}
        identities.add(
            (frozen_input.get("snapshot_id"), frozen_input.get("price_hash"), frozen_input.get("revision_hash"))
        )
        _future_known(local, close)
        key = _norm_iso(close)
        row = published.get(key)
        if row is None:
            errors.append(f"missing phase10 publication {key}")
            continue
        errors.extend(compare_published_close(local, row, catalog.get(row["snapshot_id"], [])))
    if identities != {(frozen["snapshot_id"], frozen["price_hash"], frozen["revision_hash"])}:
        errors.append("parity workers did not share one frozen identity")
    return {
        "PRESTART_ORIGINAL_PARITY": "PASS" if not errors else "FAIL",
        "HISTORICAL_CAUSALITY": "PASS" if future == 0 and not errors else "FAIL",
        "phase10_line": PHASE10_LINE,
        "closes": [item.isoformat() for item in PARITY_CLOSES],
        "worker_split": "3 contiguous blocks of 3 closes",
        "errors": errors,
        "INDEPENDENT_PUBLICATION_PARITY": {
            "5m_PD10_short_block": "PASS" if not errors else "FAIL",
            "other_combinations": "NOT_PROVEN",
            "full_month": "NOT_PROVEN",
        },
    }


def _base_report() -> dict:
    return {
        "DOGE_1M_COVERAGE": None,
        "NINE_COMBINATION_WARMUP_MATRIX": None,
        "FROZEN_INPUT_ID": None,
        "FROZEN_INPUT_READY": "NO",
        "WORKER_RESOURCE_READY": "NO",
        "PRESTART_ORIGINAL_PARITY": "NOT_RUN",
        "HISTORICAL_CAUSALITY": "NOT_RUN",
        "READY_COMBINATIONS": [],
        "BLOCKED_COMBINATIONS": [],
        "THREE_WORKER_START_ALLOWED": "NO",
        "THREE_WORKER_STARTED": "NO",
        "PROCESS_IDS": [],
        "OUTPUT_ROOT": str(HERE / "production"),
        "EXPECTED_CLOSE_COUNTS": {},
        "COMPLETED_CLOSE_COUNTS": {},
        "CHECKPOINT_RESUME_STATUS": "NOT_ARMED",
        "ERRORS": [],
        "READY_FOR_BACKTESTER": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
    }


def main() -> int:
    report = _base_report()
    preflight_path = HERE / "preflight_report.json"
    try:
        closes_by = {combo: _month_closes(combo[0]) for combo in COMBOS}
        expected_calendar = {"5m": 8640, "15m": 2880, "4h": 180}
        for timeframe, count in expected_calendar.items():
            if len(closes_by[(timeframe, 1)]) != count:
                raise RuntimeError(f"unexpected {timeframe} close count")
        print("STAGE warmup_matrix", flush=True)
        _atomic(HERE / "progress.json", {"stage": "warmup_matrix"})
        matrix = _warmup_matrix(closes_by)
        report["NINE_COMBINATION_WARMUP_MATRIX"] = matrix
        ready = [_combo_key(row["timeframe"], row["pool_detail"]) for row in matrix if row["status"] == "WARMUP_COMPLETE"]
        blocked = [
            {"combo": _combo_key(row["timeframe"], row["pool_detail"]), "reason": row["status"], "earliest_required_1m": row["earliest_required_1m"]}
            for row in matrix
            if row["status"] != "WARMUP_COMPLETE"
        ]
        report["READY_COMBINATIONS"] = ready
        report["BLOCKED_COMBINATIONS"] = blocked
        range_start = min(
            datetime.fromisoformat(row["earliest_required_1m"])
            for row in matrix
            if row["status"] == "WARMUP_COMPLETE"
        )
        print("STAGE clickhouse_coverage", range_start.isoformat(), flush=True)
        _atomic(HERE / "progress.json", {"stage": "clickhouse_coverage", "range_start": range_start.isoformat()})
        coverage = _coverage(range_start)
        report["DOGE_1M_COVERAGE"] = coverage
        inventory = _existing_snapshots(range_start)
        report["EXISTING_SNAPSHOT_INVENTORY"] = inventory
        if coverage["september_plus_warmup_complete"] != "YES" or coverage["minutes_with_differing_ohlcv"]:
            report["ERRORS"].append("1m coverage or price-revision conflict is unresolved")
            _atomic(preflight_path, report)
            return 2
        if not coverage["history_floor_confirmed"]:
            report["ERRORS"].append("DOGE history floor is not 2025-12-11T00:00:00Z")
            _atomic(preflight_path, report)
            return 2
        resources = _resources()
        report["RESOURCES"] = resources
        enough = (
            resources["ram_available_mb"] >= resources["three_worker_reserve_mb"]
            and resources["disk_free_gb"] >= 30
            and not resources["foreign_research_pids"]
        )
        report["WORKER_RESOURCE_READY"] = "YES" if enough else "NO"
        if not enough:
            report["ERRORS"].append("resource gate failed")
            _atomic(preflight_path, report)
            return 2
        print("STAGE freeze_input", range_start.isoformat(), flush=True)
        _atomic(HERE / "progress.json", {"stage": "freeze_input", "range_start": range_start.isoformat()})
        frozen = _reuse_or_capture(range_start, inventory)
        report["FROZEN_INPUT_ID"] = frozen["snapshot_id"]
        report["FROZEN_INPUT"] = frozen
        report["FROZEN_INPUT_READY"] = "YES"
        print("STAGE prestart_parity", frozen["snapshot_id"], flush=True)
        _atomic(HERE / "progress.json", {"stage": "prestart_parity", "snapshot_id": frozen["snapshot_id"]})
        parity = _parity(frozen)
        report["PRESTART_ORIGINAL_PARITY"] = parity["PRESTART_ORIGINAL_PARITY"]
        report["HISTORICAL_CAUSALITY"] = parity["HISTORICAL_CAUSALITY"]
        report["PARITY_DETAIL"] = parity
        if parity["PRESTART_ORIGINAL_PARITY"] != "PASS" or parity["HISTORICAL_CAUSALITY"] != "PASS":
            report["ERRORS"].extend(parity["errors"])
            _atomic(preflight_path, report)
            return 2
        assignment = _schedule(closes_by)
        seen: dict[str, list[str]] = {}
        for tasks in assignment:
            for task in tasks:
                seen.setdefault(_combo_key(task["timeframe"], task["pool_detail"]), []).extend(task["closes"])
        for timeframe, pool_detail in COMBOS:
            key = _combo_key(timeframe, pool_detail)
            if (timeframe, pool_detail) not in RATES:
                continue
            wanted = [item.isoformat() for item in closes_by[(timeframe, pool_detail)]]
            got = seen.get(key, [])
            if len(got) != len(set(got)) or sorted(got) != sorted(wanted):
                raise RuntimeError(f"schedule does not cover {key} exactly once")
            for task in (item for group in assignment for item in group if _combo_key(item["timeframe"], item["pool_detail"]) == key):
                if task["closes"] != sorted(task["closes"]):
                    raise RuntimeError(f"{key} block is not chronological")
        output = HERE / "production"
        plan = {
            "role": "september_production",
            "snapshot_dir": frozen["directory"],
            "snapshot_id": frozen["snapshot_id"],
            "price_hash": frozen["price_hash"],
            "revision_hash": frozen["revision_hash"],
            "output_root": str(output),
            "workers": [{"worker_id": index, "tasks": tasks} for index, tasks in enumerate(assignment)],
        }
        _atomic(output / "plan.json", plan)
        counts = {_combo_key(tf, pd): (len(closes_by[(tf, pd)]) if (tf, pd) in RATES else 0) for tf, pd in COMBOS}
        report["EXPECTED_CLOSE_COUNTS"] = counts
        report["COMPLETED_CLOSE_COUNTS"] = {key: 0 for key in counts}
        report["CHECKPOINT_RESUME_STATUS"] = "ARMED_SKIP_EXISTING_SAME_IDENTITY_NO_OVERWRITE"
        report["THREE_WORKER_START_ALLOWED"] = "YES"
        report["SCHEDULE_ESTIMATED_SECONDS"] = [
            round(sum(task["estimated_seconds"] for task in tasks), 1) for tasks in assignment
        ]
        _atomic(preflight_path, report)
        print("STAGE spawn_month_workers", flush=True)
        procs = _spawn(output / "plan.json", output / "workers", wait=False)
        alive = _wait_running(output, 300)
        start = dict(report)
        failed = [row for row in alive if row.get("status") == "FAILED"]
        if len(alive) != 3 or failed:
            for proc in procs:
                if proc.poll() is None:
                    try:
                        os.kill(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            start["ERRORS"] = list(report["ERRORS"]) + ["workers did not all reach RUNNING"]
            start["WORKER_STATUS"] = alive
            start["THREE_WORKER_STARTED"] = "NO"
            start["PROCESS_IDS"] = [proc.pid for proc in procs]
            _atomic(HERE / "start_report.json", start)
            return 3
        start["THREE_WORKER_STARTED"] = "YES"
        start["PROCESS_IDS"] = [int(row["pid"]) for row in alive]
        start["WORKER_STATUS"] = alive
        start["MONITOR"] = {
            "status": str(output / "workers" / "worker_0_status.json"),
            "logs": str(output / "workers"),
            "results": str(output / "results"),
        }
        _atomic(HERE / "start_report.json", start)
        _atomic(output / "supervisor.json", {"parent_pids_recorded": start["PROCESS_IDS"], "started_at": datetime.now(timezone.utc).isoformat()})
        return 0
    except Exception as exc:
        report["ERRORS"].append(f"{type(exc).__name__}: {exc}")
        report["TRACE"] = traceback.format_exc()
        _atomic(preflight_path, report)
        return 1


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker_index = int(sys.argv[sys.argv.index("--worker") + 1])
        plan_arg = Path(sys.argv[sys.argv.index("--plan") + 1])
        raise SystemExit(run_worker(worker_index, plan_arg))
    raise SystemExit(main())

#!/usr/bin/env python3
"""DOGEUSDT April–August 2026 historical pool production.

Three engine workers, one shared frozen 1m input, monthly release manifests.
Does not write the September production tree or ClickHouse.
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

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
    active_state_fingerprint,
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
    ohlcv_content_hash,
    publish_snapshot,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import required_source_start_unix
from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession
from research_charts.live_micro_lld_v2_ch_test.serialize import _norm_iso
from research_charts.service import _timeframe_seconds
from research_charts.trp_import import ensure_trp_on_path

HERE = Path(__file__).resolve().parent
SEARCH_ROOT = HERE.parent
INPUTS = HERE / "inputs"
PRODUCTION = HERE / "production"
RESULTS = PRODUCTION / "results"
SEPTEMBER = SEARCH_ROOT / "doge_september_2026_history"
SYMBOL = "DOGEUSDT"
FLOOR = datetime(2025, 12, 11, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 9, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
MONTHS = (
    ("2026-04", datetime(2026, 4, 1, tzinfo=timezone.utc), datetime(2026, 5, 1, tzinfo=timezone.utc)),
    ("2026-05", datetime(2026, 5, 1, tzinfo=timezone.utc), datetime(2026, 6, 1, tzinfo=timezone.utc)),
    ("2026-06", datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 1, tzinfo=timezone.utc)),
    ("2026-07", datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc)),
    ("2026-08", datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)),
)
REQUESTED = (("5m", 1), ("5m", 5), ("5m", 10), ("15m", 1), ("15m", 5), ("15m", 10), ("4h", 10))
INVESTIGATE = REQUESTED + (("4h", 1), ("4h", 5))
RATES = {
    ("5m", 1): 1.0219,
    ("5m", 5): 0.8029,
    ("5m", 10): 0.6080,
    ("15m", 1): 0.3945,
    ("15m", 5): 0.2853,
    ("15m", 10): 0.1920,
    ("4h", 10): 0.0486,
}
LIGHT = (("5m", 10), ("15m", 1), ("15m", 5), ("15m", 10), ("4h", 10))
POOL_RE_TEXT = r"^lldv2:DOGEUSDT:(5m|15m|4h):(normal|micro|reclaim):(upper|lower):(\d+)$"


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


def _combo(timeframe: str, pool_detail: int) -> str:
    return f"{timeframe}_PD{int(pool_detail)}"


def _stamp(iso: str) -> str:
    return _utc(datetime.fromisoformat(iso)).strftime("%Y%m%dT%H%M%SZ")


def _month_of(iso: str) -> str:
    return _utc(datetime.fromisoformat(iso)).strftime("%Y-%m")


def _closes(timeframe: str, start: datetime, end_exclusive: datetime) -> list[datetime]:
    step = timedelta(seconds=_timeframe_seconds(timeframe))
    cursor = start
    out = []
    while cursor < end_exclusive and cursor <= PERIOD_END:
        out.append(cursor)
        cursor += step
    return out


def _need(moment: datetime, pool_detail: int, timeframe: str) -> datetime:
    return datetime.fromtimestamp(required_source_start_unix(moment, pool_detail, timeframe), tz=timezone.utc)


def _first_feasible(series: list[datetime], pool_detail: int, timeframe: str) -> int | None:
    if _need(series[-1], pool_detail, timeframe) < FLOOR:
        return None
    if _need(series[0], pool_detail, timeframe) >= FLOOR:
        return 0
    low, high = 0, len(series) - 1
    while low < high:
        mid = (low + high) // 2
        if _need(series[mid], pool_detail, timeframe) >= FLOOR:
            high = mid
        else:
            low = mid + 1
    if _need(series[low], pool_detail, timeframe) < FLOOR:
        raise RuntimeError("partial boundary is still before the history floor")
    if low == 0 or _need(series[low - 1], pool_detail, timeframe) >= FLOOR:
        raise RuntimeError("partial boundary skipped a covered close or included an uncovered one")
    return low


def _matrix() -> tuple[list[dict], dict[tuple[str, str, int], list[datetime]]]:
    rows = []
    scheduled: dict[tuple[str, str, int], list[datetime]] = {}
    for month, start, end in MONTHS:
        for timeframe, pool_detail in INVESTIGATE:
            series = _closes(timeframe, start, end)
            first_need = _need(series[0], pool_detail, timeframe)
            last_need = _need(series[-1], pool_detail, timeframe)
            index = _first_feasible(series, pool_detail, timeframe)
            if index is None:
                status = "BLOCKED_INCOMPLETE_WARMUP"
                chosen: list[datetime] = []
            elif index == 0:
                status = "FULL"
                chosen = series
            else:
                status = "PARTIAL"
                chosen = series[index:]
            if chosen and (timeframe, pool_detail) not in RATES:
                raise RuntimeError(f"no measured rate for scheduled {_combo(timeframe, pool_detail)}")
            scheduled[(month, timeframe, pool_detail)] = chosen
            rows.append(
                {
                    "month": month,
                    "timeframe": timeframe,
                    "pool_detail": pool_detail,
                    "calendar_closes": len(series),
                    "scheduled_closes": len(chosen),
                    "first_decision_close": series[0].isoformat(),
                    "last_decision_close": series[-1].isoformat(),
                    "earliest_required_1m": first_need.isoformat(),
                    "latest_decision_required_1m": last_need.isoformat(),
                    "scheduled_from": chosen[0].isoformat() if chosen else None,
                    "scheduled_required_1m": _need(chosen[0], pool_detail, timeframe).isoformat() if chosen else None,
                    "status": status,
                }
            )
    return rows, scheduled


def _schedule(scheduled: dict[tuple[str, str, int], list[datetime]]) -> list[list[dict]]:
    streams = {combo: [] for combo in REQUESTED}
    for month, _start, _end in MONTHS:
        for timeframe, pool_detail in REQUESTED:
            streams[(timeframe, pool_detail)].extend(
                item.isoformat() for item in scheduled[(month, timeframe, pool_detail)]
            )
    workers: list[list[dict]] = [[], [], []]

    def _task(timeframe: str, pool_detail: int, closes: list[str], *, suffix: bool = False) -> dict:
        if closes != sorted(closes):
            raise RuntimeError(f"{_combo(timeframe, pool_detail)} block is not chronological")
        return {
            "timeframe": timeframe,
            "pool_detail": pool_detail,
            "closes": closes,
            "estimated_seconds": round(len(closes) * RATES[(timeframe, pool_detail)], 1),
            "contiguous_suffix": suffix,
        }

    workers[0].append(_task("5m", 1, streams[("5m", 1)]))
    workers[1].append(_task("5m", 5, streams[("5m", 5)]))
    load0 = workers[0][0]["estimated_seconds"]
    load1 = workers[1][0]["estimated_seconds"]
    if load0 - load1 >= 600:
        rate = RATES[("5m", 1)]
        move = int((load0 - load1) / 2 / rate)
        task = workers[0][0]
        if move >= 1 and move < len(task["closes"]) - 1:
            suffix = task["closes"][-move:]
            task["closes"] = task["closes"][:-move]
            task["estimated_seconds"] = round(len(task["closes"]) * rate, 1)
            workers[1].append(_task("5m", 1, suffix, suffix=True))
    for month, _start, _end in MONTHS:
        for timeframe, pool_detail in LIGHT:
            closes = [item.isoformat() for item in scheduled[(month, timeframe, pool_detail)]]
            if closes:
                workers[2].append(_task(timeframe, pool_detail, closes))
    seen: dict[str, list[str]] = {}
    for group in workers:
        for task in group:
            seen.setdefault(_combo(task["timeframe"], task["pool_detail"]), []).extend(task["closes"])
    for timeframe, pool_detail in REQUESTED:
        key = _combo(timeframe, pool_detail)
        wanted = streams[(timeframe, pool_detail)]
        got = seen.get(key, [])
        if len(got) != len(set(got)) or sorted(got) != sorted(wanted):
            raise RuntimeError(f"schedule does not cover {key} exactly once")
    return workers


def _client():
    import clickhouse_connect

    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    return cfg, clickhouse_connect.get_client(**cfg.connect_kwargs())


def _coverage(range_start: datetime) -> dict:
    cfg, client = _client()
    database, table, exchange = cfg.database, cfg.table, cfg.exchange
    where = "exchange={ex:String} AND symbol='DOGEUSDT' AND interval='1m' AND is_closed=1"
    expected = int((RANGE_END - range_start).total_seconds() // 60)
    try:
        window = client.query(
            f"SELECT count(), uniqExact(open_time), min(open_time), max(open_time) "
            f"FROM {database}.{table} FINAL WHERE {where} "
            f"AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}",
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0]
        physical = client.query(
            f"SELECT count(), uniqExact(open_time) FROM {database}.{table} WHERE {where} "
            f"AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}",
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0]
        gaps = client.query(
            f"""
            SELECT count() FROM (
              SELECT open_time, lead(open_time) OVER (ORDER BY open_time) AS nxt
              FROM {database}.{table} FINAL
              WHERE {where} AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
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
              WHERE {where} AND open_time>={{a:DateTime64(3,'UTC')}} AND open_time<{{b:DateTime64(3,'UTC')}}
              GROUP BY open_time
              HAVING uniqExact(cityHash64(toString(open), toString(high), toString(low), toString(close), toString(volume))) > 1
            )
            """,
            parameters={"ex": exchange, "a": range_start, "b": RANGE_END},
        ).result_rows[0][0]
    finally:
        client.close()
    complete = (
        int(window[0]) == expected
        and int(window[1]) == expected
        and int(gaps) == 0
        and int(price_revisions) == 0
        and _utc(window[2]) == range_start
        and _utc(window[3]) == RANGE_END - timedelta(minutes=1)
    )
    return {
        "range_start": range_start.isoformat(),
        "range_end_exclusive": RANGE_END.isoformat(),
        "expected_minutes": expected,
        "unique_minutes": int(window[1]),
        "missing_minutes": expected - int(window[1]),
        "physical_duplicate_rows": int(physical[0]) - int(physical[1]),
        "minutes_with_differing_ohlcv": int(price_revisions),
        "gap_steps": int(gaps),
        "complete": "YES" if complete else "NO",
    }


def _existing_snapshots(range_start: datetime) -> list[dict]:
    found = []
    for path in sorted(SEARCH_ROOT.rglob("*.manifest.json")):
        if HERE in path.parents or SEPTEMBER in path.parents:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("snapshot_format_version") != "immutable_candle_input_v1" or manifest.get("symbol") != SYMBOL:
            continue
        start = datetime.fromisoformat(str(manifest["range_start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(manifest["range_end_exclusive"]).replace("Z", "+00:00"))
        found.append(
            {
                "snapshot_id": manifest.get("snapshot_id"),
                "path": str(path),
                "range_start": start.isoformat(),
                "range_end_exclusive": end.isoformat(),
                "covers": start <= range_start and end >= RANGE_END,
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
        rows = client.query(query, parameters={"ex": cfg.exchange, "a": range_start, "b": RANGE_END}).result_rows
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


def _freeze(range_start: datetime) -> dict:
    own = list(INPUTS.glob("*.manifest.json")) if INPUTS.is_dir() else []
    if len(own) == 1:
        manifest = json.loads(own[0].read_text(encoding="utf-8"))
        start = datetime.fromisoformat(str(manifest["range_start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(manifest["range_end_exclusive"]).replace("Z", "+00:00"))
        if start == range_start and end == RANGE_END:
            rows = require_snapshot(INPUTS, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"])
            if revision_metadata_hash(rows) != manifest["revision_metadata_hash"] or len(rows) != int(manifest["row_count"]):
                raise RuntimeError("existing multi-month snapshot failed read-back")
            result = {
                "snapshot_id": manifest["snapshot_id"],
                "price_hash": manifest["ohlcv_content_hash"],
                "revision_hash": manifest["revision_metadata_hash"],
                "directory": str(INPUTS),
                "row_count": len(rows),
                "source": "REUSED_THIS_RUN_DIRECTORY",
            }
            del rows
            gc.collect()
            return result
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


def _result_path(root: Path, iso: str, timeframe: str, pool_detail: int) -> Path:
    return root / "results" / _month_of(iso) / _combo(timeframe, pool_detail) / f"{_stamp(iso)}.json"


def _identity_ok(path: Path, snapshot_id: str, price: str, revision: str) -> bool:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frozen = payload.get("frozen_input") or {}
    if frozen.get("snapshot_id") != snapshot_id or frozen.get("price_hash") != price or frozen.get("revision_hash") != revision:
        raise RuntimeError(f"identity conflict {path}")
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
    if "doge_september_2026_history" in str(root):
        raise RuntimeError("refusing to write the September production tree")
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
        "current_month": None,
        "current_combo": None,
    }
    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True
        status["status"] = "STOPPED"
        status["errors"].append(f"signal {signum}")
        _atomic(status_path, status)

    signal.signal(signal.SIGTERM, _handle)
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
            session.max_1m_bars = 500_000
            for iso in task["closes"]:
                if stop["flag"]:
                    status["status"] = "STOPPED"
                    _atomic(status_path, status)
                    return 0
                dest = _result_path(root, iso, timeframe, pool_detail)
                if dest.is_file():
                    _identity_ok(dest, plan["snapshot_id"], price, revision)
                    status["completed"] += 1
                    status["last_close"] = iso
                    status["current_month"] = _month_of(iso)
                    status["current_combo"] = _combo(timeframe, pool_detail)
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
                record["month"] = _month_of(iso)
                if dest.exists():
                    raise RuntimeError(f"refusing to overwrite {dest}")
                _atomic(dest, record)
                status["completed"] += 1
                status["last_close"] = iso
                status["current_month"] = record["month"]
                status["current_combo"] = _combo(timeframe, pool_detail)
                status["rss_mb"] = round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
                _atomic(status_path, status)
                print(f"{record['month']} {status['current_combo']} {_stamp(iso)} {status['completed']}/{status['expected']}", flush=True)
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


def _running_processes() -> dict[str, list[int]]:
    found = {"worker": [], "supervisor": [], "collector": []}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        command = " ".join(proc.info["cmdline"] or [])
        if "run_bybit_live_1m_collector.py" in command:
            found["collector"].append(proc.pid)
        if "run_multi_month.py" not in command or "python" not in (proc.info["name"] or ""):
            continue
        if "--worker" in command:
            found["worker"].append(proc.pid)
        elif "--supervisor" in command:
            found["supervisor"].append(proc.pid)
    return found


def _spawn(plan_path: Path, worker_ids: list[int]) -> list[subprocess.Popen]:
    log_dir = PRODUCTION / "workers"
    log_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for worker_id in worker_ids:
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
    return procs


def _wait_running(timeout: float) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = []
        for worker_id in range(3):
            path = PRODUCTION / "workers" / f"worker_{worker_id}_status.json"
            if not path.is_file():
                break
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        if len(rows) == 3 and all(row.get("status") in {"RUNNING", "COMPLETE", "FAILED"} for row in rows):
            return rows
        time.sleep(1)
    return []


def _names(month: str, combo: str) -> set[str]:
    folder = RESULTS / month / combo
    if not folder.is_dir():
        return set()
    return {entry.name[:-5] for entry in os.scandir(folder) if entry.is_file() and entry.name.endswith(".json") and not entry.name.endswith(".json.tmp")}


def _validate_file(path: Path, snapshot_id: str, price: str, revision: str, timeframe: str, pool_detail: int) -> list[str]:
    import re

    from indicators.live_micro_lld_v2.styles import strength_class

    problems = []
    payload = json.loads(path.read_text(encoding="utf-8"))
    frozen = payload.get("frozen_input") or {}
    if frozen.get("snapshot_id") != snapshot_id or frozen.get("price_hash") != price or frozen.get("revision_hash") != revision:
        problems.append("frozen_input")
    if payload.get("timeframe") != timeframe or int(payload.get("pool_detail")) != pool_detail or payload.get("symbol") != SYMBOL:
        problems.append("combo")
    decision = _utc(datetime.fromisoformat(payload["decision_close"]))
    if _stamp(decision.isoformat()) != path.stem:
        problems.append("decision_close")
    ids = list(payload.get("active_pool_ids") or [])
    sides = list(payload.get("active_sides") or [])
    pools = payload.get("pools") or {}
    if set(pools) != set(ids) or len(ids) != len(sides):
        problems.append("membership")
    elif active_state_fingerprint(ids, sides) != payload.get("active_state_fingerprint"):
        problems.append("membership_checksum")
    limit = _norm_iso(decision)
    pattern = re.compile(POOL_RE_TEXT)
    for pool_id, side in zip(ids, sides):
        pool = pools.get(pool_id)
        if pool is None or str(pool.get("side", "")).lower() != str(side).lower():
            problems.append(f"side {pool_id}")
            continue
        match = pattern.match(str(pool_id))
        if not match or match.group(1) != timeframe or match.group(3) != str(pool.get("side", "")).lower():
            problems.append(f"pool_id {pool_id}")
            continue
        if int(match.group(4)) > int(decision.timestamp()):
            problems.append(f"future_anchor {pool_id}")
        top = float(pool["top_price"])
        bottom = float(pool["bottom_price"])
        strength = float(pool["strength"])
        if not (top >= bottom > 0):
            problems.append(f"bounds {pool_id}")
        if strength < 0 or str(pool.get("strength_class")) != strength_class(strength):
            problems.append(f"strength {pool_id}")
        known = _norm_iso(pool.get("known_at"))
        if known is None or limit is None or known > limit:
            problems.append(f"known_at {pool_id}")
    for field in ("candle_load_start", "history_from_close"):
        raw = payload.get(field)
        if raw and _utc(datetime.fromisoformat(raw)) > decision:
            problems.append(field)
    return problems


def _release_month(coverage: dict, month: str, frozen: dict) -> dict:
    errors = []
    checked = 0
    combo_rows = []
    for row in coverage["combos"]:
        if row["month"] != month or (row["timeframe"], row["pool_detail"]) not in RATES and row["status"] == "BLOCKED_INCOMPLETE_WARMUP":
            if row["month"] == month:
                combo_rows.append(row)
            continue
        if row["month"] != month:
            continue
        combo_rows.append(row)
        if row["scheduled_closes"] == 0:
            continue
        combo = _combo(row["timeframe"], row["pool_detail"])
        for stamp in row["stamps"]:
            path = RESULTS / month / combo / f"{stamp}.json"
            problems = _validate_file(path, frozen["snapshot_id"], frozen["price_hash"], frozen["revision_hash"], row["timeframe"], row["pool_detail"])
            checked += 1
            if problems:
                errors.append({"file": path.name, "combo": combo, "problems": problems[:8]})
                if len(errors) >= 20:
                    break
        if len(errors) >= 20:
            break
    requested = [row for row in combo_rows if (row["timeframe"], row["pool_detail"]) in {(tf, pd) for tf, pd in REQUESTED}]
    full_requested = all(row["status"] == "FULL" for row in requested)
    status = "FAILED" if errors else ("COMPLETE" if full_requested else "PARTIAL")
    report = {
        "month": month,
        "MONTH_STATUS": status,
        "CLOSES_CHECKED": checked,
        "VALIDATION_ERRORS": errors,
        "FROZEN_INPUT_ID": frozen["snapshot_id"],
        "INPUT_REVISION_CONSISTENCY": "PASS" if not errors else "FAIL",
        "HISTORICAL_CAUSALITY": "PASS" if not errors else "FAIL",
        "ORIGINAL_POOL_PARITY": "NOT_PROVEN",
        "BACKTESTER_CANDIDATE": status == "COMPLETE",
        "PARTIAL_COMBINATIONS": [_combo(row["timeframe"], row["pool_detail"]) for row in requested if row["status"] == "PARTIAL"],
        "BLOCKED_COMBINATIONS": [_combo(row["timeframe"], row["pool_detail"]) for row in combo_rows if row["status"] == "BLOCKED_INCOMPLETE_WARMUP"],
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "released_at": datetime.now(timezone.utc).isoformat(),
    }
    month_dir = PRODUCTION / "months" / month
    _atomic(month_dir / "month_report.json", report)
    if not errors:
        _atomic(month_dir / "manifest.json", report)
    return report


def _status_snapshot(coverage: dict, stage: str) -> dict:
    workers = []
    for worker_id in range(3):
        path = PRODUCTION / "workers" / f"worker_{worker_id}_status.json"
        if path.is_file():
            workers.append(json.loads(path.read_text(encoding="utf-8")))
    months = {}
    completed = 0
    expected = 0
    for month, _start, _end in MONTHS:
        combos = {}
        manifest = PRODUCTION / "months" / month / "manifest.json"
        month_status = "RELEASED" if manifest.is_file() else "RUNNING"
        for row in coverage["combos"]:
            if row["month"] != month:
                continue
            combo = _combo(row["timeframe"], row["pool_detail"])
            found = len(_names(month, combo)) if row["scheduled_closes"] else 0
            expected += row["scheduled_closes"]
            completed += found
            if row["status"] == "BLOCKED_INCOMPLETE_WARMUP":
                combo_status = "BLOCKED_INCOMPLETE_WARMUP"
            elif manifest.is_file():
                combo_status = row["status"] if row["status"] == "PARTIAL" else "COMPLETE"
            elif found == 0:
                combo_status = "RUNNING"
            elif found < row["scheduled_closes"]:
                combo_status = "RUNNING"
                month_status = "RUNNING"
            else:
                combo_status = "VALIDATING" if stage == "VALIDATING" else "FILES_COMPLETE"
            combos[combo] = {"expected": row["scheduled_closes"], "found": found, "status": combo_status, "coverage": row["status"]}
        if any(item["found"] < item["expected"] for item in combos.values()):
            month_status = "RUNNING"
        months[month] = {"status": month_status, "combos": combos, "manifest": str(manifest) if manifest.is_file() else None}
    active = [row for row in workers if row.get("status") == "RUNNING"]
    return {
        "stage": stage,
        "WORKER_COUNT": len(active),
        "WORKER_STATUS": [
            {
                "worker_id": row.get("worker_id"),
                "pid": row.get("pid"),
                "status": row.get("status"),
                "current_month": row.get("current_month"),
                "current_combo": row.get("current_combo"),
                "completed": row.get("completed"),
                "expected": row.get("expected"),
                "errors": row.get("errors"),
                "snapshot_id": row.get("snapshot_id"),
            }
            for row in workers
        ],
        "MONTHLY_COMPLETENESS": months,
        "CLOSES_COMPLETED": completed,
        "CLOSES_EXPECTED": expected,
        "FROZEN_INPUT_ID": coverage["frozen"]["snapshot_id"],
        "ERRORS": [row.get("errors") for row in workers if row.get("errors")],
        "READY_FOR_BACKTESTER": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def run_supervisor() -> int:
    coverage = json.loads((PRODUCTION / "coverage.json").read_text(encoding="utf-8"))
    frozen = coverage["frozen"]
    released = set()
    while True:
        snapshot = _status_snapshot(coverage, "RUNNING")
        _atomic(PRODUCTION / "status.json", snapshot)
        for month, _start, _end in MONTHS:
            if month in released or (PRODUCTION / "months" / month / "manifest.json").is_file():
                released.add(month)
                continue
            ready = True
            for row in coverage["combos"]:
                if row["month"] != month or row["scheduled_closes"] == 0:
                    continue
                names = _names(month, _combo(row["timeframe"], row["pool_detail"]))
                if names != set(row["stamps"]):
                    ready = False
                    break
            if not ready:
                continue
            _atomic(PRODUCTION / "status.json", _status_snapshot(coverage, "VALIDATING"))
            report = _release_month(coverage, month, frozen)
            released.add(month)
            print(f"MONTH {month} {report['MONTH_STATUS']}", flush=True)
        workers = snapshot["WORKER_STATUS"]
        if len(workers) == 3 and all(row.get("status") in {"COMPLETE", "FAILED", "STOPPED"} for row in workers):
            for month, _start, _end in MONTHS:
                if month not in released and not (PRODUCTION / "months" / month / "manifest.json").is_file():
                    missing = []
                    for row in coverage["combos"]:
                        if row["month"] == month and row["scheduled_closes"]:
                            names = _names(month, _combo(row["timeframe"], row["pool_detail"]))
                            if names != set(row["stamps"]):
                                missing.append({"combo": _combo(row["timeframe"], row["pool_detail"]), "found": len(names), "expected": row["scheduled_closes"]})
                    if not missing:
                        report = _release_month(coverage, month, frozen)
                        released.add(month)
                    else:
                        _atomic(
                            PRODUCTION / "months" / month / "month_report.json",
                            {"month": month, "MONTH_STATUS": "FAILED", "MISSING": missing, "READY_FOR_PRODUCTION_CUTOVER": "NO"},
                        )
            final = _status_snapshot(coverage, "FINISHED")
            final["ORIGINAL_POOL_PARITY"] = "NOT_PROVEN"
            final["SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY"] = "NOT_PROVEN"
            final["RESUME_STATUS"] = "COMPLETE_OR_FAILED_SEE_WORKER_STATUS"
            released_months = []
            for month, _start, _end in MONTHS:
                manifest = PRODUCTION / "months" / month / "manifest.json"
                if manifest.is_file():
                    released_months.append(json.loads(manifest.read_text(encoding="utf-8"))["MONTH_STATUS"])
            final["RELEASED_MONTHS"] = released_months
            final["READY_FOR_BACKTESTER"] = "NO"
            _atomic(PRODUCTION / "status.json", final)
            _atomic(HERE / "final_report.json", final)
            return 0
        time.sleep(20)


def _print_status() -> int:
    path = PRODUCTION / "status.json"
    if not path.is_file():
        print("Kein Status. Der Multi-Month-Lauf wurde noch nicht gestartet.")
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(f"Frozen-Input: {payload.get('FROZEN_INPUT_ID')}")
    print(f"Closes: {payload.get('CLOSES_COMPLETED')}/{payload.get('CLOSES_EXPECTED')}")
    print(f"Aktive Worker: {payload.get('WORKER_COUNT')}")
    for row in payload.get("WORKER_STATUS") or []:
        print(
            f"  Worker {row.get('worker_id')} pid={row.get('pid')} {row.get('status')} "
            f"{row.get('current_month')} {row.get('current_combo')} {row.get('completed')}/{row.get('expected')}"
        )
    for month, info in (payload.get("MONTHLY_COMPLETENESS") or {}).items():
        print(f"  {month}: {info.get('status')}")
    if payload.get("ERRORS"):
        print(f"Fehler: {payload['ERRORS']}")
    return 0


def _expected_document(matrix: list[dict], scheduled: dict, frozen: dict) -> dict:
    combos = []
    for row in matrix:
        closes = scheduled[(row["month"], row["timeframe"], row["pool_detail"])]
        combos.append({**row, "stamps": [_stamp(item.isoformat()) for item in closes]})
    return {"frozen": frozen, "combos": combos}


def _smoke(frozen: dict, scheduled: dict) -> dict:
    series = scheduled[("2026-04", "5m", 10)]
    if not series:
        return {"HISTORICAL_CAUSALITY": "NOT_RUN", "errors": ["no April 5m PD10 close"]}
    iso = series[0].isoformat()
    dest = _result_path(PRODUCTION, iso, "5m", 10)
    if dest.is_file():
        _identity_ok(dest, frozen["snapshot_id"], frozen["price_hash"], frozen["revision_hash"])
        return {"HISTORICAL_CAUSALITY": "PASS", "close": iso, "source": "existing_file"}
    plan = {
        "snapshot_dir": frozen["directory"],
        "snapshot_id": frozen["snapshot_id"],
        "price_hash": frozen["price_hash"],
        "revision_hash": frozen["revision_hash"],
        "output_root": str(PRODUCTION),
        "workers": [{"worker_id": 0, "tasks": [{"timeframe": "5m", "pool_detail": 10, "closes": [iso]}]}],
    }
    plan_path = PRODUCTION / "smoke_plan.json"
    _atomic(plan_path, plan)
    proc = _spawn(plan_path, [0])[0]
    code = proc.wait()
    if code != 0 or not dest.is_file():
        return {"HISTORICAL_CAUSALITY": "FAIL", "close": iso, "exit_code": code}
    _identity_ok(dest, frozen["snapshot_id"], frozen["price_hash"], frozen["revision_hash"])
    return {"HISTORICAL_CAUSALITY": "PASS", "close": iso, "ORIGINAL_POOL_PARITY": "NOT_PROVEN"}


def _resources() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    processes = _running_processes()
    return {
        "ram_available_mb": round(memory.available / (1024 * 1024)),
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "load_average": [round(value, 2) for value in os.getloadavg()],
        "collector_pids": processes["collector"],
        "worker_pids": processes["worker"],
        "supervisor_pids": processes["supervisor"],
    }


def main() -> int:
    processes = _running_processes()
    if processes["worker"] or processes["supervisor"]:
        print("Ein Multi-Month-Lauf ist bereits aktiv. Es wird kein zweiter Runner gestartet.")
        print("Worker:", processes["worker"], "Supervisor:", processes["supervisor"])
        _print_status()
        return 0
    matrix, scheduled = _matrix()
    assignment = _schedule(scheduled)
    range_start = min(
        datetime.fromisoformat(row["scheduled_required_1m"])
        for row in matrix
        if row["scheduled_required_1m"]
    )
    if range_start < FLOOR:
        raise RuntimeError("scheduled warmup starts before the history floor")
    print("STAGE coverage", range_start.isoformat(), flush=True)
    coverage = _coverage(range_start)
    resources = _resources()
    blocked = [
        {"month": row["month"], "combo": _combo(row["timeframe"], row["pool_detail"]), "earliest_required_1m": row["earliest_required_1m"]}
        for row in matrix
        if row["status"] == "BLOCKED_INCOMPLETE_WARMUP"
    ]
    partial = sorted({row["month"] for row in matrix if row["status"] == "PARTIAL" and (row["timeframe"], row["pool_detail"]) in RATES})
    report = {
        "FROZEN_INPUT_VALIDATION": "PENDING",
        "WARMUP_COVERAGE_MATRIX": [{key: value for key, value in row.items()} for row in matrix],
        "WORKER_COUNT": 3,
        "WORKER_STATUS": [],
        "MONTHLY_COMPLETENESS": {},
        "CLOSES_COMPLETED": 0,
        "CLOSES_EXPECTED": sum(len(items) for items in scheduled.values()),
        "HISTORICAL_CAUSALITY": "NOT_RUN",
        "ORIGINAL_POOL_PARITY": "NOT_PROVEN",
        "INPUT_REVISION_CONSISTENCY": "PENDING",
        "RESUME_STATUS": "ARMED_SKIP_EXISTING_SAME_IDENTITY_NO_OVERWRITE",
        "PARTIAL_MONTHS": partial,
        "BLOCKED_COMBINATIONS": blocked,
        "READY_FOR_BACKTESTER": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "DOGE_1M_COVERAGE": coverage,
        "RESOURCES": resources,
        "EXISTING_SNAPSHOTS_COVERING": [item for item in _existing_snapshots(range_start) if item["covers"]],
        "SEPTEMBER_TREE_UNTOUCHED": str(SEPTEMBER),
    }
    if coverage["complete"] != "YES" or resources["ram_available_mb"] < 6000 or resources["disk_free_gb"] < 40:
        report["ERRORS"] = ["coverage or resource gate failed"]
        _atomic(HERE / "preflight_report.json", report)
        return 2
    print("STAGE freeze", flush=True)
    frozen = _freeze(range_start)
    report["FROZEN_INPUT_ID"] = frozen["snapshot_id"]
    report["FROZEN_INPUT"] = frozen
    report["FROZEN_INPUT_VALIDATION"] = "PASS"
    report["INPUT_REVISION_CONSISTENCY"] = "PASS"
    _atomic(PRODUCTION / "coverage.json", _expected_document(matrix, scheduled, frozen))
    print("STAGE smoke", flush=True)
    smoke = _smoke(frozen, scheduled)
    report["HISTORICAL_CAUSALITY"] = smoke["HISTORICAL_CAUSALITY"]
    report["SMOKE"] = smoke
    if smoke["HISTORICAL_CAUSALITY"] != "PASS":
        report["ERRORS"] = ["smoke close failed"]
        _atomic(HERE / "preflight_report.json", report)
        return 2
    plan = {
        "snapshot_dir": frozen["directory"],
        "snapshot_id": frozen["snapshot_id"],
        "price_hash": frozen["price_hash"],
        "revision_hash": frozen["revision_hash"],
        "output_root": str(PRODUCTION),
        "workers": [{"worker_id": index, "tasks": tasks} for index, tasks in enumerate(assignment)],
    }
    _atomic(PRODUCTION / "plan.json", plan)
    report["SCHEDULE_ESTIMATED_SECONDS"] = [round(sum(task["estimated_seconds"] for task in tasks), 1) for tasks in assignment]
    report["THREE_WORKER_START_ALLOWED"] = "YES"
    _atomic(HERE / "preflight_report.json", report)
    stale_status = PRODUCTION / "workers" / "worker_0_status.json"
    if stale_status.is_file():
        stale_status.unlink()
    print("STAGE spawn", flush=True)
    procs = _spawn(PRODUCTION / "plan.json", [0, 1, 2])
    alive = _wait_running(300)
    start = dict(report)
    if len(alive) != 3 or any(row.get("status") == "FAILED" for row in alive):
        for proc in procs:
            if proc.poll() is None:
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        start["THREE_WORKER_STARTED"] = "NO"
        start["PROCESS_IDS"] = [proc.pid for proc in procs]
        start["ERRORS"] = ["workers did not all reach RUNNING"]
        _atomic(HERE / "start_report.json", start)
        return 3
    supervisor_log = (PRODUCTION / "workers" / "supervisor.log").open("ab", buffering=0)
    supervisor = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--supervisor"],
        stdout=supervisor_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(HERE),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    supervisor_log.close()
    start["THREE_WORKER_STARTED"] = "YES"
    start["PROCESS_IDS"] = [int(row["pid"]) for row in alive]
    start["SUPERVISOR_PID"] = supervisor.pid
    start["WORKER_STATUS"] = alive
    start["STATUS_COMMAND"] = f"python {Path(__file__).resolve()} --status"
    _atomic(HERE / "start_report.json", start)
    print("STARTED", start["PROCESS_IDS"], "supervisor", supervisor.pid, flush=True)
    return 0


if __name__ == "__main__":
    if "--status" in sys.argv:
        raise SystemExit(_print_status())
    if "--supervisor" in sys.argv:
        raise SystemExit(run_supervisor())
    if "--worker" in sys.argv:
        worker_index = int(sys.argv[sys.argv.index("--worker") + 1])
        plan_arg = Path(sys.argv[sys.argv.index("--plan") + 1])
        raise SystemExit(run_worker(worker_index, plan_arg))
    raise SystemExit(main())

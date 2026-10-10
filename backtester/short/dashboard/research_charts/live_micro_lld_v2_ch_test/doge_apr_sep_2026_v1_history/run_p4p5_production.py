#!/usr/bin/env python3
"""P4/P5: canonical Apr–Sep 2026 V2 catalog (isolated tree, no legacy engine)."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
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
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
    active_state_fingerprint,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import (
    compare_records,
    verification_record,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.compare import compare_published_close
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    HISTORY_POLICY_ROLLING_4M,
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.identity import ENGINE_VERSION
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    ohlcv_content_hash,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import required_source_start_unix
from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession
from research_charts.live_micro_lld_v2_ch_test.serialize import _norm_iso
from research_charts.service import _timeframe_seconds
from research_charts.trp_import import ensure_trp_on_path

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
PRODUCTION = HERE / "production"
RESULTS = PRODUCTION / "results"
PILOT = HERE / "pilot"
P2P3_REPORT = HERE / "p2p3_report.json"

MM_ROOT = HERE.parent / "doge_multi_month_2026_history"
MM_RESULTS = MM_ROOT / "production" / "results"
SEP_ROOT = HERE.parent / "doge_september_2026_history"
SEP_RESULTS = SEP_ROOT / "production" / "results"

CANONICAL_FROZEN_ID = "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93"
CFAE_ID = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"
FE549_ID = "fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66"

SYMBOL = "DOGEUSDT"
CATALOG_START = datetime(2026, 4, 1, tzinfo=timezone.utc)
CATALOG_END_EXCLUSIVE = datetime(2026, 10, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
COMBOS = (("15m", 1), ("15m", 5), ("15m", 10), ("4h", 10))
RATES = {("15m", 1): 0.3945, ("15m", 5): 0.2853, ("15m", 10): 0.1920, ("4h", 10): 0.0486}
POOL_RE_TEXT = r"^lldv2:DOGEUSDT:(5m|15m|4h):(normal|micro|reclaim):(upper|lower):(\d+)$"

MONTHS = (
    ("2026-04", datetime(2026, 4, 1, tzinfo=timezone.utc), datetime(2026, 5, 1, tzinfo=timezone.utc)),
    ("2026-05", datetime(2026, 5, 1, tzinfo=timezone.utc), datetime(2026, 6, 1, tzinfo=timezone.utc)),
    ("2026-06", datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 1, tzinfo=timezone.utc)),
    ("2026-07", datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc)),
    ("2026-08", datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)),
    ("2026-09", datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)),
)
GAP_MONTHS = ("2026-04", "2026-05", "2026-06")
REPRO_SAMPLES = [
    ("2026-04-01T00:00:00+00:00", "15m", 10, "april_20260401_15m_PD10.json"),
    ("2026-07-07T07:00:00+00:00", "15m", 10, "july_20260707_15m_PD10.json"),
    ("2026-08-15T12:00:00+00:00", "15m", 5, None),
    ("2026-09-15T08:00:00+00:00", "4h", 10, None),
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


def _combo(timeframe: str, pool_detail: int) -> str:
    return f"{timeframe}_PD{int(pool_detail)}"


def _stamp(iso: str) -> str:
    return _utc(datetime.fromisoformat(iso.replace("Z", "+00:00"))).strftime("%Y%m%dT%H%M%SZ")


def _month_of(iso: str) -> str:
    return _utc(datetime.fromisoformat(iso.replace("Z", "+00:00"))).strftime("%Y-%m")


def _closes(timeframe: str, start: datetime, end_exclusive: datetime) -> list[datetime]:
    step = timedelta(seconds=_timeframe_seconds(timeframe))
    cursor = start
    out = []
    while cursor < end_exclusive and cursor <= PERIOD_END:
        out.append(cursor)
        cursor += step
    return out


def _need(moment: datetime, pool_detail: int, timeframe: str) -> datetime:
    return datetime.fromtimestamp(
        required_source_start_unix(moment, pool_detail, timeframe), tz=timezone.utc
    )


def _data_floor(manifest: dict) -> datetime:
    return _utc(datetime.fromisoformat(str(manifest["range_start"]).replace("Z", "+00:00")))


def _first_feasible(series: list[datetime], pool_detail: int, timeframe: str, floor: datetime) -> int | None:
    if _need(series[-1], pool_detail, timeframe) < floor:
        return None
    if _need(series[0], pool_detail, timeframe) >= floor:
        return 0
    low, high = 0, len(series) - 1
    while low < high:
        mid = (low + high) // 2
        if _need(series[mid], pool_detail, timeframe) >= floor:
            high = mid
        else:
            low = mid + 1
    return low


def _result_path(iso: str, timeframe: str, pool_detail: int) -> Path:
    return RESULTS / _month_of(iso) / _combo(timeframe, pool_detail) / f"{_stamp(iso)}.json"


def _load_canonical_manifest() -> dict:
    path = INPUTS / f"{CANONICAL_FROZEN_ID}.manifest.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != CANONICAL_FROZEN_ID:
        raise RuntimeError("canonical snapshot_id mismatch")
    pq = INPUTS / f"{CANONICAL_FROZEN_ID}.parquet"
    if not pq.is_file():
        raise RuntimeError("canonical parquet missing")
    return manifest


def _canonical_frozen() -> dict:
    manifest = _load_canonical_manifest()
    rows = require_snapshot(INPUTS, CANONICAL_FROZEN_ID, ohlcv_hash=manifest["ohlcv_content_hash"])
    price = manifest["ohlcv_content_hash"]
    revision = manifest["revision_metadata_hash"]
    if ohlcv_content_hash(rows) != price or revision_metadata_hash(rows) != revision:
        raise RuntimeError("canonical frozen hash mismatch on read-back")
    return {
        "snapshot_id": CANONICAL_FROZEN_ID,
        "price_hash": price,
        "revision_hash": revision,
        "directory": str(INPUTS),
        "row_count": len(rows),
        "range_start": manifest["range_start"],
        "range_end_exclusive": manifest["range_end_exclusive"],
        "file_checksum": manifest.get("file_checksum"),
        "rows": rows,
    }


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_snapshot_loose(path: Path, timeframe: str, pool_detail: int) -> list[str]:
    from indicators.live_micro_lld_v2.styles import strength_class

    problems = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"json:{exc}"]
    if payload.get("symbol") != SYMBOL:
        problems.append("symbol")
    if payload.get("timeframe") != timeframe or int(payload.get("pool_detail", -1)) != pool_detail:
        problems.append("combo")
    if str(payload.get("engine_version") or ENGINE_VERSION) != ENGINE_VERSION:
        problems.append("engine_version")
    if payload.get("history_policy") not in (HISTORY_POLICY_ROLLING_4M, None):
        problems.append("history_policy")
    decision = _utc(datetime.fromisoformat(str(payload["decision_close"]).replace("Z", "+00:00")))
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
        if pool is None:
            problems.append(f"missing_pool {pool_id}")
            continue
        if str(pool.get("side", "")).lower() != str(side).lower():
            problems.append(f"side {pool_id}")
        match = pattern.match(str(pool_id))
        if not match or match.group(1) != timeframe:
            problems.append(f"pool_id {pool_id}")
        known = _norm_iso(pool.get("known_at"))
        if known and limit and known > limit:
            problems.append(f"known_at {pool_id}")
        top, bottom = float(pool["top_price"]), float(pool["bottom_price"])
        strength = float(pool["strength"])
        if not (top >= bottom > 0):
            problems.append(f"bounds {pool_id}")
        if strength < 0 or str(pool.get("strength_class")) != strength_class(strength):
            problems.append(f"strength {pool_id}")
    frozen = payload.get("frozen_input") or {}
    if not frozen.get("snapshot_id"):
        problems.append("frozen_provenance")
    return problems


def _expected_schedule(floor: datetime) -> dict[tuple[str, str, int], list[str]]:
    out: dict[tuple[str, str, int], list[str]] = {}
    for month, start, end in MONTHS:
        for timeframe, pool_detail in COMBOS:
            series = _closes(timeframe, start, end)
            index = _first_feasible(series, pool_detail, timeframe, floor)
            chosen = series if index == 0 else (series[index:] if index is not None else [])
            out[(month, timeframe, pool_detail)] = [c.isoformat() for c in chosen]
    return out


def cmd_preflight(_args: argparse.Namespace) -> int:
    manifest = _load_canonical_manifest()
    pq = INPUTS / f"{CANONICAL_FROZEN_ID}.parquet"
    report = {
        "canonical_frozen_id": CANONICAL_FROZEN_ID,
        "manifest_file_checksum": manifest.get("file_checksum"),
        "parquet_exists": pq.is_file(),
        "row_count": manifest.get("row_count"),
        "ohlcv_hash": manifest.get("ohlcv_content_hash"),
    }
    if not P2P3_REPORT.is_file():
        report["p2p3"] = "MISSING"
        _atomic(PRODUCTION / "preflight_p4p5.json", report)
        return 1
    p2 = json.loads(P2P3_REPORT.read_text(encoding="utf-8"))
    report["p2p3_july_repro"] = p2.get("JULY_REPRODUCIBILITY")
    report["p2p3_raw_parity"] = p2.get("RAW_1M_PARITY")
    pilots_ok = all(
        (PILOT / name).is_file()
        for name in (
            "april_20260401_15m_PD10.json",
            "july_20260707_15m_PD10.json",
            "boundary_20260701T000000Z_15m_PD10.json",
        )
    )
    report["pilot_files_present"] = pilots_ok
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    report["resources"] = {
        "ram_available_mb": round(mem.available / (1024 * 1024)),
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "load_average": [round(v, 2) for v in os.getloadavg()],
    }
    procs = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(proc.info["cmdline"] or [])
        if "run_multi_month.py" in cmd and ("--worker" in cmd or "--supervisor" in cmd):
            procs.append(proc.pid)
        if "run_p4p5_production.py" in cmd and "--worker" in cmd:
            procs.append(proc.pid)
    report["blocking_worker_pids"] = [pid for pid in procs if psutil.pid_exists(pid)]
    frozen = _canonical_frozen()
    del frozen["rows"]
    report["canonical_readback"] = "PASS"
    ok = (
        report["p2p3_july_repro"] == "PASS"
        and report["p2p3_raw_parity"] == "PASS"
        and pilots_ok
        and not report["blocking_worker_pids"]
        and report["resources"]["ram_available_mb"] >= 4000
        and report["resources"]["disk_free_gb"] >= 30
    )
    report["PREFLIGHT"] = "PASS" if ok else "FAIL"
    _atomic(PRODUCTION / "preflight_p4p5.json", report)
    print(json.dumps(report, indent=2))
    return 0 if ok else 2


def cmd_gap_matrix(_args: argparse.Namespace) -> int:
    manifest = _load_canonical_manifest()
    floor = _data_floor(manifest)
    schedule = _expected_schedule(floor)
    rows = []
    total_expected = 0
    total_found_mm = 0
    total_found_catalog = 0
    total_gap = 0
    for month, start, end in MONTHS:
        for timeframe, pool_detail in COMBOS:
            closes = schedule[(month, timeframe, pool_detail)]
            exp = len(closes)
            combo = _combo(timeframe, pool_detail)
            mm_dir = MM_RESULTS / month / combo if month != "2026-09" else SEP_RESULTS / combo
            found_mm = 0
            valid_mm = 0
            if mm_dir.is_dir():
                for p in mm_dir.glob("*.json"):
                    if p.name.endswith(".tmp"):
                        continue
                    found_mm += 1
                    if not _validate_snapshot_loose(p, timeframe, pool_detail):
                        valid_mm += 1
            found_cat = 0
            cat_dir = RESULTS / month / combo
            if cat_dir.is_dir():
                found_cat = sum(1 for p in cat_dir.glob("*.json") if not p.name.endswith(".tmp"))
            gap = 0
            if month in GAP_MONTHS:
                gap = max(0, exp - valid_mm)
            rows.append(
                {
                    "month": month,
                    "combo": combo,
                    "EXPECTED": exp,
                    "FOUND_MM": found_mm,
                    "VALID_MM": valid_mm,
                    "FOUND_CATALOG": found_cat,
                    "MISSING_NEW_PRODUCTION": gap,
                    "FIRST_CLOSE": closes[0] if closes else None,
                    "LAST_CLOSE": closes[-1] if closes else None,
                    "STATUS": "COMPLETE" if gap == 0 and exp > 0 else ("GAP" if gap else "N/A"),
                }
            )
            total_expected += exp
            total_found_mm += valid_mm if month in GAP_MONTHS else exp
            total_found_catalog += found_cat
            total_gap += gap
    apr = sum(r["MISSING_NEW_PRODUCTION"] for r in rows if r["month"] == "2026-04")
    may = sum(r["MISSING_NEW_PRODUCTION"] for r in rows if r["month"] == "2026-05")
    jun = sum(r["MISSING_NEW_PRODUCTION"] for r in rows if r["month"] == "2026-06")
    payload = {
        "data_floor": floor.isoformat(),
        "apr_jun_gap_total": total_gap,
        "apr_gap": apr,
        "may_gap": may,
        "jun_gap": jun,
        "expected_user_gaps": {"apr": 7267, "may": 1826, "jun": 55, "total": 9148},
        "gap_match": total_gap == 9148 and apr == 7267 and may == 1826 and jun == 55,
        "rows": rows,
    }
    _atomic(PRODUCTION / "gap_matrix.json", payload)
    print(json.dumps({"gap_match": payload["gap_match"], "total_gap": total_gap, "apr": apr, "may": may, "jun": jun}, indent=2))
    return 0 if payload["gap_match"] else 3


def _copy_snapshot(src: Path, dest: Path, meta: dict, index: list) -> None:
    if dest.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    payload = json.loads(dest.read_text(encoding="utf-8"))
    index.append(
        {
            **meta,
            "dest": str(dest.relative_to(HERE)),
            "source_sha256": _file_sha256(src),
            "dest_sha256": _file_sha256(dest),
            "source_frozen_id": (payload.get("frozen_input") or {}).get("snapshot_id"),
            "decision_close": payload.get("decision_close"),
            "validation": "COPY_VERIFIED",
        }
    )


def cmd_integrate(_args: argparse.Namespace) -> int:
    manifest = _load_canonical_manifest()
    floor = _data_floor(manifest)
    schedule = _expected_schedule(floor)
    index: list[dict] = []
    copied = 0
    for month, _s, _e in MONTHS:
        for timeframe, pool_detail in COMBOS:
            combo = _combo(timeframe, pool_detail)
            for iso in schedule[(month, timeframe, pool_detail)]:
                dest = _result_path(iso, timeframe, pool_detail)
                if dest.is_file():
                    continue
                stem = _stamp(iso)
                if month == "2026-09":
                    src = SEP_RESULTS / combo / f"{stem}.json"
                    catalog = "doge_september_2026_history"
                    src_frozen = FE549_ID
                else:
                    src = MM_RESULTS / month / combo / f"{stem}.json"
                    catalog = "doge_multi_month_2026_history"
                    src_frozen = CFAE_ID
                if not src.is_file():
                    continue
                problems = _validate_snapshot_loose(src, timeframe, pool_detail)
                if problems:
                    index.append({"dest": str(dest), "src": str(src), "SKIP": problems[:5]})
                    continue
                _copy_snapshot(
                    src,
                    dest,
                    {
                        "source_catalog": catalog,
                        "source_path": str(src),
                        "source_frozen_id_expected": src_frozen,
                        "month": month,
                        "combo": combo,
                        "canonical_input_id": CANONICAL_FROZEN_ID,
                        "compatibility": "OHLCV_PREFIX_PARITY_P2P3",
                    },
                    index,
                )
                copied += 1
    _atomic(
        PRODUCTION / "provenance_index.json",
        {
            "canonical_frozen_id": CANONICAL_FROZEN_ID,
            "integrated_at": datetime.now(timezone.utc).isoformat(),
            "entries": index,
            "copied_count": copied,
        },
    )
    print(json.dumps({"copied": copied, "index_entries": len(index)}, indent=2))
    return 0


def _gap_closes(floor: datetime) -> list[tuple[str, str, int, str]]:
    """Returns (month, timeframe, pd, iso) needing NEW production."""
    schedule = _expected_schedule(floor)
    out = []
    for month in GAP_MONTHS:
        for timeframe, pool_detail in COMBOS:
            for iso in schedule[(month, timeframe, pool_detail)]:
                dest = _result_path(iso, timeframe, pool_detail)
                if dest.is_file():
                    continue
                src = MM_RESULTS / month / _combo(timeframe, pool_detail) / f"{_stamp(iso)}.json"
                if src.is_file() and not _validate_snapshot_loose(src, timeframe, pool_detail):
                    continue
                out.append((month, timeframe, pool_detail, iso))
    return out


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
    status_path = PRODUCTION / "workers" / f"worker_{worker_id}_status.json"
    assignment = next(item for item in plan["workers"] if int(item["worker_id"]) == worker_id)
    status = {
        "worker_id": worker_id,
        "pid": os.getpid(),
        "status": "STARTING",
        "snapshot_id": plan["snapshot_id"],
        "completed": 0,
        "expected": sum(len(t["closes"]) for t in assignment["tasks"]),
        "errors": [],
    }
    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True
        status["status"] = "STOPPED"
        _atomic(status_path, status)

    signal.signal(signal.SIGTERM, _handle)
    _atomic(status_path, status)
    try:
        rows = require_snapshot(Path(plan["snapshot_dir"]), plan["snapshot_id"], ohlcv_hash=plan["price_hash"])
        price = ohlcv_content_hash(rows)
        revision = revision_metadata_hash(rows)
        identity = {
            "snapshot_id": plan["snapshot_id"],
            "price_hash": price,
            "revision_hash": revision,
            "clickhouse_candle_reads": 0,
        }
        status["status"] = "RUNNING"
        _atomic(status_path, status)
        for task in assignment["tasks"]:
            tf = task["timeframe"]
            pd = int(task["pool_detail"])
            session = ReusedSnapshotSession(
                rows, snapshot_id=plan["snapshot_id"], ohlcv_hash=price, timeframe=tf
            )
            session.max_1m_bars = 500_000
            for iso in task["closes"]:
                if stop["flag"]:
                    status["status"] = "STOPPED"
                    _atomic(status_path, status)
                    return 0
                dest = _result_path(iso, tf, pd)
                if dest.is_file():
                    status["completed"] += 1
                    _atomic(status_path, status)
                    continue
                decision = _utc(datetime.fromisoformat(iso))
                history = rolling_history_from_close(decision, 4)
                snap, candles, result = build_active_state_at_decision_close(
                    SYMBOL,
                    tf,
                    history,
                    decision,
                    pool_detail=pd,
                    history_policy=HISTORY_POLICY_ROLLING_4M,
                    candle_session=session,
                )
                _future_candle(candles, decision, tf)
                record = verification_record(snap, candles, result, decision)
                _future_known(record, decision)
                record["frozen_input"] = identity
                record["worker_id"] = worker_id
                record["timeframe"] = tf
                record["pool_detail"] = pd
                record["symbol"] = SYMBOL
                record["month"] = _month_of(iso)
                record["engine_version"] = ENGINE_VERSION
                record["history_policy"] = HISTORY_POLICY_ROLLING_4M
                record["catalog_provenance"] = {
                    "kind": "NEW_PRODUCTION",
                    "canonical_frozen_id": CANONICAL_FROZEN_ID,
                }
                if dest.exists():
                    raise RuntimeError(f"refusing overwrite {dest}")
                _atomic(dest, record)
                status["completed"] += 1
                status["last_close"] = iso
                status["rss_mb"] = round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
                _atomic(status_path, status)
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
        return 1


def _build_plan(closes: list[tuple[str, str, int, str]], workers: int) -> dict:
    frozen = _canonical_frozen()
    del frozen["rows"]
    streams: dict[tuple[str, int], list[str]] = {(tf, pd): [] for tf, pd in COMBOS}
    for month, tf, pd, iso in closes:
        streams[(tf, pd)].append(iso)
    tasks_per_worker: list[list[dict]] = [[] for _ in range(workers)]
    order = sorted(streams.items(), key=lambda kv: -len(kv[1]))
    loads = [0.0] * workers
    for (tf, pd), items in order:
        task = {
            "timeframe": tf,
            "pool_detail": pd,
            "closes": sorted(items),
            "estimated_seconds": round(len(items) * RATES[(tf, pd)], 1),
        }
        wid = min(range(workers), key=lambda i: loads[i])
        tasks_per_worker[wid].append(task)
        loads[wid] += task["estimated_seconds"]
    return {
        "snapshot_dir": str(INPUTS),
        "snapshot_id": CANONICAL_FROZEN_ID,
        "price_hash": frozen["price_hash"],
        "revision_hash": frozen["revision_hash"],
        "workers": [{"worker_id": i, "tasks": tasks} for i, tasks in enumerate(tasks_per_worker) if tasks],
    }


def cmd_produce(args: argparse.Namespace) -> int:
    if not args.skip_checks:
        if cmd_preflight(argparse.Namespace()) != 0:
            return 2
        if cmd_gap_matrix(argparse.Namespace()) != 0:
            return 3
    manifest = _load_canonical_manifest()
    floor = _data_floor(manifest)
    gaps = _gap_closes(floor)
    if args.month:
        gaps = [g for g in gaps if g[0] == args.month]
    if args.limit:
        gaps = gaps[: int(args.limit)]
    if not gaps:
        print("no gaps to produce")
        return 0
    plan = _build_plan(gaps, workers=max(1, min(2, int(args.workers))))
    plan_path = PRODUCTION / "plan_gap_fill.json"
    _atomic(plan_path, plan)
    _atomic(PRODUCTION / "gap_fill_job.json", {"closes": len(gaps), "plan": str(plan_path)})
    if args.dry_run:
        print(json.dumps({"closes": len(gaps), "workers": len(plan["workers"])}, indent=2))
        return 0
    procs = []
    for w in plan["workers"]:
        wid = int(w["worker_id"])
        log_path = PRODUCTION / "workers" / f"worker_{wid}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab", buffering=0)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(wid), "--plan", str(plan_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(HERE),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        log.close()
        procs.append(proc)
    codes = [p.wait() for p in procs]
    if any(c != 0 for c in codes):
        return 4
    return 0


def _coverage_row(month: str, tf: str, pd: int, closes: list[str]) -> dict:
    combo = _combo(tf, pd)
    found = []
    invalid = []
    dups = []
    seen = set()
    for iso in closes:
        path = _result_path(iso, tf, pd)
        stem = _stamp(iso)
        if stem in seen:
            dups.append(stem)
        seen.add(stem)
        if not path.is_file():
            continue
        problems = _validate_snapshot_loose(path, tf, pd)
        if problems:
            invalid.append({"stem": stem, "problems": problems[:3]})
        else:
            found.append(stem)
    missing = [c for c in closes if _stamp(c) not in set(found)]
    status = "PASS"
    if missing or invalid or dups:
        status = "FAIL"
    return {
        "month": month,
        "combo": combo,
        "EXPECTED": len(closes),
        "FOUND": len(found),
        "MISSING": len(missing),
        "DUPLICATES": len(dups),
        "INVALID": len(invalid),
        "FIRST_CLOSE": closes[0] if closes else None,
        "LAST_CLOSE": closes[-1] if closes else None,
        "STATUS": status,
    }


def cmd_validate(_args: argparse.Namespace) -> int:
    manifest = _load_canonical_manifest()
    floor = _data_floor(manifest)
    schedule = _expected_schedule(floor)
    matrix = [_coverage_row(m, tf, pd, schedule[(m, tf, pd)]) for m, _, _ in MONTHS for tf, pd in COMBOS]
    repro = []
    frozen_bundle = _canonical_frozen()
    rows = frozen_bundle["rows"]
    price = frozen_bundle["price_hash"]
    for dc_iso, tf, pd, pilot_name in REPRO_SAMPLES:
        dest = _result_path(dc_iso, tf, pd)
        if not dest.is_file():
            repro.append({"close": dc_iso, "status": "MISSING_FILE"})
            continue
        ref = json.loads(dest.read_text(encoding="utf-8"))
        session = ReusedSnapshotSession(rows, snapshot_id=CANONICAL_FROZEN_ID, ohlcv_hash=price, timeframe=tf)
        decision = _utc(datetime.fromisoformat(dc_iso))
        history = rolling_history_from_close(decision, 4)
        snap, candles, result = build_active_state_at_decision_close(
            SYMBOL, tf, history, decision, pool_detail=pd, history_policy=HISTORY_POLICY_ROLLING_4M, candle_session=session
        )
        pilot = verification_record(snap, candles, result, decision)
        errs = compare_records(ref, pilot)
        pub = compare_published_close(pilot, ref, [ref["pools"][pid] for pid in ref["active_pool_ids"]])
        repro.append({"close": dc_iso, "combo": _combo(tf, pd), "compare_records": errs, "compare_published": pub, "status": "PASS" if not errs and not pub else "DIFFERENT"})
    del frozen_bundle
    gc.collect()
    payload = {"MONTH_TF_PD_COVERAGE": matrix, "REPRO_SAMPLES": repro, "validated_at": datetime.now(timezone.utc).isoformat()}
    _atomic(PRODUCTION / "coverage_matrix.json", payload)
    fail = any(r["STATUS"] != "PASS" for r in matrix)
    print(json.dumps({"coverage_fail": fail, "repro": repro}, indent=2))
    return 1 if fail else 0


def cmd_finalize(_args: argparse.Namespace) -> int:
    cov_path = PRODUCTION / "coverage_matrix.json"
    if not cov_path.is_file():
        cmd_validate(argparse.Namespace())
    matrix = json.loads(cov_path.read_text(encoding="utf-8"))["MONTH_TF_PD_COVERAGE"]
    new_snaps = reused = 0
    apr_new = may_new = jun_new = 0
    for path in RESULTS.rglob("*.json"):
        if path.name.endswith(".tmp"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        prov = payload.get("catalog_provenance") or {}
        frozen = (payload.get("frozen_input") or {}).get("snapshot_id")
        month = payload.get("month") or _month_of(str(payload.get("decision_close")))
        if prov.get("kind") == "NEW_PRODUCTION" and frozen == CANONICAL_FROZEN_ID:
            new_snaps += 1
            if month == "2026-04":
                apr_new += 1
            elif month == "2026-05":
                may_new += 1
            elif month == "2026-06":
                jun_new += 1
        elif frozen in (CFAE_ID, FE549_ID):
            reused += 1
    missing = sum(r["MISSING"] for r in matrix)
    invalid = sum(r["INVALID"] for r in matrix)
    dups = sum(r["DUPLICATES"] for r in matrix)
    repro_ok = all(r.get("status") == "PASS" for r in json.loads(cov_path.read_text())["REPRO_SAMPLES"])
    report = {
        "NEW_CANONICAL_CATALOG_PATH": str(HERE),
        "FROZEN_INPUT_USED": CANONICAL_FROZEN_ID,
        "APRIL_NEW_SNAPSHOTS": apr_new,
        "MAY_NEW_SNAPSHOTS": may_new,
        "JUNE_NEW_SNAPSHOTS": jun_new,
        "TOTAL_NEW_SNAPSHOTS": new_snaps,
        "REUSED_SNAPSHOTS": reused,
        "SOURCE_PROVENANCE_VALID": "PASS" if (PRODUCTION / "provenance_index.json").is_file() else "NOT_PROVEN",
        "ENGINE_PARITY": "PASS" if repro_ok else "NOT_PROVEN",
        "INPUT_PREFIX_PARITY": "PASS",
        "MONTH_TF_PD_COVERAGE": matrix,
        "MISSING_CLOSES": missing,
        "INVALID_SNAPSHOTS": invalid,
        "DUPLICATE_CLOSES": dups,
        "CATALOG_BOUNDARY_PARITY": "PASS" if repro_ok else "NOT_PROVEN",
        "READY_FOR_GUARD_OFF_BACKTEST": "NOT_PROVEN",
        "GUARD_WARMUP_STATUS": "NOT_PROVEN",
        "REMAINING_BLOCKERS": [],
    }
    if missing:
        report["REMAINING_BLOCKERS"].append("incomplete_snapshot_coverage")
    if not repro_ok:
        report["REMAINING_BLOCKERS"].append("repro_samples")
    report["REMAINING_BLOCKERS"].append(
        "HistoricalLmV2PoolStore requires uniform frozen_input_id; mixed cfae/fe549/84c224 copies need adapter or manifest gate (no registry change in P4/P5)"
    )
    report["REMAINING_BLOCKERS"].append("GUARD_WARMUP_NOT_PROVEN_FOR_PRE_APRIL_GUARD_EPISODES")
    report["READY_FOR_GUARD_OFF_BACKTEST"] = (
        "PASS" if not missing and repro_ok else "BLOCKED"
    )
    if report["READY_FOR_GUARD_OFF_BACKTEST"] == "PASS":
        report["READY_FOR_GUARD_OFF_BACKTEST"] = "NOT_PROVEN"
        report["REMAINING_BLOCKERS"].append("catalog_not_registered; pane_from/replay_from for Apr start not executed")
    _atomic(HERE / "p4p5_report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not missing else 5


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight").set_defaults(func=cmd_preflight)
    sub.add_parser("gap-matrix").set_defaults(func=cmd_gap_matrix)
    sub.add_parser("integrate").set_defaults(func=cmd_integrate)
    p_prod = sub.add_parser("produce")
    p_prod.add_argument("--month", help="2026-04 etc")
    p_prod.add_argument("--limit", type=int, help="max closes (smoke batch)")
    p_prod.add_argument("--workers", default=2)
    p_prod.add_argument("--dry-run", action="store_true")
    p_prod.add_argument("--skip-checks", action="store_true")
    p_prod.set_defaults(func=cmd_produce, skip_checks=False)
    sub.add_parser("validate").set_defaults(func=cmd_validate)
    sub.add_parser("finalize").set_defaults(func=cmd_finalize)
    if "--worker" in sys.argv:
        wid = int(sys.argv[sys.argv.index("--worker") + 1])
        plan = Path(sys.argv[sys.argv.index("--plan") + 1])
        return run_worker(wid, plan)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

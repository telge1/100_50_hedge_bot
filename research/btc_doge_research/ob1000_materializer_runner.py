"""Continuous OB1000 FS→CH materializer (fail-closed, gap-aware).

Keeps ``research_ob1000_snapshots_1s`` caught up with the live ``ob1000_v1``
archive for BTCUSDT/DOGEUSDT. Idempotent; safe with open ``*.zst.tmp`` segments.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

from .atomic_json import atomic_write_json
from .clickhouse import connect, rows
from .config import OB1000_ROOT, REPO_ROOT
from .contracts import TARGET_DATABASE, sanitize_json, utc
from .ob1000_live_symbols import partition_ob1000_live_symbols
from .ob1000_materializer import materialize_all
from .ob1000_storage import OB1000_PRODUCER_ID, OB1000_TABLE

RUN_DIR = REPO_ROOT / "run" / "ob1000_materializer"
LOG_PATH = REPO_ROOT / "logs" / "ob1000_materializer.log"
DEFAULT_INTERVAL_SEC = 20
DEFAULT_WARN_LAG_SEC = 90


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    return utc(value or _utc_now()).isoformat().replace("+00:00", "Z")


def ensure_run_dirs() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def heartbeat_path() -> Path:
    return RUN_DIR / "heartbeat.json"


def progress_path() -> Path:
    return RUN_DIR / "progress.json"


def completed_path() -> Path:
    return RUN_DIR / "completed_closed.json"


def runner_lock_path() -> Path:
    return RUN_DIR / "runner.lock"


def runner_pid_path() -> Path:
    return RUN_DIR / "runner.pid"


def write_heartbeat(payload: dict[str, Any]) -> None:
    body = {"updated_at": _utc_iso(), **sanitize_json(payload)}
    atomic_write_json(heartbeat_path(), body)


def write_progress(payload: dict[str, Any]) -> None:
    body = {"updated_at": _utc_iso(), **sanitize_json(payload)}
    atomic_write_json(progress_path(), body)


def load_completed() -> dict[str, str]:
    path = completed_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    completed = raw.get("completed") if isinstance(raw, dict) else None
    if not isinstance(completed, dict):
        return {}
    return {str(k): str(v) for k, v in completed.items()}


def save_completed(completed: dict[str, str]) -> None:
    atomic_write_json(
        completed_path(),
        {"updated_at": _utc_iso(), "completed": completed},
    )


def _ch_max_snapshot(client: Any, symbol: str) -> datetime | None:
    sql = f"""
        SELECT max(snapshot_ts)
        FROM {TARGET_DATABASE}.{OB1000_TABLE}
        WHERE symbol = %(symbol)s
          AND producer_id = %(producer)s
    """
    found = rows(client, sql, {"symbol": symbol, "producer": OB1000_PRODUCER_ID})
    if not found or found[0][0] is None:
        return None
    value = found[0][0]
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return utc(value)


def _fs_open_last_event_ts(root: Path, symbol: str) -> datetime | None:
    """Latest event timestamp from the symbol's open OB1000 segment (if any)."""
    from .ob200_parser import iter_json_records

    best: datetime | None = None
    root = Path(root)
    if not root.is_dir():
        return None
    for path in root.rglob(f"{symbol}_*_open_ob1000_v1.zst.tmp"):
        if not path.is_file():
            continue
        last: datetime | None = None
        try:
            for _record, obj in iter_json_records(path):
                ts = obj.get("ts")
                if isinstance(ts, int):
                    last = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        except OSError:
            continue
        if last is not None and (best is None or last > best):
            best = last
    return best


def coverage_lag(
    client: Any,
    symbols: tuple[str, ...],
    *,
    root: Path = OB1000_ROOT,
) -> dict[str, Any]:
    """Materializer lag = FS open tip − CH max (not wall-clock quiet-market lag)."""
    now = _utc_now().replace(microsecond=0)
    per_symbol: dict[str, Any] = {}
    worst = 0
    for symbol in symbols:
        ch_max = _ch_max_snapshot(client, symbol)
        fs_last = _fs_open_last_event_ts(root, symbol)
        if ch_max is None:
            lag = None
            status = "NO_DATA"
        elif fs_last is None:
            # No open segment: fall back to wall-clock freshness.
            lag = max(0, int((now - ch_max).total_seconds()) - 1)
            status = "OK" if lag <= DEFAULT_WARN_LAG_SEC else "LAG_WARN"
            worst = max(worst, lag or 0)
        else:
            lag = max(0, int((fs_last.replace(microsecond=0) - ch_max).total_seconds()))
            status = "OK" if lag <= DEFAULT_WARN_LAG_SEC else "LAG_WARN"
            worst = max(worst, lag)
        per_symbol[symbol] = {
            "ch_max_snapshot_ts": None if ch_max is None else _utc_iso(ch_max),
            "fs_open_last_event_ts": None if fs_last is None else _utc_iso(fs_last),
            "lag_seconds": lag,
            "status": status,
        }
    overall = "OK"
    if any(v["status"] == "NO_DATA" for v in per_symbol.values()):
        overall = "NO_DATA"
    elif any(v["status"] == "LAG_WARN" for v in per_symbol.values()):
        overall = "LAG_WARN"
    return {
        "overall": overall,
        "worst_lag_seconds": worst,
        "symbols": per_symbol,
        "checked_at": _utc_iso(now),
    }


class _StopFlag:
    def __init__(self) -> None:
        self.stop = False

    def request_stop(self, *_args: Any) -> None:
        self.stop = True


def _acquire_lock() -> IO[str]:
    ensure_run_dirs()
    handle = runner_lock_path().open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("ob1000 materializer already running (lock held)") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    runner_pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
    return handle


def run_once(
    *,
    symbols: tuple[str, ...],
    root: Path,
    include_open: bool = True,
    skip_completed_closed: bool = True,
) -> dict[str, Any]:
    completed = load_completed() if skip_completed_closed else {}
    out = materialize_all(
        symbols=symbols,
        root=root,
        include_open=include_open,
        skip_completed=completed,
    )
    # Persist newly completed closed segments (fingerprint → relative_path).
    if skip_completed_closed:
        changed = False
        for row in out.get("results", []):
            if row.get("is_open"):
                continue
            if not row.get("complete"):
                continue
            rel = str(row["relative_path"])
            fp = str(row["source_fingerprint"])
            if completed.get(rel) != fp:
                completed[rel] = fp
                changed = True
        if changed:
            save_completed(completed)
    client = connect()
    lag = coverage_lag(client, symbols)
    return {**out, "lag": lag}


def run_loop(
    *,
    symbols: tuple[str, ...],
    root: Path,
    interval_sec: float,
    warn_lag_sec: int = DEFAULT_WARN_LAG_SEC,
) -> int:
    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)
    lock = _acquire_lock()
    cycle = 0
    write_heartbeat(
        {
            "status": "STARTING",
            "runner_pid": os.getpid(),
            "symbols": list(symbols),
            "root": str(root),
            "interval_sec": interval_sec,
            "warn_lag_sec": warn_lag_sec,
        }
    )
    try:
        while not stop.stop:
            cycle += 1
            t0 = time.monotonic()
            try:
                out = run_once(symbols=symbols, root=root, include_open=True)
                lag = out["lag"]
                status = "RUNNING"
                if lag["overall"] == "LAG_WARN":
                    status = "LAG_WARN"
                elif lag["overall"] == "NO_DATA":
                    status = "NO_DATA"
                payload = {
                    "status": status,
                    "runner_pid": os.getpid(),
                    "cycle": cycle,
                    "rows_inserted": out["rows_inserted"],
                    "segments": out["segments"],
                    "elapsed_sec": round(time.monotonic() - t0, 3),
                    "lag": lag,
                    "warn_lag_sec": warn_lag_sec,
                }
                write_heartbeat(payload)
                write_progress(
                    {
                        "cycle": cycle,
                        "rows_inserted": out["rows_inserted"],
                        "results": out["results"],
                        "lag": lag,
                    }
                )
                print(
                    f"ob1000_materializer cycle={cycle} inserted={out['rows_inserted']} "
                    f"segments={out['segments']} lag={lag['worst_lag_seconds']}s "
                    f"status={status} elapsed={payload['elapsed_sec']}s",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 — keep loop alive; fail-visible
                write_heartbeat(
                    {
                        "status": "ERROR",
                        "runner_pid": os.getpid(),
                        "cycle": cycle,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"ob1000_materializer ERROR cycle={cycle}: {exc}", flush=True)
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, float(interval_sec) - elapsed)
            end_sleep = time.monotonic() + sleep_for
            while not stop.stop and time.monotonic() < end_sleep:
                time.sleep(min(0.5, end_sleep - time.monotonic()))
        write_heartbeat({"status": "STOPPED", "runner_pid": os.getpid(), "cycle": cycle})
        return 0
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,DOGEUSDT")
    parser.add_argument("--root", default=str(OB1000_ROOT))
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval-sec", type=float, default=DEFAULT_INTERVAL_SEC)
    parser.add_argument("--warn-lag-sec", type=int, default=DEFAULT_WARN_LAG_SEC)
    parser.add_argument("--once", action="store_true", help="Single pass (default)")
    parser.add_argument("--status", action="store_true", help="Print heartbeat + lag")
    args = parser.parse_args(argv)
    raw_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    symbols, rejected = partition_ob1000_live_symbols(raw_symbols)
    for bad, err in rejected:
        # Explicit isolation: never silent skip; continue with accepted when any remain.
        print(f"ob1000_materializer REJECT symbol={bad}: {err}", flush=True)
    if not symbols:
        raise SystemExit(
            "ob1000_materializer: no valid OB1000 live symbols after validation "
            f"(input={raw_symbols!r}, rejected={rejected!r})"
        )
    if rejected:
        print(
            f"ob1000_materializer continuing with accepted symbols={list(symbols)} "
            f"(rejected={len(rejected)})",
            flush=True,
        )
    root = Path(args.root)
    ensure_run_dirs()

    if args.status:
        hb = {}
        if heartbeat_path().is_file():
            hb = json.loads(heartbeat_path().read_text(encoding="utf-8"))
        client = connect()
        lag = coverage_lag(client, symbols)
        print(json.dumps({"heartbeat": hb, "lag": lag}, indent=2, default=str))
        return 0

    if args.loop:
        return run_loop(
            symbols=symbols,
            root=root,
            interval_sec=max(5.0, float(args.interval_sec)),
            warn_lag_sec=int(args.warn_lag_sec),
        )

    out = run_once(symbols=symbols, root=root)
    print(
        f"ob1000_materialize segments={out['segments']} rows_inserted={out['rows_inserted']} "
        f"lag={out['lag']['worst_lag_seconds']}s status={out['lag']['overall']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

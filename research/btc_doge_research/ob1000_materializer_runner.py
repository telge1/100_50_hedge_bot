"""Continuous OB1000 FS→CH materializer (fail-closed, gap-aware).

Keeps ``research_ob1000_snapshots_1s`` caught up with the live ``ob1000_v1``
archive. Idempotent; safe with open ``*.zst.tmp`` segments.

Live loop design (multi-symbol safe):
- Round-robin small symbol batches so heartbeats advance under load.
- Open-only passes for the hot path (closed catch-up every N cycles).
- Skip unchanged open files via size/mtime watermarks.
- Coverage lag uses open-file mtime (no full re-parse of every tip).
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
from .ob1000_materializer import OPEN_RE, materialize_all
from .ob1000_storage import OB1000_PRODUCER_ID, OB1000_TABLE

RUN_DIR = REPO_ROOT / "run" / "ob1000_materializer"
LOG_PATH = REPO_ROOT / "logs" / "ob1000_materializer.log"
DEFAULT_INTERVAL_SEC = 20
DEFAULT_WARN_LAG_SEC = 90
DEFAULT_BATCH_SIZE = 8
DEFAULT_CLOSED_EVERY_N = 15
DEFAULT_MAX_NEW_SECONDS_OPEN = 120

# Optional shard overlay (set by systemd template instances).
_SHARD_ID: int | None = None
_SHARD_COUNT: int | None = None


def configure_shard(shard_id: int | None, shard_count: int | None) -> None:
    """Isolate run/lock/heartbeat paths per parallel shard worker."""
    global RUN_DIR, LOG_PATH, _SHARD_ID, _SHARD_COUNT
    _SHARD_ID = shard_id
    _SHARD_COUNT = shard_count
    if shard_id is None or shard_count is None:
        RUN_DIR = REPO_ROOT / "run" / "ob1000_materializer"
        LOG_PATH = REPO_ROOT / "logs" / "ob1000_materializer.log"
    else:
        RUN_DIR = REPO_ROOT / "run" / "ob1000_materializer" / f"shard{int(shard_id)}"
        LOG_PATH = REPO_ROOT / "logs" / f"ob1000_materializer_shard{int(shard_id)}.log"


def shard_filter_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if _SHARD_ID is None or _SHARD_COUNT is None:
        return symbols
    shard_id = int(_SHARD_ID)
    shard_count = int(_SHARD_COUNT)
    if shard_count <= 1:
        return symbols
    if shard_id < 0 or shard_id >= shard_count:
        raise ValueError(f"invalid shard id={shard_id} count={shard_count}")
    return tuple(
        symbol
        for idx, symbol in enumerate(symbols)
        if idx % shard_count == shard_id
    )


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


def open_watermarks_path() -> Path:
    return RUN_DIR / "open_watermarks.json"


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


def load_open_watermarks() -> dict[str, dict[str, int]]:
    path = open_watermarks_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    body = raw.get("watermarks") if isinstance(raw, dict) else None
    if not isinstance(body, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for key, value in body.items():
        if not isinstance(value, dict):
            continue
        try:
            out[str(key)] = {
                "size": int(value["size"]),
                "mtime_ns": int(value["mtime_ns"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_open_watermarks(watermarks: dict[str, dict[str, int]]) -> None:
    atomic_write_json(
        open_watermarks_path(),
        {"updated_at": _utc_iso(), "watermarks": watermarks},
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


def _fs_open_mtime_ts(root: Path, symbol: str) -> datetime | None:
    """Cheap FS tip proxy: mtime of the symbol's open OB1000 segment (if any)."""
    root = Path(root)
    base = root / symbol
    if not base.is_dir():
        return None
    best: datetime | None = None
    for path in base.rglob(f"{symbol}_*_open_ob1000_v1.zst.tmp"):
        if not path.is_file():
            continue
        if not OPEN_RE.match(path.name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        # Brand-new / empty hour files would otherwise look infinitely lagged.
        if int(st.st_size) <= 0:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if best is None or mtime > best:
            best = mtime
    return best


def coverage_lag(
    client: Any,
    symbols: tuple[str, ...],
    *,
    root: Path = OB1000_ROOT,
    warn_lag_sec: int = DEFAULT_WARN_LAG_SEC,
) -> dict[str, Any]:
    """Materializer lag = FS open tip − CH max (not wall-clock quiet-market lag)."""
    now = _utc_now().replace(microsecond=0)
    warn_lag_sec = max(1, int(warn_lag_sec))
    per_symbol: dict[str, Any] = {}
    worst = 0
    for symbol in symbols:
        ch_max = _ch_max_snapshot(client, symbol)
        fs_last = _fs_open_mtime_ts(root, symbol)
        if ch_max is None:
            lag = None
            status = "NO_DATA"
        elif fs_last is None:
            # No live open segment for this symbol: nothing to catch up right now.
            lag = 0
            status = "OK"
        else:
            lag = max(0, int((fs_last.replace(microsecond=0) - ch_max).total_seconds()))
            status = "OK" if lag <= warn_lag_sec else "LAG_WARN"
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
        "warn_lag_sec": warn_lag_sec,
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
    open_only: bool = False,
    skip_completed_closed: bool = True,
    max_new_seconds_open: int | None = 120,
    warn_lag_sec: int = DEFAULT_WARN_LAG_SEC,
) -> dict[str, Any]:
    completed = load_completed() if skip_completed_closed else {}
    open_wm = load_open_watermarks()
    client = connect()
    emit_after_by_symbol: dict[str, datetime] = {}
    worst_lag = 0
    for symbol in symbols:
        ch_max = _ch_max_snapshot(client, symbol)
        if ch_max is not None:
            # Small overlap so near-tip gaps can heal without full-hour rewrite.
            emit_after_by_symbol[symbol] = ch_max
        fs_last = _fs_open_mtime_ts(root, symbol)
        if ch_max is not None and fs_last is not None:
            worst_lag = max(
                worst_lag,
                int(
                    (
                        fs_last.replace(microsecond=0) - ch_max.replace(microsecond=0)
                    ).total_seconds()
                ),
            )
    # When far behind, drain more per cycle so catch-up outruns live growth.
    effective_max = max_new_seconds_open
    if max_new_seconds_open is not None and max_new_seconds_open > 0:
        if worst_lag >= 900:
            effective_max = None  # unlimited until closer
        elif worst_lag >= 300:
            effective_max = max(max_new_seconds_open, 600)
        elif worst_lag >= 120:
            effective_max = max(max_new_seconds_open, 300)
    out = materialize_all(
        symbols=symbols,
        root=root,
        include_open=include_open,
        open_only=open_only,
        skip_completed=completed,
        open_watermarks=open_wm,
        emit_after_by_symbol=emit_after_by_symbol or None,
        max_new_seconds_open=effective_max,
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
    new_wm = out.get("open_watermarks")
    if isinstance(new_wm, dict) and new_wm != open_wm:
        # Keep watermarks for symbols not in this batch.
        merged = dict(open_wm)
        merged.update({str(k): v for k, v in new_wm.items() if isinstance(v, dict)})
        save_open_watermarks(merged)
        out["open_watermarks"] = merged
    client = connect()
    lag = coverage_lag(client, symbols, root=root, warn_lag_sec=warn_lag_sec)
    return {**out, "lag": lag}


def _batch_slice(symbols: tuple[str, ...], offset: int, batch_size: int) -> tuple[str, ...]:
    if not symbols:
        return ()
    n = len(symbols)
    size = max(1, min(int(batch_size), n))
    start = offset % n
    if start + size <= n:
        return symbols[start : start + size]
    return symbols[start:] + symbols[: (start + size) % n]


def _select_lag_batch(
    client: Any,
    symbols: tuple[str, ...],
    *,
    root: Path,
    batch_size: int,
    rr_offset: int,
) -> tuple[str, ...]:
    """Prefer highest FS−CH lag; RR tie-break so quiet symbols still rotate."""
    if not symbols:
        return ()
    size = max(1, min(int(batch_size), len(symbols)))
    scored: list[tuple[int, int, str]] = []
    for idx, symbol in enumerate(symbols):
        ch_max = _ch_max_snapshot(client, symbol)
        fs_last = _fs_open_mtime_ts(root, symbol)
        if ch_max is None:
            lag = 10**9
        elif fs_last is None:
            lag = 0
        else:
            lag = max(
                0,
                int(
                    (fs_last.replace(microsecond=0) - ch_max.replace(microsecond=0)).total_seconds()
                ),
            )
        # Rotate tie-break so equal-lag symbols share turns.
        tie = (idx - rr_offset) % len(symbols)
        scored.append((lag, -tie, symbol))
    scored.sort(reverse=True)
    return tuple(symbol for _lag, _tie, symbol in scored[:size])


def run_loop(
    *,
    symbols: tuple[str, ...],
    root: Path,
    interval_sec: float,
    warn_lag_sec: int = DEFAULT_WARN_LAG_SEC,
    batch_size: int = DEFAULT_BATCH_SIZE,
    closed_every_n: int = DEFAULT_CLOSED_EVERY_N,
    max_new_seconds_open: int | None = DEFAULT_MAX_NEW_SECONDS_OPEN,
) -> int:
    stop = _StopFlag()
    signal.signal(signal.SIGTERM, stop.request_stop)
    signal.signal(signal.SIGINT, stop.request_stop)
    lock = _acquire_lock()
    cycle = 0
    rr_offset = 0
    batch_size = max(1, int(batch_size))
    closed_every_n = max(1, int(closed_every_n))
    write_heartbeat(
        {
            "status": "STARTING",
            "runner_pid": os.getpid(),
            "symbols": list(symbols),
            "root": str(root),
            "interval_sec": interval_sec,
            "warn_lag_sec": warn_lag_sec,
            "batch_size": batch_size,
            "closed_every_n": closed_every_n,
            "max_new_seconds_open": max_new_seconds_open,
        }
    )
    try:
        while not stop.stop:
            cycle += 1
            t0 = time.monotonic()
            try:
                client = connect()
                # While catching up, drain the worst symbols fully. Once mostly
                # healthy, walk the full universe and rely on open watermarks.
                lag_preview = coverage_lag(
                    client, symbols, root=root, warn_lag_sec=warn_lag_sec
                )
                if int(lag_preview.get("worst_lag_seconds") or 0) > warn_lag_sec:
                    batch = _select_lag_batch(
                        client,
                        symbols,
                        root=root,
                        batch_size=batch_size,
                        rr_offset=rr_offset,
                    )
                else:
                    batch = symbols
            except Exception:  # noqa: BLE001 — fall back to pure RR
                batch = _batch_slice(symbols, rr_offset, batch_size)
            rr_offset = (rr_offset + max(1, len(batch))) % max(1, len(symbols))
            open_only = (cycle % closed_every_n) != 0
            write_heartbeat(
                {
                    "status": "RUNNING",
                    "runner_pid": os.getpid(),
                    "cycle": cycle,
                    "phase": "materialize",
                    "batch_symbols": list(batch),
                    "open_only": open_only,
                }
            )
            try:
                out = run_once(
                    symbols=batch,
                    root=root,
                    include_open=True,
                    open_only=open_only,
                    max_new_seconds_open=max_new_seconds_open,
                    warn_lag_sec=warn_lag_sec,
                )
                # Full-universe lag is expensive if done every batch; sample batch + light full check.
                client = connect()
                lag_batch = out["lag"]
                lag_all = coverage_lag(
                    client, symbols, root=root, warn_lag_sec=warn_lag_sec
                )
                lag = lag_all
                status = "RUNNING"
                if lag["overall"] == "LAG_WARN":
                    status = "LAG_WARN"
                elif lag["overall"] == "NO_DATA":
                    status = "NO_DATA"
                payload = {
                    "status": status,
                    "runner_pid": os.getpid(),
                    "cycle": cycle,
                    "batch_symbols": list(batch),
                    "open_only": open_only,
                    "rows_inserted": out["rows_inserted"],
                    "segments": out["segments"],
                    "skipped_unchanged_open": out.get("skipped_unchanged_open", 0),
                    "elapsed_sec": round(time.monotonic() - t0, 3),
                    "lag": lag,
                    "lag_batch": lag_batch,
                    "warn_lag_sec": warn_lag_sec,
                }
                write_heartbeat(payload)
                write_progress(
                    {
                        "cycle": cycle,
                        "batch_symbols": list(batch),
                        "open_only": open_only,
                        "rows_inserted": out["rows_inserted"],
                        "results": out["results"],
                        "lag": lag,
                    }
                )
                print(
                    f"ob1000_materializer cycle={cycle} batch={','.join(batch)} "
                    f"open_only={open_only} inserted={out['rows_inserted']} "
                    f"segments={out['segments']} skip_open={out.get('skipped_unchanged_open', 0)} "
                    f"lag={lag['worst_lag_seconds']}s status={status} "
                    f"elapsed={payload['elapsed_sec']}s",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 — keep loop alive; fail-visible
                write_heartbeat(
                    {
                        "status": "ERROR",
                        "runner_pid": os.getpid(),
                        "cycle": cycle,
                        "batch_symbols": list(batch),
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
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--closed-every-n",
        type=int,
        default=DEFAULT_CLOSED_EVERY_N,
        help="Every N cycles also process closed segments for the batch",
    )
    parser.add_argument("--once", action="store_true", help="Single pass (default)")
    parser.add_argument("--open-only", action="store_true", help="Only open segments")
    parser.add_argument("--status", action="store_true", help="Print heartbeat + lag")
    parser.add_argument("--shard-id", type=int, default=None)
    parser.add_argument("--shard-count", type=int, default=None)
    args = parser.parse_args(argv)
    shard_id = args.shard_id
    shard_count = args.shard_count
    if shard_id is None and os.environ.get("OB1000_MATERIALIZER_SHARD") not in {None, ""}:
        shard_id = int(os.environ["OB1000_MATERIALIZER_SHARD"])
    if shard_count is None and os.environ.get("OB1000_MATERIALIZER_SHARDS") not in {
        None,
        "",
    }:
        shard_count = int(os.environ["OB1000_MATERIALIZER_SHARDS"])
    if (shard_id is None) ^ (shard_count is None):
        raise SystemExit("ob1000_materializer: set both --shard-id and --shard-count")
    configure_shard(shard_id, shard_count)

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
    symbols = shard_filter_symbols(symbols)
    if not symbols:
        raise SystemExit(
            f"ob1000_materializer: shard {shard_id}/{shard_count} has no symbols"
        )
    if shard_id is not None:
        print(
            f"ob1000_materializer shard={shard_id}/{shard_count} "
            f"symbols={list(symbols)}",
            flush=True,
        )
    root = Path(args.root)
    ensure_run_dirs()

    if args.status:
        hb = {}
        if heartbeat_path().is_file():
            hb = json.loads(heartbeat_path().read_text(encoding="utf-8"))
        client = connect()
        lag = coverage_lag(
            client, symbols, root=root, warn_lag_sec=int(args.warn_lag_sec)
        )
        print(json.dumps({"heartbeat": hb, "lag": lag}, indent=2, default=str))
        return 0

    if args.loop:
        batch_size = int(
            os.environ.get("OB1000_MATERIALIZER_BATCH_SIZE", args.batch_size)
        )
        closed_every_n = int(
            os.environ.get("OB1000_MATERIALIZER_CLOSED_EVERY_N", args.closed_every_n)
        )
        max_new_raw = int(
            os.environ.get(
                "OB1000_MATERIALIZER_MAX_NEW_SECONDS",
                DEFAULT_MAX_NEW_SECONDS_OPEN,
            )
        )
        # 0 / negative => unlimited open catch-up per cycle.
        max_new = None if max_new_raw <= 0 else max_new_raw
        return run_loop(
            symbols=symbols,
            root=root,
            interval_sec=max(5.0, float(args.interval_sec)),
            warn_lag_sec=int(args.warn_lag_sec),
            batch_size=batch_size,
            closed_every_n=closed_every_n,
            max_new_seconds_open=max_new,
        )

    out = run_once(
        symbols=symbols,
        root=root,
        open_only=bool(args.open_only),
    )
    print(
        f"ob1000_materialize segments={out['segments']} rows_inserted={out['rows_inserted']} "
        f"lag={out['lag']['worst_lag_seconds']}s status={out['lag']['overall']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

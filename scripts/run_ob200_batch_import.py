#!/usr/bin/env python3
"""Batch runner for Bybit OB200 day imports.

Downloads missing daily OB200 ZIPs on demand and imports the extracted day
files into ``btc_doge_research.research_ob200_snapshots_1s`` one UTC day at a
time. Progress is persisted to a JSON status file so the run can be queried
while it is in flight.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.btc_doge_research.clickhouse import connect
from research.btc_doge_research.ob200_day_zip_importer import (
    _download_day,
    _daterange,
    import_ob200_day,
)
from research.orderbook.historical_bybit_replay import day_file_path

DEFAULT_STATUS_ROOT = PROJECT_ROOT / "run" / "ob200_batch_import"
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "bybit_historical_orderbook"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_day(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _status_path(symbol: str, start: str, end: str, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{symbol}_{start}_{end}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def _planned_days(symbol: str, start: str, end: str) -> list[str]:
    return _daterange(start, end)


def _build_status(
    *,
    symbol: str,
    start: str,
    end: str,
    days: list[str],
    status_file: Path,
    downloaded: int,
    imported: int,
    failed: int,
    current_day: str | None,
    current_phase: str,
    results: list[dict[str, Any]],
    started_at: datetime,
    updated_at: datetime,
    error: str | None = None,
) -> dict[str, Any]:
    elapsed_s = max(0.0, (updated_at - started_at).total_seconds())
    avg_s = elapsed_s / imported if imported else None
    remaining = len(days) - imported - failed
    eta_s = int(round(avg_s * remaining)) if avg_s is not None else None
    return {
        "symbol": symbol,
        "from": start,
        "to": end,
        "planned_days": days,
        "planned_day_count": len(days),
        "downloaded_days": downloaded,
        "imported_days": imported,
        "failed_days": failed,
        "remaining_days": remaining,
        "current_day": current_day,
        "current_phase": current_phase,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "updated_at": updated_at.isoformat().replace("+00:00", "Z"),
        "elapsed_seconds": round(elapsed_s, 3),
        "avg_seconds_per_imported_day": round(avg_s, 3) if avg_s is not None else None,
        "eta_seconds": eta_s,
        "eta_human": None if eta_s is None else str(timedelta(seconds=eta_s)),
        "status_file": str(status_file),
        "last_results": results[-20:],
        "error": error,
    }


def _load_resume_state(status_file: Path) -> dict[str, Any]:
    data = _read_json(status_file)
    return data if isinstance(data, dict) else {}


def _day_file(symbol: str, day: str, *, data_root: Path | None = None) -> Path:
    return day_file_path(symbol, day, data_root=data_root or DEFAULT_DATA_ROOT)


def run_batch(
    *,
    symbol: str,
    start: str,
    end: str,
    status_file: Path,
    data_root: Path | None,
    download_missing: bool,
    resume: bool,
    prefetch_days: int,
) -> int:
    symbol = symbol.upper().strip()
    days = _planned_days(symbol, start, end)
    started_at = _utc_now()
    downloaded = 0
    imported = 0
    failed = 0
    results: list[dict[str, Any]] = []
    current_day: str | None = None
    current_phase = "planning"

    resume_state = _load_resume_state(status_file) if resume else {}
    completed_days = {
        str(row.get("day"))
        for row in (resume_state.get("last_results") or [])
        if isinstance(row, dict) and row.get("rows_inserted") is not None
    }
    if resume_state.get("symbol") == symbol:
        downloaded = int(resume_state.get("downloaded_days") or 0)
        imported = int(resume_state.get("imported_days") or 0)
        failed = int(resume_state.get("failed_days") or 0)
        results = list(resume_state.get("last_results") or [])

    state_lock = threading.Lock()
    stop_event = threading.Event()
    error_messages: list[str] = []
    work_q: queue.Queue[tuple[str, Path] | object] = queue.Queue(
        maxsize=max(1, int(prefetch_days))
    )
    sentinel: object = object()

    def snapshot(*, error: str | None = None) -> None:
        with state_lock:
            payload = _build_status(
                symbol=symbol,
                start=start,
                end=end,
                days=days,
                status_file=status_file,
                downloaded=downloaded,
                imported=imported,
                failed=failed,
                current_day=current_day,
                current_phase=current_phase,
                results=list(results),
                started_at=started_at,
                updated_at=_utc_now(),
                error=error,
            )
        _write_json(status_file, payload)

    def put_work(item: tuple[str, Path] | object) -> None:
        while not stop_event.is_set():
            try:
                work_q.put(item, timeout=1.0)
                return
            except queue.Full:
                continue

    def producer() -> None:
        nonlocal downloaded, failed, current_day, current_phase
        try:
            for day in days:
                if stop_event.is_set():
                    break
                path = _day_file(symbol, day, data_root=data_root)
                if resume and day in completed_days and path.is_file():
                    continue
                with state_lock:
                    current_day = day
                    current_phase = "download" if download_missing else "check"
                snapshot()
                if not path.is_file():
                    if not download_missing:
                        raise FileNotFoundError(f"missing day file: {path}")
                    _download_day(symbol, day, path.parents[2])
                    with state_lock:
                        downloaded += 1
                    snapshot()
                put_work((day, path))
        except Exception as exc:  # noqa: BLE001
            with state_lock:
                failed += 1
                error_messages.append(f"{type(exc).__name__}: {exc}")
                current_phase = "download_failed"
            stop_event.set()
        finally:
            put_work(sentinel)

    client = connect()
    worker = threading.Thread(target=producer, name=f"ob200-batch-{symbol}", daemon=True)
    worker.start()
    try:
        while True:
            item = work_q.get()
            if item is sentinel:
                break
            day, path = item
            with state_lock:
                current_day = day
                current_phase = "import"
            snapshot()
            try:
                result = import_ob200_day(client, symbol, day, path)
            except Exception as exc:  # noqa: BLE001
                with state_lock:
                    failed += 1
                    error_messages.append(f"{type(exc).__name__}: {exc}")
                    current_phase = "import_failed"
                snapshot(error=error_messages[-1])
                stop_event.set()
                raise
            result["day"] = day
            result["path"] = str(path)
            with state_lock:
                results.append(result)
                imported += 1
            snapshot()
            print(json.dumps(result, default=str), flush=True)
    except Exception as exc:  # noqa: BLE001
        if not error_messages:
            error_messages.append(f"{type(exc).__name__}: {exc}")
        raise SystemExit(error_messages[-1]) from exc
    finally:
        stop_event.set()
        worker.join(timeout=5.0)
        client.close()

    current_phase = "done"
    payload = _build_status(
        symbol=symbol,
        start=start,
        end=end,
        days=days,
        status_file=status_file,
        downloaded=downloaded,
        imported=imported,
        failed=failed,
        current_day=current_day,
        current_phase="done",
        results=results,
        started_at=started_at,
        updated_at=_utc_now(),
        error=error_messages[-1] if error_messages else None,
    )
    _write_json(status_file, payload)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch-import Bybit OB200 days into ClickHouse")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from", dest="start", required=True, type=_parse_day)
    parser.add_argument("--to", dest="end", required=True, type=_parse_day)
    parser.add_argument("--status-file", type=Path, default=None)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--download", action="store_true", help="Download missing days before import")
    parser.add_argument("--resume", action="store_true", help="Continue from the status file if present")
    parser.add_argument(
        "--prefetch-days",
        type=int,
        default=3,
        help="How many days to keep downloaded ahead of the importer",
    )
    parser.add_argument("--status", action="store_true", help="Print current status and exit")
    args = parser.parse_args(argv)

    symbol = args.symbol.upper().strip()
    status_root = DEFAULT_STATUS_ROOT
    status_file = args.status_file or _status_path(symbol, args.start, args.end, status_root)

    if args.status:
        current = _read_json(status_file)
        print(json.dumps(current or {"status": "NOT_STARTED", "status_file": str(status_file)}, indent=2, sort_keys=True, default=str))
        return 0

    return run_batch(
        symbol=symbol,
        start=args.start,
        end=args.end,
        status_file=status_file,
        data_root=args.data_root,
        download_missing=bool(args.download),
        resume=bool(args.resume),
        prefetch_days=max(1, int(args.prefetch_days)),
    )


if __name__ == "__main__":
    raise SystemExit(main())

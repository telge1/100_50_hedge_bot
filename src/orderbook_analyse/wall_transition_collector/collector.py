"""Incremental wall-transition collection via offline detector chunks."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.dynamic_wall_detector import connect_readonly
from orderbook_analyse.execution_wall_detector.analysis import run_execution_wall_detector
from orderbook_analyse.execution_wall_detector.types import ExecutionWallParams
from orderbook_analyse.wall_toxicity_audit.data_access import ensure_utc
from orderbook_analyse.wall_transition_collector import COLLECTOR_VERSION
from orderbook_analyse.wall_transition_collector.io_util import (
    append_transitions,
    keys_hash,
    load_existing_keys,
)
from orderbook_analyse.wall_transition_collector.pidfile import (
    acquire_pid_file,
    release_pid_file,
)
from orderbook_analyse.wall_transition_collector.state import (
    atomic_write_json,
    default_state,
    load_state,
    touch_success,
)

LOG = logging.getLogger(__name__)


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    return ensure_utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))


def orderbook_span(symbol: str) -> tuple[datetime | None, datetime | None]:
    db = connect_readonly()
    q = db.query(
        """
        SELECT min(exchange_ts), max(exchange_ts)
        FROM orderbook_analysis.orderbook_deltas
        WHERE symbol = {s:String}
        """,
        parameters={"s": symbol},
    )
    mn, mx = q.result_rows[0]
    return (
        ensure_utc(mn) if mn else None,
        ensure_utc(mx) if mx else None,
    )


def _read_transition_csv(path: Path) -> list[dict[str, Any]]:
    import csv

    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def process_window(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_csv: Path,
    work_dir: Path,
    state_path: Path,
    params: ExecutionWallParams | None = None,
) -> dict[str, Any]:
    start, end = ensure_utc(start), ensure_utc(end)
    if end <= start:
        return {"written": 0, "note": "empty_window"}
    work_dir.mkdir(parents=True, exist_ok=True)
    run_dir = work_dir / f"run_{start.strftime('%Y%m%dT%H%M%S')}_{end.strftime('%Y%m%dT%H%M%S')}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    LOG.info("Detector chunk %s %s → %s", symbol, start.isoformat(), end.isoformat())
    run_execution_wall_detector(
        symbol=symbol,
        start=start,
        end=end,
        output_dir=run_dir,
        overwrite=True,
        params=params or ExecutionWallParams(),
    )
    trans_path = run_dir / "execution_wall_transitions.csv"
    rows = _read_transition_csv(trans_path)
    keys = load_existing_keys(output_csv)
    written, keys = append_transitions(output_csv, rows, existing_keys=keys)

    state = load_state(state_path) or default_state(symbol)
    if not state.get("symbol"):
        state = default_state(symbol)
    last_ts = None
    if rows:
        last_ts = max(str(r.get("transition_ts") or "") for r in rows)
    state = touch_success(
        state,
        last_processed_ts=end.isoformat(),
        last_written_transition_ts=last_ts or state.get("last_written_transition_ts"),
        known_transition_ids_hash=keys_hash(keys),
        transitions_written_total=int(state.get("transitions_written_total") or 0) + written,
        restart_count=int(state.get("restart_count") or 0),
    )
    atomic_write_json(state_path, state)

    # cleanup heavy candidate file to save disk; keep transitions copy in work for debug optional
    cand = run_dir / "execution_wall_candidates.csv"
    if cand.exists():
        cand.unlink()
    return {
        "written": written,
        "chunk_rows": len(rows),
        "output_csv": str(output_csv),
        "last_processed_ts": end.isoformat(),
        "collector_version": COLLECTOR_VERSION,
    }


def _bootstrap_watermark_from_existing_csv(output_csv: Path) -> datetime | None:
    if not output_csv.exists() or output_csv.stat().st_size == 0:
        return None
    import subprocess

    try:
        last = subprocess.check_output(["tail", "-n", "1", str(output_csv)], text=True).strip()
        parts = last.split(",")
        if len(parts) > 1:
            return ensure_utc(datetime.fromisoformat(parts[1].replace("Z", "+00:00")))
    except Exception:  # noqa: BLE001
        return None
    return None


def run_catchup(
    *,
    symbol: str,
    output_dir: Path,
    state_dir: Path,
    work_dir: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    chunk_hours: float = 2.0,
    max_catchup_hours: float | None = None,
    seed_from_legacy_csv: Path | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    state_dir = Path(state_dir)
    work_dir = Path(work_dir)
    out_csv = output_dir / symbol / "execution_wall_transitions.csv"
    state_path = state_dir / f"wall_transition_collector_{symbol}.json"
    state = load_state(state_path)
    if state and state.get("symbol") and state["symbol"] != symbol:
        raise RuntimeError(f"state symbol mismatch {state.get('symbol')} != {symbol}")

    ob_min, ob_max = orderbook_span(symbol)
    if ob_min is None or ob_max is None:
        return {"status": "BLOCKED_BY_MISSING_ORDERBOOK_DATA", "symbol": symbol}

    watermark = _parse_ts((state or {}).get("last_processed_ts"))
    if watermark is None:
        # prefer existing collector csv, then optional legacy research csv
        watermark = _bootstrap_watermark_from_existing_csv(out_csv)
        if watermark is None and seed_from_legacy_csv is not None:
            watermark = _bootstrap_watermark_from_existing_csv(Path(seed_from_legacy_csv))
    cur = start or watermark or ob_min
    stop = end or ob_max
    cur, stop = ensure_utc(cur), ensure_utc(stop)
    if max_catchup_hours is not None:
        stop = min(stop, cur + timedelta(hours=max_catchup_hours))
    stop = min(stop, ob_max)

    total_written = 0
    chunks = 0
    while cur < stop:
        nxt = min(cur + timedelta(hours=chunk_hours), stop)
        res = process_window(
            symbol=symbol,
            start=cur,
            end=nxt,
            output_csv=out_csv,
            work_dir=work_dir / symbol,
            state_path=state_path,
        )
        total_written += int(res.get("written") or 0)
        chunks += 1
        cur = nxt
    return {
        "status": "OK",
        "symbol": symbol,
        "written": total_written,
        "chunks": chunks,
        "output_csv": str(out_csv),
        "catchup_start": (start or watermark or ob_min).isoformat() if (start or watermark or ob_min) else None,
        "end": stop.isoformat(),
    }


def run_live(
    *,
    symbol: str,
    output_dir: Path,
    state_dir: Path,
    work_dir: Path,
    pid_path: Path,
    poll_seconds: float = 60.0,
    chunk_hours: float = 0.5,
    heartbeat_path: Path | None = None,
) -> None:
    token = f"wall_transition_collector:{symbol}"
    acquire_pid_file(pid_path, expected_token=token)
    # note: process argv should include token via CLI
    try:
        state_path = Path(state_dir) / f"wall_transition_collector_{symbol}.json"
        st = load_state(state_path)
        if not st:
            st = default_state(symbol)
            st["restart_count"] = 1
            atomic_write_json(state_path, st)
        else:
            st["restart_count"] = int(st.get("restart_count") or 0) + 1
            atomic_write_json(state_path, st)

        while True:
            try:
                _, ob_max = orderbook_span(symbol)
                if ob_max is None:
                    raise RuntimeError("no orderbook data")
                st = load_state(state_path) or default_state(symbol)
                cur = _parse_ts(st.get("last_processed_ts")) or (ob_max - timedelta(minutes=30))
                # Small overlap; append_transitions dedupes so restarts stay idempotent.
                start = cur - timedelta(seconds=30)
                end = ob_max
                if end > start:
                    # limit live chunk size
                    end = min(end, start + timedelta(hours=chunk_hours))
                    process_window(
                        symbol=symbol,
                        start=start,
                        end=end,
                        output_csv=Path(output_dir) / symbol / "execution_wall_transitions.csv",
                        work_dir=Path(work_dir) / symbol,
                        state_path=state_path,
                    )
                hb = {
                    "symbol": symbol,
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "last_processed_ts": (load_state(state_path) or {}).get("last_processed_ts"),
                    "status": "ok",
                }
                if heartbeat_path:
                    atomic_write_json(heartbeat_path, hb)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("live iteration failed: %s", exc)
                st = load_state(state_path) or default_state(symbol)
                st["last_error"] = str(exc)
                atomic_write_json(state_path, st)
                if heartbeat_path:
                    atomic_write_json(
                        heartbeat_path,
                        {
                            "symbol": symbol,
                            "ts_utc": datetime.now(timezone.utc).isoformat(),
                            "status": "error",
                            "error": str(exc),
                        },
                    )
            time.sleep(poll_seconds)
    finally:
        release_pid_file(pid_path)

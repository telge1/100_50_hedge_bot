#!/usr/bin/env python3
"""Safe resumable Orderbook V3 (ob200_v3) historical import for ~30 UTC days.

- Uses existing `orderbook_analyse.orderbook_v2.pilot` (download + parse + insert).
- Does NOT start/stop live collectors.
- Does NOT DELETE / TRUNCATE / OPTIMIZE FINAL.
- Skips symbol-days already COMPLETE in ClickHouse (>= 86000 rows).
- Idempotent ZIP download via pilot/downloader SKIPPED_EXISTING.
- Writes progress under results/orderbook_v3_30d_import/.

Default window: 2026-07-19 .. 2026-08-17 (inclusive).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from orderbook_analyse.orderbook_v2 import PARSER_VERSION  # noqa: E402
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client  # noqa: E402
from orderbook_analyse.orderbook_v2.pilot import run_pilot  # noqa: E402
from orderbook_analyse.orderbook_v2_live.universe import SYMBOLS_51  # noqa: E402

DATA_ROOT_BASE = Path(
    "/home/telgenbuescher/projects/data/orderbook_raw_v2/bybit/linear/ob200"
)
DEFAULT_START = date(2026, 7, 19)
DEFAULT_END = date(2026, 8, 17)
COMPLETE_ROW_MIN = 86000  # full UTC day ≈ 86400; allow tiny shortfall
EXPECTED_SEC_PER_DAY = 86400


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{utc_now()} {msg}", flush=True)


def daterange(start: date, end: date) -> list[date]:
    out: list[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def contiguous_ranges(days: list[date]) -> list[tuple[date, date]]:
    if not days:
        return []
    days = sorted(days)
    ranges: list[tuple[date, date]] = []
    a = b = days[0]
    for d in days[1:]:
        if d == b + timedelta(days=1):
            b = d
        else:
            ranges.append((a, b))
            a = b = d
    ranges.append((a, b))
    return ranges


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def load_progress(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def fetch_complete_sym_days(
    client, start: date, end: date
) -> set[tuple[str, date]]:
    q = """
    SELECT symbol, toDate(bucket_start) AS d, count() AS n
    FROM orderbook_analysis.orderbook_features_1s_v2
    WHERE parser_version = {p:String}
      AND depth = 200
      AND bucket_start >= toDateTime64({a:String}, 3, 'UTC')
      AND bucket_start <  toDateTime64({b:String}, 3, 'UTC')
    GROUP BY symbol, d
    """
    a = f"{start.isoformat()} 00:00:00"
    b = f"{(end + timedelta(days=1)).isoformat()} 00:00:00"
    rows = client.query(
        q, parameters={"p": PARSER_VERSION, "a": a, "b": b}
    ).result_rows
    return {(str(s), d) for s, d, n in rows if int(n) >= COMPLETE_ROW_MIN}


def audit_window(client, symbols: list[str], start: date, end: date) -> dict:
    days = daterange(start, end)
    expected_sym_days = len(symbols) * len(days)
    expected_seconds = expected_sym_days * EXPECTED_SEC_PER_DAY
    q = """
    SELECT
      symbol,
      toDate(bucket_start) AS d,
      count() AS n,
      countIf(is_valid = 0) AS invalid_n,
      min(bucket_start) AS mn,
      max(bucket_start) AS mx
    FROM orderbook_analysis.orderbook_features_1s_v2
    WHERE parser_version = {p:String}
      AND depth = 200
      AND bucket_start >= toDateTime64({a:String}, 3, 'UTC')
      AND bucket_start <  toDateTime64({b:String}, 3, 'UTC')
      AND symbol IN {syms:Array(String)}
    GROUP BY symbol, d
    ORDER BY symbol, d
    """
    a = f"{start.isoformat()} 00:00:00"
    b = f"{(end + timedelta(days=1)).isoformat()} 00:00:00"
    rows = client.query(
        q,
        parameters={
            "p": PARSER_VERSION,
            "a": a,
            "b": b,
            "syms": symbols,
        },
    ).result_rows
    by = {(str(s), d): (int(n), int(inv), mn, mx) for s, d, n, inv, mn, mx in rows}
    missing = []
    short = []
    actual = 0
    invalid = 0
    for s in symbols:
        for d in days:
            if (s, d) not in by:
                missing.append(f"{s}|{d}")
                continue
            n, inv, mn, mx = by[(s, d)]
            actual += n
            invalid += inv
            if n < COMPLETE_ROW_MIN:
                short.append({"symbol": s, "day": str(d), "n": n})
    return {
        "parser_version": PARSER_VERSION,
        "window": [start.isoformat(), end.isoformat()],
        "n_symbols": len(symbols),
        "n_days": len(days),
        "expected_sym_days": expected_sym_days,
        "expected_seconds": expected_seconds,
        "actual_rows": actual,
        "actual_sym_days_present": len(by),
        "invalid_count": invalid,
        "missing_symbol_days": missing,
        "short_symbol_days": short,
        "n_missing": len(missing),
        "n_short": len(short),
        "min_timestamp": min((by[k][2] for k in by), default=None),
        "max_timestamp": max((by[k][3] for k in by), default=None),
        "coverage_ok": len(missing) == 0 and len(short) == 0,
    }


def collectors_snapshot() -> dict:
    import subprocess

    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "orderbook_v2_live|oi_liquidation_collector"],
            text=True,
        )
    except subprocess.CalledProcessError:
        out = ""
    return {
        "snapshot_at": utc_now(),
        "lines": [ln for ln in out.splitlines() if "pgrep" not in ln],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Orderbook V3 30d resumable import")
    p.add_argument("--start-day", default=DEFAULT_START.isoformat())
    p.add_argument("--end-day", default=DEFAULT_END.isoformat())
    p.add_argument(
        "--result-dir",
        default=str(PROJECT_ROOT / "results" / "orderbook_v3_30d_import"),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--audit-only", action="store_true")
    p.add_argument(
        "--symbols",
        default="",
        help="Optional comma list; default = SYMBOLS_51",
    )
    args = p.parse_args()

    start = date.fromisoformat(args.start_day)
    end = date.fromisoformat(args.end_day)
    if end < start:
        raise SystemExit("end-day < start-day")

    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(SYMBOLS_51)
    )
    if len(symbols) != len(set(symbols)):
        raise SystemExit("duplicate symbols")

    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    progress_path = result_dir / "progress.json"
    audit_path = result_dir / "final_audit.json"
    collectors_path = result_dir / "collectors_snapshot.json"
    lock_path = result_dir / "import.lock"

    # non-blocking lock via exclusive create
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {utc_now()}\n".encode())
        os.close(fd)
    except FileExistsError:
        log(f"STOP: lock exists {lock_path}")
        return 2

    t0 = time.time()
    try:
        write_json_atomic(collectors_path, collectors_snapshot())
        log(
            f"start parser={PARSER_VERSION} window={start}..{end} "
            f"n_symbols={len(symbols)} dry_run={args.dry_run}"
        )
        if PARSER_VERSION != "ob200_v3":
            log(f"STOP: unexpected parser {PARSER_VERSION}")
            return 3

        client = get_clickhouse_client()
        client.query("SELECT 1")

        if args.audit_only:
            audit = audit_window(client, symbols, start, end)
            write_json_atomic(audit_path, audit)
            log(f"audit-only done coverage_ok={audit['coverage_ok']} missing={audit['n_missing']}")
            return 0 if audit["coverage_ok"] else 1

        complete = fetch_complete_sym_days(client, start, end)
        days = daterange(start, end)
        progress = load_progress(progress_path)
        completed_ranges = list(progress.get("completed_ranges") or [])
        errors = list(progress.get("errors") or [])
        skipped_existing = int(progress.get("skipped_existing_sym_days") or 0)
        inserted_sym_days = int(progress.get("inserted_sym_days") or 0)

        progress.update(
            {
                "run_started_at": progress.get("run_started_at") or utc_now(),
                "window": [start.isoformat(), end.isoformat()],
                "parser_version": PARSER_VERSION,
                "n_symbols": len(symbols),
                "status": "RUNNING",
                "pid": os.getpid(),
            }
        )
        write_json_atomic(progress_path, progress)

        for sym in symbols:
            missing = [d for d in days if (sym, d) not in complete]
            skipped_here = len(days) - len(missing)
            skipped_existing += skipped_here
            ranges = contiguous_ranges(missing)
            log(
                f"symbol={sym} complete_days={skipped_here}/{len(days)} "
                f"missing={len(missing)} ranges={[(a.isoformat(), b.isoformat()) for a,b in ranges]}"
            )
            if not ranges:
                continue

            for a, b in ranges:
                n_days = (b - a).days + 1
                key = f"{sym}:{a.isoformat()}:{b.isoformat()}"
                if key in completed_ranges:
                    log(f"SKIP_RANGE_ALREADY_DONE {key}")
                    continue
                data_root = DATA_ROOT_BASE / sym
                log(f"IMPORT {sym} {a}..{b} ({n_days}d) data_root={data_root}")
                progress["current"] = {
                    "symbol": sym,
                    "start": a.isoformat(),
                    "end": b.isoformat(),
                    "started_at": utc_now(),
                }
                write_json_atomic(progress_path, progress)
                coin_log = result_dir / f"{sym}_{a.isoformat()}_{b.isoformat()}.log"
                try:
                    # Tee pilot stdout into per-range log by redirecting print? run_pilot prints to stdout.
                    # Caller should redirect process stdout; also append a marker here.
                    with coin_log.open("a", encoding="utf-8") as fh:
                        fh.write(f"\n# BEGIN {utc_now()} {key}\n")
                    t_range = time.time()
                    # Capture by temporarily duplicating is heavy; rely on process nohup log + markers.
                    summary = run_pilot(
                        symbol=sym,
                        n_days=n_days,
                        data_root=data_root,
                        dry_run=args.dry_run,
                        optimize_final=False,
                        warmup_previous_day=True,
                        start_day=a,
                        end_day=b,
                    )
                    elapsed = round(time.time() - t_range, 1)
                    decision = summary.get("decision", "")
                    day_statuses = [d.get("status") for d in summary.get("days") or []]
                    n_complete = sum(1 for s in day_statuses if s == "COMPLETE")
                    n_skip_na = sum(1 for s in day_statuses if s == "SKIPPED_NOT_AVAILABLE")
                    log(
                        f"DONE_RANGE {key} decision={decision} "
                        f"complete_days={n_complete} unavailable={n_skip_na} elapsed_s={elapsed}"
                    )
                    with coin_log.open("a", encoding="utf-8") as fh:
                        fh.write(
                            json.dumps(
                                {
                                    "key": key,
                                    "decision": decision,
                                    "elapsed_s": elapsed,
                                    "day_statuses": day_statuses,
                                    "summary_n_days": summary.get("n_days"),
                                },
                                default=str,
                            )
                            + "\n"
                        )
                    if "PASSED" not in str(decision) or "WITH_WARNINGS" in str(decision):
                        # Soft-fail unavailable days: if any COMPLETE and rest SKIPPED_NOT_AVAILABLE, continue
                        hard_fail = any(
                            s
                            not in {
                                "COMPLETE",
                                "SKIPPED_NOT_AVAILABLE",
                                "DRY_RUN",
                                None,
                            }
                            for s in day_statuses
                        )
                        if hard_fail or "FAILED" in str(decision):
                            err = {
                                "key": key,
                                "decision": decision,
                                "day_statuses": day_statuses,
                                "at": utc_now(),
                            }
                            errors.append(err)
                            progress["errors"] = errors
                            progress["status"] = "STOPPED_IMPORT_FAILED"
                            progress["failed_range"] = key
                            write_json_atomic(progress_path, progress)
                            log(f"STOP_IMPORT_FAILED {err}")
                            return 4
                    inserted_sym_days += n_complete
                    completed_ranges.append(key)
                    # refresh complete set for this symbol after insert
                    complete |= {(sym, d) for d in daterange(a, b)}
                    progress.update(
                        {
                            "completed_ranges": completed_ranges,
                            "skipped_existing_sym_days": skipped_existing,
                            "inserted_sym_days": inserted_sym_days,
                            "last_completed_at": utc_now(),
                            "current": None,
                            "errors": errors,
                        }
                    )
                    write_json_atomic(progress_path, progress)
                except Exception as e:
                    err = {
                        "key": key,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                        "at": utc_now(),
                    }
                    errors.append(err)
                    progress["errors"] = errors
                    progress["status"] = "STOPPED_EXCEPTION"
                    progress["failed_range"] = key
                    write_json_atomic(progress_path, progress)
                    log(f"STOP_EXCEPTION {key} {e}")
                    return 5

        audit = audit_window(client, symbols, start, end)
        write_json_atomic(audit_path, audit)
        progress.update(
            {
                "status": "COMPLETED" if audit["coverage_ok"] else "COMPLETED_WITH_GAPS",
                "finished_at": utc_now(),
                "elapsed_s": round(time.time() - t0, 1),
                "skipped_existing_sym_days": skipped_existing,
                "inserted_sym_days": inserted_sym_days,
                "audit_coverage_ok": audit["coverage_ok"],
                "audit_n_missing": audit["n_missing"],
            }
        )
        write_json_atomic(progress_path, progress)
        write_json_atomic(collectors_path, collectors_snapshot())
        log(
            f"FINISHED status={progress['status']} elapsed_s={progress['elapsed_s']} "
            f"skipped_existing={skipped_existing} inserted={inserted_sym_days} "
            f"missing={audit['n_missing']}"
        )
        return 0 if audit["coverage_ok"] else 1
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

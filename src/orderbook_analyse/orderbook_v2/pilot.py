"""ORDERBOOK_V2 historical import CLI.

Usage:
    python -m orderbook_analyse.orderbook_v2.pilot [--symbol ADAUSDT] [--days 7]

ClickHouse is configured via CLICKHOUSE_* environment variables or project .env.
OPTIMIZE TABLE ... FINAL is off by default; pass --optimize-final to enable it.
Never writes to orderbook_deltas. Never stops live collectors.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from orderbook_analyse.orderbook_v2 import PARSER_VERSION
from orderbook_analyse.orderbook_v2.ch_writer import (
    insert_features, optimize_tables, upsert_manifest,
)
from orderbook_analyse.orderbook_v2.downloader import (
    DayAvailability, DownloadResult, download_day, list_available_days, pilot_days,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client
from orderbook_analyse.orderbook_v2.parser import (
    calendar_day_start_ms,
    parse_day_zip,
    validate_calendar_feature_rows,
    validate_warmup_sequence,
)
from orderbook_analyse.orderbook_v2.schema import apply_schema


def _ch_client():
    return get_clickhouse_client()


def maybe_optimize_tables(client, *, dry_run: bool, optimize_final: bool) -> bool:
    """Run OPTIMIZE FINAL only when explicitly requested and not a dry-run."""
    if dry_run or not optimize_final:
        return False
    print("\nRunning OPTIMIZE FINAL on _v2 tables...")
    optimize_tables(client)
    print("OPTIMIZE done")
    return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def decision_label(symbol: str, n_days: int, kind: str) -> str:
    return f"{symbol}_OB_V2_{n_days}D_PILOT_{kind}"


def _warmup_zip_path(data_root: Path, symbol: str, day: date) -> Path | None:
    day_dir = data_root / day.isoformat()
    named = day_dir / f"{day.isoformat()}_{symbol}_ob200.data.zip"
    if named.is_file():
        return named
    if day_dir.is_dir():
        zips = sorted(day_dir.glob("*.zip"))
        if zips:
            return zips[0]
    return None


def _target_days(
    n_days: int,
    start_day: str | date | None = None,
    end_day: str | date | None = None,
) -> list[date]:
    if start_day is not None and end_day is not None:
        d0 = date.fromisoformat(start_day) if isinstance(start_day, str) else start_day
        d1 = date.fromisoformat(end_day) if isinstance(end_day, str) else end_day
        days: list[date] = []
        cur = d0
        while cur <= d1:
            days.append(cur)
            cur += timedelta(days=1)
        return days
    return sorted(pilot_days(n_days))


def run_pilot(
    *,
    symbol: str = "ADAUSDT",
    n_days: int = 7,
    data_root: Path | None = None,
    dry_run: bool = False,
    optimize_final: bool = False,
    warmup_previous_day: bool = False,
    start_day: str | date | None = None,
    end_day: str | date | None = None,
) -> dict:
    """Full pilot run. Returns summary dict."""
    if data_root is None:
        data_root = Path(__file__).parents[4] / "data" / "orderbook_raw_v2" / \
                    "bybit" / "linear" / "ob200" / symbol

    print(f"\n=== ORDERBOOK_V2 PILOT: {symbol} {n_days}d ===")
    print(f"data_root: {data_root}")

    client = None
    if not dry_run:
        client = _ch_client()
        errs = apply_schema(client)
        if errs:
            print("SCHEMA ERRORS:", errs)
            return {
                "symbol": symbol,
                "n_days": n_days,
                "decision": decision_label(symbol, n_days, "FAILED"),
                "error": str(errs),
            }
        print("Schema OK (CREATE IF NOT EXISTS)")
    else:
        print("Dry-run: no ClickHouse connection, no feature/manifest writes")

    days = _target_days(n_days, start_day, end_day)
    print(f"\nPilot period: {days[0]} .. {days[-1]} (oldest-first)")

    import requests as _req
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bybit.com/data-download",
    })
    session.get("https://www.bybit.com/data-download", timeout=30,
                headers={"Accept": "text/html"})

    if warmup_previous_day and days:
        warmup_day = days[0] - timedelta(days=1)
        print(f"\nWarm-up day: {warmup_day.isoformat()} (no feature writes)")
        w_avails = list_available_days(symbol, [warmup_day], session=session)
        w_avail = w_avails[0]
        if not w_avail.available:
            err = "ETH_WARMUP_UNAVAILABLE" if symbol == "ETHUSDT" else "WARMUP_UNAVAILABLE"
            print(err, w_avail.error)
            return {
                "symbol": symbol, "n_days": n_days,
                "decision": decision_label(symbol, n_days, "FAILED"),
                "error": err,
            }
        w_dir = data_root / warmup_day.isoformat()
        existing = _warmup_zip_path(data_root, symbol, warmup_day)
        if existing is not None:
            print(f"  warmup zip exists, not overwritten: {existing}")
        else:
            w_dl = download_day(w_avail, w_dir, session=session)
            print(f"  warmup download: {w_dl.status} {w_dl.compressed_bytes/1e6:.1f} MB")
            if w_dl.status not in ("COMPLETE", "SKIPPED_EXISTING"):
                err = "ETH_WARMUP_UNAVAILABLE" if symbol == "ETHUSDT" else "WARMUP_UNAVAILABLE"
                print(err, w_dl.error)
                return {
                    "symbol": symbol, "n_days": n_days,
                    "decision": decision_label(symbol, n_days, "FAILED"),
                    "error": err,
                }

    avails = list_available_days(symbol, days, session=session)
    total_zip_bytes = sum(a.size_bytes for a in avails if a.available)
    missing = [a for a in avails if not a.available]

    print(f"\n{'Day':<12} {'Available':>10} {'Size MB':>10} URL")
    for a in avails:
        mb = f"{a.size_bytes/1e6:.1f}" if a.available else "N/A"
        av = "YES" if a.available else f"NO ({a.error})"
        print(f"  {a.day}  {av:>20}  {mb:>7}  {a.url[:60] if a.available else ''}")
    print(f"\nTotal ZIP: {total_zip_bytes/1e6:.1f} MB  Missing: {len(missing)}")

    if missing:
        print(f"WARNING: {len(missing)} days not available: {[m.day for m in missing]}")

    day_summaries = []
    pilot_start_ts = time.time()

    for avail in avails:
        day_data_dir = data_root / avail.day
        ds: dict = {"day": avail.day, "available": avail.available}

        if not avail.available:
            ds["status"] = "SKIPPED_NOT_AVAILABLE"
            ds["error"] = avail.error
            day_summaries.append(ds)
            continue

        print(f"\n--- {avail.day} ---")

        dl_start = time.time()
        dl = download_day(avail, day_data_dir, session=session)
        dl_secs = time.time() - dl_start
        ds["download_status"] = dl.status
        ds["sha256"] = dl.sha256
        ds["zip_mb"] = dl.compressed_bytes / 1e6
        ds["download_secs"] = round(dl_secs, 1)
        ds["local_path"] = dl.local_path
        print(f"  download: {dl.status} {dl.compressed_bytes/1e6:.1f} MB in {dl_secs:.1f}s")

        if dl.status not in ("COMPLETE", "SKIPPED_EXISTING"):
            ds["status"] = "DOWNLOAD_FAILED"
            ds["error"] = dl.error
            day_summaries.append(ds)
            if not dry_run and client is not None:
                _write_manifest_failed(client, avail, dl, symbol)
            continue

        zip_path = Path(dl.local_path)
        src_day = date.fromisoformat(avail.day)
        day_start_ms = calendar_day_start_ms(src_day)
        prev_day = src_day - timedelta(days=1)
        warmup = _warmup_zip_path(data_root, symbol, prev_day)
        warmup_zips = (warmup,) if warmup is not None else ()
        if warmup is not None:
            print(f"  warmup: {warmup}")
        else:
            print("  warmup: none (invalid prefix until first valid book)")

        if warmup_previous_day and src_day == days[0]:
            if warmup is None:
                err = (
                    "ETH_WARMUP_UNAVAILABLE"
                    if symbol == "ETHUSDT"
                    else "WARMUP_UNAVAILABLE"
                )
                ds["status"] = "FAILED"
                ds["error"] = err
                day_summaries.append(ds)
                return {
                    "symbol": symbol, "n_days": n_days,
                    "days": day_summaries,
                    "decision": decision_label(symbol, n_days, "FAILED"),
                    "error": err,
                }
            seq_ok, seq_msg = validate_warmup_sequence(
                warmup, zip_path, day_start_ms=day_start_ms,
            )
            print(f"  warmup_sequence: {seq_msg}")
            ds["warmup_sequence"] = seq_msg
            if not seq_ok:
                err = seq_msg if seq_msg.startswith("ETH_WARMUP") else (
                    "ETH_WARMUP_SEQUENCE_INVALID" if symbol == "ETHUSDT"
                    else "WARMUP_SEQUENCE_INVALID"
                )
                ds["status"] = "FAILED"
                ds["error"] = err
                day_summaries.append(ds)
                return {
                    "symbol": symbol, "n_days": n_days,
                    "days": day_summaries,
                    "decision": decision_label(symbol, n_days, "FAILED"),
                    "error": err,
                }

        parse_start = time.time()
        try:
            feature_rows, stats = parse_day_zip(
                zip_path, exchange="bybit", market="linear",
                symbol=symbol, depth=200,
                day_start_ms=day_start_ms,
                warmup_zips=warmup_zips,
            )
        except Exception as e:
            ds["status"] = "PARSE_FAILED"
            ds["error"] = traceback.format_exc()
            day_summaries.append(ds)
            if not dry_run and client is not None:
                _write_manifest_failed(client, avail, dl, symbol, error=str(e))
            continue
        parse_secs = time.time() - parse_start

        guard_ok, guard_msg = validate_calendar_feature_rows(
            feature_rows, day_start_ms=day_start_ms, parser_version=PARSER_VERSION,
        )
        ds["raw_records"] = stats.raw_record_count
        ds["n_snapshots"] = stats.n_snapshots
        ds["n_deltas"] = stats.n_deltas
        ds["n_seq_gaps"] = stats.n_seq_gaps
        ds["n_seq_dups"] = stats.n_seq_dups
        ds["n_event_secs"] = stats.event_seconds
        ds["n_carried_fwd_secs"] = stats.carried_forward_seconds
        ds["n_invalid_secs"] = stats.invalid_seconds
        ds["n_missing_secs"] = stats.missing_seconds
        ds["n_feature_rows"] = len(feature_rows)
        ds["coverage_ratio"] = stats.coverage_ratio
        ds["source_min_ts"] = stats.source_min_ts_ms
        ds["source_max_ts"] = stats.source_max_ts_ms
        ds["parse_secs"] = round(parse_secs, 1)
        ds["overflow_events"] = stats.overflow_events
        ds["warmup_events"] = stats.warmup_events
        ds["skipped_duplicate_events"] = stats.skipped_duplicate_events
        ds["calendar_guard"] = guard_msg
        if feature_rows:
            ds["min_bucket"] = feature_rows[0]["bucket_start"].isoformat()
            ds["max_bucket"] = feature_rows[-1]["bucket_start"].isoformat()
        print(f"  parse: {stats.raw_record_count} records -> {len(feature_rows)} rows "
              f"(event={stats.event_seconds} cf={stats.carried_forward_seconds} "
              f"invalid={stats.invalid_seconds} missing={stats.missing_seconds} "
              f"cov={stats.coverage_ratio:.4f}) in {parse_secs:.1f}s")
        print(f"  snapshots={stats.n_snapshots} deltas={stats.n_deltas} "
              f"seq_gaps={stats.n_seq_gaps} dups={stats.n_seq_dups} "
              f"overflow={stats.overflow_events} warmup_events={stats.warmup_events} "
              f"dup_skip={stats.skipped_duplicate_events}")
        print(f"  calendar_guard: {guard_msg}")

        if not guard_ok:
            ds["status"] = "WINDOW_MISALIGNED"
            ds["error"] = guard_msg
            day_summaries.append(ds)
            if not dry_run and client is not None:
                _write_manifest_failed(client, avail, dl, symbol, error=guard_msg)
            continue

        if dry_run:
            ds["status"] = "DRY_RUN"
            day_summaries.append(ds)
            continue

        import_start_ts = _utcnow()
        insert_start = time.time()
        try:
            n_inserted = insert_features(client, feature_rows)
        except Exception as e:
            ds["status"] = "INSERT_FAILED"
            ds["error"] = traceback.format_exc()
            day_summaries.append(ds)
            _write_manifest_failed(client, avail, dl, symbol, error=str(e))
            continue
        insert_secs = time.time() - insert_start
        import_completed_ts = _utcnow()

        rows_per_sec = n_inserted / insert_secs if insert_secs > 0 else 0
        ds["insert_secs"] = round(insert_secs, 1)
        ds["n_inserted"] = n_inserted
        print(f"  insert: {n_inserted} rows in {insert_secs:.1f}s ({rows_per_sec:.0f} rows/s)")

        src_min = datetime.fromtimestamp(stats.source_min_ts_ms / 1000, tz=timezone.utc)
        src_max = datetime.fromtimestamp(stats.source_max_ts_ms / 1000, tz=timezone.utc)
        manifest_row = {
            "exchange": "bybit", "market": "linear", "symbol": symbol,
            "depth": 200,
            "source_date": date.fromisoformat(avail.day),
            "source_url": avail.url, "local_path": dl.local_path,
            "sha256": dl.sha256, "compressed_bytes": dl.compressed_bytes,
            "raw_record_count": stats.raw_record_count,
            "source_min_ts": src_min, "source_max_ts": src_max,
            "downloaded_at": datetime.fromisoformat(dl.downloaded_at),
            "import_started_at": import_start_ts,
            "import_completed_at": import_completed_ts,
            "parser_version": PARSER_VERSION,
            "status": "COMPLETE",
            "error_message": "",
            "quality_flags": ",".join(stats.quality_flags),
            "inserted_feature_rows": n_inserted,
            "updated_at": _utcnow(),
        }
        upsert_manifest(client, manifest_row)
        ds["status"] = "COMPLETE"
        day_summaries.append(ds)

    if client is not None:
        maybe_optimize_tables(client, dry_run=dry_run, optimize_final=optimize_final)

    pilot_secs = time.time() - pilot_start_ts
    print(f"\nPilot total time: {pilot_secs:.0f}s")
    return {
        "symbol": symbol, "n_days": n_days, "pilot_secs": pilot_secs,
        "days": day_summaries,
        "decision": _decide(symbol, n_days, day_summaries),
    }


def _decide(symbol: str, n_days: int, day_summaries: list[dict]) -> str:
    failed = [d for d in day_summaries if d.get("status") not in ("COMPLETE", "DRY_RUN")]
    warnings = any(d.get("n_seq_gaps", 0) > 0 for d in day_summaries)
    if failed:
        return decision_label(symbol, n_days, "FAILED")
    if warnings:
        return decision_label(symbol, n_days, "PASSED_WITH_WARNINGS")
    return decision_label(symbol, n_days, "PASSED")


def _write_manifest_failed(
    client, avail: DayAvailability, dl: DownloadResult,
    symbol: str, *, error: str = "",
) -> None:
    now = _utcnow()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    manifest_row = {
        "exchange": "bybit", "market": "linear", "symbol": symbol, "depth": 200,
        "source_date": date.fromisoformat(avail.day),
        "source_url": avail.url, "local_path": dl.local_path if dl else "",
        "sha256": dl.sha256 if dl else "", "compressed_bytes": dl.compressed_bytes if dl else 0,
        "raw_record_count": 0, "source_min_ts": epoch, "source_max_ts": epoch,
        "downloaded_at": now, "import_started_at": now, "import_completed_at": now,
        "parser_version": PARSER_VERSION,
        "status": "FAILED", "error_message": error or (dl.error if dl else ""),
        "quality_flags": "", "inserted_feature_rows": 0, "updated_at": now,
    }
    try:
        upsert_manifest(client, manifest_row)
    except Exception:
        pass


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="ORDERBOOK_V2 pilot import")
    p.add_argument("--symbol", default="ADAUSDT")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--optimize-final",
        action="store_true",
        help=(
            "After inserts, run OPTIMIZE TABLE ... FINAL on the _v2 tables. "
            "Off by default; do not use for multi-coin historical rollouts."
        ),
    )
    p.add_argument("--data-root", default=None)
    p.add_argument("--start-day", default=None, help="Inclusive UTC source_date YYYY-MM-DD")
    p.add_argument("--end-day", default=None, help="Inclusive UTC source_date YYYY-MM-DD")
    p.add_argument(
        "--warmup-previous-day",
        action="store_true",
        help="Download/parse D-1 of the first target day as causal warmup only",
    )
    args = p.parse_args()

    data_root = Path(args.data_root) if args.data_root else None
    summary = run_pilot(
        symbol=args.symbol, n_days=args.days,
        data_root=data_root, dry_run=args.dry_run,
        optimize_final=args.optimize_final,
        warmup_previous_day=args.warmup_previous_day,
        start_day=args.start_day, end_day=args.end_day,
    )
    print(f"\n{'='*60}")
    print(f"DECISION: {summary['decision']}")
    print(f"{'='*60}")
    dec = str(summary.get("decision") or "")
    ok = dec.endswith("_PASSED") or dec.endswith("_PASSED_WITH_WARNINGS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resumable Bybit history backfill for a versioned universe.

Example:
  python scripts/backfill_bybit_universe.py \\
    --universe config/universe_100.json \\
    --symbols SOLUSDT XRPUSDT \\
    --start 2026-01-01T00:00:00Z \\
    --end 2026-08-10T00:00:00Z \\
    --resume \\
    --repair-missing
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.history import BybitHistoryClient, ensure_utc  # noqa: E402
from signal_generator.bybit.universe import load_universe  # noqa: E402
from signal_generator.bybit.universe_backfill import (  # noqa: E402
    run_universe_backfill,
    write_universe_artifacts,
)
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402

DEFAULT_OUT = ROOT / "results" / "bybit_100_coin_backfill"


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--universe", type=Path, required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--max-symbols", type=int, default=None)
    p.add_argument("--chunk-days", type=int, default=7)
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--request-pause", type=float, default=0.05)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument(
        "--repair-missing",
        action="store_true",
        help=(
            "Detect coalesced missing 1m ranges in ClickHouse and fetch only those "
            "via Bybit REST. Explicit (not implied by --resume). With --dry-run, "
            "lists ranges without API fetch / DB write (CH read still required)."
        ),
    )
    p.add_argument(
        "--max-missing-ranges",
        type=int,
        default=10_000,
        help="Safety limit: max coalesced missing ranges repaired per symbol",
    )
    p.add_argument(
        "--max-repair-span-minutes",
        type=int,
        default=400_000,
        help="Safety limit: max total missing minutes repaired per symbol per run",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="Defaults to <out-dir>/checkpoint.json",
    )
    return p


def _db_bytes(ch) -> int | None:
    try:
        r = ch.query(
            """
            SELECT sum(bytes_on_disk)
            FROM system.parts
            WHERE database = {db:String} AND table = 'candles_1m' AND active
            """,
            parameters={"db": ch.database},
        )
        return int(r.result_rows[0][0] or 0)
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start = _parse_utc(args.start)
    end = _parse_utc(args.end)
    if end <= start:
        print("ERROR: end must be after start", file=sys.stderr)
        return 2

    universe = load_universe(args.universe)
    out_dir = args.out_dir
    checkpoint_path = args.checkpoint or (out_dir / "checkpoint.json")
    symbols = [s.upper() for s in args.symbols] if args.symbols else None

    print(f"Universe: {args.universe} ({len(universe.symbols)} symbols)")
    print(f"Window: [{start.isoformat()}, {end.isoformat()})")
    print(
        f"resume={args.resume} retry_failed={args.retry_failed} "
        f"repair_missing={args.repair_missing} "
        f"chunk_days={args.chunk_days} dry_run={args.dry_run}"
    )

    bybit = BybitHistoryClient(request_pause_s=args.request_pause)
    ch = None
    try:
        # Repair dry-run needs CH reads; write path still skipped inside repair.
        need_ch = (not args.dry_run) or args.repair_missing
        if need_ch:
            settings = get_clickhouse_settings()
            ch = setup_clickhouse(settings=settings)
            print(f"ClickHouse {settings.host}:{settings.port}/{settings.database}")

        result = run_universe_backfill(
            universe=universe,
            ch=ch,
            requested_start=start,
            requested_end=end,
            checkpoint_path=checkpoint_path,
            symbols=symbols,
            max_symbols=args.max_symbols,
            chunk_days=args.chunk_days,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            resume=args.resume,
            retry_failed=args.retry_failed,
            repair_missing=args.repair_missing,
            bybit=bybit,
            max_missing_ranges=args.max_missing_ranges,
            max_repair_span_minutes=args.max_repair_span_minutes,
        )

        db_bytes = _db_bytes(ch) if ch is not None and not args.dry_run else None
        if not args.dry_run:
            write_universe_artifacts(
                out_dir=out_dir,
                universe=universe,
                result=result,
                db_bytes=db_bytes,
            )
            print(f"Artifacts → {out_dir}")

        counts = {}
        for q in result.quality:
            counts[q.final_status or "?"] = counts.get(q.final_status or "?", 0) + 1
        print("Status counts:", counts)
        print(
            f"Totals unique_final={result.unique_final_total} "
            f"physical={result.physical_total} db_bytes={db_bytes}"
        )

        if any((q.final_status == "FAILED") for q in result.quality):
            return 3
        if any((q.final_status == "PARTIAL") for q in result.quality):
            return 4
        if any((q.final_status == "COMPLETE_WITH_INTERNAL_GAPS") for q in result.quality):
            return 5
        return 0
    finally:
        if ch is not None:
            ch.close()


if __name__ == "__main__":
    raise SystemExit(main())

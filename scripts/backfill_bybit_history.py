#!/usr/bin/env python3
"""Backfill historical Bybit linear 1m candles into signal_generator.candles_1m.

Example:
  python scripts/backfill_bybit_history.py \\
    --symbols DOGEUSDT APTUSDT BTCUSDT \\
    --start 2026-08-01T00:00:00Z \\
    --end 2026-08-03T00:00:00Z \\
    --repeat-for-idempotency
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.backfill import (  # noqa: E402
    BackfillRunResult,
    SymbolBackfillResult,
    run_backfill,
    write_artifacts,
)
from signal_generator.bybit.history import BybitHistoryClient, ensure_utc  # noqa: E402
from signal_generator.bybit.quality import (  # noqa: E402
    audit_symbol_candles,
    pick_sample_indices,
)
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402

DEFAULT_OUT = ROOT / "results" / "bybit_history_backfill_smoke"


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True, help="UTC start inclusive, ISO-8601")
    p.add_argument("--end", required=True, help="UTC end exclusive, ISO-8601")
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--repeat-for-idempotency",
        action="store_true",
        help="Run the same import a second time and compare FINAL counts",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    return p


def _audit_run(
    *,
    bybit: BybitHistoryClient,
    repo: CandleRepository,
    symbols: list[str],
    start: datetime,
    end: datetime,
    per_symbol: list[SymbolBackfillResult],
    idempotency: dict[str, dict[str, int]] | None,
) -> BackfillRunResult:
    quality = []
    gaps = []
    crosschecks = []
    for symbol in symbols:
        candles = bybit.fetch_closed_1m(symbol, start, end)
        rows = repo.get_candles(symbol, start, end)
        physical = repo.count_physical(symbol, start, end)
        idxs = pick_sample_indices(len(candles))
        samples = [candles[i] for i in idxs.values()]
        report, cross = audit_symbol_candles(
            symbol=symbol,
            start=start,
            end=end,
            final_rows=rows,
            physical_rows=physical,
            bybit_samples=samples,
        )
        pos_by_ot = {ensure_utc(candles[i].open_time): name for name, i in idxs.items()}
        for item in cross:
            item.position = pos_by_ot.get(item.open_time, item.position)
        quality.append(report)
        gaps.extend(report.gaps)
        crosschecks.extend(cross)
    return BackfillRunResult(
        start=start,
        end=end,
        symbols=symbols,
        per_symbol=per_symbol,
        quality=quality,
        gaps=gaps,
        crosschecks=crosschecks,
        idempotency=idempotency,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start = _parse_utc(args.start)
    end = _parse_utc(args.end)
    if end <= start:
        print("ERROR: end must be after start", file=sys.stderr)
        return 2

    symbols = [s.upper() for s in args.symbols]
    print(f"Backfill window [{start.isoformat()}, {end.isoformat()}) UTC")
    print(f"Symbols: {symbols}")
    print(f"dry_run={args.dry_run} batch_size={args.batch_size}")

    bybit = BybitHistoryClient()
    ch = None
    try:
        if args.dry_run:
            run = run_backfill(
                ch=None,
                symbols=symbols,
                start=start,
                end=end,
                batch_size=args.batch_size,
                dry_run=True,
                bybit=bybit,
            )
            for s in run.per_symbol:
                print(f"{s.symbol}: fetched={s.fetched}")
            return 0

        settings = get_clickhouse_settings()
        ch = setup_clickhouse(settings=settings)
        print(f"ClickHouse: {settings.host}:{settings.port}/{settings.database}")

        first = run_backfill(
            ch=ch,
            symbols=symbols,
            start=start,
            end=end,
            batch_size=args.batch_size,
            dry_run=False,
            bybit=bybit,
        )
        for s in first.per_symbol:
            print(
                f"import1 {s.symbol}: fetched={s.fetched} inserted={s.inserted} "
                f"FINAL={s.final_count} physical={s.physical_count}"
            )

        idem: dict[str, dict[str, int]] | None = None
        per = list(first.per_symbol)
        if args.repeat_for_idempotency:
            repo = CandleRepository(ch)
            before = {
                s: {
                    "final_before": repo.count_final(s, start, end),
                    "physical_before": repo.count_physical(s, start, end),
                }
                for s in symbols
            }
            print("Re-running identical import for idempotency check…")
            second = run_backfill(
                ch=ch,
                symbols=symbols,
                start=start,
                end=end,
                batch_size=args.batch_size,
                dry_run=False,
                bybit=bybit,
            )
            idem = {}
            for s in symbols:
                idem[s] = {
                    **before[s],
                    "final_after": repo.count_final(s, start, end),
                    "physical_after": repo.count_physical(s, start, end),
                }
            per = list(second.per_symbol)
            for s, c in idem.items():
                print(
                    f"idempotency {s}: FINAL {c['final_before']} → {c['final_after']} "
                    f"(physical {c['physical_before']} → {c['physical_after']})"
                )

        repo = CandleRepository(ch)
        run = _audit_run(
            bybit=bybit,
            repo=repo,
            symbols=symbols,
            start=start,
            end=end,
            per_symbol=per,
            idempotency=idem,
        )
        write_artifacts(run, args.out_dir)
        print(f"Artifacts written to {args.out_dir}")

        for q in run.quality:
            cc = "PASS" if q.crosscheck_pass else "FAIL"
            print(
                f"quality {q.symbol}: final={q.unique_final}/{q.expected} "
                f"gaps={q.gap_count} ohlc_err={len(q.ohlc_errors)} cross={cc}"
            )

        if any(q.crosscheck_pass is False for q in run.quality):
            return 3
        if any(len(q.ohlc_errors) > 0 or q.close_time_errors > 0 for q in run.quality):
            return 4
        if any(q.gap_count > 0 for q in run.quality):
            return 5
        if idem and any(c["final_before"] != c["final_after"] for c in idem.values()):
            return 6
        return 0
    finally:
        if ch is not None:
            ch.close()


if __name__ == "__main__":
    raise SystemExit(main())

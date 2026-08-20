#!/usr/bin/env python3
"""Ingest Bybit public-trade CSV.GZ files into public_trades_archive.

Does not write candles_1m, live public_trades, or any signal_generator table.
Inserts are throttled so the live collector on the same ClickHouse stays usable.

Example (existing local files only, no Bybit download):

  PYTHONPATH=src python scripts/ingest_public_trades_archive.py \\
    --files-root imports/apt_public_trades_july/gz \\
    --files-root /home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_trades \\
    --pause-ms 250 \\
    --batch-size 4000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.config import load_settings  # noqa: E402
from orderbook_analyse.public_trade_source.archive_ingest import (  # noqa: E402
    ARCHIVE_DATABASE,
    ARCHIVE_FQN,
    ArchiveIngestError,
    ArchiveIngestWriter,
    discover_trade_files,
    ingest_files,
)

DEFAULT_CHECKPOINT = ROOT / "results" / "public_trades_archive_ingest" / "checkpoint.json"
DEFAULT_ROOTS = [
    ROOT / "imports" / "apt_public_trades_july" / "gz",
    Path(
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_trades"
    ),
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--files-root", action="append", type=Path, dest="files_roots")
    p.add_argument("--symbol", action="append", dest="symbols")
    p.add_argument("--batch-size", type=int, default=4000)
    p.add_argument("--pause-ms", type=int, default=250)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    p.add_argument("--no-checkpoint", action="store_true")
    p.add_argument("--skip-invalid", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    roots = args.files_roots or DEFAULT_ROOTS
    files: list[Path] = []
    symbols = [s.upper() for s in (args.symbols or [])] or None
    if symbols:
        for sym in symbols:
            files.extend(discover_trade_files(roots, symbol=sym))
    else:
        files.extend(discover_trade_files(roots))
    # unique preserve order
    seen: set[str] = set()
    uniq: list[Path] = []
    for path in files:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    files = uniq
    print(f"target={ARCHIVE_FQN} files={len(files)} dry_run={args.dry_run}")
    for path in files:
        print(f"  {path}")
    if not files:
        print("no csv.gz trade files found")
        return 2

    writer = None
    client = None
    if not args.dry_run:
        settings = load_settings()
        if settings.clickhouse_database != ARCHIVE_DATABASE:
            print(
                f"ERROR: CLICKHOUSE_DATABASE={settings.clickhouse_database} "
                f"(need {ARCHIVE_DATABASE}); refusing so the scanner DB is untouched",
                file=sys.stderr,
            )
            return 3
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_http_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
        )
        writer = ArchiveIngestWriter(client, database=settings.clickhouse_database)
        writer.ensure_table()

    checkpoint = None if args.no_checkpoint or args.dry_run else args.checkpoint
    try:
        run = ingest_files(
            writer=writer,
            files=files,
            batch_size=args.batch_size,
            pause_ms=args.pause_ms,
            dry_run=args.dry_run,
            checkpoint_path=checkpoint,
            skip_invalid=args.skip_invalid,
        )
    except ArchiveIngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    finally:
        if client is not None:
            client.close()

    out = ROOT / "results" / "public_trades_archive_ingest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "last_run.json").write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")
    print(f"rows_inserted={run.rows_inserted} files={len(run.files)}")
    print(f"wrote {out / 'last_run.json'}")
    if any(f.status == "FAILED" for f in run.files):
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

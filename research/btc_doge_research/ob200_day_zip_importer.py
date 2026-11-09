"""Import Bybit historical orderbook.200 day files into research_ob200_snapshots_1s.

Source files are the extracted NDJSON archives from
``scripts/download_bybit_historical_orderbook.py``:

    data/bybit_historical_orderbook/<SYMBOL>/<YYYY-MM-DD>/<YYYY-MM-DD>_<SYMBOL>_ob200.data

Each UTC second keeps the last reconstructed book whose exchange ``ts`` falls
in that second. Seconds that already exist for the symbol are not inserted
again, so a rerun cannot duplicate rows and cannot mix a second OB200 copy
over the live/shadow producer. This writer never targets the OB1000 table.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from research.orderbook.historical_bybit_replay import (
    HistoricalBybitReplayer,
    ReplayError,
    day_file_path,
    iter_messages,
)

from .clickhouse import connect, insert, rows
from .contracts import TARGET_DATABASE, stable_hash
from .full_history_contracts import DAY_ZIP_PRODUCER_ID
from .phase2_contracts import TICK_SIZE
from .phase2_day_loader import OB_COLUMNS
from .phase2_transform import compact_ob_state

OB200_TABLE = "research_ob200_snapshots_1s"
SOURCE_SEMANTICS = "bybit_historical_ob200_day_ndjson_v1"
CONTRACT_VERSION = "ob200_day_zip_1s_v1"
MAX_LEVELS = 200
INSERT_BATCH = 100


def _utc_day(day: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_tick(symbol: str) -> None:
    if symbol not in TICK_SIZE:
        raise ValueError(
            f"{symbol} has no OB200 tick size; refusing import so it cannot "
            "fall through to the OB1000 tick map"
        )


def iter_day_snapshots(path: Path, symbol: str, day: str) -> Iterator[dict[str, Any]]:
    """Yield one end-of-second book for messages inside ``day`` (UTC)."""
    _require_tick(symbol)
    day_start, day_end = _utc_day(day)
    start_s = int(day_start.timestamp())
    end_s = int(day_end.timestamp())
    replayer = HistoricalBybitReplayer()
    open_second: int | None = None
    events = 0
    gap_base = 0
    last_msg = None
    scanned = 0

    def emit(second: int) -> dict[str, Any] | None:
        if second < start_s or second >= end_s or events <= 0 or last_msg is None:
            return None
        if not replayer.book.has_snapshot:
            return None
        bids = tuple(replayer.book.sorted_bids()[:MAX_LEVELS])
        asks = tuple(replayer.book.sorted_asks()[:MAX_LEVELS])
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            return None
        try:
            state = compact_ob_state(symbol, bids, asks, expected_depth=MAX_LEVELS)
        except ValueError:
            return None
        quality = (
            "POSSIBLE_GAP"
            if replayer.diag.u_gap_count > gap_base
            else "EXACT_EVENT_ORDER"
        )
        return {
            "snapshot_ts": datetime.fromtimestamp(second, tz=timezone.utc),
            "state": state,
            "source_event_count": events,
            "source_event_time": datetime.fromtimestamp(last_msg.ts_ms / 1000.0, tz=timezone.utc),
            "source_update_id": int(last_msg.update_id),
            "ordering_quality": quality,
        }

    for item in iter_messages(path, expected_symbol=symbol, skip_malformed=True):
        scanned += 1
        if scanned % 500_000 == 0:
            print(f"ob200_scan {symbol} {day} messages={scanned}", flush=True)
        if isinstance(item, tuple):
            continue
        second = item.ts_ms // 1000
        if open_second is not None and second != open_second:
            row = emit(open_second)
            if row is not None:
                yield row
            events = 0
            gap_base = replayer.diag.u_gap_count
            last_msg = None
        open_second = second
        try:
            replayer.apply_message(item)
        except ReplayError:
            continue
        events += 1
        last_msg = item
    if open_second is not None:
        row = emit(open_second)
        if row is not None:
            yield row


def _existing_seconds(client: Any, symbol: str, day: str) -> set[datetime]:
    start, end = _utc_day(day)
    found = rows(
        client,
        f"""
        SELECT DISTINCT snapshot_ts
        FROM {TARGET_DATABASE}.{OB200_TABLE}
        WHERE symbol = %(symbol)s
          AND snapshot_ts >= %(start)s
          AND snapshot_ts < %(end)s
        """,
        {"symbol": symbol, "start": start, "end": end},
    )
    out: set[datetime] = set()
    for (value,) in found:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        out.add(value.replace(microsecond=0))
    return out


def _row(
    symbol: str,
    snap: dict[str, Any],
    *,
    fingerprint: str,
    build_id: str,
    now: datetime,
    coverage_status: str,
) -> tuple[Any, ...]:
    state = snap["state"]
    return (
        symbol,
        snap["snapshot_ts"],
        DAY_ZIP_PRODUCER_ID,
        state["bid_price_ticks"],
        state["bid_quantities"],
        state["ask_price_ticks"],
        state["ask_quantities"],
        state["best_bid"],
        state["best_ask"],
        state["mid"],
        state["spread"],
        state["bid_level_count"],
        state["ask_level_count"],
        state["genuine_depth"],
        "EVENT_TIME_END_OF_SECOND",
        snap["source_event_count"],
        snap["source_event_time"],
        snap["source_update_id"],
        snap["ordering_quality"],
        fingerprint,
        CONTRACT_VERSION,
        SOURCE_SEMANTICS,
        build_id,
        coverage_status,
        now,
    )


def import_ob200_day(
    client: Any,
    symbol: str,
    day: str,
    path: Path,
    *,
    batch_size: int = INSERT_BATCH,
) -> dict[str, Any]:
    """Insert missing 1s OB200 snapshots for one UTC day. Idempotent."""
    symbol = symbol.upper()
    _require_tick(symbol)
    if OB200_TABLE != "research_ob200_snapshots_1s":
        raise RuntimeError("OB200 importer target drifted")
    fingerprint = _sha256(path)
    build_id = stable_hash(
        {
            "symbol": symbol,
            "day": day,
            "producer_id": DAY_ZIP_PRODUCER_ID,
            "source_fingerprint": fingerprint,
            "semantics": SOURCE_SEMANTICS,
        }
    )
    existing = _existing_seconds(client, symbol, day)
    now = datetime.now(timezone.utc)
    seen = 0
    skipped = 0
    inserted = 0
    batch: list[tuple[Any, ...]] = []
    qualities: set[str] = set()

    def flush() -> None:
        nonlocal inserted, batch
        if not batch:
            return
        insert(client, OB200_TABLE, batch, OB_COLUMNS)
        inserted += len(batch)
        batch = []

    for snap in iter_day_snapshots(path, symbol, day):
        seen += 1
        qualities.add(str(snap["ordering_quality"]))
        if snap["snapshot_ts"] in existing:
            skipped += 1
            continue
        # Coverage is filled after the scan; rows written in this run share it.
        batch.append(
            _row(
                symbol,
                snap,
                fingerprint=fingerprint,
                build_id=build_id,
                now=now,
                coverage_status=(
                    "PARTIAL" if snap["ordering_quality"] == "POSSIBLE_GAP" else "COMPLETE"
                ),
            )
        )
        if len(batch) >= batch_size:
            flush()
            if inserted and inserted % 5000 == 0:
                print(
                    f"ob200_import {symbol} {day} inserted={inserted} skipped={skipped}",
                    flush=True,
                )
    flush()
    coverage = "COMPLETE" if seen >= 80_000 and "POSSIBLE_GAP" not in qualities else "PARTIAL"
    return {
        "symbol": symbol,
        "day": day,
        "table": f"{TARGET_DATABASE}.{OB200_TABLE}",
        "producer_id": DAY_ZIP_PRODUCER_ID,
        "source_path": str(path),
        "source_fingerprint": fingerprint,
        "build_id": build_id,
        "seconds_seen": seen,
        "seconds_skipped_existing": skipped,
        "rows_inserted": inserted,
        "coverage_status": coverage,
    }


def _download_day(symbol: str, day: str, root: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "download_bybit_historical_orderbook.py"
    import subprocess

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--symbol",
            symbol,
            "--dates",
            day,
            "--out-root",
            str(root),
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"OB200 download failed for {symbol} {day}")


def _daterange(start: str, end: str) -> list[str]:
    cursor, stop = _utc_day(start)[0], _utc_day(end)[0]
    if stop < cursor:
        raise ValueError("--to is before --from")
    out: list[str] = []
    while cursor <= stop:
        out.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Bybit OB200 day files into ClickHouse")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--date", action="append", default=[])
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--download", action="store_true", help="Download a missing day before import")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    symbol = args.symbol.upper()
    days = list(args.date)
    if args.start or args.end:
        if not args.start or not args.end:
            raise SystemExit("--from and --to are required together")
        days.extend(_daterange(args.start, args.end))
    if not days:
        raise SystemExit("pass --date or --from/--to")
    root = args.root
    client = connect()
    results = []
    try:
        for day in days:
            path = day_file_path(symbol, day, data_root=root)
            if not path.is_file():
                if not args.download:
                    raise SystemExit(f"missing OB200 file: {path}")
                _download_day(symbol, day, path.parents[2])
            if not path.is_file():
                raise SystemExit(f"OB200 file still missing after download: {path}")
            results.append(import_ob200_day(client, symbol, day, path))
            print(json.dumps(results[-1], default=str), flush=True)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

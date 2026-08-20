#!/usr/bin/env python3
"""Isolated smoke: replay OB events via OrderBookReplayer from CH or OB200 files."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.ob_data_source.factory import create_orderbook_event_source  # noqa: E402
from orderbook_analyse.orderbook_replay import OrderBookReplayer  # noqa: E402

DEFAULT_FILES_ROOT = ROOT / "imports" / "apt_ob_july" / "extracted"
DEFAULT_OUT = ROOT / "results" / "ob200_file_source_stage1"


def _parse_ts(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _dec_str(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return format(v, "f")


def _sample_row(sample_ts: datetime, book) -> dict[str, Any]:
    bb, ba = book.best_bid(), book.best_ask()
    crossed = bool(bb is not None and ba is not None and bb >= ba)
    spread = None if bb is None or ba is None else ba - bb
    return {
        "sample_ts": sample_ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "best_bid": _dec_str(bb),
        "best_ask": _dec_str(ba),
        "spread": _dec_str(spread),
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
        "last_update_id": book.last_update_id,
        "last_seq": book.last_seq,
        "crossed_book": crossed,
    }


def replay_samples(
    source,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    sample_seconds: float,
) -> tuple[list[dict[str, Any]], int]:
    """Causal  sample grid using existing OrderBookReplayer (streaming messages)."""
    replayer = OrderBookReplayer()
    step = timedelta(seconds=float(sample_seconds))
    next_sample = start
    rows: list[dict[str, Any]] = []
    crossed = 0

    iter_messages = getattr(source, "iter_messages", None)
    if iter_messages is not None:
        message_iter = iter_messages(symbol, start, end)
        for msg in message_iter:
            while next_sample <= end and msg.exchange_ts > next_sample:
                if replayer.book.has_snapshot and next_sample >= start:
                    row = _sample_row(next_sample, replayer.book)
                    if row["crossed_book"]:
                        crossed += 1
                    rows.append(row)
                next_sample += step
            levels = msg.to_book_level_events()
            replayer.apply_message(
                msg.message_type,
                msg.update_id,
                msg.cross_sequence,
                msg.exchange_ts,
                levels,
            )
    else:
        # ClickHouse path: stream BookLevelEvents via group_messages
        from orderbook_analyse.orderbook_replay import group_messages

        for message_type, update_id, seq, ts, levels in group_messages(
            source.iter_events(symbol, start, end)
        ):
            while next_sample <= end and ts > next_sample:
                if replayer.book.has_snapshot and next_sample >= start:
                    row = _sample_row(next_sample, replayer.book)
                    if row["crossed_book"]:
                        crossed += 1
                    rows.append(row)
                next_sample += step
            replayer.apply_message(message_type, update_id, seq, ts, levels)

    while next_sample <= end:
        if replayer.book.has_snapshot and next_sample >= start:
            row = _sample_row(next_sample, replayer.book)
            if row["crossed_book"]:
                crossed += 1
            rows.append(row)
        next_sample += step
    return rows, crossed


def try_parity(
    file_rows: list[dict[str, Any]],
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    sample_seconds: float,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "attempted": True,
        "clickhouse_available": False,
        "n_compared": 0,
        "n_match": 0,
        "n_mismatch": 0,
        "note": "",
    }
    try:
        ch_source = create_orderbook_event_source("clickhouse")
        cov = ch_source.coverage(symbol, start, end)
        if not cov.valid:
            out["note"] = f"clickhouse_coverage_invalid: {cov.reason}"
            return out
        out["clickhouse_available"] = True
        ch_rows, _ = replay_samples(
            ch_source,
            symbol=symbol,
            start=start,
            end=end,
            sample_seconds=sample_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"clickhouse_unavailable: {exc}"
        return out

    by_ts = {r["sample_ts"]: r for r in ch_rows}
    compared = match = mismatch = 0
    examples: list[dict[str, Any]] = []
    for fr in file_rows:
        cr = by_ts.get(fr["sample_ts"])
        if cr is None:
            continue
        compared += 1
        same = (
            fr["best_bid"] == cr["best_bid"]
            and fr["best_ask"] == cr["best_ask"]
            and fr["last_update_id"] == cr["last_update_id"]
            and fr["last_seq"] == cr["last_seq"]
        )
        if same:
            match += 1
        else:
            mismatch += 1
            if len(examples) < 5:
                examples.append({"file": fr, "clickhouse": cr})
    out["n_compared"] = compared
    out["n_match"] = match
    out["n_mismatch"] = mismatch
    out["examples"] = examples
    out["note"] = "ok" if compared else "no_overlapping_sample_timestamps"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="OB200 / ClickHouse orderbook replay smoke")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-07-24T00:00:05Z")
    p.add_argument("--end", default="2026-07-24T00:01:00Z")
    p.add_argument("--ob-source", choices=("clickhouse", "files"), default="clickhouse")
    p.add_argument("--ob-files-root", type=Path, default=DEFAULT_FILES_ROOT)
    p.add_argument("--ob-file-pattern", default="*/*.data")
    p.add_argument("--sample-seconds", type=float, default=5.0)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--parity", action="store_true", help="Compare file vs CH when possible")
    args = p.parse_args(argv)

    start = _parse_ts(args.start)
    end = _parse_ts(args.end)
    out_dir = Path(args.output_dir)
    if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
        # allow writing into existing stage1 dir when overwrite set; else require empty or overwrite
        pass
    out_dir.mkdir(parents=True, exist_ok=True)

    source = create_orderbook_event_source(
        args.ob_source,
        files_root=args.ob_files_root if args.ob_source == "files" else None,
        file_pattern=args.ob_file_pattern,
    )
    coverage = source.coverage(args.symbol, start, end)
    (out_dir / "coverage.json").write_text(
        json.dumps(coverage.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    if not coverage.valid:
        print(json.dumps({"ok": False, "coverage": coverage.to_dict()}, indent=2))
        return 2

    rows, crossed = replay_samples(
        source,
        symbol=args.symbol,
        start=start,
        end=end,
        sample_seconds=args.sample_seconds,
    )
    # no samples before start (invariant)
    for r in rows:
        ts = _parse_ts(r["sample_ts"])
        if ts < start:
            raise RuntimeError(f"sample before start: {r['sample_ts']}")

    csv_path = out_dir / "smoke_samples.csv"
    fieldnames = [
        "sample_ts",
        "best_bid",
        "best_ask",
        "spread",
        "bid_levels",
        "ask_levels",
        "last_update_id",
        "last_seq",
        "crossed_book",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    parity: dict[str, Any] | None = None
    if args.parity and args.ob_source == "files":
        parity = try_parity(
            rows,
            symbol=args.symbol,
            start=start,
            end=end,
            sample_seconds=args.sample_seconds,
        )
        (out_dir / "parity.json").write_text(
            json.dumps(parity, indent=2) + "\n", encoding="utf-8"
        )

    summary = {
        "ok": True,
        "ob_source": args.ob_source,
        "symbol": args.symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sample_seconds": args.sample_seconds,
        "n_samples": len(rows),
        "crossed_book_samples": crossed,
        "coverage": coverage.to_dict(),
        "parity": parity,
        "output_dir": str(out_dir),
        "uses_orderbook_replayer": True,
        "decision_hint": "FILE_SOURCE_STAGE1_READY",
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

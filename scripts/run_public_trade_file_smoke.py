#!/usr/bin/env python3
"""Isolated smoke: public trades from ClickHouse or Bybit CSV.GZ files."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.public_trade_source.aggregate import (  # noqa: E402
    aggregate_trade_flow_5s,
)
from orderbook_analyse.public_trade_source.decisions import (  # noqa: E402
    NOT_READY,
    decision_hint_from_coverage,
)
from orderbook_analyse.public_trade_source.factory import (  # noqa: E402
    create_public_trade_source,
)

DEFAULT_FILES_ROOT = ROOT / "imports" / "apt_public_trades_july" / "gz"
DEFAULT_OUT = ROOT / "results" / "public_trade_file_source_stage1"


def _parse_ts(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def try_parity(
    file_trades: list,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "attempted": True,
        "clickhouse_available": False,
        "note": "",
        "file_count": len(file_trades),
        "ch_count": None,
        "n_trade_id_overlap": None,
    }
    try:
        ch = create_public_trade_source("clickhouse")
        cov = ch.coverage(symbol, start, end)
        if not cov.valid:
            out["note"] = f"clickhouse_coverage_invalid: {cov.reason}"
            return out
        ch_trades = list(ch.iter_trades(symbol, start, end))
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"clickhouse_unavailable: {exc}"
        return out

    out["clickhouse_available"] = True
    out["ch_count"] = len(ch_trades)
    out["ch_buy"] = sum(1 for t in ch_trades if t.side == "Buy")
    out["ch_sell"] = sum(1 for t in ch_trades if t.side == "Sell")
    out["file_buy"] = sum(1 for t in file_trades if t.side == "Buy")
    out["file_sell"] = sum(1 for t in file_trades if t.side == "Sell")
    out["file_notional"] = format(sum((t.notional for t in file_trades), start=0), "f")
    out["ch_notional"] = format(sum((t.notional for t in ch_trades), start=0), "f")
    file_ids = {t.trade_id for t in file_trades}
    ch_ids = {t.trade_id for t in ch_trades}
    out["n_trade_id_overlap"] = len(file_ids & ch_ids)
    out["trade_id_in_clickhouse"] = True  # schema has trade_id from WS i
    out["count_match"] = len(file_trades) == len(ch_trades)
    out["note"] = "ok"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Public trade file / ClickHouse smoke")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-07-24T00:00:00Z")
    p.add_argument("--end", default="2026-07-24T00:05:00Z")
    p.add_argument("--trades-source", choices=("clickhouse", "files"), default="clickhouse")
    p.add_argument("--trades-files-root", type=Path, default=DEFAULT_FILES_ROOT)
    p.add_argument("--trades-file-pattern", default="*.csv.gz")
    p.add_argument("--trades-file-strict", action="store_true", default=True)
    p.add_argument("--allow-partial-trade-coverage", action="store_true")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--max-trades-csv", type=int, default=5000)
    p.add_argument("--parity", action="store_true")
    args = p.parse_args(argv)

    start = _parse_ts(args.start)
    end = _parse_ts(args.end)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source = create_public_trade_source(
        args.trades_source,
        files_root=args.trades_files_root if args.trades_source == "files" else None,
        file_pattern=args.trades_file_pattern,
        strict=bool(args.trades_file_strict),
        allow_partial_coverage=bool(args.allow_partial_trade_coverage),
    )
    coverage = source.coverage(args.symbol, start, end)
    hint = decision_hint_from_coverage(
        coverage, allow_partial=bool(args.allow_partial_trade_coverage)
    )
    (out_dir / "coverage.json").write_text(
        json.dumps(coverage.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    if hint == NOT_READY:
        print(
            json.dumps(
                {"ok": False, "decision_hint": hint, "coverage": coverage.to_dict()},
                indent=2,
            )
        )
        return 2

    trades = list(source.iter_trades(args.symbol, start, end))
    # sample CSV (cap)
    sample = trades[: int(args.max_trades_csv)]
    trades_path = out_dir / "trades_sample.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "trade_ts",
                "side",
                "size",
                "price",
                "notional",
                "trade_id",
                "tick_direction",
            ],
        )
        w.writeheader()
        for t in sample:
            w.writerow(
                {
                    "trade_ts": _iso(t.trade_ts),
                    "side": t.side,
                    "size": format(t.size, "f"),
                    "price": format(t.price, "f"),
                    "notional": format(t.notional, "f"),
                    "trade_id": t.trade_id,
                    "tick_direction": t.tick_direction,
                }
            )

    flow = aggregate_trade_flow_5s(trades)
    flow_path = out_dir / "trade_flow_5s.csv"
    with flow_path.open("w", newline="", encoding="utf-8") as fh:
        if flow:
            w = csv.DictWriter(fh, fieldnames=list(flow[0].keys()))
            w.writeheader()
            w.writerows(flow)
        else:
            fh.write("")

    parity = None
    if args.parity and args.trades_source == "files":
        parity = try_parity(trades, symbol=args.symbol, start=start, end=end)
        (out_dir / "parity.json").write_text(
            json.dumps(parity, indent=2) + "\n", encoding="utf-8"
        )

    summary = {
        "ok": True,
        "trades_source": args.trades_source,
        "symbol": args.symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_trades": len(trades),
        "n_flow_buckets": len(flow),
        "buy_count": sum(1 for t in trades if t.side == "Buy"),
        "sell_count": sum(1 for t in trades if t.side == "Sell"),
        "coverage": coverage.to_dict(),
        "parity": parity,
        "output_dir": str(out_dir),
        "side_semantics": "Buy=taker buy, Sell=taker sell (same as CH ingest)",
        "decision_hint": hint,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI for causal market-event short reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from orderbook_analyse.market_event_report.build import build_report, write_artifacts
from orderbook_analyse.market_event_report.loaders import (
    default_fetch_window,
    fetch_candles_1m,
    fetch_oi_liq_optional,
    fetch_orderbook_1m,
    fetch_trades_1m,
    parse_event_time,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_market_event_report",
        description=(
            "Causal market-event short report (research diagnostic). "
            "Not a trading signal; SELECT-only; no collector control."
        ),
    )
    p.add_argument("--symbol", required=True, help="e.g. ADAUSDT")
    p.add_argument(
        "--event-time",
        required=True,
        help="UTC event time, e.g. 2026-08-12T21:24:00Z (floored to minute)",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        help="Artifact directory, e.g. results/market_event_reports/ADAUSDT_20260812_2124",
    )
    p.add_argument(
        "--trp-root",
        default="/home/telgenbuescher/projects/trading_research_platform",
        help="Optional path to trading_research_platform for LLD",
    )
    p.add_argument(
        "--skip-oi-liq",
        action="store_true",
        help="Skip OI/liquidation probe (mark unavailable)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbol = args.symbol.strip().upper()
    if not symbol or "," in symbol or " " in symbol:
        print("ERROR: pass exactly one --symbol", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[3]
    # When imported from installed package, parents differ; also try cwd project .env
    for candidate in (
        Path.cwd() / ".env",
        Path("/home/telgenbuescher/projects/orderbook_analyse/.env"),
        root / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            break

    event_t = parse_event_time(args.event_time)
    warmup, _, end_incl = default_fetch_window(event_t)
    from datetime import timedelta

    end_excl = end_incl + timedelta(minutes=1)

    client = get_clickhouse_client()
    candles = fetch_candles_1m(client, symbol, warmup, end_incl)
    trades = fetch_trades_1m(client, symbol, warmup, end_excl)
    orderbook = fetch_orderbook_1m(client, symbol, warmup, end_excl)

    if args.skip_oi_liq:
        oi_liq = {"available": False, "reason": "skipped_by_flag"}
    else:
        oi_liq = fetch_oi_liq_optional(client, symbol, warmup, end_excl)

    report = build_report(
        symbol=symbol,
        event_time_utc=event_t,
        candles=candles,
        trades=trades,
        orderbook=orderbook,
        oi_liq=oi_liq,
        trp_root=Path(args.trp_root) if args.trp_root else None,
    )
    paths = write_artifacts(
        Path(args.output_dir),
        report=report,
        candles=candles,
        trades=trades,
        orderbook=orderbook,
    )

    primary = report["summary"]["classification"]["primary"]
    print(f"symbol={symbol} event={event_t.isoformat()}Z primary={primary}")
    for name, path in paths.items():
        print(f"wrote {name} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the Bybit live 1m candle collector (recovery-first, shadow signals).

Default symbol source: config/live_universe.json

Examples:
  python scripts/run_bybit_live_1m_collector.py
  python scripts/run_bybit_live_1m_collector.py --live-universe config/live_universe.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.history import BybitHistoryClient  # noqa: E402
from signal_generator.bybit.live.collector import (  # noqa: E402
    Live1mCollector,
    install_signal_handlers,
)
from signal_generator.bybit.live.live_universe import (  # noqa: E402
    default_live_universe_path,
    load_live_universe,
    partition_valid_symbols,
    validate_live_symbols,
)
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--live-universe",
        type=Path,
        default=default_live_universe_path(),
        help="Sole symbol source (default: config/live_universe.json)",
    )
    p.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Optional override (still cannot include BTCUSDT)",
    )
    p.add_argument("--skip-symbol-validation", action="store_true")
    p.add_argument("--request-pause", type=float, default=0.05)
    p.add_argument("--stale-symbol-minutes", type=float, default=3.0)
    p.add_argument("--no-signals", action="store_true")
    p.add_argument("--no-internal-repair", action="store_true")
    p.add_argument("--log-level", default="INFO")
    return p


def resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        uni = load_live_universe(args.live_universe)
        symbols = list(uni.symbols)
    if "BTCUSDT" in symbols:
        raise SystemExit("BTCUSDT is not allowed in the live collector")
    if not args.skip_symbol_validation:
        results = validate_live_symbols(symbols)
        valid, invalid = partition_valid_symbols(results)
        for r in invalid:
            logging.error("INVALID_SYMBOL %s: %s", r.symbol, r.reason)
        symbols = valid
    if not symbols:
        raise SystemExit("Empty symbol list after validation")
    return symbols


async def _amain(args: argparse.Namespace) -> int:
    symbols = resolve_symbols(args)
    settings = get_clickhouse_settings()
    ch = setup_clickhouse(settings=settings)
    history = BybitHistoryClient(request_pause_s=args.request_pause)
    collector = Live1mCollector(
        symbols=symbols,
        ch=ch,
        history=history,
        stale_symbol_minutes=args.stale_symbol_minutes,
        enable_signals=not args.no_signals,
        repair_internal=not args.no_internal_repair,
        desired_state="RUNNING",
    )
    loop = asyncio.get_running_loop()
    install_signal_handlers(collector, loop)
    logging.info("collector symbols=%s db=%s", symbols, settings.database)
    try:
        await collector.run()
    finally:
        ch.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())

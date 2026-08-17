#!/usr/bin/env python3
"""Supervised live 1m collector + localhost control API (shadow mode).

Respects persistent desired_state:
  RUNNING → run collector (restart on crash)
  STOPPED → keep process idle (no auto-restart of collector loop)

Example:
  python scripts/run_live_collector_service.py --live-universe config/live_universe.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal as signal_mod
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.history import BybitHistoryClient  # noqa: E402
from signal_generator.bybit.live.collector import (  # noqa: E402
    Live1mCollector,
    acquire_singleton_lock,
    install_signal_handlers,
)
from signal_generator.bybit.live.control_api import (  # noqa: E402
    CollectorControlService,
    start_control_api,
)
from signal_generator.bybit.live.candle_universe import (  # noqa: E402
    filter_signal_demand,
    load_candle_universe,
    resolve_universes,
)
from signal_generator.bybit.live.demand_symbols import (  # noqa: E402
    DemandSymbolStore,
    default_demand_path,
)
from signal_generator.bybit.live.desired_state import (  # noqa: E402
    DesiredStateStore,
    default_desired_state_path,
)
from signal_generator.bybit.live.health import CollectorState, HealthState  # noqa: E402
from signal_generator.bybit.live.live_universe import (  # noqa: E402
    default_live_universe_path,
    load_live_universe,
    partition_valid_symbols,
    validate_live_symbols,
)
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.client import ClickHouseClient  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--live-universe",
        type=Path,
        default=default_live_universe_path(),
        help="Path to config/live_universe.json (legacy validation / watermark seed)",
    )
    p.add_argument(
        "--candle-universe",
        type=Path,
        default=None,
        help=(
            "Optional Gold-51 candle JSON (e.g. config/universe_tradeable_51.json). "
            "Unset = current demand-only ingest (do not pass until rollout)."
        ),
    )
    p.add_argument("--skip-symbol-validation", action="store_true")
    p.add_argument("--api-host", default="127.0.0.1")
    p.add_argument("--api-port", type=int, default=8787)
    p.add_argument("--desired-state-file", type=Path, default=default_desired_state_path())
    p.add_argument("--demand-symbols-file", type=Path, default=default_demand_path())
    p.add_argument(
        "--default-desired",
        choices=("RUNNING", "STOPPED"),
        default="RUNNING",
        help="Used only when desired-state file is missing",
    )
    p.add_argument("--request-pause", type=float, default=0.05)
    p.add_argument("--stale-symbol-minutes", type=float, default=3.0)
    p.add_argument("--batch-max-rows", type=int, default=50)
    p.add_argument("--batch-flush-interval", type=float, default=0.5)
    p.add_argument("--internal-lookback-days", type=int, default=30)
    p.add_argument("--recent-continuity-minutes", type=int, default=120)
    p.add_argument("--signal-workers", type=int, default=4)
    p.add_argument("--signal-queue-maxsize", type=int, default=1000)
    p.add_argument("--signal-shutdown-drain-s", type=float, default=5.0)
    p.add_argument("--no-signals", action="store_true")
    p.add_argument("--no-internal-repair", action="store_true")
    p.add_argument(
        "--enable-public-trades",
        action="store_true",
        help=(
            "Subscribe publicTrade.{symbol} for the 51 candle-universe coins and "
            "insert into orderbook_analysis.public_trades_canonical. Default off."
        ),
    )
    p.add_argument("--public-trade-queue-maxsize", type=int, default=5000)
    p.add_argument("--public-trade-batch-size", type=int, default=500)
    p.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "results" / "live_collector" / "collector.lock",
    )
    p.add_argument("--log-level", default="INFO")
    return p


async def run_supervised(args: argparse.Namespace) -> int:
    universe = load_live_universe(args.live_universe)
    symbols = list(universe.symbols)
    invalid_meta: list[dict[str, str]] = []

    if not args.skip_symbol_validation:
        results = validate_live_symbols(symbols)
        valid, invalid = partition_valid_symbols(results)
        for r in invalid:
            logger.error("INVALID_SYMBOL %s: %s", r.symbol, r.reason)
            invalid_meta.append({"symbol": r.symbol, "reason": r.reason})
        symbols = valid
        if not symbols:
            raise SystemExit("No valid symbols after universe validation")

    universe_symbols = list(symbols)
    demand = DemandSymbolStore(path=args.demand_symbols_file)
    desired = DesiredStateStore(path=args.desired_state_file, default=args.default_desired)
    if not args.desired_state_file.is_file():
        desired.write(args.default_desired, reason="bootstrap_default")

    settings = get_clickhouse_settings()
    ch = setup_clickhouse(settings=settings)
    # Separate CH client for ThreadingHTTPServer control API — clickhouse-connect
    # sessions are not safe for concurrent queries with the collector pipeline.
    ch_api = ClickHouseClient.from_settings(settings)
    history = BybitHistoryClient(request_pause_s=args.request_pause)
    acquire_singleton_lock(args.lock_file)

    collector: Live1mCollector | None = None
    stop_main = asyncio.Event()

    def on_desired_change(value: str) -> None:
        logger.info("desired_state → %s", value)
        if value == "STOPPED" and collector is not None:
            collector.request_stop()

    idle_health = HealthState(
        desired_state=desired.read(),
        shadow_mode=True,
        trading_enabled=False,
    )
    idle_health.init_symbols(symbols)
    idle_health.invalid_symbols = invalid_meta
    idle_health.set_state(CollectorState.STOPPED, reason="idle")

    from signal_generator.db.outcomes import SignalOutcomeRepository
    from signal_generator.db.processing_state import ProcessingStateRepository
    from signal_generator.pipeline.versions import (
        STRATEGY_VERSION,
        STRATEGY_VERSION_BE50_FROZEN,
    )

    # Seed NO_BE50 watermarks from frozen tip so first boot does not cold-replay days.
    try:
        seeded = ProcessingStateRepository(ch).seed_watermarks_from_strategy(
            source_strategy_version=STRATEGY_VERSION_BE50_FROZEN,
            target_strategy_version=STRATEGY_VERSION,
            symbols=symbols,
        )
        if seeded:
            logger.info(
                "seeded %s processing watermarks %s → %s",
                seeded,
                STRATEGY_VERSION_BE50_FROZEN,
                STRATEGY_VERSION,
            )
    except Exception:  # noqa: BLE001
        logger.exception("watermark seed failed (continuing)")

    def _active_universes() -> tuple[list[str], list[str]]:
        demand_syms = demand.read()
        if args.candle_universe is not None:
            loaded = load_candle_universe(args.candle_universe)
            return resolve_universes(
                candle_symbols=loaded,
                signal_symbols=demand_syms,
            )
        signals = filter_signal_demand(demand_syms)
        return signals, signals

    service = CollectorControlService(
        health=idle_health,
        desired=desired,
        signals=SignalRepository(ch_api),
        outcomes=SignalOutcomeRepository(ch_api),
        candles=CandleRepository(ch_api),
        on_desired_change=on_desired_change,
        demand=demand,
        get_collector=lambda: collector,
    )
    httpd, _thread = start_control_api(service, host=args.api_host, port=args.api_port)

    loop = asyncio.get_running_loop()

    def _sig() -> None:
        logger.info("process signal → stop supervisor")
        if collector is not None:
            collector.request_stop()
        stop_main.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal_mod, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _sig)
        except NotImplementedError:
            pass

    try:
        while not stop_main.is_set():
            ds = desired.read()
            if ds != "RUNNING":
                service.health = idle_health
                idle_health.desired_state = ds
                idle_health.set_state(CollectorState.STOPPED, reason="desired_STOPPED")
                try:
                    await asyncio.wait_for(stop_main.wait(), timeout=2.0)
                    break
                except asyncio.TimeoutError:
                    continue

            candle_syms, signal_syms = _active_universes()
            idle_health.init_symbols(candle_syms)
            idle_health.candle_symbols = list(candle_syms)
            idle_health.signal_symbols = list(signal_syms)
            if not candle_syms:
                service.health = idle_health
                idle_health.desired_state = ds
                idle_health.set_state(CollectorState.STOPPED, reason="no_demand_symbol")
                try:
                    await asyncio.wait_for(stop_main.wait(), timeout=2.0)
                    break
                except asyncio.TimeoutError:
                    continue

            collector = Live1mCollector(
                candle_symbols=candle_syms,
                signal_symbols=signal_syms,
                ch=ch,
                history=history,
                stale_symbol_minutes=args.stale_symbol_minutes,
                enable_signals=not args.no_signals,
                repair_internal=not args.no_internal_repair,
                internal_lookback_days=args.internal_lookback_days,
                recent_continuity_minutes=args.recent_continuity_minutes,
                batch_max_rows=args.batch_max_rows,
                batch_flush_interval_s=args.batch_flush_interval,
                desired_state=ds,
                signal_workers=args.signal_workers,
                signal_queue_maxsize=args.signal_queue_maxsize,
                signal_shutdown_drain_s=args.signal_shutdown_drain_s,
                enable_public_trades=args.enable_public_trades,
                public_trade_symbols=candle_syms if args.enable_public_trades else None,
                public_trade_queue_maxsize=args.public_trade_queue_maxsize,
                public_trade_batch_size=args.public_trade_batch_size,
            )
            collector.health.invalid_symbols = invalid_meta
            service.health = collector.health
            install_signal_handlers(collector, loop)
            logger.info(
                "collector start candle_symbols=%s signal_symbols=%s db=%s desired=%s",
                candle_syms,
                signal_syms,
                settings.database,
                ds,
            )
            try:
                await collector.run()
            except Exception:
                logger.exception("collector crashed")

            collector = None
            service.health = idle_health

            if stop_main.is_set():
                break
            if desired.read() != "RUNNING":
                logger.info("collector stopped intentionally (desired=STOPPED)")
                continue
            logger.warning("collector exited while desired=RUNNING → restart in 5s")
            try:
                await asyncio.wait_for(stop_main.wait(), timeout=5.0)
                break
            except asyncio.TimeoutError:
                continue
    finally:
        httpd.shutdown()
        ch.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run_supervised(args))


if __name__ == "__main__":
    raise SystemExit(main())

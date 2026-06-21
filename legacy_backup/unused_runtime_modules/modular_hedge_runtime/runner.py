from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from strategy.config import StrategyConfig

from .registry import STRATEGY_REGISTRY, build_registered_runtime, list_strategy_names
from .runtime import configure_runtime_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runner fuer modulare Hedge-Strategien.")
    parser.add_argument(
        "--strategy",
        choices=list_strategy_names(),
        default="dynamic_breakeven",
        help="Welche registrierte Strategie gestartet werden soll.",
    )
    parser.add_argument("--symbol", default=None, help="Optionales Symbol-Override.")
    parser.add_argument(
        "--price-poll-interval",
        type=float,
        default=None,
        help="Optionales Override fuer das REST-Preis-Polling in Sekunden.",
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=None,
        help="Optionales Override fuer den REST-Reconcile-Loop in Sekunden.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optionales Override fuer das normale Runtime-Logfile.",
    )
    parser.add_argument(
        "--audit-log-file",
        default=None,
        help="Optionales Override fuer das Audit-JSONL-Logfile.",
    )
    parser.add_argument(
        "--strategy-state-file",
        default=None,
        help="Optionaler Pfad fuer persistierten Strategy-State.",
    )
    return parser


def build_runtime_from_args(args: argparse.Namespace):
    base_config = StrategyConfig()
    if not base_config.api_key or not base_config.secret_key:
        raise RuntimeError("API-Keys missing in env file")
    if args.symbol:
        base_config.default_symbol = args.symbol.upper()
    runtime = build_registered_runtime(args.strategy, base_config)
    if args.price_poll_interval is not None:
        runtime.config.price_poll_interval_seconds = args.price_poll_interval
    if args.reconcile_interval is not None:
        runtime.config.reconcile_interval_seconds = args.reconcile_interval
    if args.log_file:
        runtime.config.log_file = args.log_file
    if args.audit_log_file:
        runtime.config.audit_log_file = args.audit_log_file
        runtime.audit.audit_log_path = Path(args.audit_log_file)
        runtime.audit.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    if args.strategy_state_file:
        runtime.config.strategy_state_file = args.strategy_state_file
    configure_runtime_logging(runtime.config.log_file)
    return runtime


def run_runtime(strategy_name: str | None = None) -> None:
    parser = build_parser()
    cli_args = [] if strategy_name is None else ["--strategy", strategy_name]
    args = parser.parse_args(cli_args)
    runtime = build_runtime_from_args(args)
    runtime.start()
    stop_event = threading.Event()
    try:
        while not stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        runtime.stop()
        time.sleep(0.2)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    runtime = build_runtime_from_args(args)
    runtime.start()
    stop_event = threading.Event()
    try:
        while not stop_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        runtime.stop()
        time.sleep(0.2)


if __name__ == "__main__":
    main()

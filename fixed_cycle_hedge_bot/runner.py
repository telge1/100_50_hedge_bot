from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from .strategy_config_legacy import StrategyConfig

from .registry import STRATEGY_REGISTRY, build_registered_runtime, list_strategy_names
from .runtime import configure_runtime_logging
from .fixed_cycle_strategy import (
    configure_cycle_state_file,
    configure_calc_audit_log_file,
    configure_confirmed_order_pnl_history_file,
    set_default_bot_name,
)


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
    parser.add_argument(
        "--strategy-config-file",
        default=None,
        help="Optionaler JSON-Pfad fuer strategie-spezifische Konfiguration.",
    )
    parser.add_argument(
        "--bot-name",
        default="long_bot_1",
        help="Optionaler Bot-Identifikator (wird z.B. fuer Logs/Audit genutzt).",
    )
    parser.add_argument(
        "--calc-audit-log-file",
        default=None,
        help="Optionaler Pfad fuer das Calc-Audit-Log.",
    )
    parser.add_argument(
        "--confirmed-pnl-history-file",
        default=None,
        help="Optionaler Pfad fuer die confirmed order PnL History.",
    )
    return parser


def build_runtime_from_args(args: argparse.Namespace):
    env_path = Path(__file__).resolve().parents[1] / "env" / ".env.local"
    if env_path.exists():
        load_dotenv(env_path)

    base_config = StrategyConfig()
    base_config.api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY") or base_config.api_key
    base_config.secret_key = os.getenv("BYBIT_API_SECRET") or os.getenv("SECRET_KEY") or base_config.secret_key
    if not base_config.api_key or not base_config.secret_key:
        raise RuntimeError("API-Keys missing in env file")
    if args.symbol:
        base_config.default_symbol = args.symbol.upper()
    runtime = build_registered_runtime(args.strategy, base_config, args.strategy_config_file)
    if args.price_poll_interval is not None:
        runtime.config.price_poll_interval_seconds = args.price_poll_interval
    if args.reconcile_interval is not None:
        runtime.config.reconcile_interval_seconds = args.reconcile_interval
    if args.bot_name:
        runtime.config.bot_name = args.bot_name
        runtime.audit.update_extra_fields({"bot_name": runtime.config.bot_name})
    if args.calc_audit_log_file:
        runtime.config.calc_audit_log_file = args.calc_audit_log_file
    if args.confirmed_pnl_history_file:
        runtime.config.confirmed_pnl_history_file = args.confirmed_pnl_history_file
    configure_calc_audit_log_file(
        args.calc_audit_log_file or runtime.config.calc_audit_log_file
    )
    configure_confirmed_order_pnl_history_file(
        args.confirmed_pnl_history_file
        or runtime.config.confirmed_pnl_history_file
        or "logs/confirmed_order_pnl_history.jsonl"
    )
    cycle_state_file = None
    if args.strategy_state_file:
        cycle_state_file = str(
            Path(args.strategy_state_file).resolve().with_name("fixed_cycle_cycle_state.json")
        )
    configure_cycle_state_file(cycle_state_file)
    set_default_bot_name(runtime.config.bot_name)
    if args.log_file:
        runtime.config.log_file = args.log_file
    if args.audit_log_file:
        runtime.config.audit_log_file = args.audit_log_file
        runtime.audit.set_audit_log_path(runtime.config.audit_log_file)
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

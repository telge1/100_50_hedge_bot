from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .order_manager import BybitOrderManager

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "fixed_cycle_config.json"
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_CATEGORY = "linear"


def _load_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / "env" / ".env.local"
    if env_path.exists():
        load_dotenv(env_path)


def _load_config(config_file: Path | None) -> dict[str, str]:
    target = config_file or DEFAULT_CONFIG_PATH
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        logger.warning("cleanup_config_invalid", extra={"path": str(target)})
        return {}


def _resolve_symbol_and_category(
    args: argparse.Namespace, config: dict[str, str]
) -> tuple[str, str]:
    symbol = args.symbol or config.get("symbol") or DEFAULT_SYMBOL
    category = args.category or config.get("category") or DEFAULT_CATEGORY
    return symbol.upper(), category


def _resolve_api_keys(args: argparse.Namespace) -> tuple[str, str]:
    api_key = args.api_key or os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
    secret_key = args.secret_key or os.getenv("BYBIT_API_SECRET") or os.getenv("SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("API keys unavailable for cleanup command")
    return api_key, secret_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cancel open fixed-cycle orders on Bybit.")
    parser.add_argument("--symbol", help="Override symbol used for cancellation.")
    parser.add_argument("--category", help="Override Bybit category (linear/inverse/spot/option).")
    parser.add_argument("--config-file", type=Path, help="Explicit path to fixed cycle config.")
    parser.add_argument("--api-key", help="Optional API key override.")
    parser.add_argument("--secret-key", help="Optional API secret override.")
    parser.add_argument("--base-url", default="https://api.bybit.com", help="Bybit API base URL.")
    args = parser.parse_args(argv or sys.argv[1:])

    _load_env()
    config = _load_config(args.config_file)
    symbol, category = _resolve_symbol_and_category(args, config)
    try:
        api_key, secret_key = _resolve_api_keys(args)
    except RuntimeError as exc:
        logger.error("cleanup_api_key_missing", extra={"error": str(exc)})
        return 1

    manager = BybitOrderManager(api_key=api_key, secret_key=secret_key, base_url=args.base_url)
    logger.info("cleanup_cancel_all", extra={"symbol": symbol, "category": category})
    success = manager.cancel_all_orders(symbol=symbol, category=category)
    if success:
        logger.info("cleanup_cancel_all_success", extra={"symbol": symbol, "category": category})
        return 0
    logger.error("cleanup_cancel_all_failed", extra={"symbol": symbol, "category": category})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

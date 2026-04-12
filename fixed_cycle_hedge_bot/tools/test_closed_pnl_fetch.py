from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy.config_legacy import StrategyConfig

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager


ENV_ROOT = Path(__file__).resolve().parents[2] / "env" / ".env.local"


def _load_credentials() -> StrategyConfig:
    if ENV_ROOT.exists():
        load_dotenv(ENV_ROOT)
        print(f"Loaded credentials from {ENV_ROOT}")
    config = StrategyConfig()
    config.api_key = (
        os.getenv("BYBIT_API_KEY")
        or os.getenv("API_KEY")
        or config.api_key
    )
    config.secret_key = (
        os.getenv("BYBIT_API_SECRET")
        or os.getenv("SECRET_KEY")
        or config.secret_key
    )
    if not config.api_key or not config.secret_key:
        raise RuntimeError("Missing BYBIT_API_KEY / BYBIT_API_SECRET in env")
    return config


def _parse_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid timestamp: {value}") from exc
        return int(parsed.timestamp() * 1000)


def _format_row(row: Mapping[str, Any]) -> str:
    fields = (
        "orderId",
        "symbol",
        "side",
        "qty",
        "closedSize",
        "avgEntryPrice",
        "avgExitPrice",
        "closedPnl",
        "fillCount",
        "createdTime",
        "updatedTime",
        "orderType",
        "execType",
    )
    parts = []
    for name in fields:
        value = row.get(name)
        parts.append(f"{name}={value}")
    return " | ".join(parts)


def _print_rows(rows: list[Mapping[str, Any]], order_id: str | None) -> None:
    if not rows:
        print("No closed-PnL rows returned.")
        return
    match_id = order_id.strip() if order_id else None
    matched = None
    if match_id:
        for row in rows:
            if str(row.get("orderId") or "").strip() == match_id:
                matched = row
                break
    if matched:
        print("Matched row:")
        print("  ▶", _format_row(matched))
        remaining = [row for row in rows if row is not matched]
    else:
        remaining = rows
    if match_id and not matched:
        print(f"No exact match for order id {match_id}; showing candidates sorted by updatedTime:")
    for row in sorted(
        remaining,
        key=lambda r: int(r.get("updatedTime") or r.get("createdTime") or 0),
        reverse=True,
    ):
        marker = "  *" if matched and row is matched else "   "
        print(f"{marker} {_format_row(row)}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query Bybit /v5/position/closed-pnl with the bot's credentials."
    )
    parser.add_argument("--symbol", default=None, help="Symbol override.")
    parser.add_argument(
        "--category",
        default=None,
        help="Product category override (defaults to config category).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum rows to request (1-100).",
    )
    parser.add_argument(
        "--start-time-ms",
        type=_parse_ms,
        default=None,
        help="Start time in ms or ISO8601 string.",
    )
    parser.add_argument(
        "--order-id",
        default=None,
        help="Optional orderId filter (exact match).",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    config = _load_credentials()
    symbol = (args.symbol or config.default_symbol or "BTCUSDT").upper()
    category = args.category or config.category or "linear"
    limit = max(1, min(100, args.limit or 100))
    manager = BybitOrderManager(config.api_key, config.secret_key)
    response = manager.fetch_closed_pnl(
        symbol=symbol,
        category=category,
        limit=limit,
        start_time_ms=args.start_time_ms,
    )
    rows = response or []
    _print_rows(rows, args.order_id)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

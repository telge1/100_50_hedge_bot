#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

BOT_ROOT = PROJECT_ROOT / "live_bots" / "100_50_hedge_bot"
CONFIG_PATH = BOT_ROOT / "config" / "config.yaml"
LOG_PATH = BOT_ROOT / "logs" / "wallet_refill_watchdog.log"
JSON_LOG_PATH = BOT_ROOT / "logs" / "wallet_refill_watchdog.jsonl"
WATCHER_NAME = "wallet_refill_watchdog"
DEFAULT_REFILL_THRESHOLD_PCT = 50
DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT = 50
DEFAULT_CASHOUT_PROFIT_SHARE_PCT = 50
MIN_START_WALLET_USDT = 0.01


def setup_logger() -> logging.Logger:
    logger = logging.getLogger(WATCHER_NAME)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)
    return logger


def write_json_event(event: str, payload: dict[str, Any]) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    try:
        JSON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with JSON_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor wallet refill needs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--bot-name", type=str)
    parser.add_argument("--capture-start-wallet", action="store_true")
    parser.add_argument("--reset-start-wallet", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_profile_accounts(logger: logging.Logger) -> Mapping[str, dict[str, Any]]:
    if not CONFIG_PATH.exists():
        logger.error("Config missing: %s", CONFIG_PATH)
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.exception("Failed to parse config: %s", exc)
        return {}
    accounts: dict[str, dict[str, Any]] = {}
    for name, data in raw.items():
        profile = str(name).lower()
        if not profile.startswith("long_bot_"):
            continue
        if isinstance(data, dict) and "api_key" in data and "secret_key" in data:
            accounts[profile] = data
    return accounts


def resolve_bot_metadata(bot_name: str, logger: logging.Logger) -> tuple[str, str] | None:
    config_path = BOT_ROOT / bot_name / "config" / "fixed_cycle_config.json"
    if not config_path.exists():
        logger.warning("Config missing for %s", bot_name)
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read config for %s: %s", bot_name, exc)
        return None
    symbol = payload.get("symbol")
    category = payload.get("category") or "linear"
    if not symbol:
        logger.warning("No symbol in config for %s", bot_name)
        return None
    return symbol.upper(), category


def is_bot_runner_active(bot_name: str) -> bool:
    bot_dir = BOT_ROOT / bot_name
    if not bot_dir.exists():
        return False
    pid_paths = [bot_dir / "run" / "bot.pid", bot_dir / "pids" / "fixed_cycle_bot.pid"]
    for pid_path in pid_paths:
        if not pid_path.exists():
            continue
        try:
            pid = int(pid_path.read_text().strip())
        except Exception:
            continue
        if pid <= 0:
            continue
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            continue
        if "fixed_cycle_hedge_bot.runner" not in cmd:
            continue
        if bot_name in cmd or str(bot_dir / "config" / "fixed_cycle_config.json") in cmd:
            return True
    return False


def get_wallet_guard_path(bot_name: str) -> Path:
    return BOT_ROOT / bot_name / "state" / "wallet_guard.json"


def read_wallet_guard(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_wallet_guard(path: Path, data: dict[str, Any]) -> None:
    guard_dir = path.parent
    guard_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_current_wallet(order_manager: BybitOrderManager, symbol: str) -> tuple[float | None, str | None]:
    return order_manager.fetch_wallet_balance(account_type="UNIFIED", coin="USDT")


def capture_start_wallet(
    bot_name: str,
    order_manager: BybitOrderManager,
    symbol: str,
    logger: logging.Logger,
    reset: bool,
) -> None:
    path = get_wallet_guard_path(bot_name)
    guard = read_wallet_guard(path)
    if guard.get("start_wallet_usdt") and not reset:
        logger.info("[%s] start wallet already captured", bot_name)
        write_json_event("wallet_start_already_exists", {"bot_name": bot_name, "symbol": symbol})
        return
    balance, metric = fetch_current_wallet(order_manager, symbol)
    if balance is None:
        logger.warning("[%s] unable to fetch wallet for capture", bot_name)
        write_json_event("wallet_fetch_failed", {"bot_name": bot_name, "symbol": symbol})
        return
    refill_threshold_pct = guard.get("refill_threshold_pct", guard.get("threshold_pct", DEFAULT_REFILL_THRESHOLD_PCT))
    refill_threshold_wallet = balance * (refill_threshold_pct / 100.0)
    cashout_trigger_wallet = balance * (1 + DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT / 100.0)
    for legacy in ("threshold_pct", "threshold_wallet_usdt"):
        guard.pop(legacy, None)
    guard.update(
        {
            "bot_name": bot_name,
            "symbol": symbol,
            "start_wallet_usdt": balance,
            "current_wallet_usdt": balance,
            "wallet_metric_used": metric,
            "refill_threshold_pct": refill_threshold_pct,
            "refill_threshold_wallet_usdt": refill_threshold_wallet,
            "refill_required": False,
            "refill_amount_usdt": 0.0,
            "cashout_profit_trigger_pct": DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT,
            "cashout_trigger_wallet_usdt": cashout_trigger_wallet,
            "cashout_profit_share_pct": DEFAULT_CASHOUT_PROFIT_SHARE_PCT,
            "profit_usdt": 0.0,
            "cashout_required": False,
            "cashout_amount_usdt": 0.0,
            "last_refill_at": None,
            "last_cashout_at": None,
            "refill_count": 0,
            "cashout_count": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_wallet_guard(path, guard)
    write_json_event("wallet_start_captured", {"bot_name": bot_name, "symbol": symbol, "start_wallet_usdt": balance})


def monitor_wallet(
    bot_name: str,
    order_manager: BybitOrderManager,
    symbol: str,
    logger: logging.Logger,
    runner_active: bool,
) -> None:
    path = get_wallet_guard_path(bot_name)
    guard = read_wallet_guard(path)
    if "start_wallet_usdt" not in guard:
        logger.warning("[%s] no start wallet recorded", bot_name)
        write_json_event("wallet_skipped_missing_start", {"bot_name": bot_name, "symbol": symbol})
        return
    balance, metric = fetch_current_wallet(order_manager, symbol)
    if balance is None:
        write_json_event("wallet_fetch_failed", {"bot_name": bot_name, "symbol": symbol})
        return
    start_wallet = float(guard["start_wallet_usdt"])
    now = datetime.now(timezone.utc).isoformat()
    guard.update(
        {
            "current_wallet_usdt": balance,
            "wallet_metric_used": metric,
            "updated_at": now,
        }
    )
    if start_wallet < MIN_START_WALLET_USDT:
        write_wallet_guard(path, guard)
        write_json_event(
            "wallet_skipped_invalid_start_wallet",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "start_wallet_usdt": start_wallet,
                "current_wallet_usdt": balance,
                "wallet_metric_used": metric,
                "runner_active": runner_active,
            },
        )
        if not runner_active:
            write_json_event(
                "wallet_monitoring_without_active_runner",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "current_wallet_usdt": balance,
                    "refill_threshold_wallet_usdt": guard.get("refill_threshold_wallet_usdt"),
                },
            )
        return
    refill_threshold_pct = guard.get("refill_threshold_pct", guard.get("threshold_pct", DEFAULT_REFILL_THRESHOLD_PCT))
    refill_threshold_wallet = start_wallet * (refill_threshold_pct / 100.0)
    refill_required = balance <= refill_threshold_wallet
    refill_amount = max(0.0, start_wallet - balance)
    cashout_profit_trigger_pct = guard.get("cashout_profit_trigger_pct", DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT)
    cashout_profit_share_pct = guard.get("cashout_profit_share_pct", DEFAULT_CASHOUT_PROFIT_SHARE_PCT)
    cashout_trigger_wallet = start_wallet * (1 + cashout_profit_trigger_pct / 100.0)
    profit_usdt = max(0.0, balance - start_wallet)
    cashout_required = balance >= cashout_trigger_wallet
    cashout_amount = profit_usdt * (cashout_profit_share_pct / 100.0) if cashout_required else 0.0
    for legacy in ("threshold_pct", "threshold_wallet_usdt"):
        guard.pop(legacy, None)
    guard.update(
        {
            "current_wallet_usdt": balance,
            "refill_threshold_pct": refill_threshold_pct,
            "refill_threshold_wallet_usdt": refill_threshold_wallet,
            "refill_required": refill_required,
            "refill_amount_usdt": refill_amount,
            "cashout_profit_trigger_pct": cashout_profit_trigger_pct,
            "cashout_trigger_wallet_usdt": cashout_trigger_wallet,
            "cashout_profit_share_pct": cashout_profit_share_pct,
            "profit_usdt": profit_usdt,
            "cashout_required": cashout_required,
            "cashout_amount_usdt": cashout_amount,
            "wallet_metric_used": metric,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    write_wallet_guard(path, guard)
    common_payload = {
        "bot_name": bot_name,
        "symbol": symbol,
        "current_wallet_usdt": balance,
        "start_wallet_usdt": start_wallet,
        "wallet_metric_used": metric,
        "refill_threshold_wallet_usdt": refill_threshold_wallet,
        "refill_required": refill_required,
        "refill_amount_usdt": refill_amount,
        "cashout_trigger_wallet_usdt": cashout_trigger_wallet,
        "profit_usdt": profit_usdt,
        "cashout_required": cashout_required,
        "cashout_amount_usdt": cashout_amount,
        "runner_active": runner_active,
    }
    write_json_event("wallet_check", common_payload)
    if not runner_active:
        write_json_event(
            "wallet_monitoring_without_active_runner",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "current_wallet_usdt": balance,
                "refill_threshold_wallet_usdt": refill_threshold_wallet,
            },
        )
    if refill_required:
        write_json_event(
            "wallet_refill_required",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "refill_amount_usdt": refill_amount,
                "runner_active": runner_active,
            },
        )
        write_json_event(
            "wallet_refill_skipped_phase1_no_transfer",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "refill_amount_usdt": refill_amount,
                "runner_active": runner_active,
            },
        )
    if cashout_required:
        cashout_payload = {
            "bot_name": bot_name,
            "symbol": symbol,
            "start_wallet_usdt": start_wallet,
            "current_wallet_usdt": balance,
            "profit_usdt": profit_usdt,
            "cashout_amount_usdt": cashout_amount,
            "cashout_profit_trigger_pct": cashout_profit_trigger_pct,
            "cashout_profit_share_pct": cashout_profit_share_pct,
            "runner_active": runner_active,
        }
        write_json_event("wallet_cashout_required", cashout_payload)
        write_json_event("wallet_cashout_skipped_phase1_no_transfer", cashout_payload)


def main() -> None:
    args = parse_args()
    logger = setup_logger()
    accounts = load_profile_accounts(logger)

    first_iteration = True if args.loop else False

    def run_cycle(reset_start: bool) -> None:
        for profile_name, data in accounts.items():
            if args.bot_name and profile_name != args.bot_name:
                continue
            api_key = (data.get("api_key") or "").strip()
            secret_key = (data.get("secret_key") or "").strip()
            if not api_key or not secret_key:
                continue
            bot_name = profile_name.lower()
            resolved = resolve_bot_metadata(bot_name, logger)
            if not resolved:
                continue
            symbol, category = resolved
            order_manager = BybitOrderManager(api_key, secret_key)
            if args.capture_start_wallet:
                capture_start_wallet(bot_name, order_manager, symbol, logger, args.reset_start_wallet)
            runner_active = is_bot_runner_active(bot_name)
            monitor_wallet(bot_name, order_manager, symbol, logger, runner_active)

    if args.capture_start_wallet:
        run_cycle(args.reset_start_wallet)

    if not args.once and not args.loop:
        args.once = True

    while True:
        if args.once and not args.loop and not first_iteration:
            break
        run_cycle(False)
        first_iteration = False
        if not args.loop:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()

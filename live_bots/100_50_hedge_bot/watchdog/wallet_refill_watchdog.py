#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

BOT_ROOT = PROJECT_ROOT / "live_bots" / "100_50_hedge_bot"
CONFIG_PATH = BOT_ROOT / "config" / "config.yaml"
EXECUTOR_PATH = BOT_ROOT / "watchdog" / "wallet_transfer_executor.py"
LOG_PATH = BOT_ROOT / "logs" / "wallet_refill_watchdog.log"
JSON_LOG_PATH = BOT_ROOT / "logs" / "wallet_refill_watchdog.jsonl"
WATCHER_NAME = "wallet_refill_watchdog"
DEFAULT_REFILL_THRESHOLD_PCT = 50
DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT = 50
DEFAULT_CASHOUT_PROFIT_SHARE_PCT = 50
MIN_START_WALLET_USDT = 0.01
DEFAULT_TRANSFER_ENABLED = False
DEFAULT_TRANSFER_DRY_RUN = True
DEFAULT_TRANSFER_COIN = "USDT"
DEFAULT_MIN_TRANSFER_AMOUNT = 1.0
DEFAULT_TRANSFER_COOLDOWN = 300
DEFAULT_RESET_TRANSFER_COOLDOWN_ON_START = True


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


def _validate_runner_process(bot_name: str, logger: logging.Logger) -> bool:
    bot_dir = BOT_ROOT / bot_name
    pid_file = bot_dir / "run" / "bot.pid"
    reason = None
    if not pid_file.exists():
        reason = "no_pid_file"
    else:
        try:
            pid = int(pid_file.read_text().strip())
        except Exception:
            reason = "invalid_pid"
        else:
            if pid <= 0:
                reason = "invalid_pid"
            else:
                cmd_path = Path(f"/proc/{pid}/cmdline")
                if not cmd_path.exists():
                    reason = "pid_not_running"
                else:
                    try:
                        cmdline = cmd_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
                    except Exception:
                        reason = "cmdline_unreadable"
                    else:
                        tokens = [
                            "fixed_cycle_hedge_bot.runner",
                            f"--bot-name {bot_name}",
                            str(PROJECT_ROOT),
                        ]
                        for token in tokens:
                            if token not in cmdline:
                                reason = f"missing_{token.replace(' ', '_')}"
                                break
    if reason:
        logger.info("watchdog_skip_inactive_bot bot=%s reason=%s", bot_name, reason)
        return False
    return True


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


@dataclass
class TransferSettings:
    enabled: bool
    dry_run: bool
    config_file: Path
    coin: str
    min_amount: Decimal
    cooldown_seconds: int
    reset_cooldown_on_start: bool
    allow_transfer_without_runner: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor wallet refill needs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--bot-name", type=str)
    parser.add_argument("--capture-start-wallet", action="store_true")
    parser.add_argument("--reset-start-wallet", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebaseline-on-start", dest="rebaseline_on_start", action="store_true")
    parser.add_argument("--no-rebaseline-on-start", dest="rebaseline_on_start", action="store_false")
    parser.set_defaults(rebaseline_on_start=None)
    parser.add_argument("--reset-transfer-cooldown-on-start", dest="reset_transfer_cooldown_on_start", action="store_true")
    parser.add_argument("--no-reset-transfer-cooldown-on-start", dest="reset_transfer_cooldown_on_start", action="store_false")
    parser.set_defaults(reset_transfer_cooldown_on_start=None)
    parser.add_argument("--allow-transfer-without-runner", action="store_true")
    parser.add_argument("--no-allow-transfer-without-runner", action="store_false", dest="allow_transfer_without_runner")
    parser.set_defaults(allow_transfer_without_runner=False)
    parser.add_argument("--enable-transfer", action="store_true")
    parser.add_argument("--transfer-dry-run", dest="transfer_dry_run", action="store_true")
    parser.add_argument("--no-transfer-dry-run", dest="transfer_dry_run", action="store_false")
    parser.set_defaults(transfer_dry_run=None)
    parser.add_argument("--transfer-config-file", type=Path, default=CONFIG_PATH)
    parser.add_argument("--transfer-coin", type=str, default=DEFAULT_TRANSFER_COIN)
    parser.add_argument("--min-transfer-amount", type=float, default=DEFAULT_MIN_TRANSFER_AMOUNT)
    parser.add_argument("--transfer-cooldown-seconds", type=int, default=DEFAULT_TRANSFER_COOLDOWN)
    return parser.parse_args()


def format_decimal(amount: Decimal) -> str:
    normalized = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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


def load_transfer_defaults(logger: logging.Logger) -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.exception("Failed to parse transfer defaults: %s", exc)
        return {}
    transfer_section = raw.get("wallet_transfer") or {}
    defaults = {
        "auto_transfer_enabled": transfer_section.get("auto_transfer_enabled", raw.get("auto_transfer_enabled", DEFAULT_TRANSFER_ENABLED)),
        "transfer_dry_run": transfer_section.get("transfer_dry_run", raw.get("transfer_dry_run", DEFAULT_TRANSFER_DRY_RUN)),
        "transfer_config_file": transfer_section.get("transfer_config_file", raw.get("transfer_config_file")),
        "transfer_coin": transfer_section.get("transfer_coin", raw.get("transfer_coin", DEFAULT_TRANSFER_COIN)),
        "min_transfer_amount": transfer_section.get("min_transfer_amount", raw.get("min_transfer_amount", DEFAULT_MIN_TRANSFER_AMOUNT)),
        "transfer_cooldown_seconds": transfer_section.get("transfer_cooldown_seconds", raw.get("transfer_cooldown_seconds", DEFAULT_TRANSFER_COOLDOWN)),
    }
    return defaults


def resolve_transfer_settings(
    args: argparse.Namespace, logger: logging.Logger, rebaseline_enabled: bool
) -> TransferSettings:
    defaults = load_transfer_defaults(logger)
    enabled = args.enable_transfer or bool(defaults.get("auto_transfer_enabled", DEFAULT_TRANSFER_ENABLED))
    dry_run = args.transfer_dry_run if args.transfer_dry_run is not None else bool(defaults.get("transfer_dry_run", DEFAULT_TRANSFER_DRY_RUN))
    config_file = args.transfer_config_file or Path(defaults.get("transfer_config_file") or CONFIG_PATH)
    coin = args.transfer_coin or str(defaults.get("transfer_coin", DEFAULT_TRANSFER_COIN))
    min_amount = Decimal(str(args.min_transfer_amount or defaults.get("min_transfer_amount", DEFAULT_MIN_TRANSFER_AMOUNT)))
    cooldown = int(args.transfer_cooldown_seconds or defaults.get("transfer_cooldown_seconds", DEFAULT_TRANSFER_COOLDOWN))
    reset_on_start = (
        args.reset_transfer_cooldown_on_start
        if args.reset_transfer_cooldown_on_start is not None
        else (DEFAULT_RESET_TRANSFER_COOLDOWN_ON_START if rebaseline_enabled else False)
    )
    return TransferSettings(
        enabled=enabled,
        dry_run=dry_run,
        config_file=config_file,
        coin=coin,
        min_amount=min_amount,
        cooldown_seconds=cooldown,
        reset_cooldown_on_start=reset_on_start,
        allow_transfer_without_runner=args.allow_transfer_without_runner,
    )


def resolve_rebaseline_setting(args: argparse.Namespace) -> bool:
    return True if args.rebaseline_on_start is None else args.rebaseline_on_start


def initialize_wallet_baselines_on_start(
    accounts: Mapping[str, dict[str, Any]],
    logger: logging.Logger,
    enabled: bool,
    transfer_settings: TransferSettings,
) -> None:
    if not enabled or not accounts:
        return
    write_json_event("wallet_baseline_init_started", {"bot_name": "all", "count": len(accounts)})
    for profile_name, data in accounts.items():
        bot_name = profile_name
        symbol_info = resolve_bot_metadata(bot_name, logger)
        if not symbol_info:
            continue
        symbol, _ = symbol_info
        runner_active = is_bot_runner_active(bot_name)
        guard_path = get_wallet_guard_path(bot_name)
        guard = read_wallet_guard(guard_path)
        old_start = guard.get("start_wallet_usdt")
        if runner_active:
            write_json_event(
                "wallet_baseline_init_skipped_runner_active",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "current_wallet_usdt": guard.get("current_wallet_usdt"),
                    "old_start_wallet_usdt": old_start,
                    "new_start_wallet_usdt": old_start,
                    "wallet_metric_used": guard.get("wallet_metric_used"),
                    "runner_active": True,
                },
            )
            continue
        api_key = (data.get("api_key") or "").strip()
        secret_key = (data.get("secret_key") or "").strip()
        if not api_key or not secret_key:
            continue
        order_manager = BybitOrderManager(api_key, secret_key)
        balance, metric = fetch_current_wallet(order_manager, symbol)
        if balance is None or balance <= 0:
            write_json_event(
                "wallet_baseline_init_skipped_invalid_wallet",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "current_wallet_usdt": balance,
                    "old_start_wallet_usdt": old_start,
                    "new_start_wallet_usdt": old_start,
                    "wallet_metric_used": metric,
                    "runner_active": runner_active,
                },
            )
            continue
        guard.update(
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "start_wallet_usdt": balance,
                "current_wallet_usdt": balance,
                "baseline_wallet_usdt": balance,
                "wallet_metric_used": metric,
                "refill_required": False,
                "refill_amount_usdt": 0.0,
                "cashout_required": False,
                "cashout_amount_usdt": 0.0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        old_last = None
        old_amount = None
        if transfer_settings.reset_cooldown_on_start:
            old_last = guard.pop("last_auto_transfer_at", None)
            old_amount = guard.pop("last_auto_transfer_amount_usdt", None)
            if old_last or old_amount:
                write_json_event(
                    "wallet_refill_transfer_cooldown_reset_on_start",
                    {
                        "bot_name": bot_name,
                        "old_last_auto_transfer_at": old_last,
                        "old_last_auto_transfer_amount_usdt": old_amount,
                        "reason": "rebaseline_on_start",
                    },
                )
        write_wallet_guard(guard_path, guard)
        write_json_event(
            "wallet_baseline_initialized",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "current_wallet_usdt": balance,
                "old_start_wallet_usdt": old_start,
                "new_start_wallet_usdt": balance,
                "wallet_metric_used": metric,
                "runner_active": runner_active,
            },
        )
    write_json_event("wallet_baseline_init_completed", {"bot_name": "all", "count": len(accounts)})


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


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def build_transfer_event_payload(
    bot_name: str,
    current_wallet: float,
    threshold_wallet: float,
    requested_amount: float,
    final_amount: float,
    coin: str,
    transfer_settings: TransferSettings,
    executor_path: Path,
) -> dict[str, Any]:
    return {
        "bot_name": bot_name,
        "current_wallet_usdt": current_wallet,
        "threshold_wallet_usdt": threshold_wallet,
        "requested_amount": requested_amount,
        "final_amount": final_amount,
        "coin": coin,
        "transfer_dry_run": transfer_settings.dry_run,
        "config_file": str(transfer_settings.config_file),
        "executor_path": str(executor_path),
    }


def handle_auto_refill_transfer(
    bot_name: str,
    guard: dict[str, Any],
    path: Path,
    current_wallet: float,
    threshold_wallet: float,
    refill_amount: float,
    transfer_settings: TransferSettings,
    logger: logging.Logger,
    runner_active: bool,
) -> None:
    requested_amount = float(refill_amount)
    final_decimal = max(Decimal(str(refill_amount)), transfer_settings.min_amount)
    quantized_amount = final_decimal.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    final_amount = float(quantized_amount)
    formatted_amount = format_decimal(quantized_amount)
    base_payload = build_transfer_event_payload(
        bot_name,
        current_wallet,
        threshold_wallet,
        requested_amount,
        final_amount,
        transfer_settings.coin,
        transfer_settings,
        EXECUTOR_PATH,
    )
    write_json_event("wallet_refill_transfer_needed", base_payload)
    if not transfer_settings.enabled:
        write_json_event("wallet_refill_transfer_skipped_disabled", base_payload)
        return
    if not runner_active:
        if transfer_settings.allow_transfer_without_runner:
            write_json_event(
                "wallet_refill_transfer_runner_inactive_override_enabled",
                {
                    **base_payload,
                    "runner_active": runner_active,
                },
            )
        else:
            write_json_event(
                "wallet_refill_transfer_skipped_runner_inactive",
                {
                    **base_payload,
                    "runner_active": runner_active,
                },
            )
            return
    now = datetime.now(timezone.utc)
    cooldown = transfer_settings.cooldown_seconds
    last_transfer = iso_to_datetime(guard.get("last_auto_transfer_at"))
    if last_transfer:
        seconds_since_last = (now - last_transfer).total_seconds()
        if seconds_since_last < cooldown:
            cooldown_payload = {
                **base_payload,
                "final_amount": final_amount,
                "cooldown_seconds": cooldown,
                "last_auto_transfer_at": guard.get("last_auto_transfer_at"),
                "seconds_since_last_transfer": seconds_since_last,
            }
            write_json_event("wallet_refill_transfer_skipped_cooldown", cooldown_payload)
            return
    if not transfer_settings.config_file.exists():
        error_payload = {**base_payload, "error": "transfer config file missing"}
        write_json_event("wallet_refill_transfer_executor_failed", error_payload)
        return
    if not EXECUTOR_PATH.exists():
        error_payload = {**base_payload, "error": f"executor missing at {EXECUTOR_PATH}"}
        write_json_event("wallet_refill_transfer_executor_failed", error_payload)
        return
    formatted_amount = format_decimal(quantized_amount)
    cmd = [
        sys.executable,
        str(EXECUTOR_PATH),
        "--config-file",
        str(transfer_settings.config_file),
        "--bot-name",
        bot_name,
        "--direction",
        "refill",
        "--amount",
        formatted_amount,
        "--coin",
        transfer_settings.coin,
    ]
    if transfer_settings.dry_run:
        cmd.append("--dry-run")
    started_payload = {**base_payload, "final_amount": final_amount}
    if transfer_settings.dry_run:
        logger.info("Dry-run transfer: runner_active=%s", runner_active)
    write_json_event("wallet_refill_transfer_executor_started", started_payload)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        failure = (
            result.returncode != 0
            or "ERROR:" in stderr
            or "Transfer failed" in stderr
        )
        transfer_payload = {
            **base_payload,
            "final_amount": final_amount,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "requested_amount": requested_amount,
        }
        if failure:
            write_json_event("wallet_refill_transfer_executor_failed", transfer_payload)
            return
        write_json_event("wallet_refill_transfer_executor_success", transfer_payload)
    except Exception as exc:
        logger.exception("Transfer executor failed", exc_info=exc)
        error_payload = {**base_payload, "error": str(exc)}
        write_json_event("wallet_refill_transfer_executor_failed", error_payload)
        return
    guard["last_auto_transfer_at"] = now.isoformat()
    guard["last_auto_transfer_amount_usdt"] = final_amount
    guard["auto_transfer_count"] = guard.get("auto_transfer_count", 0) + 1
    guard["updated_at"] = now.isoformat()
    write_wallet_guard(path, guard)


def handle_auto_cashout_transfer(
    bot_name: str,
    guard: dict[str, Any],
    path: Path,
    current_wallet: float,
    start_wallet: float,
    profit_usdt: float,
    cashout_amount: float,
    transfer_settings: TransferSettings,
    logger: logging.Logger,
    runner_active: bool,
) -> None:
    requested_amount = float(cashout_amount)
    final_decimal = max(Decimal(str(cashout_amount)), transfer_settings.min_amount)
    quantized_amount = final_decimal.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    final_amount = float(quantized_amount)
    formatted_amount = format_decimal(quantized_amount)
    base_payload = build_transfer_event_payload(
        bot_name,
        current_wallet,
        start_wallet,
        requested_amount,
        final_amount,
        transfer_settings.coin,
        transfer_settings,
        EXECUTOR_PATH,
    )
    base_payload.update(
        {
            "start_wallet_usdt": start_wallet,
            "profit_usdt": profit_usdt,
            "runner_active": runner_active,
        }
    )
    base_payload.pop("threshold_wallet_usdt", None)
    write_json_event("wallet_cashout_transfer_needed", base_payload)
    if not transfer_settings.enabled:
        write_json_event("wallet_cashout_transfer_skipped_disabled", base_payload)
        return
    if not runner_active:
        if transfer_settings.allow_transfer_without_runner:
            write_json_event(
                "wallet_cashout_transfer_runner_inactive_override_enabled",
                {**base_payload},
            )
        else:
            write_json_event(
                "wallet_cashout_transfer_skipped_runner_inactive",
                {**base_payload},
            )
            return
    now = datetime.now(timezone.utc)
    cooldown = transfer_settings.cooldown_seconds
    last_transfer = iso_to_datetime(guard.get("last_auto_cashout_at"))
    if last_transfer:
        seconds_since_last = (now - last_transfer).total_seconds()
        if seconds_since_last < cooldown:
            cooldown_payload = {
                **base_payload,
                "final_amount": final_amount,
                "cooldown_seconds": cooldown,
                "last_auto_cashout_at": guard.get("last_auto_cashout_at"),
                "seconds_since_last_transfer": seconds_since_last,
            }
            write_json_event("wallet_cashout_transfer_skipped_cooldown", cooldown_payload)
            return
    if not transfer_settings.config_file.exists():
        error_payload = {**base_payload, "error": "transfer config file missing"}
        write_json_event("wallet_cashout_transfer_executor_failed", error_payload)
        return
    if not EXECUTOR_PATH.exists():
        error_payload = {**base_payload, "error": f"executor missing at {EXECUTOR_PATH}"}
        write_json_event("wallet_cashout_transfer_executor_failed", error_payload)
        return
    cmd = [
        sys.executable,
        str(EXECUTOR_PATH),
        "--config-file",
        str(transfer_settings.config_file),
        "--bot-name",
        bot_name,
        "--direction",
        "cashout",
        "--amount",
        formatted_amount,
        "--coin",
        transfer_settings.coin,
    ]
    if transfer_settings.dry_run:
        cmd.append("--dry-run")
    started_payload = {**base_payload, "final_amount": final_amount}
    if transfer_settings.dry_run:
        logger.info("Dry-run cashout transfer: runner_active=%s", runner_active)
    write_json_event("wallet_cashout_transfer_executor_started", started_payload)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        failure = (
            result.returncode != 0
            or "ERROR:" in stderr
            or "Transfer failed" in stderr
        )
        transfer_payload = {
            **base_payload,
            "final_amount": final_amount,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "requested_amount": requested_amount,
        }
        if failure:
            write_json_event("wallet_cashout_transfer_executor_failed", transfer_payload)
            return
        write_json_event("wallet_cashout_transfer_executor_success", transfer_payload)
    except Exception as exc:
        logger.exception("Cashout executor failed", exc_info=exc)
        error_payload = {**base_payload, "error": str(exc)}
        write_json_event("wallet_cashout_transfer_executor_failed", error_payload)
        return
    guard["last_auto_cashout_at"] = now.isoformat()
    guard["last_auto_cashout_amount_usdt"] = final_amount
    guard["auto_cashout_count"] = guard.get("auto_cashout_count", 0) + 1
    guard["updated_at"] = now.isoformat()
    write_wallet_guard(path, guard)


def monitor_wallet(
    bot_name: str,
    order_manager: BybitOrderManager,
    symbol: str,
    logger: logging.Logger,
    runner_active: bool,
    transfer_settings: TransferSettings,
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
    should_log_check = refill_required or cashout_required or not runner_active
    if should_log_check:
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
        handle_auto_refill_transfer(
            bot_name,
            guard,
            path,
            balance,
            refill_threshold_wallet,
            refill_amount,
            transfer_settings,
            logger,
            runner_active,
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
        handle_auto_cashout_transfer(
            bot_name,
            guard,
            path,
            balance,
            start_wallet,
            profit_usdt,
            cashout_amount,
            transfer_settings,
            logger,
            runner_active,
        )


def main() -> None:
    args = parse_args()
    logger = setup_logger()
    rebaseline_enabled = resolve_rebaseline_setting(args)
    transfer_settings = resolve_transfer_settings(args, logger, rebaseline_enabled)
    accounts = load_profile_accounts(logger)
    initialize_wallet_baselines_on_start(accounts, logger, rebaseline_enabled, transfer_settings)

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
            if not _validate_runner_process(bot_name, logger):
                continue
            resolved = resolve_bot_metadata(bot_name, logger)
            if not resolved:
                continue
            symbol, category = resolved
            order_manager = BybitOrderManager(api_key, secret_key)
            if args.capture_start_wallet:
                capture_start_wallet(bot_name, order_manager, symbol, logger, args.reset_start_wallet)
            runner_active = is_bot_runner_active(bot_name)
            monitor_wallet(bot_name, order_manager, symbol, logger, runner_active, transfer_settings)

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

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from fixed_cycle_hedge_bot.order_manager import BybitOrderManager

BOT_ROOT = PROJECT_ROOT / "live_bots" / "100_50_hedge_bot"
CONFIG_PATH = BOT_ROOT / "config" / "config.yaml"
LOG_PATH = BOT_ROOT / "logs" / "safety_order_watchdog.log"
DEBUG_LOG_PATH = BOT_ROOT / "logs" / "safety_order_watchdog_debug.jsonl"
SIGNIFICANT_DEBUG_EVENTS = {
    "safety_action_required",
    "safety_action_result",
}
STOP_SCRIPT = BOT_ROOT / "shared_scripts" / "stop_with_cleanup.sh"
RETRY_DELAYS = [0.0, 2.0, 2.0, 3.0]


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("safety_order_watchdog")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        logger.handlers.clear()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)
    return logger


def _validate_runner_process(bot_name: str, logger: logging.Logger) -> bool:
    bot_name = bot_name.lower()
    pid_file = BOT_ROOT / bot_name / "run" / "bot.pid"
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
                proc_cmd = Path(f"/proc/{pid}/cmdline")
                if not proc_cmd.exists():
                    reason = "pid_not_running"
                else:
                    try:
                        cmdline = proc_cmd.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
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


def write_debug_event(logger: logging.Logger, event_name: str, payload: dict[str, Any]) -> None:
    if event_name not in SIGNIFICANT_DEBUG_EVENTS:
        return
    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now().astimezone().isoformat(), "event": event_name, **payload}
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Failed to write debug event %s: %s", event_name, exc)


def required_groups_for_position(long_qty: float, short_qty: float) -> list[str]:
    groups: list[str] = []
    if long_qty > 0:
        groups.append("long_exit")
    if short_qty > 0:
        groups.append("short_exit")
    if long_qty > 0 or short_qty > 0:
        groups.append("cycle")
    return groups


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safety watchdog for fixed-cycle hedge bots")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Run the check exactly once (default behavior)")
    group.add_argument("--loop", action="store_true", help="Continuous loop with --interval wait")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds when --loop is set")
    parser.add_argument("--dry-run", action="store_true", help="Log actions but do not cancel/close/kill")
    return parser.parse_args()


def load_profile_accounts(logger: logging.Logger) -> Mapping[str, dict[str, Any]]:
    if not CONFIG_PATH.exists():
        logger.error("Config file missing: %s", CONFIG_PATH)
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.exception("Failed to parse %s", CONFIG_PATH)
        return {}

    accounts: dict[str, dict[str, Any]] = {}
    for name, data in raw.items():
        profile_name = str(name).lower()
        if not profile_name.startswith("long_bot_"):
            continue
        if (
            isinstance(data, dict)
            and "api_key" in data
            and "secret_key" in data
        ):
            accounts[profile_name] = data
    return accounts


def resolve_bot_metadata(bot_name: str, logger: logging.Logger) -> tuple[str, str] | None:
    bot_name = bot_name.lower()
    config_path = BOT_ROOT / bot_name / "config" / "fixed_cycle_config.json"
    if not config_path.exists():
        logger.warning("Config missing for bot %s; skipping", bot_name)
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "Failed to read config for %s: %s", bot_name, exc, exc_info=False
        )
        return None
    symbol = payload.get("symbol")
    category = payload.get("category")
    if not symbol:
        logger.warning("No symbol found for bot %s; skipping", bot_name)
        return None
    return (symbol.upper(), category or "linear")


def load_bot_runtime_state(bot_name: str, logger: logging.Logger) -> dict[str, Any]:
    state_path = BOT_ROOT / bot_name.lower() / "state" / "fixed_cycle_state.json"
    if not state_path.exists():
        return {"path": str(state_path), "strategy_state": {}, "active_orders": []}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read strategy state for %s: %s", bot_name, exc, exc_info=False)
        return {"path": str(state_path), "strategy_state": {}, "active_orders": []}
    strategy_state = payload.get("strategy_state") if isinstance(payload, dict) else {}
    active_orders = payload.get("active_orders") if isinstance(payload, dict) else []
    return {
        "path": str(state_path),
        "strategy_state": strategy_state if isinstance(strategy_state, dict) else {},
        "active_orders": active_orders if isinstance(active_orders, list) else [],
    }


def detect_active_symbol_and_qty(
    order_manager: BybitOrderManager, symbol: str, category: str
) -> tuple[str | None, float, float]:
    settle_coin = "USDT" if str(category or "").lower() == "linear" else None
    positions = order_manager.fetch_positions(
        symbol=None,
        category=category,
        settle_coin=settle_coin,
    )
    if not positions and symbol:
        positions = order_manager.fetch_positions(symbol=symbol, category=category)
    if not positions:
        return None, 0.0, 0.0
    totals: dict[str, dict[str, float]] = {}
    for pos in positions:
        symbol = str(pos.get("symbol") or "").upper()
        if not symbol:
            info = pos.get("info") or {}
            symbol = str(info.get("symbol") or "").upper()
        if not symbol:
            continue
        size_raw = pos.get("size") or (pos.get("info") or {}).get("size")
        try:
            size = float(size_raw or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        side = str(pos.get("side") or (pos.get("info") or {}).get("side") or "").lower()
        entry = totals.setdefault(symbol, {"long": 0.0, "short": 0.0})
        if side in {"buy", "long"}:
            entry["long"] += size
        elif side in {"sell", "short"}:
            entry["short"] += size
    if not totals:
        return None, 0.0, 0.0
    symbol, data = max(totals.items(), key=lambda x: x[1]["long"] + x[1]["short"])
    return symbol, data["long"], data["short"]


def summarize_order(order: Mapping[str, Any]) -> dict[str, Any]:
    def safe_str(value: Any) -> str:
        return str(value) if value is not None else ""

    def safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    summary = {
        "orderLinkId": safe_str(order.get("orderLinkId") or order.get("order_link_id") or order.get("clientOrderId")),
        "client_order_id": safe_str(order.get("client_order_id")),
        "purpose": safe_str(order.get("purpose")),
        "side": safe_str(order.get("side")),
        "qty": safe_float(order.get("qty")),
        "status": safe_str(order.get("orderStatus") or order.get("order_status")),
    }
    return summary


def extract_order_signature(order: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("orderLinkId", "clientOrderId", "order_link_id", "client_order_id", "purpose"):
        value = order.get(key)
        if isinstance(value, str) and value:
            pieces.append(value.upper())
    return " ".join(pieces)


def classify_order_groups(orders: list[Mapping[str, Any]]) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    groups = {"long_exit": False, "short_exit": False, "cycle": False, "refill": False}
    summary: list[dict[str, Any]] = []
    for order in orders:
        signature = extract_order_signature(order)
        summary.append(summarize_order(order))
        if "LONG_TP_EXIT" in signature or "LONG_TP_EXIT_RECOVERY" in signature:
            groups["long_exit"] = True
        if "SHORT_SL_EXIT" in signature or "SHORT_SL_EXIT_RECOVERY" in signature:
            groups["short_exit"] = True
        if "CYCLE_" in signature:
            groups["cycle"] = True
        if "REFILL_LONG" in signature or "REFILL_SHORT" in signature:
            groups["refill"] = True
    return groups, summary


def fetch_positions(order_manager: BybitOrderManager, symbol: str, category: str) -> tuple[float, float]:
    long_qty = 0.0
    short_qty = 0.0
    positions = order_manager.fetch_positions(symbol=symbol, category=category)
    for pos in positions:
        try:
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        side = str(pos.get("side") or "").capitalize()
        if side == "Buy":
            long_qty = max(long_qty, size)
        elif side == "Sell":
            short_qty = max(short_qty, size)
    return long_qty, short_qty


def kill_bot_process(bot_name: str, logger: logging.Logger, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"status": "dry-run"}
    if not STOP_SCRIPT.exists():
        logger.warning("Stop script missing: %s", STOP_SCRIPT)
        return {"status": "stop_script_missing"}
    logger.info("Invoking stop_with_cleanup for %s", bot_name)
    try:
        process = subprocess.run(
            [str(STOP_SCRIPT), bot_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "status": "success" if process.returncode == 0 else "failed",
            "returncode": process.returncode,
            "stdout": process.stdout.strip(),
            "stderr": process.stderr.strip(),
        }
    except Exception as exc:
        logger.exception("Failed to kill %s: %s", bot_name, exc)
        return {"status": "exception", "error": str(exc)}


def log_safety_event(
    logger: logging.Logger,
    profile_name: str,
    bot_name: str,
    symbol: str,
    long_qty: float,
    short_qty: float,
    orders: list[dict[str, Any]],
    missing_groups: list[str],
    actions: list[str],
    dry_run: bool,
    results: dict[str, Any],
) -> None:
        event = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "profile": profile_name,
            "bot_name": bot_name,
            "symbol": symbol,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "missing_groups": missing_groups,
            "open_orders": orders,
            "actions": actions,
            "dry_run": dry_run,
            "outcome": results,
        }
        logger.warning("safety_event %s", json.dumps(event, ensure_ascii=False))


def handle_profile(
    profile_name: str, data: dict[str, Any], logger: logging.Logger, dry_run: bool
) -> None:
    api_key = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    if not api_key or not secret_key:
        return
    bot_name = profile_name.lower()
    if not _validate_runner_process(bot_name, logger):
        return
    resolved = resolve_bot_metadata(bot_name, logger)
    if not resolved:
        return
    config_symbol, category = resolved
    order_manager = BybitOrderManager(api_key, secret_key)
    detected_symbol, long_qty, short_qty = detect_active_symbol_and_qty(
        order_manager, config_symbol, category
    )
    symbol = detected_symbol or config_symbol
    write_debug_event(
        logger,
        "profile_check_started",
        {
            "profile_name": profile_name,
            "bot_name": bot_name,
            "resolved_symbol": config_symbol,
            "category": category,
        },
    )
    if not detected_symbol:
        write_debug_event(
            logger,
            "positions_fetched",
            {
                "profile_name": profile_name,
                "bot_name": bot_name,
                "symbol": symbol,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "has_position": False,
            },
        )
        write_debug_event(
            logger,
            "profile_skipped_no_position",
            {
                "profile_name": profile_name,
                "bot_name": bot_name,
                "symbol": symbol,
            },
        )
        return
    write_debug_event(
        logger,
        "positions_fetched",
        {
            "profile_name": profile_name,
            "bot_name": bot_name,
            "symbol": symbol,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "has_position": long_qty > 0 or short_qty > 0,
        },
    )
    has_position = long_qty > 0 or short_qty > 0
    if not has_position:
        write_debug_event(
            logger,
            "profile_skipped_no_position",
            {
                "profile_name": profile_name,
                "bot_name": bot_name,
                "symbol": symbol,
            },
        )
        return

    has_position = long_qty > 0 or short_qty > 0
    order_manager = BybitOrderManager(api_key, secret_key)

    successful_orders: list[dict[str, Any]] = []
    groups_present: dict[str, bool] = {"long_exit": False, "short_exit": False, "cycle": False, "refill": False}
    fetched_once = False
    missing_groups: list[str] = []
    required_groups = required_groups_for_position(long_qty, short_qty)
    logged_refill_pending_tolerated = False

    for attempt_idx, delay in enumerate(RETRY_DELAYS):
        if attempt_idx > 0:
            time.sleep(delay)
        orders = order_manager.fetch_open_orders(symbol=symbol, category=category)
        if orders is None:
            logger.warning("Failed to fetch orders for %s (%s)", profile_name, symbol)
            continue
        fetched_once = True
        runtime_state_payload = load_bot_runtime_state(bot_name, logger)
        strategy_state = runtime_state_payload.get("strategy_state") or {}
        persisted_active_orders = runtime_state_payload.get("active_orders") or []
        combined_orders = list(orders) + list(persisted_active_orders)
        groups_present, successful_orders = classify_order_groups(combined_orders)
        required_groups = required_groups_for_position(long_qty, short_qty)
        bot_state = str(strategy_state.get("bot_state") or "").upper()
        refill_pending = bot_state == "REFILL_PENDING" or bool(strategy_state.get("refill_pending"))
        refill_in_progress = bool(strategy_state.get("refill_in_progress"))
        refill_state = strategy_state.get("refill_state") or {}
        if refill_pending:
            required_groups = [name for name in required_groups if name != "cycle"]
            if refill_in_progress:
                required_groups.append("refill")
                if not groups_present.get("refill"):
                    logger.warning(
                        "safety_refill_in_progress_without_orders bot=%s symbol=%s attempt=%s refill_state=%s",
                        bot_name,
                        symbol,
                        attempt_idx + 1,
                        refill_state,
                    )
            elif not logged_refill_pending_tolerated:
                logger.info(
                    "safety_refill_pending_cycle_gap_tolerated bot=%s symbol=%s refill_state=%s",
                    bot_name,
                    symbol,
                    refill_state,
                )
                logged_refill_pending_tolerated = True
        missing_groups = [name for name in required_groups if not groups_present.get(name)]
        write_debug_event(
            logger,
            "order_group_check",
            {
                "profile_name": profile_name,
                "bot_name": bot_name,
                "symbol": symbol,
                "attempt": attempt_idx + 1,
                "delay_before_attempt": RETRY_DELAYS[attempt_idx],
                "groups_present": groups_present,
                "required_groups": required_groups,
                "missing_groups": missing_groups,
                "open_orders_summary": successful_orders,
                "bot_state": bot_state,
                "refill_pending": refill_pending,
                "refill_in_progress": refill_in_progress,
                "refill_state": refill_state,
            },
        )
        if not missing_groups:
            write_debug_event(
                logger,
                "profile_ok_all_required_orders_present",
                {
                    "profile_name": profile_name,
                    "bot_name": bot_name,
                    "symbol": symbol,
                },
            )
            return

        if missing_groups and attempt_idx < len(RETRY_DELAYS) - 1:
            write_debug_event(
                logger,
                "waiting_for_missing_orders",
                {
                    "profile_name": profile_name,
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "next_attempt": attempt_idx + 2,
                    "delay": RETRY_DELAYS[attempt_idx + 1],
                },
            )
            continue

    if not fetched_once:
        logger.warning("Unable to fetch orders for %s; skipping safety action", profile_name)
        return

    write_debug_event(
        logger,
        "safety_action_required",
        {
            "profile_name": profile_name,
            "bot_name": bot_name,
            "symbol": symbol,
            "long_qty": long_qty,
            "short_qty": short_qty,
            "groups_present": groups_present,
            "required_groups": required_groups,
            "missing_groups": missing_groups,
            "open_orders_summary": successful_orders,
            "dry_run": dry_run,
        },
    )
    logger.warning(
        "safety_alert missing groups %s for %s symbol=%s long=%s short=%s",
        missing_groups,
        profile_name,
        symbol,
        long_qty,
        short_qty,
    )

    actions: list[str] = []
    results: dict[str, Any] = {}
    actions.append("cancel_orders")
    if dry_run:
        results["cancel_orders"] = "dry-run"
    else:
        canceled = order_manager.cancel_all_orders(symbol=symbol, category=category)
        results["cancel_orders"] = "success" if canceled else "failed"
    write_debug_event(
        logger,
        "safety_action_result",
        {
            "action": "cancel_orders",
            "result": results["cancel_orders"],
            "profile_name": profile_name,
            "bot_name": bot_name,
            "symbol": symbol,
        },
    )

    actions.append("stop_with_cleanup")
    results["stop_with_cleanup"] = kill_bot_process(bot_name, logger, dry_run)
    write_debug_event(
        logger,
        "safety_action_result",
        {
            "action": "stop_with_cleanup",
            "result": results["stop_with_cleanup"],
            "profile_name": profile_name,
            "bot_name": bot_name,
            "symbol": symbol,
        },
    )

    log_safety_event(
        logger,
        profile_name,
        bot_name,
        symbol,
        long_qty,
        short_qty,
        successful_orders,
        missing_groups,
        actions,
        dry_run,
        results,
    )


def main() -> None:
    args = parse_args()
    logger = setup_logger()
    if not args.once and not args.loop:
        args.once = True

    first_iteration = True
    while True:
        if first_iteration or args.loop:
            accounts = load_profile_accounts(logger)
            write_debug_event(
                logger,
                "watchdog_iteration_started",
                {
                    "dry_run": args.dry_run,
                    "mode": "loop" if args.loop else "once",
                    "interval": args.interval,
                    "account_count": len(accounts),
                },
            )
            for profile_name, data in accounts.items():
                try:
                    handle_profile(profile_name, data, logger, args.dry_run)
                except Exception as exc:
                    logger.exception("Safety watchdog failed for %s: %s", profile_name, exc)
            first_iteration = False
        if args.once and not args.loop:
            break
        if not args.loop:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()

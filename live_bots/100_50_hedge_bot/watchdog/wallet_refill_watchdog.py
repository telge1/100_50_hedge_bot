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
from uuid import uuid4

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
DEFAULT_WALLET_STATE_VERSION = 2
DEFAULT_SMALL_WALLET_DELTA_TOLERANCE = 0.05


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


def _recover_runner_pid_file(pid_file: Path, bot_name: str, logger: logging.Logger) -> bool:
    run_dir = pid_file.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    pattern = "fixed_cycle_hedge_bot.runner"
    try:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    except Exception:
        return False
    for line in (result.stdout or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        cmd_path = Path(f"/proc/{candidate}/cmdline")
        if not cmd_path.exists():
            continue
        try:
            cmdline = cmd_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            continue
        if f"--bot-name {bot_name}" not in cmdline:
            continue
        if str(PROJECT_ROOT) not in cmdline:
            continue
        logger.info(
            "wallet_refill_watchdog_pid_file_missing_recovery_attempt bot=%s pid=%s",
            bot_name,
            candidate,
        )
        pid_file.write_text(candidate, encoding="utf-8")
        logger.info(
            "wallet_refill_watchdog_pid_file_recovered bot=%s pid=%s",
            bot_name,
            candidate,
        )
        return True
    return False


def _validate_runner_process(bot_name: str, logger: logging.Logger) -> bool:
    bot_name = bot_name.lower()
    bot_dir = BOT_ROOT / bot_name
    run_dir = bot_dir / "run"
    primary_pid_file = run_dir / "bot.pid"
    legacy_pid_file = bot_dir / "pids" / "fixed_cycle_bot.pid"

    def _read_pid_file(path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        try:
            pid_value = int(text)
        except (TypeError, ValueError):
            return None
        if pid_value <= 0:
            return None
        return pid_value

    def _pid_looks_like_runner(pid_value: int) -> bool:
        cmd_path = Path(f"/proc/{pid_value}/cmdline")
        if not cmd_path.exists():
            return False
        try:
            cmdline = cmd_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            return False
        tokens = [
            "fixed_cycle_hedge_bot.runner",
            f"--bot-name {bot_name}",
            str(PROJECT_ROOT),
        ]
        return all(token in cmdline for token in tokens)

    def _scan_runner_pids() -> list[int]:
        try:
            process = subprocess.run(
                ["ps", "-o", "pid=", "-o", "args=", "-C", "python", "-C", "python3"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            logger.info(
                "wallet_refill_runner_scan_failed bot=%s error=%s",
                bot_name,
                exc,
            )
            return []
        pids: list[int] = []
        for line in process.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid_str, args = parts
            if (
                "fixed_cycle_hedge_bot.runner" in args
                and f"--bot-name {bot_name}" in args
                and str(PROJECT_ROOT) in args
            ):
                try:
                    pids.append(int(pid_str))
                except (TypeError, ValueError):
                    continue
        return pids

    runner_pid: int | None = None
    runner_source: str | None = None

    candidate = _read_pid_file(primary_pid_file)
    if candidate is not None and _pid_looks_like_runner(candidate):
        runner_pid = candidate
        runner_source = "run_pid_file"
    else:
        legacy_candidate = _read_pid_file(legacy_pid_file)
        if legacy_candidate is not None and _pid_looks_like_runner(legacy_candidate):
            runner_pid = legacy_candidate
            runner_source = "legacy_pid_file"
        else:
            scanned = _scan_runner_pids()
            if scanned:
                runner_pid = scanned[0]
                runner_source = "process_scan"

    if runner_pid is not None and (
        not primary_pid_file.exists() or _read_pid_file(primary_pid_file) != runner_pid
    ):
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            primary_pid_file.write_text(str(runner_pid), encoding="utf-8")
            logger.info(
                "wallet_refill_runner_pid_recovered bot=%s source=%s pid=%s",
                bot_name,
                runner_source,
                runner_pid,
            )
        except Exception as exc:
            logger.info(
                "wallet_refill_runner_pid_recover_failed bot=%s source=%s pid=%s error=%s",
                bot_name,
                runner_source,
                runner_pid,
                exc,
            )

    runtime_state = load_bot_runtime_state(bot_name, logger)
    strategy_state = runtime_state.get("strategy_state") or {}
    active_orders = runtime_state.get("active_orders") or []
    status_payload = _load_bot_status(bot_name, logger)
    state_active = _strategy_state_looks_active(strategy_state, active_orders)
    status_inactive = _status_clearly_inactive(status_payload)

    if runner_pid is not None:
        logger.info(
            "wallet_refill_profile_checked bot=%s runner_status=active pid=%s source=%s state_active=%s status_inactive=%s",
            bot_name,
            runner_pid,
            runner_source,
            state_active,
            status_inactive,
        )
        return True

    if state_active:
        logger.warning(
            "wallet_refill_runner_missing_but_state_active bot=%s state_path=%s",
            bot_name,
            runtime_state.get("path"),
        )
        logger.info(
            "wallet_refill_profile_checked bot=%s runner_status=missing state_active=%s status_inactive=%s",
            bot_name,
            state_active,
            status_inactive,
        )
        return True

    if not state_active and status_inactive:
        logger.info(
            "wallet_refill_runner_validation_failed_but_state_inactive bot=%s status=%s",
            bot_name,
            (status_payload.get("status") if isinstance(status_payload, dict) else None),
        )
        logger.info(
            "wallet_refill_profile_skipped_inactive bot=%s reason=status_inactive",
            bot_name,
        )
        return False

    logger.info(
        "wallet_refill_profile_checked bot=%s runner_status=missing state_active=%s status_inactive=%s",
        bot_name,
        state_active,
        status_inactive,
    )
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


def _load_bot_status(bot_name: str, logger: logging.Logger) -> dict[str, Any]:
    status_path = BOT_ROOT / bot_name.lower() / "run" / "status.json"
    if not status_path.exists():
        return {}
    try:
        raw = status_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.info("wallet_refill_status_read_failed bot=%s path=%s error=%s", bot_name, status_path, exc)
        return {}
    try:
        data = json.loads(raw) or {}
    except Exception as exc:
        logger.info("wallet_refill_status_parse_failed bot=%s path=%s error=%s", bot_name, status_path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _status_clearly_inactive(status_payload: dict[str, Any] | None) -> bool:
    if not isinstance(status_payload, dict):
        return False
    status_value = str(status_payload.get("status") or "").strip().lower()
    return status_value in {"stopped", "idle", "inactive"}


def _strategy_state_looks_active(
    strategy_state: Mapping[str, Any] | None,
    active_orders: list[Mapping[str, Any]] | None,
) -> bool:
    state = dict(strategy_state or {})
    orders = list(active_orders or [])

    if state.get("initial_entry_submitted") or state.get("initial_structure_built"):
        return True
    if state.get("trade_active"):
        return True
    if state.get("active_orders"):
        return True
    if orders:
        return True

    for key in ("initial_long_qty", "initial_short_qty", "long_qty", "short_qty"):
        try:
            value = float(state.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return True

    bot_state = str(state.get("bot_state") or "").strip().upper()
    if bot_state and bot_state not in {"IDLE", "STOPPED", "CLOSED"}:
        return True

    cycle_state = state.get("cycle_state") or {}
    if isinstance(cycle_state, Mapping):
        if (
            cycle_state.get("trade_active")
            or cycle_state.get("refill_in_progress")
            or cycle_state.get("refill_required")
        ):
            return True

    if state.get("emergency_flat_required") or state.get("emergency_flat_in_progress"):
        return True

    return False


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
    parser.add_argument(
        "--allow-inactive-baseline",
        action="store_true",
        help="Deprecated no-op: baseline/wallet monitoring always runs regardless of runner state.",
    )
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_transfer_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _append_finalized_transfer(guard: dict[str, Any], entry: dict[str, Any]) -> None:
    confirmed = _normalize_transfer_entries(guard.get("confirmed_internal_transfers"))
    confirmed.append(dict(entry))
    guard["confirmed_internal_transfers"] = confirmed[-100:]


def _build_initial_wallet_guard(
    bot_name: str,
    symbol: str,
    balance: float,
    metric: str | None,
    *,
    previous_guard: Mapping[str, Any] | None = None,
    reason: str = "baseline_initialized",
) -> dict[str, Any]:
    previous = dict(previous_guard or {})
    refill_threshold_pct = _safe_float(
        previous.get("refill_threshold_pct", previous.get("threshold_pct", DEFAULT_REFILL_THRESHOLD_PCT)),
        DEFAULT_REFILL_THRESHOLD_PCT,
    ) or DEFAULT_REFILL_THRESHOLD_PCT
    cashout_profit_trigger_pct = _safe_float(
        previous.get("cashout_profit_trigger_pct", DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT),
        DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT,
    ) or DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT
    cashout_profit_share_pct = _safe_float(
        previous.get("cashout_profit_share_pct", DEFAULT_CASHOUT_PROFIT_SHARE_PCT),
        DEFAULT_CASHOUT_PROFIT_SHARE_PCT,
    ) or DEFAULT_CASHOUT_PROFIT_SHARE_PCT
    timestamp = _now().isoformat()
    refill_threshold_wallet = balance * (refill_threshold_pct / 100.0)
    cashout_trigger_wallet = balance * (1 + cashout_profit_trigger_pct / 100.0)
    return {
        "wallet_state_version": DEFAULT_WALLET_STATE_VERSION,
        "bot_name": bot_name,
        "symbol": symbol,
        "start_wallet_usdt": balance,
        "baseline_wallet_usdt": balance,
        "current_wallet_usdt": balance,
        "last_observed_wallet_usdt": balance,
        "last_observed_at": timestamp,
        "pending_internal_transfers": [],
        "confirmed_internal_transfers": [],
        "manual_deposit_total_usdt": 0.0,
        "manual_withdraw_total_usdt": 0.0,
        "watcher_refill_total_usdt": 0.0,
        "watcher_cashout_total_usdt": 0.0,
        "last_wallet_delta_usdt": 0.0,
        "last_wallet_delta_reason": reason,
        "last_decision_reason": reason,
        "wallet_metric_used": metric,
        "refill_threshold_pct": refill_threshold_pct,
        "refill_threshold_wallet_usdt": refill_threshold_wallet,
        "refill_required": False,
        "refill_amount_usdt": 0.0,
        "cashout_profit_trigger_pct": cashout_profit_trigger_pct,
        "cashout_trigger_wallet_usdt": cashout_trigger_wallet,
        "cashout_profit_share_pct": cashout_profit_share_pct,
        "profit_usdt": 0.0,
        "cashout_required": False,
        "cashout_amount_usdt": 0.0,
        "last_refill_at": None,
        "last_cashout_at": None,
        "last_auto_transfer_at": None,
        "last_auto_transfer_amount_usdt": None,
        "last_auto_cashout_at": None,
        "last_auto_cashout_amount_usdt": None,
        "refill_count": 0,
        "cashout_count": 0,
        "auto_transfer_count": 0,
        "auto_cashout_count": 0,
        "updated_at": timestamp,
    }


def _normalize_wallet_guard(
    guard: Mapping[str, Any] | None,
    *,
    bot_name: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    data = dict(guard or {})
    baseline_wallet = _safe_float(data.get("baseline_wallet_usdt"), None)
    start_wallet = _safe_float(data.get("start_wallet_usdt"), None)
    current_wallet = _safe_float(data.get("current_wallet_usdt"), None)
    if baseline_wallet is None:
        baseline_wallet = start_wallet if start_wallet is not None else (current_wallet or 0.0)
    if start_wallet is None:
        start_wallet = baseline_wallet
    if current_wallet is None:
        current_wallet = baseline_wallet
    last_observed_wallet = _safe_float(data.get("last_observed_wallet_usdt"), None)
    if last_observed_wallet is None:
        last_observed_wallet = current_wallet
    data.update(
        {
            "wallet_state_version": int(data.get("wallet_state_version") or DEFAULT_WALLET_STATE_VERSION),
            "bot_name": bot_name or data.get("bot_name"),
            "symbol": symbol or data.get("symbol"),
            "start_wallet_usdt": start_wallet,
            "baseline_wallet_usdt": baseline_wallet,
            "current_wallet_usdt": current_wallet,
            "last_observed_wallet_usdt": last_observed_wallet,
            "last_observed_at": data.get("last_observed_at"),
            "pending_internal_transfers": _normalize_transfer_entries(data.get("pending_internal_transfers")),
            "confirmed_internal_transfers": _normalize_transfer_entries(data.get("confirmed_internal_transfers")),
            "manual_deposit_total_usdt": _safe_float(data.get("manual_deposit_total_usdt"), 0.0) or 0.0,
            "manual_withdraw_total_usdt": _safe_float(data.get("manual_withdraw_total_usdt"), 0.0) or 0.0,
            "watcher_refill_total_usdt": _safe_float(data.get("watcher_refill_total_usdt"), 0.0) or 0.0,
            "watcher_cashout_total_usdt": _safe_float(data.get("watcher_cashout_total_usdt"), 0.0) or 0.0,
            "last_wallet_delta_usdt": _safe_float(data.get("last_wallet_delta_usdt"), 0.0) or 0.0,
            "last_wallet_delta_reason": data.get("last_wallet_delta_reason") or "uninitialized",
            "last_decision_reason": data.get("last_decision_reason") or "",
        }
    )
    return data


def _backup_wallet_guard(path: Path, guard: Mapping[str, Any], suffix: str) -> Path:
    timestamp = _now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak_{suffix}_{timestamp}")
    backup_path.write_text(json.dumps(dict(guard), ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path


def _pending_match_tolerance(amount_usdt: float) -> float:
    return max(DEFAULT_SMALL_WALLET_DELTA_TOLERANCE, abs(amount_usdt) * 0.02)


def _write_pending_internal_transfer(
    guard: dict[str, Any],
    path: Path,
    *,
    bot_name: str,
    transfer_type: str,
    direction: str,
    amount_usdt: float,
    wallet_before_usdt: float,
) -> dict[str, Any]:
    timestamp = _now().isoformat()
    transfer_id = f"{transfer_type}_{uuid4().hex[:12]}"
    expected_wallet_after = wallet_before_usdt + amount_usdt if direction == "in" else wallet_before_usdt - amount_usdt
    entry = {
        "id": transfer_id,
        "type": transfer_type,
        "direction": direction,
        "amount_usdt": amount_usdt,
        "created_at": timestamp,
        "status": "pending",
        "wallet_before_usdt": wallet_before_usdt,
        "expected_wallet_after_usdt": expected_wallet_after,
    }
    pending = _normalize_transfer_entries(guard.get("pending_internal_transfers"))
    pending.append(entry)
    guard["pending_internal_transfers"] = pending
    guard["updated_at"] = timestamp
    write_wallet_guard(path, guard)
    write_json_event(
        "wallet_internal_transfer_pending_written",
        {
            "bot_name": bot_name,
            **entry,
        },
    )
    return entry


def _finalize_pending_internal_transfer(
    guard: dict[str, Any],
    path: Path,
    *,
    transfer_id: str,
    status: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    pending = _normalize_transfer_entries(guard.get("pending_internal_transfers"))
    entry: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for item in pending:
        if entry is None and str(item.get("id") or "") == transfer_id:
            entry = dict(item)
            continue
        remaining.append(dict(item))
    if entry is None:
        return None
    entry.update(dict(extra or {}))
    entry["status"] = status
    entry["finalized_at"] = _now().isoformat()
    guard["pending_internal_transfers"] = remaining
    _append_finalized_transfer(guard, entry)
    guard["updated_at"] = _now().isoformat()
    write_wallet_guard(path, guard)
    return entry


def _classify_wallet_delta(
    bot_name: str,
    symbol: str,
    guard: dict[str, Any],
    current_wallet: float,
) -> tuple[float, str]:
    now_iso = _now().isoformat()
    baseline_wallet = _safe_float(guard.get("baseline_wallet_usdt"), 0.0) or 0.0
    start_wallet = _safe_float(guard.get("start_wallet_usdt"), baseline_wallet) or baseline_wallet
    last_observed_wallet = _safe_float(guard.get("last_observed_wallet_usdt"), None)
    pending = _normalize_transfer_entries(guard.get("pending_internal_transfers"))

    if last_observed_wallet is None:
        delta = 0.0
        delta_reason = "initial_observation"
    else:
        delta = current_wallet - last_observed_wallet
        delta_reason = "no_change"
        match_index: int | None = None
        match_entry: dict[str, Any] | None = None
        match_tolerance = 0.0
        best_distance: float | None = None
        for index, entry in enumerate(pending):
            amount_usdt = _safe_float(entry.get("amount_usdt"), 0.0) or 0.0
            expected_delta = amount_usdt if str(entry.get("direction") or "").lower() == "in" else -amount_usdt
            tolerance = _pending_match_tolerance(amount_usdt)
            distance = abs(delta - expected_delta)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                match_index = index
                match_entry = entry
                match_tolerance = tolerance
                best_distance = distance
        if match_index is not None and match_entry is not None:
            matched_entry = dict(pending.pop(match_index))
            matched_entry.update(
                {
                    "status": "confirmed",
                    "confirmed_at": now_iso,
                    "observed_delta_usdt": delta,
                    "match_tolerance_usdt": match_tolerance,
                }
            )
            guard["pending_internal_transfers"] = pending
            _append_finalized_transfer(guard, matched_entry)
            transfer_type = str(matched_entry.get("type") or "")
            amount_usdt = _safe_float(matched_entry.get("amount_usdt"), 0.0) or 0.0
            if transfer_type == "watcher_refill":
                guard["watcher_refill_total_usdt"] = (_safe_float(guard.get("watcher_refill_total_usdt"), 0.0) or 0.0) + amount_usdt
            elif transfer_type == "watcher_cashout":
                guard["watcher_cashout_total_usdt"] = (_safe_float(guard.get("watcher_cashout_total_usdt"), 0.0) or 0.0) + amount_usdt
            delta_reason = f"{transfer_type}_confirmed"
            write_json_event(
                "wallet_internal_transfer_confirmed",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    **matched_entry,
                },
            )
        elif delta > DEFAULT_SMALL_WALLET_DELTA_TOLERANCE:
            guard["manual_deposit_total_usdt"] = (_safe_float(guard.get("manual_deposit_total_usdt"), 0.0) or 0.0) + delta
            baseline_wallet = max(0.0, baseline_wallet + delta)
            start_wallet = baseline_wallet
            delta_reason = "manual_deposit"
            write_json_event(
                "wallet_manual_deposit_detected",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "delta_usdt": delta,
                    "current_wallet_usdt": current_wallet,
                    "last_observed_wallet_usdt": last_observed_wallet,
                },
            )
            write_json_event(
                "wallet_baseline_adjusted_external_delta",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "delta_usdt": delta,
                    "direction": "deposit",
                    "new_baseline_wallet_usdt": baseline_wallet,
                },
            )
        elif delta < -DEFAULT_SMALL_WALLET_DELTA_TOLERANCE:
            guard["manual_withdraw_total_usdt"] = (_safe_float(guard.get("manual_withdraw_total_usdt"), 0.0) or 0.0) + abs(delta)
            baseline_wallet = max(0.0, baseline_wallet + delta)
            start_wallet = baseline_wallet
            delta_reason = "manual_withdraw"
            write_json_event(
                "wallet_manual_withdraw_detected",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "delta_usdt": delta,
                    "current_wallet_usdt": current_wallet,
                    "last_observed_wallet_usdt": last_observed_wallet,
                },
            )
            write_json_event(
                "wallet_baseline_adjusted_external_delta",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "delta_usdt": delta,
                    "direction": "withdraw",
                    "new_baseline_wallet_usdt": baseline_wallet,
                },
            )
        elif abs(delta) > 0:
            delta_reason = "small_delta"

    guard["start_wallet_usdt"] = start_wallet
    guard["baseline_wallet_usdt"] = baseline_wallet
    guard["current_wallet_usdt"] = current_wallet
    guard["last_observed_wallet_usdt"] = current_wallet
    guard["last_observed_at"] = now_iso
    guard["last_wallet_delta_usdt"] = delta
    guard["last_wallet_delta_reason"] = delta_reason
    guard["updated_at"] = now_iso
    return delta, delta_reason


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


def _guard_needs_baseline_init(raw_guard: Mapping[str, Any] | None) -> bool:
    if not raw_guard:
        return True
    return (
        _safe_float(raw_guard.get("baseline_wallet_usdt"), None) is None
        or _safe_float(raw_guard.get("start_wallet_usdt"), None) is None
    )


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
        guard = _normalize_wallet_guard(read_wallet_guard(guard_path), bot_name=bot_name)
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
        if _normalize_symbol(guard.get("symbol")) and _normalize_symbol(guard.get("symbol")) != _normalize_symbol(symbol):
            backup_path = _backup_wallet_guard(guard_path, guard, "stale_symbol")
            write_json_event(
                "wallet_guard_stale_symbol_backed_up",
                {
                    "bot_name": bot_name,
                    "old_symbol": guard.get("symbol"),
                    "new_symbol": symbol,
                    "backup_path": str(backup_path),
                },
            )
        guard = _build_initial_wallet_guard(
            bot_name,
            symbol,
            balance,
            metric,
            previous_guard=guard,
            reason="baseline_initialized_on_start",
        )
        old_last = None
        old_amount = None
        if transfer_settings.reset_cooldown_on_start:
            old_last = guard.get("last_auto_transfer_at")
            old_amount = guard.get("last_auto_transfer_amount_usdt")
            guard["last_auto_transfer_at"] = None
            guard["last_auto_transfer_amount_usdt"] = None
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
    """Resolve active bot symbol/category from runtime sources first.

    Für 100_50_hedge_bot:
    - Runtime/Reserved/State enthalten den aktuellen Coin.
    - config/fixed_cycle_config.json kann stale sein.
    """
    bot_name = bot_name.lower()
    bot_root = BOT_ROOT / bot_name

    candidates: list[tuple[str, str, str]] = []

    def _read_json(path: Path) -> dict:
        try:
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning("Failed to read metadata json for %s path=%s error=%s", bot_name, path, exc)
            return {}

    def _add_candidate(source: str, symbol: object, category: object = None) -> None:
        if symbol is None:
            return
        symbol_s = str(symbol).strip().upper()
        if not symbol_s:
            return
        category_s = str(category or "linear").strip() or "linear"
        candidates.append((source, symbol_s, category_s))

    runtime_payload = _read_json(bot_root / "run" / "fixed_cycle_config.runtime.json")
    _add_candidate("runtime_config", runtime_payload.get("symbol"), runtime_payload.get("category"))

    reserved_payload = _read_json(bot_root / "run" / "reserved_best_coin.json")
    _add_candidate("reserved_best_coin", reserved_payload.get("symbol"), reserved_payload.get("category"))

    state_payload = _read_json(bot_root / "state" / "fixed_cycle_state.json")
    _add_candidate("state", state_payload.get("symbol"), state_payload.get("category"))

    strategy_state = state_payload.get("strategy_state") or {}
    if isinstance(strategy_state, dict):
        _add_candidate(
            "strategy_state",
            strategy_state.get("symbol"),
            strategy_state.get("category") or state_payload.get("category"),
        )

        cycle_state = strategy_state.get("cycle_state") or {}
        if isinstance(cycle_state, dict):
            _add_candidate(
                "cycle_state",
                cycle_state.get("symbol"),
                cycle_state.get("category") or strategy_state.get("category") or state_payload.get("category"),
            )

    config_payload = _read_json(bot_root / "config" / "fixed_cycle_config.json")
    _add_candidate("config", config_payload.get("symbol"), config_payload.get("category"))

    if not candidates:
        logger.warning("No symbol resolved for %s", bot_name)
        return None

    selected_source, selected_symbol, selected_category = candidates[0]
    logger.info(
        "wallet_refill_resolved_symbol_candidates bot=%s selected_symbol=%s selected_source=%s candidates=%s",
        bot_name,
        selected_symbol,
        selected_source,
        [(source, symbol) for source, symbol, _category in candidates],
    )
    return selected_symbol, selected_category


def is_bot_runner_active(bot_name: str) -> bool:
    bot_name = bot_name.lower()
    bot_dir = BOT_ROOT / bot_name
    run_dir = bot_dir / "run"
    primary_pid_file = run_dir / "bot.pid"
    legacy_pid_file = bot_dir / "pids" / "fixed_cycle_bot.pid"

    def _read_pid_file(path: Path) -> int | None:
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        try:
            pid_value = int(text)
        except (TypeError, ValueError):
            return None
        if pid_value <= 0:
            return None
        return pid_value

    def _pid_looks_like_runner(pid_value: int) -> bool:
        cmd_path = Path(f"/proc/{pid_value}/cmdline")
        if not cmd_path.exists():
            return False
        try:
            cmdline = cmd_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except Exception:
            return False
        if "fixed_cycle_hedge_bot.runner" not in cmdline:
            return False
        if f"--bot-name {bot_name}" not in cmdline:
            return False
        if str(PROJECT_ROOT) not in cmdline:
            return False
        return True

    def _scan_runner_pids() -> list[int]:
        try:
            process = subprocess.run(
                ["ps", "-o", "pid=", "-o", "args=", "-C", "python", "-C", "python3"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return []
        pids: list[int] = []
        for line in process.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid_str, args = parts
            if (
                "fixed_cycle_hedge_bot.runner" in args
                and f"--bot-name {bot_name}" in args
                and str(PROJECT_ROOT) in args
            ):
                try:
                    pids.append(int(pid_str))
                except (TypeError, ValueError):
                    continue
        return pids

    runner_pid: int | None = None

    candidate = _read_pid_file(primary_pid_file)
    if candidate is not None and _pid_looks_like_runner(candidate):
        runner_pid = candidate
    else:
        legacy_candidate = _read_pid_file(legacy_pid_file)
        if legacy_candidate is not None and _pid_looks_like_runner(legacy_candidate):
            runner_pid = legacy_candidate
        else:
            scanned = _scan_runner_pids()
            if scanned:
                runner_pid = scanned[0]

    return runner_pid is not None


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
    guard = _normalize_wallet_guard(read_wallet_guard(path), bot_name=bot_name, symbol=symbol)
    if guard.get("start_wallet_usdt") and not reset:
        logger.info("[%s] start wallet already captured", bot_name)
        write_json_event("wallet_start_already_exists", {"bot_name": bot_name, "symbol": symbol})
        return
    balance, metric = fetch_current_wallet(order_manager, symbol)
    if balance is None:
        logger.warning("[%s] unable to fetch wallet for capture", bot_name)
        write_json_event("wallet_fetch_failed", {"bot_name": bot_name, "symbol": symbol})
        return
    guard = _build_initial_wallet_guard(
        bot_name,
        symbol,
        balance,
        metric,
        previous_guard=guard,
        reason="wallet_start_captured",
    )
    write_wallet_guard(path, guard)
    write_json_event("wallet_start_captured", {"bot_name": bot_name, "symbol": symbol, "start_wallet_usdt": balance})


def _attempt_baseline_recovery(
    bot_name: str,
    order_manager: BybitOrderManager,
    symbol: str,
    logger: logging.Logger,
    guard_path: Path,
    runner_active: bool,
) -> bool:
    guard = read_wallet_guard(guard_path)
    write_json_event(
        "wallet_refill_watchdog_missing_baseline_detected",
        {
            "bot_name": bot_name,
            "symbol": symbol,
            "wallet_guard_path": str(guard_path),
            "runner_active": runner_active,
        },
    )
    write_json_event(
        "wallet_refill_watchdog_baseline_recovery_attempt",
        {
            "bot_name": bot_name,
            "symbol": symbol,
            "wallet_guard_path": str(guard_path),
            "runner_active": runner_active,
        },
    )
    capture_start_wallet(bot_name, order_manager, symbol, logger, reset=False)
    guard = read_wallet_guard(guard_path)
    baseline = guard.get("start_wallet_usdt")
    if baseline is not None:
        write_json_event(
            "wallet_refill_watchdog_baseline_recovered",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "wallet_guard_path": str(guard_path),
                "runner_active": runner_active,
                "start_wallet_usdt": baseline,
                "current_wallet_usdt": guard.get("current_wallet_usdt"),
            },
        )
        return True
    write_json_event(
        "wallet_refill_watchdog_baseline_recovery_failed",
        {
            "bot_name": bot_name,
            "symbol": symbol,
            "wallet_guard_path": str(guard_path),
            "runner_active": runner_active,
            "reason": "capture_failed",
        },
    )
    return False


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
    guard = _normalize_wallet_guard(guard, bot_name=bot_name)
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
    pending_same_type = [
        entry
        for entry in _normalize_transfer_entries(guard.get("pending_internal_transfers"))
        if str(entry.get("type") or "") == "watcher_refill" and str(entry.get("status") or "") == "pending"
    ]
    if pending_same_type:
        write_json_event(
            "wallet_refill_transfer_skipped_pending_exists",
            {
                **base_payload,
                "pending_internal_transfer_count": len(pending_same_type),
            },
        )
        return
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
    if transfer_settings.dry_run:
        write_json_event(
            "wallet_refill_transfer_planned_dry_run",
            {
                **base_payload,
                "planned_action": "watcher_refill",
                "runner_active": runner_active,
            },
        )
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
    pending_entry = _write_pending_internal_transfer(
        guard,
        path,
        bot_name=bot_name,
        transfer_type="watcher_refill",
        direction="in",
        amount_usdt=final_amount,
        wallet_before_usdt=current_wallet,
    )
    started_payload = {**base_payload, "final_amount": final_amount}
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
            failed_entry = _finalize_pending_internal_transfer(
                guard,
                path,
                transfer_id=str(pending_entry.get("id") or ""),
                status="failed",
                extra=transfer_payload,
            )
            write_json_event("wallet_refill_transfer_executor_failed", transfer_payload)
            if failed_entry:
                write_json_event("wallet_internal_transfer_failed", {"bot_name": bot_name, **failed_entry})
            return
        write_json_event("wallet_refill_transfer_executor_success", transfer_payload)
    except Exception as exc:
        logger.exception("Transfer executor failed", exc_info=exc)
        error_payload = {**base_payload, "error": str(exc)}
        failed_entry = _finalize_pending_internal_transfer(
            guard,
            path,
            transfer_id=str(pending_entry.get("id") or ""),
            status="failed",
            extra=error_payload,
        )
        write_json_event("wallet_refill_transfer_executor_failed", error_payload)
        if failed_entry:
            write_json_event("wallet_internal_transfer_failed", {"bot_name": bot_name, **failed_entry})
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
    guard = _normalize_wallet_guard(guard, bot_name=bot_name)
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
    pending_same_type = [
        entry
        for entry in _normalize_transfer_entries(guard.get("pending_internal_transfers"))
        if str(entry.get("type") or "") == "watcher_cashout" and str(entry.get("status") or "") == "pending"
    ]
    if pending_same_type:
        write_json_event(
            "wallet_cashout_transfer_skipped_pending_exists",
            {
                **base_payload,
                "pending_internal_transfer_count": len(pending_same_type),
            },
        )
        return
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
    if transfer_settings.dry_run:
        write_json_event(
            "wallet_cashout_transfer_planned_dry_run",
            {
                **base_payload,
                "planned_action": "watcher_cashout",
                "runner_active": runner_active,
            },
        )
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
    pending_entry = _write_pending_internal_transfer(
        guard,
        path,
        bot_name=bot_name,
        transfer_type="watcher_cashout",
        direction="out",
        amount_usdt=final_amount,
        wallet_before_usdt=current_wallet,
    )
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
            failed_entry = _finalize_pending_internal_transfer(
                guard,
                path,
                transfer_id=str(pending_entry.get("id") or ""),
                status="failed",
                extra=transfer_payload,
            )
            write_json_event("wallet_cashout_transfer_executor_failed", transfer_payload)
            if failed_entry:
                write_json_event("wallet_internal_transfer_failed", {"bot_name": bot_name, **failed_entry})
            return
        write_json_event("wallet_cashout_transfer_executor_success", transfer_payload)
    except Exception as exc:
        logger.exception("Cashout executor failed", exc_info=exc)
        error_payload = {**base_payload, "error": str(exc)}
        failed_entry = _finalize_pending_internal_transfer(
            guard,
            path,
            transfer_id=str(pending_entry.get("id") or ""),
            status="failed",
            extra=error_payload,
        )
        write_json_event("wallet_cashout_transfer_executor_failed", error_payload)
        if failed_entry:
            write_json_event("wallet_internal_transfer_failed", {"bot_name": bot_name, **failed_entry})
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
    balance, metric = fetch_current_wallet(order_manager, symbol)
    if balance is None:
        write_json_event("wallet_fetch_failed", {"bot_name": bot_name, "symbol": symbol})
        return
    raw_guard = read_wallet_guard(path)
    guard = _normalize_wallet_guard(raw_guard, bot_name=bot_name, symbol=symbol)
    guard_symbol = _normalize_symbol(guard.get("symbol"))
    if raw_guard and guard_symbol and guard_symbol != _normalize_symbol(symbol):
        backup_path = _backup_wallet_guard(path, raw_guard, "stale_symbol")
        write_json_event(
            "wallet_guard_stale_symbol_reinitialized",
            {
                "bot_name": bot_name,
                "old_symbol": guard_symbol,
                "new_symbol": symbol,
                "backup_path": str(backup_path),
                "runner_active": runner_active,
            },
        )
        guard = _build_initial_wallet_guard(
            bot_name,
            symbol,
            balance,
            metric,
            previous_guard=guard,
            reason="stale_symbol_reinitialized",
        )
        write_wallet_guard(path, guard)
    elif _guard_needs_baseline_init(raw_guard if isinstance(raw_guard, Mapping) else None):
        guard = _build_initial_wallet_guard(
            bot_name,
            symbol,
            balance,
            metric,
            previous_guard=guard,
            reason="missing_guard_initialized",
        )
        write_wallet_guard(path, guard)
        write_json_event(
            "wallet_baseline_initialized",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "current_wallet_usdt": balance,
                "old_start_wallet_usdt": raw_guard.get("start_wallet_usdt") if isinstance(raw_guard, Mapping) else None,
                "new_start_wallet_usdt": balance,
                "wallet_metric_used": metric,
                "runner_active": runner_active,
                "reason": "missing_guard_initialized",
            },
        )

    delta_usdt, delta_reason = _classify_wallet_delta(bot_name, symbol, guard, balance)
    baseline_wallet = _safe_float(guard.get("baseline_wallet_usdt"), balance) or balance
    start_wallet = _safe_float(guard.get("start_wallet_usdt"), baseline_wallet) or baseline_wallet
    guard["wallet_metric_used"] = metric
    if baseline_wallet < MIN_START_WALLET_USDT:
        decision_reason = "invalid_baseline_wallet"
        guard["last_decision_reason"] = decision_reason
        write_wallet_guard(path, guard)
        write_json_event(
            "wallet_skipped_invalid_start_wallet",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "start_wallet_usdt": baseline_wallet,
                "current_wallet_usdt": balance,
                "wallet_metric_used": metric,
                "runner_active": runner_active,
            },
        )
        write_json_event(
            "wallet_check",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "current_wallet_usdt": balance,
                "baseline_wallet_usdt": baseline_wallet,
                "start_wallet_usdt": start_wallet,
                "last_observed_wallet_usdt": guard.get("last_observed_wallet_usdt"),
                "delta_usdt": delta_usdt,
                "delta_reason": delta_reason,
                "pending_internal_transfer_count": len(
                    _normalize_transfer_entries(guard.get("pending_internal_transfers"))
                ),
                "runner_active": runner_active,
                "decision_reason": decision_reason,
            },
        )
        if not runner_active:
            write_json_event(
                "wallet_monitoring_without_active_runner",
                {
                    "bot_name": bot_name,
                    "symbol": symbol,
                    "current_wallet_usdt": balance,
                    "baseline_wallet_usdt": baseline_wallet,
                },
            )
        return
    refill_threshold_pct = _safe_float(
        guard.get("refill_threshold_pct", guard.get("threshold_pct", DEFAULT_REFILL_THRESHOLD_PCT)),
        DEFAULT_REFILL_THRESHOLD_PCT,
    ) or DEFAULT_REFILL_THRESHOLD_PCT
    refill_threshold_wallet = baseline_wallet * (refill_threshold_pct / 100.0)
    refill_required = balance <= refill_threshold_wallet
    refill_amount = max(0.0, baseline_wallet - balance)
    cashout_profit_trigger_pct = _safe_float(
        guard.get("cashout_profit_trigger_pct", DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT),
        DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT,
    ) or DEFAULT_CASHOUT_PROFIT_TRIGGER_PCT
    cashout_profit_share_pct = _safe_float(
        guard.get("cashout_profit_share_pct", DEFAULT_CASHOUT_PROFIT_SHARE_PCT),
        DEFAULT_CASHOUT_PROFIT_SHARE_PCT,
    ) or DEFAULT_CASHOUT_PROFIT_SHARE_PCT
    cashout_trigger_wallet = baseline_wallet * (1 + cashout_profit_trigger_pct / 100.0)
    profit_usdt = max(0.0, balance - baseline_wallet)
    cashout_required = balance >= cashout_trigger_wallet
    cashout_amount = profit_usdt * (cashout_profit_share_pct / 100.0) if cashout_required else 0.0
    for legacy in ("threshold_pct", "threshold_wallet_usdt"):
        guard.pop(legacy, None)
    if refill_required:
        decision_reason = "wallet_below_baseline_threshold"
    elif cashout_required:
        decision_reason = "wallet_above_baseline_cashout_trigger"
    elif delta_reason in {"manual_deposit", "manual_withdraw"}:
        decision_reason = f"within_baseline_range_after_{delta_reason}"
    else:
        decision_reason = "within_baseline_range"
    guard.update(
        {
            "current_wallet_usdt": balance,
            "baseline_wallet_usdt": baseline_wallet,
            "start_wallet_usdt": start_wallet,
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
            "last_decision_reason": decision_reason,
            "updated_at": _now().isoformat(),
        }
    )
    write_wallet_guard(path, guard)
    common_payload = {
        "bot_name": bot_name,
        "symbol": symbol,
        "current_wallet_usdt": balance,
        "baseline_wallet_usdt": baseline_wallet,
        "start_wallet_usdt": start_wallet,
        "last_observed_wallet_usdt": guard.get("last_observed_wallet_usdt"),
        "delta_usdt": delta_usdt,
        "delta_reason": delta_reason,
        "wallet_metric_used": metric,
        "refill_threshold_wallet_usdt": refill_threshold_wallet,
        "refill_required": refill_required,
        "refill_amount_usdt": refill_amount,
        "cashout_trigger_wallet_usdt": cashout_trigger_wallet,
        "profit_usdt": profit_usdt,
        "cashout_required": cashout_required,
        "cashout_amount_usdt": cashout_amount,
        "pending_internal_transfer_count": len(_normalize_transfer_entries(guard.get("pending_internal_transfers"))),
        "runner_active": runner_active,
        "decision_reason": decision_reason,
    }
    write_json_event("wallet_check", common_payload)
    if not runner_active:
        write_json_event(
            "wallet_monitoring_without_active_runner",
            {
                "bot_name": bot_name,
                "symbol": symbol,
                "current_wallet_usdt": balance,
                "baseline_wallet_usdt": baseline_wallet,
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
                "decision_reason": decision_reason,
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
            "decision_reason": decision_reason,
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
    initialize_wallet_baselines_on_start(
        accounts,
        logger,
        rebaseline_enabled,
        transfer_settings,
    )

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
            _validate_runner_process(bot_name, logger)
            resolved = resolve_bot_metadata(bot_name, logger)
            if not resolved:
                continue
            symbol, category = resolved
            order_manager = BybitOrderManager(api_key, secret_key)
            if args.capture_start_wallet:
                capture_start_wallet(bot_name, order_manager, symbol, logger, args.reset_start_wallet)
            runner_active = is_bot_runner_active(bot_name)
            monitor_wallet(
                bot_name,
                order_manager,
                symbol,
                logger,
                runner_active,
                transfer_settings,
            )

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

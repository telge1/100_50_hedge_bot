#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_ROOT = PROJECT_ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.app import _get_account_keys_by_profile
from dashboard.vendor.core.bybit_order_manager import BybitOrderManager

UTC_PLUS_3 = timezone(timedelta(hours=3))


def now_utc3_iso() -> str:
    return datetime.now(UTC_PLUS_3).isoformat()


def _fetch_wallet_snapshot() -> tuple[float | None, str, str, float | None]:
    _log_event(
        "fixed_cycle_wallet_snapshot_wallet_fetch_start",
        {
            "profile": "bot_1",
            "timestamp_utc3": now_utc3_iso(),
        },
    )
    try:
        long_key, long_secret, _, _ = _get_account_keys_by_profile("bot_1")
    except Exception as exc:
        _log(f"Failed to resolve bot keys: {exc}")
        return None, "", "", None
    if not long_key or not long_secret:
        _log_event(
            "fixed_cycle_wallet_snapshot_wallet_keys_missing",
            {"bot_name": "long_bot_1"},
        )
        return None, "", "", None
    try:
        manager = BybitOrderManager(long_key, long_secret)
    except Exception as exc:
        _log_event(
            "fixed_cycle_wallet_snapshot_manager_build_failed",
            {"error": str(exc)},
        )
        return None, "", "", None

    wallet = None
    metric = ""
    source = ""
    available = None

    try:
        margin = manager.get_account_margin_balance()
        if margin is not None and margin != "":
            wallet = float(margin)
            metric = "margin_balance"
            source = "bybit_total_margin_balance"
            _log_event(
                "fixed_cycle_wallet_snapshot_margin_balance_success",
                {"wallet_metric_used": metric, "wallet_balance_source": source, "wallet_value": wallet},
            )
    except Exception as exc:
        _log_event(
            "fixed_cycle_wallet_snapshot_margin_balance_failed",
            {"error": str(exc)},
        )

    if wallet is None:
        try:
            equity = manager.get_account_equity()
            if equity is not None and equity != "":
                wallet = float(equity)
                metric = "equity"
                source = "bybit_total_equity"
                _log_event(
                    "fixed_cycle_wallet_snapshot_equity_success",
                    {"wallet_metric_used": metric, "wallet_balance_source": source, "wallet_value": wallet},
                )
        except Exception as exc:
            _log_event(
                "fixed_cycle_wallet_snapshot_equity_failed",
                {"error": str(exc)},
            )

    try:
        avail = manager.get_account_available_balance()
        if avail is not None and avail != "":
            available = float(avail)
            _log_event(
                "fixed_cycle_wallet_snapshot_available_balance_success",
                {"available_wallet_usdt": available},
            )
    except Exception:
        pass

    if wallet is None:
        _log_event(
            "fixed_cycle_wallet_snapshot_wallet_fetch_failed",
            {"metric": metric, "source": source, "available_wallet_usdt": available},
        )
    return wallet, metric, source, available

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = PROJECT_ROOT / "logs" / "fixed_cycle_state.json"
DEFAULT_BOT_NAME = "long_bot_1"


def _default_output_file(bot_name: str) -> Path:
    return PROJECT_ROOT / "logs" / f"fixed_cycle_wallet_snapshot_{bot_name}.json"


def _log(message: str) -> None:
    print(f"[fixed_cycle_wallet_snapshot] {message}", file=sys.stderr)


def _log_event(event: str, payload: dict | None = None) -> None:
    try:
        safe_payload = dict(payload or {})
        safe_payload["event"] = event
        safe_payload["timestamp_utc3"] = now_utc3_iso()
        print("[fixed_cycle_wallet_snapshot] " + json.dumps(safe_payload, ensure_ascii=False, default=str), file=sys.stderr)
    except Exception:
        try:
            print(f"[fixed_cycle_wallet_snapshot] {event}", file=sys.stderr)
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Async wallet snapshot writer for fixed-cycle dashboard")
    parser.add_argument("--bot-name", default=DEFAULT_BOT_NAME, help="Bot identifier (e.g. long_bot_1)")
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help="Path to strategy state file (JSON)",
    )
    parser.add_argument(
        "--output-file",
        default="",
        help="Path to the wallet snapshot output file (defaults to logs/fixed_cycle_wallet_snapshot_{bot_name}.json)",
    )
    parser.add_argument(
        "--force-flat",
        action="store_true",
        help="Treat the bot as flat regardless of state (for testing)",
    )
    parser.add_argument(
        "--long-qty",
        type=float,
        help="Override long quantity for flat detection",
    )
    parser.add_argument(
        "--short-qty",
        type=float,
        help="Override short quantity for flat detection",
    )
    parser.add_argument(
        "--mode",
        choices=("flat", "start"),
        default="flat",
        help="Mode of the snapshot: 'start' for trade start, 'flat' for confirmed flat exit",
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        _log(f"Could not parse {path}: {exc}")
        return None


def _load_strategy_state(path: Path) -> dict | None:
    data = _load_json(path)
    if not isinstance(data, dict):
        return None
    return data.get("strategy_state") or data


def _position_values(state: dict) -> tuple[float | None, float | None]:
    snapshot = state.get("snapshot") or {}
    candidates = [
        (state.get("long_qty"), state.get("short_qty")),
        (state.get("long_size"), state.get("short_size")),
        (snapshot.get("long_qty"), snapshot.get("short_qty")),
        (snapshot.get("long_size"), snapshot.get("short_size")),
        (state.get("strategy_state", {}).get("long_qty"), state.get("strategy_state", {}).get("short_qty")),
    ]
    for long_val, short_val in candidates:
        if long_val is None or short_val is None:
            continue
        try:
            return float(long_val), float(short_val)
        except (TypeError, ValueError):
            continue
    return None, None


def _is_zero(value: float | None) -> bool:
    if value is None:
        return False
    return abs(value) < 1e-9


def _write_output(path: Path, payload: dict) -> None:
    _log_event(
        "fixed_cycle_wallet_snapshot_write_attempt",
        {
            "output_file": str(path),
            "payload_snapshot_phase": payload.get("snapshot_phase"),
            "payload_trade_block_id": payload.get("trade_block_id"),
        },
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_text(data, encoding="utf-8")
        _log_event(
            "fixed_cycle_wallet_snapshot_write_success",
            {
                "output_file": str(path),
                "bytes_written": len(data),
            },
        )
    except Exception as exc:
        _log_event(
            "fixed_cycle_wallet_snapshot_write_failed",
            {"output_file": str(path), "error": str(exc)},
        )


def main() -> None:
    args = _parse_args()
    bot_name = args.bot_name
    state_file = Path(args.state_file).expanduser()
    if args.output_file:
        output_file = Path(args.output_file).expanduser()
    else:
        output_file = _default_output_file(bot_name)

    _log_event(
        "fixed_cycle_wallet_snapshot_start",
        {
            "mode": args.mode,
            "bot_name": bot_name,
            "state_file": str(state_file),
            "output_file": str(output_file),
            "force_flat": args.force_flat,
            "cli_long_qty": args.long_qty,
            "cli_short_qty": args.short_qty,
        },
    )
    raw_state = _load_strategy_state(state_file)
    state_missing = raw_state is None
    state = raw_state or {}
    if state_missing and (args.long_qty is not None and args.short_qty is not None):
        _log_event(
            "fixed_cycle_wallet_snapshot_state_missing_but_cli_flat_allowed",
            {
                "state_file": str(state_file),
                "mode": args.mode,
                "bot_name": bot_name,
                "cli_long_qty": args.long_qty,
                "cli_short_qty": args.short_qty,
            },
        )
    elif state_missing:
        _log_event(
            "fixed_cycle_wallet_snapshot_state_missing_or_invalid",
            {
                "state_file": str(state_file),
                "mode": args.mode,
                "bot_name": bot_name,
                "exit_reason": "state_missing_or_invalid",
            },
        )
        return
    _log_event(
        "fixed_cycle_wallet_snapshot_state_loaded",
        {
            "state_file": str(state_file),
            "state_exists": state is not None and state != {},
            "state_valid": state_missing is False,
            "symbol": state.get("symbol"),
            "trade_block_id": state.get("trade_block_id") or state.get("last_trade_block_id"),
            "detection_fields": {
                "long_qty": state.get("long_qty"),
                "short_qty": state.get("short_qty"),
            },
        },
    )

    previous = _load_json(output_file) or {}
    _log_event(
        "fixed_cycle_wallet_snapshot_previous_loaded",
        {
            "output_file": str(output_file),
            "previous_exists": bool(previous),
            "previous_snapshot_phase": previous.get("snapshot_phase"),
            "previous_trade_block_id": previous.get("trade_block_id"),
            "previous_start_wallet_usdt": previous.get("start_wallet_usdt"),
            "previous_current_wallet_usdt": previous.get("current_wallet_usdt"),
            "previous_next_start_wallet_usdt": previous.get("next_start_wallet_usdt"),
            "previous_last_trade_profit_usdt": previous.get("last_trade_profit_usdt"),
            "previous_last_trade_profit_available": previous.get(
                "last_trade_profit_available"
            ),
        },
    )

    detection_source = "state_snapshot"
    long_qty, short_qty = None, None
    if args.force_flat:
        _log("Force-flat flag enabled via CLI.")
        _log(
            "fixed_cycle_wallet_snapshot_force_flat_enabled {'bot_name': '%s', 'flat_detection_source': 'cli_force_flat'}"
            % bot_name
        )
        detection_source = "cli_force_flat"
        long_qty = 0.0
        short_qty = 0.0
    elif args.long_qty is not None and args.short_qty is not None:
        detection_source = "cli_quantities"
        long_qty = args.long_qty
        short_qty = args.short_qty
    else:
        long_qty, short_qty = _position_values(state)
        if long_qty is None or short_qty is None:
            detection_source = "state_unknown"

    detection_long_qty = long_qty
    detection_short_qty = short_qty
    _log_event(
        "fixed_cycle_wallet_snapshot_flat_detection",
        {
            "detection_source": detection_source,
            "long_qty_detected": detection_long_qty,
            "short_qty_detected": detection_short_qty,
            "flat_detected": _is_zero(long_qty) if long_qty is not None else False,
            "flat_state_unknown": long_qty is None or short_qty is None,
            "mode": args.mode,
            "bot_name": bot_name,
        },
    )
    if long_qty is None or short_qty is None:
        _log(
            f"fixed_cycle_wallet_snapshot_skipped_flat_unknown bot={bot_name} source={detection_source}"
        )
        _log_event(
            "fixed_cycle_wallet_snapshot_skipped_flat_unknown",
            {"detection_source": detection_source, "mode": args.mode, "bot_name": bot_name},
        )
        return

    if not (_is_zero(long_qty) and _is_zero(short_qty)):
        _log(
            f"fixed_cycle_wallet_snapshot_skipped_not_flat bot={bot_name} long_qty={long_qty} short_qty={short_qty} source={detection_source}"
        )
        _log_event(
            "fixed_cycle_wallet_snapshot_skipped_not_flat",
            {
                "long_qty": long_qty,
                "short_qty": short_qty,
                "mode": args.mode,
                "bot_name": bot_name,
                "trade_block_id": state.get("trade_block_id"),
            },
        )
        return

    trade_block_id = state.get("trade_block_id") or state.get("last_trade_block_id") or ""
    # Snapshot is written only when flat; retrieve previous values for upcoming writes
    previous = _load_json(output_file) or {}
    prev_block = previous.get("trade_block_id")
    prev_phase = previous.get("snapshot_phase")
    duplicate_cond = (
        prev_block
        and trade_block_id
        and prev_block == trade_block_id
        and prev_phase == "flat_exit"
        and previous.get("last_trade_profit_available")
    )
    if duplicate_cond:
        if args.mode == "flat":
            _log("trade_block already processed as flat_exit; skipping profit calc.")
            _log_event(
                "fixed_cycle_wallet_snapshot_skipped_duplicate_flat_exit",
                {
                    "trade_block_id": trade_block_id,
                    "previous_trade_block_id": prev_block,
                    "previous_snapshot_phase": prev_phase,
                    "previous_last_trade_profit_available": previous.get("last_trade_profit_available"),
                    "reason": "already_processed_flat_exit",
                },
            )
            return
        _log_event(
            "fixed_cycle_wallet_snapshot_duplicate_guard_ignored_for_start",
            {
                "mode": args.mode,
                "trade_block_id": trade_block_id,
                "previous_trade_block_id": prev_block,
                "previous_snapshot_phase": prev_phase,
                "previous_last_trade_profit_available": previous.get("last_trade_profit_available"),
                "reason": "start_snapshot_must_refresh_baseline",
            },
        )
    current_wallet, metric, source, available_wallet = _fetch_wallet_snapshot()
    if current_wallet is None:
        payload = {
            "bot_name": bot_name,
            "symbol": str(state.get("symbol") or "").upper(),
            "flat_detected": True,
            "wallet_metric_used": metric,
            "wallet_balance_source": source,
            "flat_reason": "wallet_api_failed",
            "last_trade_profit_available": False,
            "trade_block_id": trade_block_id,
            "updated_at_utc3": now_utc3_iso(),
            "flat_detection_source": detection_source,
            "flat_state_unknown": False,
            "long_qty_detected": detection_long_qty,
            "short_qty_detected": detection_short_qty,
        }
        if available_wallet is not None:
            payload["available_wallet_usdt"] = available_wallet
        _write_output(output_file, payload)
        return

    if args.mode == "start":
        _log_event(
            "fixed_cycle_wallet_snapshot_start_calculation",
            {
                "current_wallet_usdt": current_wallet,
                "start_wallet_usdt": current_wallet,
                "next_start_wallet_usdt": current_wallet,
                "previous_wallet_usdt": previous.get("current_wallet_usdt"),
                "previous_last_trade_profit_usdt": previous.get("last_trade_profit_usdt"),
                "trade_block_id": trade_block_id,
                "output_file": str(output_file),
            },
        )
        timestamp = now_utc3_iso()
        payload_start = {
            "bot_name": bot_name,
            "symbol": str(state.get("symbol") or "").upper(),
            "flat_detected": True,
            "wallet_metric_used": metric,
            "wallet_balance_source": source,
            "current_wallet_usdt": current_wallet,
            "start_wallet_usdt": current_wallet,
            "previous_wallet_usdt": previous.get("current_wallet_usdt"),
            "last_trade_profit_usdt": None,
            "last_trade_profit_source": "trade_start_snapshot",
            "last_trade_profit_available": False,
            "last_trade_profit_reason": "trade_start_baseline_no_profit_yet",
            "last_trade_profit_timestamp_utc3": timestamp,
            "next_start_wallet_usdt": current_wallet,
            "snapshot_phase": "start",
            "trade_block_id": trade_block_id,
            "source": "trade_start_snapshot",
            "flat_reason": "trade_start_snapshot",
            "flat_detection_source": detection_source,
            "flat_state_unknown": False,
            "long_qty_detected": detection_long_qty,
            "short_qty_detected": detection_short_qty,
            "updated_at_utc3": timestamp,
        }
        if available_wallet is not None:
            payload_start["available_wallet_usdt"] = available_wallet
        _write_output(output_file, payload_start)
        _log("fixed_cycle_wallet_snapshot_trade_start_snapshot written.")
        _log_event(
            "fixed_cycle_wallet_snapshot_start_written",
            {
                "output_file": str(output_file),
                "bot_name": bot_name,
                "symbol": payload_start.get("symbol"),
                "trade_block_id": trade_block_id,
                "snapshot_phase": "start",
                "start_wallet_usdt": payload_start.get("start_wallet_usdt"),
                "current_wallet_usdt": current_wallet,
                "wallet_metric_used": metric,
                "wallet_balance_source": source,
            },
        )
        return

    start_wallet = (
        previous.get("start_wallet_usdt")
        or previous.get("next_start_wallet_usdt")
        or previous.get("current_wallet_usdt")
    )
    profit = None
    if start_wallet is not None:
        profit = float(current_wallet) - float(start_wallet)

    timestamp = now_utc3_iso()
    if start_wallet is None:
        _log_event(
            "fixed_cycle_wallet_snapshot_start_wallet_missing",
            {
                "previous_keys_available": bool(previous),
                "reason": "missing_start_wallet",
                "last_trade_profit_available": False,
            },
        )
    _log_event(
        "fixed_cycle_wallet_snapshot_flat_calculation",
        {
            "current_wallet_usdt": current_wallet,
            "start_wallet_usdt": start_wallet,
            "previous_wallet_usdt": start_wallet,
            "profit_usdt": profit,
            "profit_available": profit is not None,
            "profit_formula": "current_wallet_usdt - start_wallet_usdt",
            "trade_block_id": trade_block_id,
            "previous_snapshot_phase": previous.get("snapshot_phase"),
            "output_file": str(output_file),
        },
    )
    payload = {
        "bot_name": bot_name,
        "symbol": str(state.get("symbol") or "").upper(),
        "flat_detected": True,
        "wallet_metric_used": metric,
        "wallet_balance_source": source,
        "source": source,
        "current_wallet_usdt": current_wallet,
        "previous_wallet_usdt": start_wallet,
        "start_wallet_usdt": start_wallet,
        "last_trade_profit_usdt": profit,
        "last_trade_profit_source": "wallet_delta_after_flat_async",
        "last_trade_profit_available": profit is not None,
        "last_trade_profit_reason": "wallet_delta_after_flat_async"
        if profit is not None
        else "missing_start_wallet",
        "last_trade_profit_timestamp_utc3": timestamp,
        "trade_block_id": trade_block_id,
        "snapshot_phase": "flat_exit",
        "flat_reason": "wallet_delta_after_flat_async" if profit is not None else "missing_start_wallet",
        "next_start_wallet_usdt": current_wallet,
        "updated_at_utc3": timestamp,
        "flat_detection_source": detection_source,
        "flat_state_unknown": False,
        "long_qty_detected": detection_long_qty,
        "short_qty_detected": detection_short_qty,
    }
    if available_wallet is not None:
        payload["available_wallet_usdt"] = available_wallet
    _write_output(output_file, payload)
    _log(f"Flat state detected; snapshot written with profit={profit}.")
    _log_event(
        "fixed_cycle_wallet_snapshot_flat_written",
        {
            "output_file": str(output_file),
            "bot_name": bot_name,
            "symbol": payload.get("symbol"),
            "trade_block_id": trade_block_id,
            "snapshot_phase": "flat_exit",
            "start_wallet_usdt": start_wallet,
            "current_wallet_usdt": current_wallet,
            "previous_wallet_usdt": start_wallet,
            "last_trade_profit_usdt": profit,
            "last_trade_profit_available": profit is not None,
            "wallet_metric_used": metric,
            "wallet_balance_source": source,
        },
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log(f"Unhandled error: {exc}")
    sys.exit(0)

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
from datetime import datetime, timezone
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CURRENT_DIR) in sys.path:
    sys.path.remove(str(CURRENT_DIR))

from emergency_100.config import Emergency100Config
from emergency_100.executor import Emergency100Executor
from emergency_100.logging_utils import append_jsonl, configure_audit_log, log_event
from emergency_100.state import Emergency100Mode, Emergency100RuntimeState, HedgeSnapshot, MarketBias
from emergency_100.strategy import Emergency100Strategy
from strategy.config import StrategyConfig
from strategy.order_manager import BybitOrderManager


def build_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("emergency_100")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run emergency 100:100 strategy runner.")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--settle-coin", default="USDT")
    parser.add_argument("--live-snapshot", action="store_true")
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--long-usdt", type=float, default=None)
    parser.add_argument("--short-usdt", type=float, default=None)
    parser.add_argument("--long-avg", type=float, default=None)
    parser.add_argument("--short-avg", type=float, default=None)
    parser.add_argument("--atr-pct", type=float, default=None)
    parser.add_argument("--price-speed-pct", type=float, default=None)
    parser.add_argument(
        "--market-bias",
        choices=[bias.value for bias in MarketBias],
        default=MarketBias.UNCLEAR.value,
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in Emergency100Mode],
        default=None,
    )
    parser.add_argument("--bridge-step-index", type=int, default=None)
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--runtime-state-file", default=None)
    parser.add_argument("--runtime-history-file", default=None)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=float, default=None)
    parser.add_argument("--reset-runtime", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    return parser.parse_args()


def _resolve_cycle_id(explicit_cycle_id: str | None) -> str:
    if explicit_cycle_id:
        return explicit_cycle_id
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"em100-{timestamp}-{uuid4().hex[:6]}"


def _build_decision_id(cycle_id: str, decision_count: int) -> str:
    return f"{cycle_id}-d{decision_count:04d}"


def _load_runtime_state(path: str) -> Emergency100RuntimeState | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return Emergency100RuntimeState.from_dict(payload)


def _save_runtime_state(path: str, runtime: Emergency100RuntimeState) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(runtime.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _merge_runtime_state(
    loaded_runtime: Emergency100RuntimeState | None,
    *,
    mode_arg: str | None,
    bridge_step_index_arg: int | None,
    cycle_id_arg: str | None,
    reset_runtime: bool,
) -> Emergency100RuntimeState:
    runtime = Emergency100RuntimeState() if reset_runtime or loaded_runtime is None else loaded_runtime
    runtime.mode = Emergency100Mode(mode_arg) if mode_arg else runtime.mode
    runtime.bridge_step_index = (
        bridge_step_index_arg if bridge_step_index_arg is not None else runtime.bridge_step_index
    )
    runtime.cycle_id = cycle_id_arg or runtime.cycle_id or _resolve_cycle_id(None)
    if reset_runtime:
        runtime.decision_count = 0
        runtime.last_decision_id = ""
        runtime.last_action = "none"
        runtime.last_reason = ""
        runtime.notes = []
    return runtime


def _advance_runtime_state(
    runtime: Emergency100RuntimeState,
    *,
    decision_id: str,
    decision_mode: Emergency100Mode,
    decision_reason: str,
    decision_reason_code: str,
    action_kinds: list[str],
) -> Emergency100RuntimeState:
    next_runtime = Emergency100RuntimeState.from_dict(runtime.to_dict())
    next_runtime.mode = decision_mode
    next_runtime.decision_count += 1
    next_runtime.last_decision_id = decision_id
    next_runtime.last_action = action_kinds[0] if action_kinds else "none"
    next_runtime.last_reason = decision_reason
    if decision_reason_code == "enter_emergency":
        next_runtime.bridge_step_index = 0
    elif decision_reason_code == "bridge_target_satisfied":
        next_runtime.bridge_step_index += 1
    note = (
        f"{decision_id}:{decision_reason_code}:mode={decision_mode.value}:"
        f"action={next_runtime.last_action}:bridge_step={next_runtime.bridge_step_index}"
    )
    next_runtime.notes = (next_runtime.notes + [note])[-100:]
    return next_runtime


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _load_live_snapshot(
    *,
    logger: logging.Logger,
    config: StrategyConfig,
    order_manager: BybitOrderManager,
    symbol: str | None,
    settle_coin: str | None,
    atr_pct: float | None,
    price_speed_pct: float | None,
) -> HedgeSnapshot:
    log_event(
        logger,
        "live_snapshot_fetch_start",
        symbol=symbol,
        settle_coin=settle_coin,
        category=config.category,
    )
    positions = order_manager.fetch_positions(
        symbol=symbol,
        category=config.category,
        settle_coin=None if symbol else settle_coin,
    )
    log_event(
        logger,
        "live_snapshot_positions_fetched",
        requested_symbol=symbol,
        settle_coin=settle_coin,
        position_count=len(positions),
        positions=positions,
    )
    if not positions:
        raise RuntimeError("No live positions returned from Bybit.")

    active_positions = [
        position for position in positions if _safe_float(position.get("size")) > 0
    ]
    log_event(
        logger,
        "live_snapshot_positions_filtered",
        requested_symbol=symbol,
        active_position_count=len(active_positions),
        active_positions=active_positions,
    )
    if not active_positions:
        raise RuntimeError("Live positions exist call returned no active hedge legs.")

    resolved_symbol = symbol
    if not resolved_symbol:
        resolved_symbol = str(active_positions[0].get("symbol") or config.default_symbol).upper()

    long_size_coin = 0.0
    short_size_coin = 0.0
    long_avg = 0.0
    short_avg = 0.0

    for position in active_positions:
        position_symbol = str(position.get("symbol") or "").upper()
        if position_symbol != resolved_symbol:
            continue
        side = str(position.get("side") or "").lower()
        size = _safe_float(position.get("size"))
        avg = _safe_float(position.get("avgPrice") or position.get("entryPrice"))
        if side == "buy":
            long_size_coin = size
            long_avg = avg
        elif side == "sell":
            short_size_coin = size
            short_avg = avg

    if long_size_coin <= 0 and short_size_coin <= 0:
        raise RuntimeError(f"No active hedge legs found for symbol {resolved_symbol}.")

    current_price = order_manager.fetch_mark_price(resolved_symbol, config.category)
    log_event(
        logger,
        "live_snapshot_price_fetched",
        resolved_symbol=resolved_symbol,
        current_price=current_price,
        category=config.category,
    )
    if current_price is None or current_price <= 0:
        raise RuntimeError(f"Unable to fetch mark price for {resolved_symbol}.")

    long_size_usdt = long_size_coin * current_price
    short_size_usdt = short_size_coin * current_price
    return HedgeSnapshot(
        symbol=resolved_symbol,
        current_price=current_price,
        long_size_usdt=long_size_usdt,
        short_size_usdt=short_size_usdt,
        long_avg=long_avg,
        short_avg=short_avg,
        atr_pct=atr_pct,
        price_speed_pct=price_speed_pct,
    )


def _load_manual_snapshot(args: argparse.Namespace, default_symbol: str) -> HedgeSnapshot:
    missing = [
        name
        for name, value in (
            ("price", args.price),
            ("long_usdt", args.long_usdt),
            ("short_usdt", args.short_usdt),
            ("long_avg", args.long_avg),
            ("short_avg", args.short_avg),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "Manual mode requires these arguments: " + ", ".join(missing)
        )

    return HedgeSnapshot(
        symbol=(args.symbol or default_symbol).upper(),
        current_price=float(args.price),
        long_size_usdt=float(args.long_usdt),
        short_size_usdt=float(args.short_usdt),
        long_avg=float(args.long_avg),
        short_avg=float(args.short_avg),
        atr_pct=args.atr_pct,
        price_speed_pct=args.price_speed_pct,
    )


def main() -> None:
    args = parse_args()
    base_config = StrategyConfig()
    default_symbol = (args.symbol or base_config.default_symbol).upper()
    config = Emergency100Config(default_symbol=default_symbol)
    runtime_state_file = args.runtime_state_file or config.runtime_state_file
    runtime_history_file = args.runtime_history_file or config.runtime_history_file
    configure_audit_log(config.audit_log_file)
    logger = build_logger(config.log_file)
    strategy = Emergency100Strategy(config)
    order_manager = BybitOrderManager(base_config.api_key, base_config.secret_key)
    executor = Emergency100Executor(base_config, order_manager, logger)
    log_event(
        logger,
        "runner_start",
        args=vars(args),
        strategy_config={
            "default_symbol": default_symbol,
            "category": base_config.category,
            "min_order_value": base_config.min_order_value,
            "add_size_usdt": config.add_size_usdt,
            "emergency_spread_trigger_pct": config.emergency_spread_trigger_pct,
            "emergency_speed_trigger_pct": config.emergency_speed_trigger_pct,
            "atr_speed_multiple": config.atr_speed_multiple,
            "bridge_resume_spread_pct": config.bridge_resume_spread_pct,
            "bridge_targets": config.bridge_targets,
            "ratio_tolerance": config.ratio_tolerance,
            "audit_log_file": config.audit_log_file,
            "runtime_state_file": runtime_state_file,
            "runtime_history_file": runtime_history_file,
        },
    )

    try:
        loaded_runtime = None if args.reset_runtime else _load_runtime_state(runtime_state_file)
        log_event(
            logger,
            "runtime_state_loaded",
            runtime_state_file=runtime_state_file,
            reset_runtime=args.reset_runtime,
            loaded_runtime=loaded_runtime,
        )
        runtime = _merge_runtime_state(
            loaded_runtime,
            mode_arg=args.mode,
            bridge_step_index_arg=args.bridge_step_index,
            cycle_id_arg=args.cycle_id,
            reset_runtime=args.reset_runtime,
        )
        bias = MarketBias(args.market_bias)
        final_payload: dict[str, Any] | None = None
        iterations = max(1, int(args.iterations))
        sleep_seconds = config.loop_interval_seconds if args.sleep_seconds is None else max(0.0, args.sleep_seconds)

        for loop_index in range(iterations):
            if loop_index > 0 and sleep_seconds > 0:
                time.sleep(sleep_seconds)

            if args.live_snapshot:
                snapshot = _load_live_snapshot(
                    logger=logger,
                    config=base_config,
                    order_manager=order_manager,
                    symbol=args.symbol,
                    settle_coin=args.settle_coin,
                    atr_pct=args.atr_pct,
                    price_speed_pct=args.price_speed_pct,
                )
            else:
                snapshot = _load_manual_snapshot(args, default_symbol)

            decision_id = _build_decision_id(runtime.cycle_id or "em100", runtime.decision_count + 1)
            log_event(
                logger,
                "runtime_initialized",
                runtime=runtime,
                decision_id=decision_id,
                market_bias=bias,
                execute_live=args.execute_live,
                loop_index=loop_index,
                iterations=iterations,
            )

            log_event(
                logger,
                "snapshot",
                cycle_id=runtime.cycle_id,
                decision_id=decision_id,
                snapshot=snapshot,
                source="live" if args.live_snapshot else "manual",
                loop_index=loop_index,
            )

            runtime_before = runtime.to_dict()
            decision = strategy.decide(snapshot=snapshot, runtime=runtime, market_bias=bias)
            execution_results = executor.execute_actions(
                snapshot=snapshot,
                actions=decision.actions,
                cycle_id=runtime.cycle_id or "em100",
                decision_id=decision_id,
                execute_live=args.execute_live,
            )
            action_kinds = [action.kind.value for action in decision.actions]
            runtime = _advance_runtime_state(
                runtime,
                decision_id=decision_id,
                decision_mode=decision.mode,
                decision_reason=decision.reason,
                decision_reason_code=decision.reason_code,
                action_kinds=action_kinds,
            )
            _save_runtime_state(runtime_state_file, runtime)
            log_event(
                logger,
                "runtime_state_saved",
                runtime_state_file=runtime_state_file,
                runtime=runtime,
            )

            payload = {
                "loop_index": loop_index,
                "iterations": iterations,
                "cycle_id": runtime.cycle_id,
                "decision_id": decision_id,
                "mode": decision.mode.value,
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "reason_details": decision.reason_details,
                "decision_path": decision.decision_path,
                "metrics": decision.metrics,
                "runtime_before": runtime_before,
                "runtime_after": runtime.to_dict(),
                "snapshot": {
                    "symbol": snapshot.symbol,
                    "current_price": snapshot.current_price,
                    "long_size_usdt": snapshot.long_size_usdt,
                    "short_size_usdt": snapshot.short_size_usdt,
                    "long_avg": snapshot.long_avg,
                    "short_avg": snapshot.short_avg,
                    "spread_pct": snapshot.spread_pct,
                    "short_ratio": snapshot.short_ratio,
                },
                "actions": [
                    {
                        "kind": action.kind.value,
                        "size_usdt": action.size_usdt,
                        "reason": action.reason,
                        "reason_code": action.reason_code,
                        "metadata": action.metadata,
                    }
                    for action in decision.actions
                ],
                "execution": [
                    {
                        "cycle_id": result.cycle_id,
                        "decision_id": result.decision_id,
                        "action": result.action,
                        "action_reason": result.action_reason,
                        "action_reason_code": result.action_reason_code,
                        "status": result.status,
                        "reason": result.reason,
                        "requested_size_usdt": result.requested_size_usdt,
                        "submitted_qty": result.submitted_qty,
                        "response": result.response,
                    }
                    for result in execution_results
                ],
            }
            append_jsonl(runtime_history_file, payload)
            log_event(
                logger,
                "runtime_history_appended",
                runtime_history_file=runtime_history_file,
                cycle_id=runtime.cycle_id,
                decision_id=decision_id,
                loop_index=loop_index,
            )
            log_event(logger, "decision", **payload)
            final_payload = payload

        print(json.dumps(final_payload, indent=2, sort_keys=True))
    except Exception as exc:
        log_event(
            logger,
            "runner_failed",
            error=str(exc),
            execute_live=args.execute_live,
            args=vars(args),
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

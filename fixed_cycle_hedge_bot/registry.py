from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from strategy.config import StrategyConfig

from .basket_exit_strategy import BasketExitConfig, BasketExitHedgeStrategy
from .dynamic_breakeven_strategy import DynamicBreakevenConfig, DynamicBreakevenHedgeStrategy
from .fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from .runtime import GenericHedgeRuntime, GenericRuntimeConfig

logger = logging.getLogger(__name__)


def _load_startup_best_coin_info(config: FixedCycleHedgeConfig) -> dict[str, Any] | None:
    if not config.dynamic_symbol_enabled:
        logger.info("startup_best_coin_skipped", {"reason": "dynamic_disabled"})
        return None
    file_path = Path(config.best_coin_file or "logs/best_coin.json")
    if not file_path.exists():
        logger.info("startup_best_coin_skipped", {"reason": "missing_file", "path": str(file_path)})
        return None
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        logger.info(
            "startup_best_coin_skipped",
            {"reason": "invalid_json", "path": str(file_path), "error": str(exc)},
        )
        return None
    if not isinstance(payload, dict):
        logger.info(
            "startup_best_coin_skipped",
            {"reason": "invalid_json", "path": str(file_path), "error": "payload not object"},
        )
        return None
    symbol = payload.get("symbol")
    timestamp_raw = payload.get("timestamp")
    if not symbol or not isinstance(symbol, str):
        logger.info(
            "startup_best_coin_skipped",
            {"reason": "invalid_symbol", "path": str(file_path), "symbol": symbol},
        )
        return None
    symbol_upper = symbol.upper()
    if not symbol_upper.endswith("USDT"):
        logger.info(
            "startup_best_coin_skipped",
            {
                "reason": "invalid_symbol",
                "path": str(file_path),
                "symbol": symbol_upper,
                "detail": "must end with USDT",
            },
        )
        return None
    if not timestamp_raw or not isinstance(timestamp_raw, str):
        logger.info(
            "startup_best_coin_skipped",
            {"reason": "invalid_timestamp", "path": str(file_path), "symbol": symbol_upper},
        )
        return None
    try:
        timestamp = datetime.fromisoformat(timestamp_raw)
    except ValueError as exc:
        logger.info(
            "startup_best_coin_skipped",
            {"reason": "invalid_timestamp", "path": str(file_path), "symbol": symbol_upper, "error": str(exc)},
        )
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_minutes = (now - timestamp).total_seconds() / 60
    max_age = float(config.best_coin_max_age_minutes or 0) or 0
    if max_age and age_minutes > max_age:
        logger.info(
            "startup_best_coin_skipped",
            {
                "reason": "stale",
                "path": str(file_path),
                "symbol": symbol_upper,
                "age_minutes": round(age_minutes, 1),
                "max_age_minutes": max_age,
            },
        )
        return None
    hold_minutes = float(config.dynamic_symbol_hold_minutes or 0) or 0
    minute_in_cycle = now.minute % 30
    if hold_minutes and minute_in_cycle < hold_minutes:
        logger.info(
            "startup_best_coin_skipped",
            {
                "reason": "hold_window",
                "path": str(file_path),
                "symbol": symbol_upper,
                "hold_minutes": hold_minutes,
                "minute_in_cycle": minute_in_cycle,
            },
        )
        return None
    logger.info(
        "startup_best_coin_loaded",
        {
            "path": str(file_path),
            "symbol": symbol_upper,
            "score": payload.get("score"),
            "timestamp": timestamp_raw,
            "reason": payload.get("reason"),
        },
    )
    return {
        "symbol": symbol_upper,
        "score": payload.get("score"),
        "timestamp": timestamp,
        "reason": payload.get("reason"),
        "age_minutes": age_minutes,
    }


def apply_startup_best_coin_symbol(
    strategy_config: FixedCycleHedgeConfig,
) -> tuple[str, str] | None:
    best_coin = _load_startup_best_coin_info(strategy_config)
    if not best_coin:
        return None
    old_symbol = strategy_config.symbol
    new_symbol = best_coin["symbol"]
    strategy_config.symbol = new_symbol
    logger.info(
        "startup_best_coin_applied",
        {
            "path": strategy_config.best_coin_file,
            "old_symbol": old_symbol,
            "new_symbol": new_symbol,
            "score": best_coin.get("score"),
            "reason": best_coin.get("reason"),
        },
    )
    logger.info(
        "dynamic_symbol_updated",
        {"reason": "startup_best_coin", "old_symbol": old_symbol, "new_symbol": new_symbol},
    )
    return old_symbol, new_symbol


@dataclass(frozen=True)
class StrategyRegistration:
    name: str
    description: str
    build_runtime: Callable[[StrategyConfig, str | None], GenericHedgeRuntime]


def _build_dynamic_breakeven_runtime(base_config: StrategyConfig, strategy_config_path: str | None = None) -> GenericHedgeRuntime:
    runtime_config = GenericRuntimeConfig(
        api_key=base_config.api_key,
        secret_key=base_config.secret_key,
        symbol=base_config.default_symbol,
        category=base_config.category,
        min_order_value=base_config.min_order_value,
        price_poll_interval_seconds=1.0,
        reconcile_interval_seconds=base_config.order_sync_interval_seconds,
        log_file="logs/dynamic_breakeven_runtime.log",
        audit_log_file="logs/dynamic_breakeven_runtime_audit.jsonl",
    )
    strategy = DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig())
    return GenericHedgeRuntime(runtime_config, strategy)


def _build_basket_exit_runtime(base_config: StrategyConfig, strategy_config_path: str | None = None) -> GenericHedgeRuntime:
    runtime_config = GenericRuntimeConfig(
        api_key=base_config.api_key,
        secret_key=base_config.secret_key,
        symbol=base_config.default_symbol,
        category=base_config.category,
        min_order_value=base_config.min_order_value,
        price_poll_interval_seconds=1.0,
        reconcile_interval_seconds=base_config.order_sync_interval_seconds,
        log_file="logs/basket_exit_runtime.log",
        audit_log_file="logs/basket_exit_runtime_audit.jsonl",
    )
    strategy = BasketExitHedgeStrategy(BasketExitConfig())
    return GenericHedgeRuntime(runtime_config, strategy)


STRATEGY_CLASS_MAP: dict[str, type[FixedCycleHedgeStrategy]] = {
    "FixedCycleHedgeStrategy": FixedCycleHedgeStrategy,
    "ShortFixedCycleHedgeStrategy": ShortFixedCycleHedgeStrategy,
}


def _select_fixed_cycle_strategy_class(name: str) -> type[FixedCycleHedgeStrategy]:
    cls = STRATEGY_CLASS_MAP.get(name)
    if cls is None:
        valid = ", ".join(sorted(STRATEGY_CLASS_MAP.keys()))
        raise ValueError(
            f"Unknown fixed-cycle strategy_class '{name}'. Allowed: {valid}"
        )
    return cls


def _build_fixed_cycle_runtime(base_config: StrategyConfig, strategy_config_path: str | None = None) -> GenericHedgeRuntime:
    strategy_config = FixedCycleHedgeConfig.from_json_file(strategy_config_path)
    applied_symbols = apply_startup_best_coin_symbol(strategy_config)
    if applied_symbols:
        old_symbol, new_symbol = applied_symbols
        logger.info(
            "runtime_active_symbol_updated",
            {
                "reason": "startup_best_coin",
                "old_symbol": old_symbol,
                "new_symbol": new_symbol,
            },
        )
    runtime_config = GenericRuntimeConfig(
        api_key=base_config.api_key,
        secret_key=base_config.secret_key,
        symbol=strategy_config.symbol or base_config.default_symbol,
        category=strategy_config.category or base_config.category,
        min_order_value=strategy_config.min_notional_usdt or base_config.min_order_value,
        price_poll_interval_seconds=1.0,
        reconcile_interval_seconds=max(base_config.order_sync_interval_seconds, 1.0),
        log_file="logs/fixed_cycle_hedge_runtime.log",
        audit_log_file="logs/fixed_cycle_hedge_runtime_audit.jsonl",
    )
    strategy_class_name = strategy_config.strategy_class or "FixedCycleHedgeStrategy"
    strategy_cls = _select_fixed_cycle_strategy_class(strategy_class_name)
    logger.info(
        "fixed_cycle_strategy_class_selected",
        {
            "strategy": "fixed_cycle",
            "strategy_class": strategy_class_name,
            "config_path": strategy_config_path,
            "default_symbol": runtime_config.symbol,
        },
    )
    strategy = strategy_cls(strategy_config)
    return GenericHedgeRuntime(runtime_config, strategy)


STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    "dynamic_breakeven": StrategyRegistration(
        name="dynamic_breakeven",
        description="Fill-getriebener Breakeven-Hedge-Bot",
        build_runtime=_build_dynamic_breakeven_runtime,
    ),
    "basket_exit": StrategyRegistration(
        name="basket_exit",
        description="Schliesst den kompletten Hedge am Basket-Breakeven oder besser",
        build_runtime=_build_basket_exit_runtime,
    ),
    "fixed_cycle": StrategyRegistration(
        name="fixed_cycle",
        description="Geplanter Hedge-Zyklus mit vorbereiteten Downside- und Exit-Orders",
        build_runtime=_build_fixed_cycle_runtime,
    ),
}


def list_strategy_names() -> list[str]:
    return sorted(STRATEGY_REGISTRY)


def build_registered_runtime(
    strategy_name: str,
    base_config: StrategyConfig,
    strategy_config_path: str | None = None,
) -> GenericHedgeRuntime:
    registration = STRATEGY_REGISTRY.get(strategy_name)
    if registration is None:
        available = ", ".join(list_strategy_names())
        raise ValueError(f"Unknown strategy '{strategy_name}'. Available: {available}")
    return registration.build_runtime(base_config, strategy_config_path)

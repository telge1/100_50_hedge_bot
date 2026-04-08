from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from strategy.config import StrategyConfig

from .basket_exit_strategy import BasketExitConfig, BasketExitHedgeStrategy
from .dynamic_breakeven_strategy import DynamicBreakevenConfig, DynamicBreakevenHedgeStrategy
from .fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from .runtime import GenericHedgeRuntime, GenericRuntimeConfig


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


def _build_fixed_cycle_runtime(base_config: StrategyConfig, strategy_config_path: str | None = None) -> GenericHedgeRuntime:
    strategy_config = FixedCycleHedgeConfig.from_json_file(strategy_config_path)
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
    strategy = FixedCycleHedgeStrategy(strategy_config)
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

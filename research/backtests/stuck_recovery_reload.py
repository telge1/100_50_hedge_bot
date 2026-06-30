"""Backtest-only stuck SHORT_REDUCE recovery reload (optimizer-ready)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .hedge_bot_original_simulator import HedgeBotOriginalSimulator

SHORT_REDUCE_PURPOSE_RE = re.compile(r"^CYCLE_(\d+)_SHORT_REDUCE$", re.I)


@dataclass
class StuckRecoveryReloadConfig:
    enabled: bool = False
    reload_min_cycle_index: int = 5
    reload_wait_candles_after_last_fill: int = 500
    max_reloads_per_trade: int = 1
    reload_long_notional_usdt: float | None = None
    reload_short_notional_usdt: float | None = None
    fallback_reload_long_notional_usdt: float = 100.0
    fallback_reload_short_notional_usdt: float = 50.0
    name: str = "manual_default"


@dataclass
class StuckRecoveryReloadTrigger:
    cycle_index: int
    active_purpose: str
    candles_since_last_fill: int
    realized_pnl_before: float


@dataclass
class StuckRecoveryReloadRecord:
    reload_count_for_trade: int = 0
    reload_cycle_index: int | None = None
    reload_reason: str = ""
    reload_candles_since_last_fill: int | None = None
    reload_realized_pnl_before: float | None = None
    reload_long_notional_usdt: float | None = None
    reload_short_notional_usdt: float | None = None
    reload_long_qty: float | None = None
    reload_short_qty: float | None = None
    active_purpose_before_reload: str | None = None
    active_purposes_after_reload: list[str] = field(default_factory=list)
    stuck_recovery_reload_triggered: bool = False


def default_stuck_recovery_reload_config() -> StuckRecoveryReloadConfig:
    return StuckRecoveryReloadConfig(enabled=True)


def config_from_dict(payload: Mapping[str, Any]) -> StuckRecoveryReloadConfig:
    return StuckRecoveryReloadConfig(
        enabled=bool(payload.get("enabled", False)),
        reload_min_cycle_index=int(payload.get("reload_min_cycle_index", 5)),
        reload_wait_candles_after_last_fill=int(
            payload.get("reload_wait_candles_after_last_fill", 500)
        ),
        max_reloads_per_trade=int(payload.get("max_reloads_per_trade", 1)),
        reload_long_notional_usdt=_optional_float(payload.get("reload_long_notional_usdt")),
        reload_short_notional_usdt=_optional_float(payload.get("reload_short_notional_usdt")),
        fallback_reload_long_notional_usdt=float(
            payload.get("fallback_reload_long_notional_usdt", 100.0)
        ),
        fallback_reload_short_notional_usdt=float(
            payload.get("fallback_reload_short_notional_usdt", 50.0)
        ),
        name=str(payload.get("name") or "manual_default"),
    )


def config_to_dict(config: StuckRecoveryReloadConfig) -> dict[str, Any]:
    return asdict(config)


def config_from_json_string(raw: str) -> StuckRecoveryReloadConfig:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("stuck recovery reload config JSON must be an object")
    return config_from_dict(payload)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def is_cycle_short_reduce_purpose(purpose: object) -> bool:
    return bool(SHORT_REDUCE_PURPOSE_RE.match(str(purpose or "").strip().upper()))


def extract_cycle_index_from_short_reduce_purpose(purpose: object) -> int:
    match = SHORT_REDUCE_PURPOSE_RE.match(str(purpose or "").strip().upper())
    if not match:
        return 0
    return int(match.group(1))


def find_active_short_reduce_order(
    sim: HedgeBotOriginalSimulator,
) -> tuple[str, int] | None:
    for order in sim.book.active_orders():
        purpose = str(order.purpose or "").strip().upper()
        if not is_cycle_short_reduce_purpose(purpose):
            continue
        return purpose, extract_cycle_index_from_short_reduce_purpose(purpose)
    return None


def _initial_long_notional_usdt(strategy_state: dict[str, Any], strategy_config: Any) -> float:
    explicit = strategy_state.get("initial_long_notional_usdt")
    if explicit is not None and float(explicit) > 0:
        return float(explicit)
    entry = float(strategy_state.get("entry_reference_price") or 0.0)
    qty = float(strategy_state.get("initial_long_qty") or 0.0)
    if entry > 0 and qty > 0:
        return entry * qty
    base = float(getattr(strategy_config, "base_notional_usdt", 0.0) or 0.0)
    return base


def _initial_short_notional_usdt(strategy_state: dict[str, Any], strategy_config: Any) -> float:
    explicit = strategy_state.get("initial_short_notional_usdt")
    if explicit is not None and float(explicit) > 0:
        return float(explicit)
    entry = float(strategy_state.get("entry_reference_price") or 0.0)
    qty = float(strategy_state.get("initial_short_qty") or 0.0)
    if entry > 0 and qty > 0:
        return entry * qty
    base = float(getattr(strategy_config, "base_notional_usdt", 0.0) or 0.0)
    ratio = float(getattr(strategy_config, "hedge_ratio_short", 0.5) or 0.5)
    return base * ratio


def resolve_reload_notionals(
    config: StuckRecoveryReloadConfig,
    strategy_state: dict[str, Any],
    strategy_config: Any,
) -> tuple[float, float]:
    long_notional = config.reload_long_notional_usdt
    if long_notional is None or long_notional <= 0:
        long_notional = _initial_long_notional_usdt(strategy_state, strategy_config)
    if long_notional <= 0:
        long_notional = float(config.fallback_reload_long_notional_usdt)

    short_notional = config.reload_short_notional_usdt
    if short_notional is None or short_notional <= 0:
        short_notional = _initial_short_notional_usdt(strategy_state, strategy_config)
    if short_notional <= 0:
        short_notional = float(config.fallback_reload_short_notional_usdt)
    return float(long_notional), float(short_notional)


def should_trigger_stuck_recovery_reload(
    sim: HedgeBotOriginalSimulator,
    *,
    config: StuckRecoveryReloadConfig,
    cumulative_pnl: float,
    candles_since_last_fill: int,
    reload_count_for_trade: int,
    trade_closed: bool,
) -> tuple[bool, StuckRecoveryReloadTrigger | None]:
    if not config.enabled or trade_closed:
        return False, None
    if reload_count_for_trade >= int(config.max_reloads_per_trade):
        return False, None
    if cumulative_pnl >= 0:
        return False, None
    if candles_since_last_fill < int(config.reload_wait_candles_after_last_fill):
        return False, None
    match = find_active_short_reduce_order(sim)
    if match is None:
        return False, None
    purpose, cycle_index = match
    if cycle_index < int(config.reload_min_cycle_index):
        return False, None
    return True, StuckRecoveryReloadTrigger(
        cycle_index=cycle_index,
        active_purpose=purpose,
        candles_since_last_fill=candles_since_last_fill,
        realized_pnl_before=float(cumulative_pnl),
    )


def build_stuck_recovery_reload_metadata(
    *,
    config: StuckRecoveryReloadConfig,
    record: StuckRecoveryReloadRecord,
    trigger: StuckRecoveryReloadTrigger | None = None,
) -> dict[str, Any]:
    return {
        "stuck_recovery_reload_enabled": bool(config.enabled),
        "stuck_recovery_reload_triggered": bool(record.stuck_recovery_reload_triggered),
        "stuck_recovery_reload_config_name": config.name,
        "reload_cycle_index": record.reload_cycle_index,
        "reload_reason": record.reload_reason,
        "reload_candles_since_last_fill": record.reload_candles_since_last_fill,
        "reload_realized_pnl_before": record.reload_realized_pnl_before,
        "reload_long_notional_usdt": record.reload_long_notional_usdt,
        "reload_short_notional_usdt": record.reload_short_notional_usdt,
        "reload_long_qty": record.reload_long_qty,
        "reload_short_qty": record.reload_short_qty,
        "reload_count_for_trade": record.reload_count_for_trade,
        "active_purpose_before_reload": record.active_purpose_before_reload,
        "active_purposes_after_reload": list(record.active_purposes_after_reload),
        "cycle_index": record.reload_cycle_index,
        "purpose": trigger.active_purpose if trigger is not None else record.active_purpose_before_reload,
    }

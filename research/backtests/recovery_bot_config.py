"""Backtest-only configuration for integrated long-gap recovery bot."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

DEFAULT_RECOVERY_START_PURPOSE = "CYCLE_4_LONG_ADD"
DEFAULT_RECOVERY_TIMEOUT_ACTION = "gap_reduction"
ALLOWED_RECOVERY_START_PURPOSES = frozenset(
    {
        "CYCLE_3_SHORT_REDUCE",
        "CYCLE_4_LONG_ADD",
        "CYCLE_4_SHORT_REDUCE",
    }
)
ALLOWED_RECOVERY_TIMEOUT_ACTIONS = frozenset({"gap_reduction", "close_all"})


@dataclass
class RecoveryBotConfig:
    """Backtest-only long-gap recovery integrated into the continuous backtester."""

    enabled: bool = False
    recovery_start_purpose: str = DEFAULT_RECOVERY_START_PURPOSE
    recovery_wait_candles: int = 144
    recovery_gap_reduce_steps: int = 4
    recovery_gap_reduce_fraction_per_step: float = 0.25
    step_trigger_pct: float = 1.0
    cancel_open_cycle_orders_on_activation: bool = True
    stop_new_cycle_orders_on_activation: bool = True
    # Backtest-only timeout alternative: after wait, either start gap reduction
    # or fully close both legs. Default preserves historical behaviour.
    recovery_timeout_action: str = DEFAULT_RECOVERY_TIMEOUT_ACTION
    # Optional loss gate for close_all. None => always close at timeout candle.
    # Positive value X means close only when estimated net exit PnL <= -X.
    # Evaluated once at the timeout candle only (no later re-check).
    recovery_timeout_min_loss_usdt: float | None = None
    # Optional continuous max-loss stop during the wait phase (backtest-only).
    # Positive value X means close_all as soon as estimated net exit PnL <= -X
    # on any candle after the reference fill. None => disabled (timeout-only).
    recovery_max_loss_usdt: float | None = None
    # Optional additional-loss stop relative to the reference-fill baseline.
    # Positive value X means close_all when
    # (reference_net_exit_pnl - current_net_exit_pnl) >= X.
    # None => disabled. Can run in parallel with recovery_max_loss_usdt.
    recovery_max_additional_loss_usdt: float | None = None
    name: str = "manual_default"


def normalize_recovery_timeout_action(value: str | None) -> str:
    action = str(value or DEFAULT_RECOVERY_TIMEOUT_ACTION).strip().lower()
    if action not in ALLOWED_RECOVERY_TIMEOUT_ACTIONS:
        raise ValueError(
            f"unsupported recovery_timeout_action={action!r}; "
            f"allowed={sorted(ALLOWED_RECOVERY_TIMEOUT_ACTIONS)}"
        )
    return action


def normalize_recovery_start_purpose(value: str | None) -> str:
    purpose = str(value or DEFAULT_RECOVERY_START_PURPOSE).strip()
    if purpose not in ALLOWED_RECOVERY_START_PURPOSES:
        raise ValueError(
            f"unsupported recovery_start_purpose={purpose!r}; "
            f"allowed={sorted(ALLOWED_RECOVERY_START_PURPOSES)}"
        )
    return purpose


def default_recovery_bot_config() -> RecoveryBotConfig:
    return RecoveryBotConfig()


def config_from_mapping(payload: Mapping[str, Any]) -> RecoveryBotConfig:
    cfg = default_recovery_bot_config()
    if "enabled" in payload:
        cfg.enabled = bool(payload["enabled"])
    if "recovery_start_purpose" in payload:
        cfg.recovery_start_purpose = normalize_recovery_start_purpose(
            str(payload["recovery_start_purpose"])
        )
    if "recovery_wait_candles" in payload:
        cfg.recovery_wait_candles = max(0, int(payload["recovery_wait_candles"]))
    if "recovery_gap_reduce_steps" in payload:
        cfg.recovery_gap_reduce_steps = max(1, int(payload["recovery_gap_reduce_steps"]))
    if "recovery_gap_reduce_fraction_per_step" in payload:
        cfg.recovery_gap_reduce_fraction_per_step = float(
            payload["recovery_gap_reduce_fraction_per_step"]
        )
    if "step_trigger_pct" in payload:
        cfg.step_trigger_pct = float(payload["step_trigger_pct"])
    if "cancel_open_cycle_orders_on_activation" in payload:
        cfg.cancel_open_cycle_orders_on_activation = bool(
            payload["cancel_open_cycle_orders_on_activation"]
        )
    if "stop_new_cycle_orders_on_activation" in payload:
        cfg.stop_new_cycle_orders_on_activation = bool(
            payload["stop_new_cycle_orders_on_activation"]
        )
    if "recovery_timeout_action" in payload:
        cfg.recovery_timeout_action = normalize_recovery_timeout_action(
            str(payload["recovery_timeout_action"])
        )
    if "recovery_timeout_min_loss_usdt" in payload:
        raw = payload["recovery_timeout_min_loss_usdt"]
        if raw is None or raw == "":
            cfg.recovery_timeout_min_loss_usdt = None
        else:
            cfg.recovery_timeout_min_loss_usdt = float(raw)
    if "recovery_max_loss_usdt" in payload:
        raw = payload["recovery_max_loss_usdt"]
        if raw is None or raw == "":
            cfg.recovery_max_loss_usdt = None
        else:
            cfg.recovery_max_loss_usdt = float(raw)
    if "recovery_max_additional_loss_usdt" in payload:
        raw = payload["recovery_max_additional_loss_usdt"]
        if raw is None or raw == "":
            cfg.recovery_max_additional_loss_usdt = None
        else:
            cfg.recovery_max_additional_loss_usdt = float(raw)
    if "name" in payload:
        cfg.name = str(payload["name"])
    return cfg


def config_from_json_string(payload: str) -> RecoveryBotConfig:
    return config_from_mapping(json.loads(payload))


def to_long_gap_reduction_config(cfg: RecoveryBotConfig, *, fee_rate: float | None) -> Any:
    from .long_gap_reduction import LongGapReductionConfig

    return LongGapReductionConfig(
        step_trigger_pct=float(cfg.step_trigger_pct),
        num_steps=int(cfg.recovery_gap_reduce_steps),
        fee_rate=fee_rate,
        gap_reduce_fraction_per_step=float(cfg.recovery_gap_reduce_fraction_per_step),
    )


def recovery_bot_config_dict(cfg: RecoveryBotConfig) -> dict[str, Any]:
    payload = asdict(cfg)
    payload["recovery_start_purpose"] = normalize_recovery_start_purpose(
        cfg.recovery_start_purpose
    )
    payload["recovery_timeout_action"] = normalize_recovery_timeout_action(
        cfg.recovery_timeout_action
    )
    return payload

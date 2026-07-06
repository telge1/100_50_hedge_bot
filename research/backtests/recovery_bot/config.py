from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


_TRIGGER_PATTERN = re.compile(r"^CYCLE_(\d+)_SHORT_REDUCE$", re.IGNORECASE)


@dataclass
class RecoveryBotConfig:
    """Configuration for the backtest-only recovery bot (Phase 1).

    All values are purely simulator/backtest settings; nothing in this module
    is used by the live strategy.
    """

    enabled: bool = False

    trigger_order: str = "CYCLE_3_SHORT_REDUCE"
    trigger_price_drop_pct: float = 0.0
    trigger_wait_candles: int = 0
    max_recovery_runs_per_trade: int = 1

    neutralize_step_price_drop_pct: float = 1.0
    neutralize_reduce_mode: str = "fixed_steps"
    neutralize_reduce_qty: float | None = None
    neutralize_reduce_pct: float | None = None
    neutralize_target_steps: int = 5
    neutralize_exact_final_step: bool = True

    pair_reduce_move_pct: float = 1.0
    pair_reduce_on_up_move: bool = True
    pair_reduce_on_down_move: bool = True
    pair_reduce_mode: str = "percent"
    pair_reduce_qty: float | None = None
    pair_reduce_pct: float | None = 10.0

    minimum_pair_qty: float = 0.0
    minimum_pair_notional_usdt: float = 0.0

    loss_budget_mode: str = "profit_share"
    available_profit_pool_usdt: float = 0.0
    loss_budget_profit_share_pct: float = 20.0
    fixed_loss_budget_usdt: float | None = None
    minimum_loss_budget_usdt: float = 0.0
    maximum_loss_budget_usdt: float | None = None

    include_fees: bool = True
    include_funding: bool = False
    slippage_buffer_pct: float = 0.0

    close_when_within_loss_budget: bool = True

    reload_enabled: bool = False
    reload_max_count: int = 0
    reload_min_exit_improvement_pct: float = 0.0
    reload_max_additional_capital_usdt: float = 0.0

    def __post_init__(self) -> None:
        # Ensure all instances are validated, regardless of construction path.
        validate_config(asdict(self))


def _require_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0 (got {value})")


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be > 0 (got {value})")


def _require_between(
    name: str,
    value: float,
    *,
    min_inclusive: float,
    max_inclusive: float,
) -> None:
    if value < min_inclusive or value > max_inclusive:
        raise ValueError(
            f"{name} must be between {min_inclusive} and {max_inclusive} "
            f"(got {value})"
        )


def validate_config(payload: Mapping[str, Any]) -> None:
    """Validate a RecoveryBotConfig payload.

    Raises ValueError with a human-readable message when invalid.
    """

    trigger_order = str(payload.get("trigger_order") or "").strip()
    match = _TRIGGER_PATTERN.match(trigger_order)
    if not match:
        raise ValueError(
            "trigger_order must match 'CYCLE_<N>_SHORT_REDUCE' "
            f"(got {trigger_order!r})"
        )
    cycle_index = int(match.group(1))
    if cycle_index <= 0:
        raise ValueError(
            f"trigger_order cycle index must be >= 1 (got {cycle_index})"
        )

    trigger_price_drop_pct = float(payload.get("trigger_price_drop_pct", 0.0) or 0.0)
    _require_non_negative("trigger_price_drop_pct", trigger_price_drop_pct)

    trigger_wait_candles = int(payload.get("trigger_wait_candles", 0) or 0)
    if trigger_wait_candles < 0:
        raise ValueError(
            f"trigger_wait_candles must be >= 0 (got {trigger_wait_candles})"
        )

    max_recovery_runs_per_trade = int(
        payload.get("max_recovery_runs_per_trade", 1) or 0
    )
    if max_recovery_runs_per_trade < 0:
        raise ValueError(
            "max_recovery_runs_per_trade must be >= 0 "
            f"(got {max_recovery_runs_per_trade})"
        )

    neutralize_step_price_drop_pct = float(
        payload.get("neutralize_step_price_drop_pct", 0.0) or 0.0
    )
    _require_non_negative(
        "neutralize_step_price_drop_pct",
        neutralize_step_price_drop_pct,
    )

    neutralize_target_steps = int(payload.get("neutralize_target_steps", 1) or 0)
    if neutralize_target_steps < 1:
        raise ValueError(
            f"neutralize_target_steps must be >= 1 (got {neutralize_target_steps})"
        )

    neutralize_reduce_mode = str(
        payload.get("neutralize_reduce_mode") or "fixed_steps"
    ).strip()
    allowed_neutralize_modes = {"fixed_steps", "fixed_qty", "percent"}
    if neutralize_reduce_mode not in allowed_neutralize_modes:
        raise ValueError(
            "neutralize_reduce_mode must be one of "
            f"{sorted(allowed_neutralize_modes)} "
            f"(got {neutralize_reduce_mode!r})"
        )

    neutralize_reduce_qty = payload.get("neutralize_reduce_qty")
    neutralize_reduce_pct = payload.get("neutralize_reduce_pct")

    if neutralize_reduce_mode == "fixed_qty":
        if neutralize_reduce_qty is None:
            raise ValueError(
                "neutralize_reduce_qty must be set for neutralize_reduce_mode "
                "'fixed_qty'"
            )
        _require_positive(
            "neutralize_reduce_qty",
            float(neutralize_reduce_qty),
        )
    elif neutralize_reduce_mode == "percent":
        if neutralize_reduce_pct is None:
            raise ValueError(
                "neutralize_reduce_pct must be set for neutralize_reduce_mode "
                "'percent'"
            )
        _require_between(
            "neutralize_reduce_pct",
            float(neutralize_reduce_pct),
            min_inclusive=0.0,
            max_inclusive=100.0,
        )

    pair_reduce_move_pct = float(payload.get("pair_reduce_move_pct", 0.0) or 0.0)
    _require_non_negative("pair_reduce_move_pct", pair_reduce_move_pct)

    pair_reduce_mode = str(payload.get("pair_reduce_mode") or "percent").strip()
    allowed_pair_modes = {"fixed_qty", "percent"}
    if pair_reduce_mode not in allowed_pair_modes:
        raise ValueError(
            "pair_reduce_mode must be one of "
            f"{sorted(allowed_pair_modes)} (got {pair_reduce_mode!r})"
        )

    pair_reduce_qty = payload.get("pair_reduce_qty")
    pair_reduce_pct = payload.get("pair_reduce_pct")

    if pair_reduce_mode == "fixed_qty":
        if pair_reduce_qty is None:
            raise ValueError(
                "pair_reduce_qty must be set for pair_reduce_mode 'fixed_qty'"
            )
        _require_positive("pair_reduce_qty", float(pair_reduce_qty))
    elif pair_reduce_mode == "percent":
        if pair_reduce_pct is None:
            raise ValueError(
                "pair_reduce_pct must be set for pair_reduce_mode 'percent'"
            )
        _require_between(
            "pair_reduce_pct",
            float(pair_reduce_pct),
            min_inclusive=0.0,
            max_inclusive=100.0,
        )

    minimum_pair_qty = float(payload.get("minimum_pair_qty", 0.0) or 0.0)
    _require_non_negative("minimum_pair_qty", minimum_pair_qty)

    minimum_pair_notional_usdt = float(
        payload.get("minimum_pair_notional_usdt", 0.0) or 0.0
    )
    _require_non_negative(
        "minimum_pair_notional_usdt",
        minimum_pair_notional_usdt,
    )

    loss_budget_mode = str(payload.get("loss_budget_mode") or "profit_share").strip()
    allowed_budget_modes = {"fixed", "profit_share", "hybrid"}
    if loss_budget_mode not in allowed_budget_modes:
        raise ValueError(
            "loss_budget_mode must be one of "
            f"{sorted(allowed_budget_modes)} (got {loss_budget_mode!r})"
        )

    available_profit_pool_usdt = float(
        payload.get("available_profit_pool_usdt", 0.0) or 0.0
    )
    _require_non_negative(
        "available_profit_pool_usdt",
        available_profit_pool_usdt,
    )

    loss_budget_profit_share_pct = float(
        payload.get("loss_budget_profit_share_pct", 0.0) or 0.0
    )
    _require_non_negative(
        "loss_budget_profit_share_pct",
        loss_budget_profit_share_pct,
    )

    fixed_loss_budget_usdt_raw = payload.get("fixed_loss_budget_usdt")
    if fixed_loss_budget_usdt_raw is not None:
        _require_non_negative(
            "fixed_loss_budget_usdt",
            float(fixed_loss_budget_usdt_raw),
        )

    minimum_loss_budget_usdt = float(
        payload.get("minimum_loss_budget_usdt", 0.0) or 0.0
    )
    _require_non_negative(
        "minimum_loss_budget_usdt",
        minimum_loss_budget_usdt,
    )

    maximum_loss_budget_usdt_raw = payload.get("maximum_loss_budget_usdt")
    if maximum_loss_budget_usdt_raw is not None:
        _require_non_negative(
            "maximum_loss_budget_usdt",
            float(maximum_loss_budget_usdt_raw),
        )

    if (
        maximum_loss_budget_usdt_raw is not None
        and float(maximum_loss_budget_usdt_raw) > 0.0
        and minimum_loss_budget_usdt > float(maximum_loss_budget_usdt_raw)
    ):
        raise ValueError(
            "minimum_loss_budget_usdt must be <= maximum_loss_budget_usdt "
            f"(got minimum={minimum_loss_budget_usdt}, "
            f"maximum={float(maximum_loss_budget_usdt_raw)})"
        )

    slippage_buffer_pct = float(payload.get("slippage_buffer_pct", 0.0) or 0.0)
    _require_non_negative("slippage_buffer_pct", slippage_buffer_pct)

    reload_max_count = int(payload.get("reload_max_count", 0) or 0)
    if reload_max_count < 0:
        raise ValueError(f"reload_max_count must be >= 0 (got {reload_max_count})")

    reload_min_exit_improvement_pct = float(
        payload.get("reload_min_exit_improvement_pct", 0.0) or 0.0
    )
    _require_non_negative(
        "reload_min_exit_improvement_pct",
        reload_min_exit_improvement_pct,
    )

    reload_max_additional_capital_usdt = float(
        payload.get("reload_max_additional_capital_usdt", 0.0) or 0.0
    )
    _require_non_negative(
        "reload_max_additional_capital_usdt",
        reload_max_additional_capital_usdt,
    )


def config_from_dict(payload: Mapping[str, Any]) -> RecoveryBotConfig:
    """Create a RecoveryBotConfig from a plain mapping, with validation."""
    validate_config(payload)
    return RecoveryBotConfig(
        enabled=bool(payload.get("enabled", False)),
        trigger_order=str(payload.get("trigger_order") or "CYCLE_3_SHORT_REDUCE"),
        trigger_price_drop_pct=float(payload.get("trigger_price_drop_pct", 0.0) or 0.0),
        trigger_wait_candles=int(payload.get("trigger_wait_candles", 0) or 0),
        max_recovery_runs_per_trade=int(
            payload.get("max_recovery_runs_per_trade", 1) or 0
        ),
        neutralize_step_price_drop_pct=float(
            payload.get("neutralize_step_price_drop_pct", 1.0) or 0.0
        ),
        neutralize_reduce_mode=str(
            payload.get("neutralize_reduce_mode") or "fixed_steps"
        ),
        neutralize_reduce_qty=(
            None
            if payload.get("neutralize_reduce_qty") is None
            else float(payload.get("neutralize_reduce_qty"))
        ),
        neutralize_reduce_pct=(
            None
            if payload.get("neutralize_reduce_pct") is None
            else float(payload.get("neutralize_reduce_pct"))
        ),
        neutralize_target_steps=int(
            payload.get("neutralize_target_steps", 5) or 0
        ),
        neutralize_exact_final_step=bool(
            payload.get("neutralize_exact_final_step", True)
        ),
        pair_reduce_move_pct=float(payload.get("pair_reduce_move_pct", 1.0) or 0.0),
        pair_reduce_on_up_move=bool(
            payload.get("pair_reduce_on_up_move", True)
        ),
        pair_reduce_on_down_move=bool(
            payload.get("pair_reduce_on_down_move", True)
        ),
        pair_reduce_mode=str(payload.get("pair_reduce_mode") or "percent"),
        pair_reduce_qty=(
            None
            if payload.get("pair_reduce_qty") is None
            else float(payload.get("pair_reduce_qty"))
        ),
        pair_reduce_pct=(
            None
            if payload.get("pair_reduce_pct") is None
            else float(payload.get("pair_reduce_pct"))
        ),
        minimum_pair_qty=float(payload.get("minimum_pair_qty", 0.0) or 0.0),
        minimum_pair_notional_usdt=float(
            payload.get("minimum_pair_notional_usdt", 0.0) or 0.0
        ),
        loss_budget_mode=str(payload.get("loss_budget_mode") or "fixed"),
        available_profit_pool_usdt=float(
            payload.get("available_profit_pool_usdt", 0.0) or 0.0
        ),
        loss_budget_profit_share_pct=float(
            payload.get("loss_budget_profit_share_pct", 20.0) or 0.0
        ),
        fixed_loss_budget_usdt=(
            None
            if payload.get("fixed_loss_budget_usdt") is None
            else float(payload.get("fixed_loss_budget_usdt"))
        ),
        minimum_loss_budget_usdt=float(
            payload.get("minimum_loss_budget_usdt", 0.0) or 0.0
        ),
        maximum_loss_budget_usdt=(
            None
            if payload.get("maximum_loss_budget_usdt") is None
            else float(payload.get("maximum_loss_budget_usdt"))
        ),
        include_fees=bool(payload.get("include_fees", True)),
        include_funding=bool(payload.get("include_funding", False)),
        slippage_buffer_pct=float(
            payload.get("slippage_buffer_pct", 0.0) or 0.0
        ),
        close_when_within_loss_budget=bool(
            payload.get("close_when_within_loss_budget", True)
        ),
        reload_enabled=bool(payload.get("reload_enabled", False)),
        reload_max_count=int(payload.get("reload_max_count", 0) or 0),
        reload_min_exit_improvement_pct=float(
            payload.get("reload_min_exit_improvement_pct", 0.0) or 0.0
        ),
        reload_max_additional_capital_usdt=float(
            payload.get("reload_max_additional_capital_usdt", 0.0) or 0.0
        ),
    )


def config_from_json_string(raw: str) -> RecoveryBotConfig:
    """Create a RecoveryBotConfig from a JSON string."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("recovery bot config JSON must be an object")
    return config_from_dict(data)


def config_to_dict(config: RecoveryBotConfig) -> dict[str, Any]:
    """Serialize a RecoveryBotConfig to a plain dict."""
    return asdict(config)


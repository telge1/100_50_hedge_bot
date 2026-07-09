from __future__ import annotations

"""Backtest-only config and helpers for Blocker Addon Short Recovery.

This module is intentionally live-bot agnostic. It defines:

- AddonShortRecoveryConfig: optimizer-ready config container
- AddonShortRecoveryEvent: per-addon-trade event payload for exports

Runtime wiring, simulator access and long-reduce fills are implemented in
addon_short_recovery_shim.py so the backtest harness can opt-in cleanly.
"""

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Mapping


@dataclass
class AddonShortRecoveryConfig:
    """Backtest-only configuration for Blocker Addon Short Recovery."""

    enabled: bool = False

    activation_order: str = "CYCLE_3_SHORT_REDUCE"

    cancel_open_cycle_orders: bool = True
    stop_new_cycle_orders: bool = True
    keep_existing_exit_orders: bool = True

    sizing_basis: str = "activation_net_long_gap"
    addon_short_step_fraction: float = 0.25
    allow_net_short: bool = False

    addon_short_first_entry_distance_pct: float = 0.0

    addon_short_tp_pct: float = 0.75

    addon_short_reentry_buffer_pct: float = 0.20
    addon_short_reentry_reference: str = "previous_low"

    addon_short_min_favorable_move_pct: float = 0.20
    addon_short_rebound_close_pct: float = 0.50

    addon_short_hard_stop_pct: float = 1.00

    long_reduce_profit_usage_fraction: float = 0.90

    stop_when_long_qty_reaches_normal_short_qty: bool = True

    name: str = "manual_default"


@dataclass
class AddonShortRecoveryEvent:
    """Per-addon-short trade event for detailed exports.

    One logical addon-short trade consists aus:
    - one ADDON_RECOVERY_SHORT_ENTRY event
    - one of ADDON_RECOVERY_SHORT_TP / _REBOUND_EXIT / _HARD_STOP
    - zero or more ADDON_RECOVERY_LONG_REDUCE events that are causally linked
    """

    event_type: str
    trade_index: int
    entry_timestamp: str | None = None
    close_timestamp: str | None = None
    entry_candle_index: int | None = None
    close_candle_index: int | None = None
    entry_price: float | None = None
    close_price: float | None = None
    entry_qty: float | None = None
    close_qty: float | None = None
    previous_low: float | None = None
    maximum_favorable_move_pct: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    long_reduce_qty: float | None = None
    long_reduce_pnl: float | None = None
    long_reduce_price: float | None = None
    activation_price: float | None = None
    activation_timestamp: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)


def default_addon_short_recovery_config() -> AddonShortRecoveryConfig:
    """Return a config suitable as baseline when CLI flag is used."""

    cfg = AddonShortRecoveryConfig()
    cfg.enabled = True
    return cfg


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def config_from_dict(payload: Mapping[str, Any]) -> AddonShortRecoveryConfig:
    """Build config from a plain dict (e.g. JSON payload)."""

    base = AddonShortRecoveryConfig()
    # Use getattr-style fallback to preserve defaults when keys are missing.
    return AddonShortRecoveryConfig(
        enabled=bool(payload.get("enabled", base.enabled)),
        activation_order=str(payload.get("activation_order", base.activation_order)),
        cancel_open_cycle_orders=bool(
            payload.get("cancel_open_cycle_orders", base.cancel_open_cycle_orders)
        ),
        stop_new_cycle_orders=bool(
            payload.get("stop_new_cycle_orders", base.stop_new_cycle_orders)
        ),
        keep_existing_exit_orders=bool(
            payload.get("keep_existing_exit_orders", base.keep_existing_exit_orders)
        ),
        sizing_basis=str(payload.get("sizing_basis", base.sizing_basis)),
        addon_short_step_fraction=float(
            payload.get("addon_short_step_fraction", base.addon_short_step_fraction)
        ),
        allow_net_short=bool(payload.get("allow_net_short", base.allow_net_short)),
        addon_short_first_entry_distance_pct=float(
            payload.get(
                "addon_short_first_entry_distance_pct",
                base.addon_short_first_entry_distance_pct,
            )
        ),
        addon_short_tp_pct=float(
            payload.get("addon_short_tp_pct", base.addon_short_tp_pct)
        ),
        addon_short_reentry_buffer_pct=float(
            payload.get(
                "addon_short_reentry_buffer_pct",
                base.addon_short_reentry_buffer_pct,
            )
        ),
        addon_short_reentry_reference=str(
            payload.get(
                "addon_short_reentry_reference",
                base.addon_short_reentry_reference,
            )
        ),
        addon_short_min_favorable_move_pct=float(
            payload.get(
                "addon_short_min_favorable_move_pct",
                base.addon_short_min_favorable_move_pct,
            )
        ),
        addon_short_rebound_close_pct=float(
            payload.get(
                "addon_short_rebound_close_pct",
                base.addon_short_rebound_close_pct,
            )
        ),
        addon_short_hard_stop_pct=float(
            payload.get("addon_short_hard_stop_pct", base.addon_short_hard_stop_pct)
        ),
        long_reduce_profit_usage_fraction=float(
            payload.get(
                "long_reduce_profit_usage_fraction",
                base.long_reduce_profit_usage_fraction,
            )
        ),
        stop_when_long_qty_reaches_normal_short_qty=bool(
            payload.get(
                "stop_when_long_qty_reaches_normal_short_qty",
                base.stop_when_long_qty_reaches_normal_short_qty,
            )
        ),
        name=str(payload.get("name", base.name or "manual_default")),
    )


def config_to_dict(config: AddonShortRecoveryConfig) -> dict[str, Any]:
    return asdict(config)


def config_from_json_string(raw: str) -> AddonShortRecoveryConfig:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("addon short recovery config JSON must be an object")
    return config_from_dict(payload)


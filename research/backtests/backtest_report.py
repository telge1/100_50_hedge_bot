"""Backtest result structures and logging helpers (Phase 4/6)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot.models import FillEvent

from .intent_diagnostics import last_intent_summary, summarize_exit_diagnostics
from .backtest_config_loader import HIGHLIGHT_BOT_CONFIG_KEYS
from .config_diagnostics import config_diagnostics_summary_fields
from .intent_diagnostics import _metadata_excerpt
from .purpose_utils import purpose_log_fields
from .simulated_order_book import SimulatedOrderBook, SyntheticCandle, VirtualOrder

SUMMARY_CSV_FIELDS = (
    "symbol",
    "direction",
    "start_time",
    "end_time",
    "candles_processed",
    "entry_price",
    "final_status",
    "exit_reason",
    "open_reason_detail",
    "realized_pnl",
    "realized_pnl_pct",
    "max_drawdown_pct",
    "fills_count",
    "orders_submitted",
    "active_orders_count",
    "cycles_seen",
    "final_long_qty",
    "final_short_qty",
    "final_active_order_purposes",
    "fill_model",
    "max_fills_per_candle",
    "same_candle_fills_count",
    "paired_exit_fills_count",
    "final_active_order_diagnostics_summary",
    "last_intent_purpose",
    "last_intent_trigger_price",
    "last_intent_price",
    "last_intent_source_fill_purpose",
    "config_source",
    "initial_exit_trigger",
    "initial_exit_trigger_distance_abs",
    "initial_exit_trigger_distance_pct",
    "nearest_config_candidate",
    "nearest_config_candidate_source",
    "config_path",
    "config_loaded",
    "config_load_warning",
    "price_tick_size",
    "tp_profit_target_pct",
    "long_fill_distance_pct",
    "short_fill_distance_pct",
    "base_notional_usdt",
    "hedge_ratio_short",
    "recovery_activation_timing",
    "recovery_mode_trigger_override_enabled",
    "recovery_mode_trigger_override_pct",
    "time_distance_refill_trigger_minutes",
    # Addon Short Recovery (backtest-only)
    "addon_short_recovery_enabled",
    "addon_short_recovery_activation_order",
    "addon_short_recovery_activated",
    "addon_short_recovery_activation_candle_index",
    "addon_short_recovery_activation_price",
    "addon_short_recovery_long_qty_at_activation",
    "addon_short_recovery_normal_short_qty_at_activation",
    "addon_short_recovery_gap_at_activation",
    "addon_short_recovery_completed",
    "addon_short_recovery_completion_reason",
    "addon_short_recovery_completed_candle_index",
    "addon_short_realized_profit",
    "addon_short_realized_loss",
    "addon_short_net_realized_pnl",
    "addon_short_trade_count",
    "addon_short_tp_count",
    "addon_short_rebound_exit_count",
    "addon_short_hard_stop_count",
    "addon_short_long_reduce_total_qty",
    "addon_short_long_reduce_total_pnl",
)


def resolve_net_closed_pnl(record: dict[str, Any]) -> float | None:
    """Prefer confirmed_closed_pnl; fall back to closed_pnl only when confirmed is None."""
    confirmed_closed_pnl = record.get("confirmed_closed_pnl")
    if confirmed_closed_pnl is not None:
        try:
            return float(confirmed_closed_pnl)
        except (TypeError, ValueError):
            return None
    closed_pnl = record.get("closed_pnl")
    if closed_pnl is not None:
        try:
            return float(closed_pnl)
        except (TypeError, ValueError):
            return None
    return None


def build_fill_log_entry(
    fill: FillEvent,
    book: SimulatedOrderBook,
    *,
    timestamp: datetime | None = None,
    candle_index: int | None = None,
    candle: SyntheticCandle | None = None,
    order_check_price: float | None = None,
) -> dict[str, Any]:
    ts = timestamp or fill.occurred_at
    metadata = dict(fill.metadata or {})
    purpose_fields = purpose_log_fields(fill.purpose or metadata.get("purpose"), metadata)
    closed_pnl = float(metadata.get("closed_pnl") or metadata.get("confirmed_closed_pnl") or 0.0)
    runtime_pnl = metadata.get("runtime_calculated_pnl")
    confirmed_pnl = metadata.get("confirmed_closed_pnl")
    entry: dict[str, Any] = {
        "candle_index": candle_index,
        "timestamp": ts.isoformat() if ts is not None else None,
        "symbol": metadata.get("symbol") or book.symbol,
        "side": fill.side,
        "reduce_only": bool(fill.reduce_only),
        "qty": float(fill.exec_qty),
        "order_check_price": order_check_price or metadata.get("order_check_price"),
        "fill_price": float(fill.exec_price),
        "order_id": fill.client_order_id,
        "closed_pnl": closed_pnl,
        "runtime_calculated_pnl": float(runtime_pnl) if runtime_pnl is not None else closed_pnl,
        "confirmed_closed_pnl": float(confirmed_pnl) if confirmed_pnl is not None else closed_pnl,
        "long_qty_after": float(book.long_qty),
        "short_qty_after": float(book.short_qty),
        "long_avg_after": float(book.long_avg),
        "short_avg_after": float(book.short_avg),
        "active_orders_after_count": len(book.active_orders()),
        **purpose_fields,
    }
    metadata_excerpt = _metadata_excerpt(metadata)
    if metadata_excerpt:
        entry["metadata_excerpt"] = metadata_excerpt
    for key in ("trigger_touched", "trigger_touch_rule", "trigger_warning"):
        if key in metadata:
            entry[key] = metadata.get(key)
    if candle is not None:
        entry["candle_open"] = float(candle.open if candle.open is not None else candle.close)
        entry["candle_high"] = float(candle.high if candle.high is not None else candle.close)
        entry["candle_low"] = float(candle.low if candle.low is not None else candle.close)
        entry["candle_close"] = float(candle.close)
    if metadata.get("gross_pnl") is not None:
        entry["gross_realized_pnl_event"] = float(metadata.get("gross_pnl"))
    if metadata.get("entry_fee") is not None:
        entry["entry_fee"] = float(metadata.get("entry_fee"))
    if metadata.get("exit_fee") is not None:
        entry["exit_fee"] = float(metadata.get("exit_fee"))
    fee_rate = metadata.get("runtime_fee_rate") or metadata.get("fee_rate") or book.fee_rate
    if fee_rate is not None:
        entry["fee_rate"] = float(fee_rate)
    return entry


def build_order_log_entry(
    order: VirtualOrder,
    *,
    timestamp: datetime | None = None,
    candle_index: int | None = None,
    event_type: str = "submitted",
    status: str | None = None,
    replaced_old_order_id: str | None = None,
    new_order_id: str | None = None,
    intent_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = timestamp or order.created_at
    purpose_fields = purpose_log_fields(order.purpose, order.metadata)
    entry: dict[str, Any] = {
        "timestamp": ts.isoformat() if ts is not None else None,
        "candle_index": candle_index,
        "event_type": event_type,
        "order_id": order.order_id,
        "side": order.side,
        "qty": float(order.qty),
        "price": order.price,
        "trigger_price": order.trigger_price,
        "trigger_direction": order.trigger_direction,
        "reduce_only": bool(order.reduce_only),
        "status": status or order.status,
        **purpose_fields,
    }
    metadata_excerpt = _metadata_excerpt(dict(order.metadata or {}))
    if metadata_excerpt:
        entry["metadata_excerpt"] = metadata_excerpt
    if replaced_old_order_id:
        entry["replaced_old_order_id"] = replaced_old_order_id
    if new_order_id:
        entry["new_order_id"] = new_order_id
    if intent_mapping:
        entry.update(intent_mapping)
    return entry


@dataclass
class BacktestResult:
    symbol: str
    direction: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    candles_processed: int = 0
    entry_price: float | None = None
    final_status: str = "open"
    realized_pnl: float = 0.0
    realized_pnl_pct: float | None = None
    max_drawdown_pct: float | None = None
    fills_count: int = 0
    orders_submitted: int = 0
    active_orders_count: int = 0
    cycles_seen: int | None = None
    exit_reason: str = ""
    fill_log: list[dict[str, Any]] = field(default_factory=list)
    order_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    final_long_qty: float | None = None
    final_short_qty: float | None = None
    final_long_avg_price: float | None = None
    final_short_avg_price: float | None = None
    final_price: float | None = None
    unrealized_long_pnl: float | None = None
    unrealized_short_pnl: float | None = None
    unrealized_pnl: float | None = None
    overall_pnl: float | None = None
    final_active_orders: list[dict[str, Any]] = field(default_factory=list)
    final_active_order_purposes: list[str] = field(default_factory=list)
    final_strategy_state_excerpt: dict[str, Any] = field(default_factory=dict)
    last_fill: dict[str, Any] | None = None
    last_order: dict[str, Any] | None = None
    first_fill_time: str | None = None
    last_fill_time: str | None = None
    open_reason_detail: str = ""
    fill_model: str = "conservative"
    max_fills_per_candle: int = 1
    same_candle_fills_count: int = 0
    paired_exit_fills_count: int = 0
    intent_log: list[dict[str, Any]] = field(default_factory=list)
    final_active_order_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    config_diagnostics: dict[str, Any] = field(default_factory=dict)
    live_config_comparison: dict[str, Any] = field(default_factory=dict)
    exit_level_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    config_source: str = ""
    config_path: str | None = None
    config_loaded: bool = False
    config_load_warning: str | None = None
    config_unknown_keys: list[str] = field(default_factory=list)
    config_overlay_missing_keys: list[str] = field(default_factory=list)
    loaded_bot_config: dict[str, Any] = field(default_factory=dict)
    price_tick_size: float | None = None
    tp_profit_target_pct: float | None = None
    long_fill_distance_pct: float | None = None
    short_fill_distance_pct: float | None = None
    base_notional_usdt: float | None = None
    hedge_ratio_short: float | None = None
    recovery_activation_timing: str | None = None
    recovery_mode_trigger_override_enabled: bool | None = None
    recovery_mode_trigger_override_pct: float | None = None
    time_distance_refill_trigger_minutes: int | None = None
    initial_exit_trigger: float | None = None
    initial_exit_trigger_distance_abs: float | None = None
    initial_exit_trigger_distance_pct: float | None = None
    nearest_config_candidate: float | None = None
    nearest_config_candidate_source: str = ""
    start_index: int | None = None
    end_index: int | None = None
    input_slice_start_index: int | None = None
    window_candles: int | None = None
    trade_number: int | None = None
    trade_block_id: str | None = None
    exit_quality: str = ""
    # Addon Short Recovery (backtest-only aggregates and flags)
    addon_short_recovery_enabled: bool | None = None
    addon_short_recovery_activation_order: str | None = None
    addon_short_recovery_activated: bool | None = None
    addon_short_recovery_activation_candle_index: int | None = None
    addon_short_recovery_activation_price: float | None = None
    addon_short_recovery_long_qty_at_activation: float | None = None
    addon_short_recovery_normal_short_qty_at_activation: float | None = None
    addon_short_recovery_gap_at_activation: float | None = None
    addon_short_recovery_completed: bool | None = None
    addon_short_recovery_completion_reason: str | None = None
    addon_short_recovery_completed_candle_index: int | None = None
    addon_short_realized_profit: float | None = None
    addon_short_realized_loss: float | None = None
    addon_short_net_realized_pnl: float | None = None
    addon_short_trade_count: int | None = None
    addon_short_tp_count: int | None = None
    addon_short_rebound_exit_count: int | None = None
    addon_short_hard_stop_count: int | None = None
    addon_short_long_reduce_total_qty: float | None = None
    addon_short_long_reduce_total_pnl: float | None = None
    addon_short_events: list[dict[str, Any]] = field(default_factory=list)
    # Optional snapshot for the state immediately after CYCLE_3_SHORT_REDUCE
    # (used by long-gap-reduction offline audits and continuous exports).
    cycle3_snapshot: dict[str, Any] | None = None
    # Integrated long-gap recovery bot (backtest-only)
    recovery_bot_enabled: bool | None = None
    recovery_activated: bool | None = None
    recovery_reference_purpose: str | None = None
    recovery_reference_absolute_candle_index: int | None = None
    recovery_reference_timestamp: str | None = None
    recovery_activation_absolute_candle_index: int | None = None
    recovery_activation_timestamp: str | None = None
    recovery_exit_absolute_candle_index: int | None = None
    recovery_exit_timestamp: str | None = None
    recovery_wait_candles: int | None = None
    recovery_initial_gap_qty: float | None = None
    recovery_total_reduced_qty: float | None = None
    recovery_remaining_gap_qty: float | None = None
    recovery_gap_fully_closed: bool | None = None
    recovery_total_gap_reduction_net_pnl: float | None = None
    recovery_final_pnl: float | None = None
    recovery_duration_candles: int | None = None
    recovery_duration_minutes: int | None = None
    recovery_diagnostic_events: list[dict[str, Any]] = field(default_factory=list)
    recovery_gap_reduction_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.start_time is not None:
            payload["start_time"] = self.start_time.isoformat()
        if self.end_time is not None:
            payload["end_time"] = self.end_time.isoformat()
        return payload


def result_to_summary_row(result: BacktestResult) -> dict[str, Any]:
    payload = result.to_dict()
    intent_summary = last_intent_summary(result.intent_log or [])
    payload.update(intent_summary)
    payload["final_active_order_diagnostics_summary"] = summarize_exit_diagnostics(
        result.final_active_order_diagnostics or []
    )
    if result.config_diagnostics:
        payload.update(config_diagnostics_summary_fields(result.config_diagnostics))
    if result.loaded_bot_config:
        payload.update(result.loaded_bot_config)
    row: dict[str, Any] = {}
    for key in SUMMARY_CSV_FIELDS:
        value = payload.get(key)
        if key == "final_active_order_purposes":
            purposes = value or []
            row[key] = "|".join(str(purpose) for purpose in purposes if purpose)
            continue
        if value is None:
            row[key] = ""
        else:
            row[key] = value
    return row


def write_summary_csv(path: str | Path, results: Iterable[BacktestResult]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    rows = [result_to_summary_row(result) for result in results]
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(SUMMARY_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_results_json(
    path: str | Path,
    *,
    symbol: str,
    limit: int | None,
    max_candles: int | None,
    results: dict[str, BacktestResult],
    meta: dict[str, Any] | None = None,
) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "symbol": symbol.upper(),
        "limit": limit,
        "max_candles": max_candles,
        "runs": {direction: result.to_dict() for direction, result in results.items()},
    }
    if meta:
        payload["meta"] = meta
    with path_obj.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path_obj


def default_output_paths(output_dir: str | Path, symbol: str) -> tuple[Path, Path]:
    base = Path(output_dir)
    symbol_upper = symbol.upper()
    json_path = base / f"{symbol_upper}_original_hedge_5m_results.json"
    csv_path = base / f"{symbol_upper}_original_hedge_5m_summary.csv"
    return json_path, csv_path


def comparison_output_paths(output_dir: str | Path, symbol: str) -> tuple[Path, Path]:
    base = Path(output_dir)
    symbol_upper = symbol.upper()
    json_path = base / f"{symbol_upper}_original_hedge_5m_fill_model_comparison_results.json"
    csv_path = base / f"{symbol_upper}_original_hedge_5m_fill_model_comparison_summary.csv"
    return json_path, csv_path

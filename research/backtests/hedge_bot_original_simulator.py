"""Minimal Phase-1/2/3 harness: run original hedge strategies without Bybit."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
    configure_confirmed_order_pnl_history_file,
    configure_cycle_state_file,
    set_default_bot_name,
)
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, RuntimeState, StrategyIntent, snapshot_from_mapping

from .backtest_report import build_order_log_entry
from .backtest_config_loader import BacktestConfigLoadResult, extract_highlight_bot_config
from .cycle_fill_reference_repair import install_cycle_fill_reference_repair
from .cycle_short_tp_relief import CycleShortTpReliefConfig
from .cycle_short_tp_relief_shim import install_cycle_short_tp_relief
from .dynamic_cycle_order_scaling import DynamicCycleOrderScalingConfig
from .dynamic_cycle_order_scaling_shim import install_dynamic_cycle_order_scaling
from .exit_pnl_audit_shim import install_exit_pnl_audit_shim
from .stuck_recovery_reload import StuckRecoveryReloadConfig
from .stuck_recovery_reload_shim import attach_stuck_recovery_reload_tracker
from .fill_models import FillModelConfig, resolve_fill_model_config
from .intent_diagnostics import build_intent_log_entry, build_intent_to_order_mapping
from .purpose_utils import preserve_bot_purpose
from .simulated_execution import (
    fill_entry_intents_at_candle_close,
    fill_order_at_candle_close,
    is_immediate_market_fill,
    is_immediate_refill_market_fill,
    process_candle_fills,
)
from .simulated_order_book import SimulatedOrderBook, SyntheticCandle, VirtualOrder

Signal = Literal["long", "short"]

KNOWN_STRUCTURE_PURPOSE_PREFIXES = ("CYCLE_",)
KNOWN_STRUCTURE_PURPOSE_SUFFIXES = ("_EXIT",)


@dataclass
class SimulationResult:
    signal: Signal
    strategy_name: str
    entry_intents: list[StrategyIntent] = field(default_factory=list)
    entry_fills: list[FillEvent] = field(default_factory=list)
    post_fill_intents: list[StrategyIntent] = field(default_factory=list)
    resting_orders: list[VirtualOrder] = field(default_factory=list)
    final_snapshot: HedgeSnapshot | None = None
    runtime_state: RuntimeState | None = None
    strategy_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessCandleResult:
    candle: SyntheticCandle
    candle_fills: list[FillEvent] = field(default_factory=list)
    on_fill_intents: list[StrategyIntent] = field(default_factory=list)
    tick_intents: list[StrategyIntent] = field(default_factory=list)
    snapshot: HedgeSnapshot | None = None
    strategy_state: dict[str, Any] = field(default_factory=dict)
    same_candle_fill_count: int = 0
    paired_exit_fills_count: int = 0


def _default_instrument_rules(symbol: str, *, price_tick_size: float | None = None) -> dict[str, Decimal]:
    tick = Decimal(str(price_tick_size if price_tick_size is not None else 0.1))
    return {
        "min_order_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.001"),
        "tick_size": tick,
    }


def build_test_config(*, signal: Signal, symbol: str = "BTCUSDT") -> FixedCycleHedgeConfig:
    bot_name = "long_bot_1" if signal == "long" else "short_bot_1"
    return FixedCycleHedgeConfig(
        bot_name=bot_name,
        strategy_side=signal,
        symbol=symbol,
        category="linear",
        restart=False,
        dynamic_symbol_enabled=False,
        rest_poll_after_fill_ms=0,
        base_notional_usdt=100.0,
        hedge_ratio_short=0.5,
        initial_entry_order_type="Market",
        qty_step=0.001,
        min_order_qty=0.001,
        min_notional_usdt=5.0,
        price_tick_size=0.1,
        order_refresh_cooldown_ms=0,
    )


def build_strategy(signal: Signal, config: FixedCycleHedgeConfig | None = None):
    cfg = config or build_test_config(signal=signal)
    if signal == "long":
        return FixedCycleHedgeStrategy(cfg)
    return ShortFixedCycleHedgeStrategy(cfg)


def build_runtime_state(*, symbol: str, price_tick_size: float | None = None) -> RuntimeState:
    runtime_state = RuntimeState(strategy_state={})
    runtime_state.instrument_rules[symbol.upper()] = _default_instrument_rules(
        symbol,
        price_tick_size=price_tick_size,
    )
    return runtime_state


def build_flat_snapshot(
    *,
    symbol: str,
    price: float,
    runtime_state: RuntimeState,
) -> HedgeSnapshot:
    return snapshot_from_mapping(
        symbol=symbol,
        current_price=price,
        positions={"long_qty": 0.0, "short_qty": 0.0, "long_avg": 0.0, "short_avg": 0.0},
        runtime_state=runtime_state,
        source="backtest_smoke_flat",
    )


def build_context(
    *,
    symbol: str,
    runtime_state: RuntimeState,
) -> StrategyContext:
    def _refresh_snapshot(_reason: str) -> HedgeSnapshot:
        if runtime_state.last_snapshot is not None:
            return runtime_state.last_snapshot
        return snapshot_from_mapping(
            symbol=symbol,
            current_price=0.0,
            positions={"long_qty": 0.0, "short_qty": 0.0, "long_avg": 0.0, "short_avg": 0.0},
            runtime_state=runtime_state,
            source="backtest_smoke_refresh_fallback",
        )

    stub_order_manager = mock.Mock()
    stub_order_manager.fetch_closed_pnl.return_value = []
    stub_order_manager.fetch_wallet_balance.return_value = (None, "unavailable")
    stub_order_manager.fetch_open_orders.return_value = []

    return StrategyContext(
        audit=AuditLogger(logging.getLogger("research.backtests.smoke")),
        runtime_name="backtest_smoke",
        symbol=symbol,
        category="linear",
        min_order_value=5.0,
        order_manager=stub_order_manager,
        refresh_snapshot=_refresh_snapshot,
        cancel_open_orders_by_purpose=None,
    )


class HedgeBotOriginalSimulator:
    """Run original hedge strategies against synthetic candles without Bybit."""

    def __init__(
        self,
        *,
        signal: Signal,
        symbol: str = "BTCUSDT",
        candle_close: float = 100.0,
        config: FixedCycleHedgeConfig | None = None,
        config_load: BacktestConfigLoadResult | None = None,
        temp_dir: Path | None = None,
        dynamic_cycle_scaling_config: DynamicCycleOrderScalingConfig | None = None,
        stuck_recovery_reload_config: StuckRecoveryReloadConfig | None = None,
        cycle_short_tp_relief_config: CycleShortTpReliefConfig | None = None,
    ) -> None:
        self.signal = signal
        self.symbol = symbol.upper()
        self.candle = SyntheticCandle(symbol=self.symbol, close=float(candle_close))
        self._temp_dir = temp_dir
        self._owned_temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if config_load is not None:
            self.config = config_load.config
            self.config_source = config_load.config_source
            self.config_path = config_load.config_path
            self.config_loaded = config_load.config_loaded
            self.config_load_warning = config_load.config_load_warning
            self.config_unknown_keys = list(config_load.config_unknown_keys)
            self.config_overlay_missing_keys = list(config_load.config_overlay_missing_keys)
        elif config is not None:
            self.config = config
            self.config_source = "custom"
            self.config_path = None
            self.config_loaded = False
            self.config_load_warning = None
            self.config_unknown_keys = []
            self.config_overlay_missing_keys = []
        else:
            self.config = build_test_config(signal=signal, symbol=self.symbol)
            self.config_source = "test"
            self.config_path = None
            self.config_loaded = False
            self.config_load_warning = None
            self.config_unknown_keys = []
            self.config_overlay_missing_keys = []
        self.loaded_bot_config = extract_highlight_bot_config(self.config)
        self.strategy = build_strategy(signal, self.config)
        install_cycle_fill_reference_repair(self.strategy)
        install_exit_pnl_audit_shim(self.strategy)
        install_dynamic_cycle_order_scaling(self.strategy, dynamic_cycle_scaling_config)
        install_cycle_short_tp_relief(self.strategy, cycle_short_tp_relief_config)
        self.dynamic_cycle_scaling_config = dynamic_cycle_scaling_config
        self.cycle_short_tp_relief_config = cycle_short_tp_relief_config
        self.stuck_recovery_reload_tracker = attach_stuck_recovery_reload_tracker(
            self,
            stuck_recovery_reload_config,
        )
        self.stuck_recovery_reload_config = stuck_recovery_reload_config
        self.runtime_state = build_runtime_state(
            symbol=self.symbol,
            price_tick_size=float(self.config.price_tick_size),
        )
        self.book = SimulatedOrderBook(symbol=self.symbol)
        self.snapshot = build_flat_snapshot(
            symbol=self.symbol,
            price=self.candle.close,
            runtime_state=self.runtime_state,
        )
        self.runtime_state.last_snapshot = self.snapshot
        self.context = build_context(
            symbol=self.symbol,
            runtime_state=self.runtime_state,
        )
        self.orders_submitted = 0
        self.order_log: list[dict[str, Any]] = []
        self.intent_log: list[dict[str, Any]] = []
        self.entry_price: float | None = float(candle_close)
        self.candle_index = 0
        self._wire_order_book_callbacks()
        self._configure_isolated_paths()

    def _wire_order_book_callbacks(self) -> None:
        def _cancel_open_orders_by_purpose(purposes: list[str]) -> None:
            for purpose in purposes:
                canceled_ids = self.book.cancel_by_purpose(purpose)
                for order_id in canceled_ids:
                    order = self.book.get_order(order_id)
                    if order is None:
                        continue
                    self.order_log.append(
                        build_order_log_entry(
                            order,
                            timestamp=self.candle.timestamp,
                            candle_index=self.candle_index,
                            event_type="cancelled",
                            status="CANCELED",
                        )
                    )
            self.book.sync_runtime_state(self.runtime_state)
            self._refresh_snapshot_from_book(source="after_cancel_by_purpose")

        self.context.cancel_open_orders_by_purpose = _cancel_open_orders_by_purpose

    def _record_order_event(
        self,
        order: VirtualOrder,
        *,
        event_type: str,
        status: str | None = None,
        replaced_old_order_id: str | None = None,
        intent_mapping: dict[str, Any] | None = None,
    ) -> None:
        self.order_log.append(
            build_order_log_entry(
                order,
                timestamp=self.candle.timestamp,
                candle_index=self.candle_index,
                event_type=event_type,
                status=status,
                replaced_old_order_id=replaced_old_order_id,
                new_order_id=order.order_id if event_type == "replaced" else None,
                intent_mapping=intent_mapping,
            )
        )

    def _log_intent(
        self,
        intent: StrategyIntent,
        *,
        event_source: str,
        source_fill_purpose: str | None = None,
    ) -> int:
        entry = build_intent_log_entry(
            intent,
            timestamp=self.candle.timestamp,
            candle_index=self.candle_index,
            event_source=event_source,
            source_fill_purpose=source_fill_purpose,
            entry_price=self.entry_price,
            config=self.config,
            config_source=self.config_source,
            strategy_state=dict(self.runtime_state.strategy_state),
        )
        self.intent_log.append(entry)
        return len(self.intent_log) - 1

    def _log_intents(
        self,
        intents: list[StrategyIntent],
        *,
        event_source: str,
        source_fill_purpose: str | None = None,
    ) -> list[int]:
        return [
            self._log_intent(
                intent,
                event_source=event_source,
                source_fill_purpose=source_fill_purpose,
            )
            for intent in intents
        ]

    def _submit_intent_with_logging(
        self,
        intent: StrategyIntent,
        *,
        replace: bool,
        intent_log_index: int | None = None,
    ) -> VirtualOrder | None:
        if str(intent.purpose or "").strip().upper() in {
            "INITIAL_LONG_ENTRY",
            "INITIAL_SHORT_ENTRY",
        }:
            return None
        order, replaced_ids = self.book.submit_intent(intent, replace=replace)
        order.created_candle_index = self.candle_index
        intent_mapping = build_intent_to_order_mapping(
            intent,
            order,
            intent_log_index=intent_log_index,
        )
        for old_id in replaced_ids:
            old_order = self.book.get_order(old_id)
            if old_order is not None:
                self._record_order_event(
                    old_order,
                    event_type="cancelled",
                    status="CANCELED",
                )
                self._record_order_event(
                    order,
                    event_type="replaced",
                    status=order.status,
                    replaced_old_order_id=old_id,
                    intent_mapping=intent_mapping,
                )
            else:
                self._record_order_event(
                    order,
                    event_type="submitted",
                    intent_mapping=intent_mapping,
                )
        if not replaced_ids:
            self._record_order_event(
                order,
                event_type="submitted",
                intent_mapping=intent_mapping,
            )
        return order

    def _configure_isolated_paths(self) -> None:
        if self._temp_dir is None:
            self._owned_temp_dir = tempfile.TemporaryDirectory(prefix="backtest_smoke_")
            self._temp_dir = Path(self._owned_temp_dir.name)
        logs_dir = self._temp_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        configure_confirmed_order_pnl_history_file(logs_dir / "confirmed_order_pnl_history.jsonl")
        configure_cycle_state_file(logs_dir / "cycle_state.json")
        set_default_bot_name(self.config.bot_name or ("long_bot_1" if self.signal == "long" else "short_bot_1"))

    def close(self) -> None:
        configure_confirmed_order_pnl_history_file(None)
        configure_cycle_state_file(None)
        if self._owned_temp_dir is not None:
            self._owned_temp_dir.cleanup()
            self._owned_temp_dir = None

    def _refresh_snapshot_from_book(self, *, source: str, price: float | None = None) -> HedgeSnapshot:
        self.book.sync_runtime_state(self.runtime_state)
        current_price = float(price if price is not None else self.candle.close)
        self.snapshot = snapshot_from_mapping(
            symbol=self.symbol,
            current_price=current_price,
            positions=self.book.positions_mapping(),
            runtime_state=self.runtime_state,
            source=source,
        )
        self.runtime_state.last_snapshot = self.snapshot
        return self.snapshot

    def _cancel_active_orders_when_flat(self, *, source: str) -> list[str]:
        """Cancel simulated active orders that are invalid after a leg is flat.

        If both legs are flat, cancel every remaining active order.

        If only one leg is flat, cancel only orders that would reduce that flat
        leg. Otherwise stale cycle orders can fill later with zero quantity/PnL,
        e.g. LONG_TP_EXIT closes the long side and an old CYCLE_*_LONG_ADD
        still fills after long_qty is already zero.
        """
        long_flat = float(self.book.long_qty or 0.0) <= 1e-12
        short_flat = float(self.book.short_qty or 0.0) <= 1e-12

        if not long_flat and not short_flat:
            return []

        def should_cancel(order) -> bool:
            purpose = str(getattr(order, "purpose", "") or "").upper()

            if long_flat and short_flat:
                return True

            if long_flat:
                if purpose == "LONG_TP_EXIT":
                    return True
                if purpose.startswith("CYCLE_") and purpose.endswith("_LONG_ADD"):
                    return True
                if purpose in {"REFILL_LONG", "RECOVERY_REFILL_LONG"}:
                    return True

            if short_flat:
                if purpose == "SHORT_SL_EXIT":
                    return True
                if purpose.startswith("CYCLE_") and purpose.endswith("_SHORT_REDUCE"):
                    return True
                if purpose in {"REFILL_SHORT", "RECOVERY_REFILL_SHORT"}:
                    return True

            return False

        cancelled: list[str] = []
        for order in list(self.book.active_orders()):
            if not should_cancel(order):
                continue
            if not self.book.cancel_by_order_id(order.order_id):
                continue
            cancelled.append(order.order_id)
            self._record_order_event(
                order,
                event_type="cancelled",
                status="CANCELED",
            )

        if cancelled:
            self.book.sync_runtime_state(self.runtime_state)
            self._refresh_snapshot_from_book(source=source)
        return cancelled


    def _mark_refill_registry_submitted(self, intent: StrategyIntent, order: VirtualOrder) -> None:
        purpose = preserve_bot_purpose(intent.purpose)
        if purpose not in {"REFILL_LONG", "REFILL_SHORT"}:
            return
        update_registry = getattr(self.strategy, "_update_refill_registry_status", None)
        if not callable(update_registry):
            return
        update_registry(
            self.runtime_state.strategy_state,
            purpose,
            "SUBMITTED",
            client_order_id=order.order_id,
            exchange_order_id=order.exchange_order_id,
        )

    def _dispatch_fill_to_strategy(
        self,
        fill_event: FillEvent,
        *,
        event_source: str,
        source_fill_purpose: str | None = None,
    ) -> list[StrategyIntent]:
        self._refresh_snapshot_from_book(
            source="after_immediate_market_fill",
            price=self.candle.close,
        )
        follow_up = self.strategy.on_fill(
            fill_event,
            self.snapshot,
            self.runtime_state,
            self.context,
        ) or []
        if follow_up:
            self.submit_intents_to_book(
                follow_up,
                event_source=event_source,
                source_fill_purpose=source_fill_purpose or fill_event.purpose,
            )
        self._cancel_active_orders_when_flat(source="after_immediate_market_fill")
        return list(follow_up)

    def _fill_immediate_refill_market_intent(
        self,
        intent: StrategyIntent,
        order: VirtualOrder,
        *,
        event_source: str,
        source_fill_purpose: str | None = None,
    ) -> FillEvent:
        self._mark_refill_registry_submitted(intent, order)
        fill_event = fill_order_at_candle_close(
            book=self.book,
            runtime_state=self.runtime_state,
            order_id=order.order_id,
            candle=self.candle,
        )
        filled_order = self.book.get_order(fill_event.client_order_id)
        if filled_order is not None:
            self._record_order_event(
                filled_order,
                event_type="filled",
                status="FILLED",
            )
        self._dispatch_fill_to_strategy(
            fill_event,
            event_source=event_source,
            source_fill_purpose=source_fill_purpose or preserve_bot_purpose(intent.purpose),
        )
        return fill_event

    def _resolve_intent_log_index(self, intent: StrategyIntent) -> int | None:
        purpose = preserve_bot_purpose(intent.purpose)
        for idx in range(len(self.intent_log) - 1, -1, -1):
            entry = self.intent_log[idx]
            if entry.get("purpose") != purpose:
                continue
            if float(entry.get("qty") or 0.0) != float(intent.qty):
                continue
            entry_trigger = entry.get("trigger_price")
            intent_trigger = float(intent.trigger_price) if intent.trigger_price is not None else None
            if entry_trigger != intent_trigger:
                continue
            return idx
        return None

    def submit_intents_to_book(
        self,
        intents: list[StrategyIntent],
        *,
        replace: bool = True,
        log_orders: bool = True,
        event_source: str = "unknown",
        source_fill_purpose: str | None = None,
        log_intents: bool = True,
    ) -> list[VirtualOrder]:
        intent_indices = (
            self._log_intents(
                intents,
                event_source=event_source,
                source_fill_purpose=source_fill_purpose,
            )
            if log_intents
            else [self._resolve_intent_log_index(intent) for intent in intents]
        )
        resting: list[VirtualOrder] = []
        submitted_pairs: list[tuple[StrategyIntent, VirtualOrder]] = []
        for intent, intent_log_index in zip(intents, intent_indices):
            if log_orders:
                order = self._submit_intent_with_logging(
                    intent,
                    replace=replace,
                    intent_log_index=intent_log_index,
                )
            else:
                order, _ = self.book.submit_intent(intent, replace=replace)
                if order is not None:
                    order.created_candle_index = self.candle_index
            if order is None:
                continue
            submitted_pairs.append((intent, order))
        self.book.sync_runtime_state(self.runtime_state)
        self.orders_submitted += len(submitted_pairs)
        self._refresh_snapshot_from_book(source="after_submit_intents")
        for intent, order in submitted_pairs:
            if is_immediate_market_fill(intent):
                self._fill_immediate_refill_market_intent(
                    intent,
                    order,
                    event_source=event_source,
                    source_fill_purpose=source_fill_purpose,
                )
                continue
            resting.append(order)
        return resting

    def process_candle(
        self,
        candle: SyntheticCandle,
        *,
        fill_model: str = "conservative",
        max_fills_per_candle: int | None = None,
        conservative_fill_order: bool = True,
    ) -> ProcessCandleResult:
        self.candle = candle
        self._refresh_snapshot_from_book(source="before_process_candle", price=candle.close)

        fill_config = resolve_fill_model_config(
            fill_model=fill_model,
            max_fills_per_candle=max_fills_per_candle,
        )
        eligible_orders = list(self.book.active_orders())
        candle_fills, fill_stats = process_candle_fills(
            book=self.book,
            runtime_state=self.runtime_state,
            candle=candle,
            eligible_orders=eligible_orders,
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
            conservative_fill_order=conservative_fill_order,
        )
        on_fill_intents: list[StrategyIntent] = []

        for fill_event in candle_fills:
            self._refresh_snapshot_from_book(source="after_candle_fill", price=candle.close)
            filled_order = self.book.get_order(fill_event.client_order_id)
            if filled_order is not None:
                self._record_order_event(
                    filled_order,
                    event_type="filled",
                    status="FILLED",
                )
            intents = self.strategy.on_fill(
                fill_event,
                self.snapshot,
                self.runtime_state,
                self.context,
            )
            on_fill_intents.extend(intents)
            self.submit_intents_to_book(
                intents,
                event_source="after_fill",
                source_fill_purpose=fill_event.purpose,
            )
            self._cancel_active_orders_when_flat(source="after_fill_flat_cleanup")

        self._refresh_snapshot_from_book(source="before_on_tick", price=candle.close)
        tick_intents = self.strategy.on_tick(
            self.snapshot,
            self.runtime_state,
            self.context,
        ) or []
        self.submit_intents_to_book(tick_intents, event_source="after_candle")
        self._refresh_snapshot_from_book(source="after_on_tick", price=candle.close)

        return ProcessCandleResult(
            candle=candle,
            candle_fills=candle_fills,
            on_fill_intents=on_fill_intents,
            tick_intents=list(tick_intents),
            snapshot=self.snapshot,
            strategy_state=dict(self.runtime_state.strategy_state),
            same_candle_fill_count=int(fill_stats.get("same_candle_fill_count", len(candle_fills))),
            paired_exit_fills_count=int(fill_stats.get("paired_exit_fills_count", 0)),
        )

    def run_entry_smoke(self) -> SimulationResult:
        entry_intents = self.strategy.on_start(self.snapshot, self.runtime_state, self.context)
        self._log_intents(entry_intents, event_source="initial")
        if not entry_intents:
            state = dict(self.runtime_state.strategy_state)
            return SimulationResult(
                signal=self.signal,
                strategy_name=type(self.strategy).__name__,
                entry_intents=[],
                final_snapshot=self.snapshot,
                runtime_state=self.runtime_state,
                strategy_state=state,
            )

        filled_pairs = fill_entry_intents_at_candle_close(
            book=self.book,
            runtime_state=self.runtime_state,
            intents=entry_intents,
            candle=self.candle,
        )
        entry_fills = [fill for _, fill in filled_pairs]
        self._refresh_snapshot_from_book(source="after_entry_fills")

        post_fill_intents: list[StrategyIntent] = []
        long_fill = next(
            (fill for fill in entry_fills if fill.purpose == self.strategy.LONG_ENTRY_PURPOSE),
            None,
        )
        short_fill = next(
            (fill for fill in entry_fills if fill.purpose == self.strategy.SHORT_ENTRY_PURPOSE),
            None,
        )

        if long_fill is not None:
            self._refresh_snapshot_from_book(source="before_long_entry_on_fill")
            long_post_intents = self.strategy.on_fill(
                long_fill, self.snapshot, self.runtime_state, self.context
            )
            self._log_intents(
                long_post_intents,
                event_source="after_fill",
                source_fill_purpose=long_fill.purpose,
            )
            post_fill_intents.extend(long_post_intents)

        if short_fill is not None:
            self._refresh_snapshot_from_book(source="before_short_entry_on_fill")
            short_post_intents = self.strategy.on_fill(
                short_fill, self.snapshot, self.runtime_state, self.context
            )
            self._log_intents(
                short_post_intents,
                event_source="after_fill",
                source_fill_purpose=short_fill.purpose,
            )
            post_fill_intents.extend(short_post_intents)

        resting_orders = self.submit_intents_to_book(
            post_fill_intents,
            event_source="after_fill",
            log_intents=False,
        )
        self._cancel_active_orders_when_flat(source="after_entry_fill_flat_cleanup")

        state = dict(self.runtime_state.strategy_state)
        return SimulationResult(
            signal=self.signal,
            strategy_name=type(self.strategy).__name__,
            entry_intents=list(entry_intents),
            entry_fills=entry_fills,
            post_fill_intents=list(post_fill_intents),
            resting_orders=resting_orders,
            final_snapshot=self.snapshot,
            runtime_state=self.runtime_state,
            strategy_state=state,
        )

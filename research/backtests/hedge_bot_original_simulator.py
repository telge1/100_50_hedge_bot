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
from .simulated_execution import fill_entry_intents_at_candle_close, process_candle_fills
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


def _default_instrument_rules(symbol: str) -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.001"),
        "tick_size": Decimal("0.1"),
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


def build_runtime_state(*, symbol: str) -> RuntimeState:
    runtime_state = RuntimeState(strategy_state={})
    runtime_state.instrument_rules[symbol.upper()] = _default_instrument_rules(symbol)
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
        temp_dir: Path | None = None,
    ) -> None:
        self.signal = signal
        self.symbol = symbol.upper()
        self.candle = SyntheticCandle(symbol=self.symbol, close=float(candle_close))
        self._temp_dir = temp_dir
        self._owned_temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.config = config or build_test_config(signal=signal, symbol=self.symbol)
        self.strategy = build_strategy(signal, self.config)
        self.runtime_state = build_runtime_state(symbol=self.symbol)
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
            )
        )

    def _submit_intent_with_logging(
        self,
        intent: StrategyIntent,
        *,
        replace: bool,
    ) -> VirtualOrder | None:
        if str(intent.purpose or "").strip().upper() in {
            "INITIAL_LONG_ENTRY",
            "INITIAL_SHORT_ENTRY",
        }:
            return None
        order, replaced_ids = self.book.submit_intent(intent, replace=replace)
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
                )
            else:
                self._record_order_event(order, event_type="submitted")
        if not replaced_ids:
            self._record_order_event(order, event_type="submitted")
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

    def submit_intents_to_book(
        self,
        intents: list[StrategyIntent],
        *,
        replace: bool = True,
        log_orders: bool = True,
    ) -> list[VirtualOrder]:
        resting: list[VirtualOrder] = []
        for intent in intents:
            if log_orders:
                order = self._submit_intent_with_logging(intent, replace=replace)
            else:
                order, _ = self.book.submit_intent(intent, replace=replace)
            if order is None:
                continue
            resting.append(order)
        self.book.sync_runtime_state(self.runtime_state)
        self.orders_submitted += len(resting)
        self._refresh_snapshot_from_book(source="after_submit_intents")
        return resting

    def process_candle(
        self,
        candle: SyntheticCandle,
        *,
        max_fills_per_candle: int | None = None,
        conservative_fill_order: bool = True,
    ) -> ProcessCandleResult:
        self.candle = candle
        self._refresh_snapshot_from_book(source="before_process_candle", price=candle.close)

        candle_fills = process_candle_fills(
            book=self.book,
            runtime_state=self.runtime_state,
            candle=candle,
            max_fills_per_candle=max_fills_per_candle,
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
            self.submit_intents_to_book(intents)

        self._refresh_snapshot_from_book(source="before_on_tick", price=candle.close)
        tick_intents = self.strategy.on_tick(
            self.snapshot,
            self.runtime_state,
            self.context,
        ) or []
        self.submit_intents_to_book(tick_intents)
        self._refresh_snapshot_from_book(source="after_on_tick", price=candle.close)

        return ProcessCandleResult(
            candle=candle,
            candle_fills=candle_fills,
            on_fill_intents=on_fill_intents,
            tick_intents=list(tick_intents),
            snapshot=self.snapshot,
            strategy_state=dict(self.runtime_state.strategy_state),
        )

    def run_entry_smoke(self) -> SimulationResult:
        entry_intents = self.strategy.on_start(self.snapshot, self.runtime_state, self.context)
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
            post_fill_intents.extend(
                self.strategy.on_fill(long_fill, self.snapshot, self.runtime_state, self.context)
            )

        if short_fill is not None:
            self._refresh_snapshot_from_book(source="before_short_entry_on_fill")
            post_fill_intents.extend(
                self.strategy.on_fill(short_fill, self.snapshot, self.runtime_state, self.context)
            )

        resting_orders = self.submit_intents_to_book(post_fill_intents)

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

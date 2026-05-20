import logging
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fixed_cycle_hedge_bot import cleanup
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import ActiveOrderSnapshot, FillEvent, HedgeSnapshot, ManagedOrder, StrategyIntent
from fixed_cycle_hedge_bot.runtime import GenericHedgeRuntime, GenericRuntimeConfig
from utils.math_utils import calculate_pnl


class FakeOrderManager:
    def __init__(self) -> None:
        self.positions = []
        self.current_price = 100.0
        self.limit_orders = []
        self.market_orders = []
        self.tp_orders = []
        self.cancel_calls = []
        self.open_orders = []
        self.order_histories = {}
        self.leverage_calls = []
        self.ensure_max_leverage_calls = []
        self.cancel_all_orders_calls = []
        self.closed_pnl_rows: list[dict[str, Any]] = []

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def fetch_positions(self, symbol: str | None = None, category: str = "linear", settle_coin: str | None = None):
        return list(self.positions)

    def fetch_mark_price(self, symbol: str, category: str = "linear") -> float:
        return self.current_price

    def place_limit_order(self, payload):
        self.limit_orders.append(payload)
        return {"result": {"orderId": f"ex-{payload.order_link_id}"}}

    def place_market_order(self, **kwargs):
        self.market_orders.append(kwargs)
        return {"result": {"orderId": f"ex-{kwargs['order_link_id']}"}}

    def place_reduce_market_order(self, **kwargs):
        self.market_orders.append(kwargs)
        return {"result": {"orderId": f"ex-{kwargs['order_link_id']}"}}

    def set_short_take_profit_limit(
        self,
        *,
        symbol: str,
        tp_price: float,
        tp_limit_price: float,
        position_size: float,
        position_idx: int = 2,
        category: str = "linear",
        trigger_by: str = "LastPrice",
    ):
        payload = {
            "symbol": symbol,
            "tp_price": tp_price,
            "tp_limit_price": tp_limit_price,
            "position_size": position_size,
            "position_idx": position_idx,
            "category": category,
            "trigger_by": trigger_by,
        }
        self.tp_orders.append(payload)
        return {"result": {"orderId": f"ex-short-tp-{len(self.tp_orders)}"}}

    def cancel_order(self, order_id: str, *, symbol: str | None = None, category: str = "linear") -> bool:
        self.cancel_calls.append({"order_id": order_id, "symbol": symbol, "category": category})
        return True

    def cancel_all_orders(self, *, symbol: str, category: str = "linear") -> bool:
        self.cancel_all_orders_calls.append({"symbol": symbol, "category": category})
        return True

    def get_cached_instrument_rules(self, symbol: str, category: str = "linear") -> dict[str, Decimal]:
        if symbol and symbol.upper() == "BTCUSDT":
            return {
                "tick_size": Decimal("0.01"),
                "qty_step": Decimal("0.01"),
                "min_order_qty": Decimal("0.01"),
                "min_notional_value": Decimal("5"),
                "min_notional": Decimal("5"),
            }
        return {}

    def ensure_hedge_mode(self, symbol: str, category: str = "linear") -> bool:
        return True

    def ensure_max_leverage(self, symbol: str, category: str = "linear") -> bool:
        self.ensure_max_leverage_calls.append({"symbol": symbol, "category": category})
        return True

    def set_leverage(self, symbol: str, buy_leverage, sell_leverage, category: str = "linear") -> bool:
        self.leverage_calls.append(
            {
                "symbol": symbol,
                "buy_leverage": buy_leverage,
                "sell_leverage": sell_leverage,
                "category": category,
            }
        )
        return True

    def fetch_open_orders(self, symbol: str | None = None, category: str = "linear", settle_coin: str | None = None):
        return list(self.open_orders)

    def fetch_order_history(
        self,
        symbol: str | None = None,
        category: str = "linear",
        *,
        order_id: str | None = None,
        order_link_id: str | None = None,
        limit: int = 20,
    ):
        key = order_link_id or order_id
        return list(self.order_histories.get(key, []))

    def fetch_closed_pnl(
        self,
        symbol: str,
        category: str,
        *,
        limit: int = 20,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ):
        return list(self.closed_pnl_rows)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def log_event(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


class FixedCycleStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        # ensure no persisted cycle state leaks into tests
        state_path = Path(__file__).resolve().parent / "state.json"
        if state_path.exists():
            state_path.unlink()

    def build_runtime(self, order_manager: FakeOrderManager, config: FixedCycleHedgeConfig | None = None) -> GenericHedgeRuntime:
        strategy = FixedCycleHedgeStrategy(
            config
            or FixedCycleHedgeConfig(
                symbol="BTCUSDT",
                category="linear",
                rest_poll_after_fill_ms=0,
                order_refresh_cooldown_ms=0,
                max_cycles=3,
                price_tick_size=0.1,
                qty_step=0.001,
                reduction_pct_per_fill=15,
                long_fill_distance_pct=0.15,
            )
        )
        runtime = GenericHedgeRuntime(
            GenericRuntimeConfig(
                api_key="key",
                secret_key="secret",
                symbol="BTCUSDT",
                category="linear",
                min_order_value=1.0,
                ensure_exchange_ready=False,
                audit_log_file=None,
            ),
            strategy,
            logger=logging.getLogger("test.fixed_cycle"),
            order_manager=order_manager,
        )
        symbol_key = strategy.config.symbol.upper()
        rules = order_manager.get_cached_instrument_rules(symbol_key, strategy.config.category)
        if rules:
            runtime.runtime_state.instrument_rules[symbol_key] = rules
        return runtime

    def _seed_confirmed_cycle_state(
        self,
        runtime: GenericHedgeRuntime,
        *,
        cycle_index: int = 1,
        long_pnl: float = -0.15,
        short_pnl: float = 0.25,
    ) -> None:
        state = runtime.runtime_state.strategy_state
        ledger = state.setdefault(
            "audit_pnl_ledger",
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        cycle_key = str(cycle_index)
        ledger["cycle_long_reduce_pnl"][cycle_key] = long_pnl
        ledger["cycle_short_tp_pnl"][cycle_key] = short_pnl
        cycle_state = state.setdefault("cycle_state", runtime.strategy._default_cycle_state())
        last_snapshot = runtime.runtime_state.last_snapshot or HedgeSnapshot(
            symbol=runtime.config.symbol,
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=100.0,
            short_avg=100.0,
            source="test",
        )
        long_fills = cycle_state.setdefault("long_fills", {})
        long_fill = long_fills.setdefault(
            cycle_key,
            {
                "price": float(last_snapshot.long_avg or 0.0) or 100.0,
                "qty": float(last_snapshot.long_qty or 1.0),
                "client_order_id": f"cycle-long-{cycle_index}",
                "exec_id": f"exec-cycle-long-{cycle_index}",
            },
        )
        long_fill["confirmed_closed_pnl"] = long_pnl
        long_fill["closed_pnl_ready"] = True
        short_fills = cycle_state.setdefault("short_fills", {})
        short_fill = short_fills.setdefault(
            cycle_key,
            {
                "price": float(last_snapshot.short_avg or 0.0) or 100.0,
                "qty": float(last_snapshot.short_qty or 0.5),
                "client_order_id": f"cycle-short-{cycle_index}",
            },
        )
        short_fill["confirmed_closed_pnl"] = short_pnl
        short_fill["closed_pnl_ready"] = True
        state.update(
            {
                "cycle_long_add_filled": True,
                "cycle_short_tp_filled": True,
                "cycle_completed_count": cycle_index,
                "cycle_pair_count": cycle_index,
                "short_tp_pending_cycle": 0,
                "cycle_waiting_for_short_tp": False,
                "block_exit_rebuild_until_pnl_ready": False,
            }
        )

    def _ensure_cycle_order(
        self,
        runtime: GenericHedgeRuntime,
        *,
        cycle_index: int = 1,
        side: str = "long",
        purpose: str = "CYCLE_1_LONG_ADD",
        status: str = "OPEN",
        price: float = 99.0,
    ) -> ManagedOrder:
        client_id = f"cycle-{side}-{cycle_index}"
        order = ManagedOrder(
            client_order_id=client_id,
            exchange_order_id=f"ex-{client_id}",
            side=side,
            qty=1.0,
            purpose=purpose,
            price=price,
            order_type="Limit",
            reduce_only=side == "short",
            status=status,
        )
        runtime.runtime_state.active_orders[client_id] = order
        runtime.runtime_state.exchange_to_client_id[order.exchange_order_id] = client_id
        return order
    def test_safe_float_helper(self) -> None:
        strategy = FixedCycleHedgeStrategy()
        self.assertIsNone(strategy._safe_float(None, None))
        self.assertEqual(strategy._safe_float("", 5.5), 5.5)
        self.assertAlmostEqual(strategy._safe_float("123.45", 0.0), 123.45)
        self.assertEqual(strategy._safe_float("invalid", 2.5), 2.5)

    def test_audit_strategy_noop_is_throttled_and_compact(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["long-exit"] = ManagedOrder(
            client_order_id="long-exit",
            exchange_order_id="ex-long-exit",
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.1,
            remaining_qty=0.9,
            metadata={"nested": {"very": "large"}},
        )
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=99.0,
            short_avg=101.0,
            active_orders=(runtime.runtime_state.active_orders["long-exit"].to_snapshot(),),
            source="tick",
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", [], snapshot)
            runtime._dispatch("tick", [], snapshot)

        noop_calls = [
            call for call in log_event_mock.call_args_list if call.args and call.args[0] == "strategy_noop"
        ]
        self.assertEqual(len(noop_calls), 1)
        payload = noop_calls[0].kwargs
        self.assertNotIn("snapshot", payload)
        self.assertIn("active_orders", payload)
        self.assertNotIn("metadata", payload["active_orders"][0])
        self.assertNotIn("client_order_id", payload["active_orders"][0])

    def test_audit_snapshot_refreshed_tick_is_throttled_and_compact(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["long-exit"] = ManagedOrder(
            client_order_id="long-exit",
            exchange_order_id="ex-long-exit",
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.2,
            remaining_qty=0.8,
            metadata={"secret": "noisy"},
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 101.0},
        ]

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime.refresh_snapshot("tick")
            runtime.refresh_snapshot("tick")

        snapshot_calls = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "snapshot_refreshed"
        ]
        self.assertEqual(len(snapshot_calls), 1)
        snapshot_payload = snapshot_calls[0].kwargs["snapshot"]
        self.assertNotIn("current_price", snapshot_payload)
        self.assertNotIn("updated_at", snapshot_payload)
        self.assertNotIn("metadata", snapshot_payload["active_orders"][0])

    def test_audit_fixed_cycle_fast_path_skip_is_throttled(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="initial-long",
                    exchange_order_id="ex-initial-long",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose=runtime.strategy.LONG_ENTRY_PURPOSE,
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
            source="websocket",
        )
        fill_event = FillEvent(
            exchange_order_id="ex-fill",
            client_order_id="fill",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=1.0,
            exec_price=100.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-fill",
            metadata={},
        )

        with patch.object(runtime.context.audit, "log_event") as log_event_mock:
            runtime.strategy._fast_path_second_order(fill_event, snapshot, runtime.runtime_state, runtime.context)
            runtime.strategy._fast_path_second_order(fill_event, snapshot, runtime.runtime_state, runtime.context)

        fast_path_calls = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_fast_path_skip"
        ]
        self.assertEqual(len(fast_path_calls), 1)

    def test_audit_fixed_cycle_structure_skip_is_throttled(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="initial-long",
                    exchange_order_id="ex-initial-long",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose=runtime.strategy.LONG_ENTRY_PURPOSE,
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
            source="tick",
        )

        with patch.object(runtime.context.audit, "log_event") as log_event_mock:
            runtime.strategy._rebuild_structure(snapshot, runtime.runtime_state, runtime.context, reason="test")
            runtime.strategy._rebuild_structure(snapshot, runtime.runtime_state, runtime.context, reason="test")

        structure_calls = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_structure_skip"
        ]
        self.assertEqual(len(structure_calls), 1)

    def test_audit_intent_submitted_payload_is_compact_by_default(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_ENTRY_PURPOSE,
            metadata={"entry_role": "initial_long"},
        )

        with (
            patch.object(runtime, "_submit_to_exchange", return_value={"result": {"orderId": "ex-1"}}),
            patch.object(runtime.audit, "log_event") as log_event_mock,
        ):
            runtime.submit_intent(intent, snapshot, source="tick")

        submitted_calls = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "intent_submitted"
        ]
        self.assertEqual(len(submitted_calls), 1)
        payload = submitted_calls[0].kwargs
        self.assertEqual(payload["purpose"], runtime.strategy.LONG_ENTRY_PURPOSE)
        self.assertNotIn("intent", payload)
        self.assertNotIn("traces", payload)

    def test_audit_critical_events_still_log_after_noise_reduction(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_ENTRY_PURPOSE,
        )
        runtime.runtime_state.last_snapshot = snapshot
        runtime.runtime_state.active_orders["cycle-long-fill"] = ManagedOrder(
            client_order_id="cycle-long-fill",
            exchange_order_id="ex-cycle-long-fill",
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=1.0,
            metadata={"cycle_index": 1, "cycle_role": "long_reduce", "entry_price": 101.0},
        )

        with patch.object(runtime.audit, "log_event") as audit_log_mock:
            with patch.object(runtime, "_submit_to_exchange", return_value={"result": {"orderId": "ex-1"}}):
                runtime.submit_intent(intent, snapshot, source="tick")
            with patch.object(runtime, "_submit_to_exchange", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    runtime.submit_intent(intent, snapshot, source="tick")
            with (
                patch.object(runtime, "refresh_snapshot", return_value=snapshot),
                patch.object(runtime.strategy, "on_fill", return_value=[]),
            ):
                runtime._ingest_fill_event(
                    exchange_order_id="ex-cycle-long-fill",
                    client_id="cycle-long-fill",
                    qty=1.0,
                    price=100.0,
                    exec_id="exec-fill",
                    cumulative_qty=1.0,
                    source="websocket",
                )

        logged_events = [call.args[0] for call in audit_log_mock.call_args_list if call.args]
        self.assertIn("order_submitted", logged_events)
        self.assertIn("order_rejected", logged_events)
        self.assertIn("fill_received", logged_events)

        state = runtime.runtime_state.strategy_state
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.80,
            "final_short_exit_pnl": -0.50,
            "total_realized_pnl": 0.0,
        }
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "short-exit"}
        state["trade_block_id"] = "trade-audit"

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as strategy_log_mock:
            runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
                runtime.runtime_state,
                runtime.context,
                "test_audit_critical_events",
            )

        strategy_events = [call.args[0] for call in strategy_log_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_last_trade_pnl_persisted", strategy_events)
        self.assertIn("fixed_cycle_trade_pnl_finalized", strategy_events)

    def test_cycle_long_add_intent_enforces_reduce_only_and_sell(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=120.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=1.5,
            price=119.0,
            order_type="Limit",
            purpose="CYCLE_2_LONG_ADD",
            trigger_price=119.0,
            reduce_only=False,
            position_idx=2,
            metadata={"cycle_role": "long_add"},
        )
        captured: dict[str, ManagedOrder] = {}

        def fake_submit(managed_order: ManagedOrder, *_: Any, force_market_fallback: bool = False) -> dict[str, Any]:
            captured["managed_order"] = managed_order
            return {"result": {"orderId": "ex-cycle-long-add"}}

        with patch.object(runtime, "_submit_to_exchange", side_effect=fake_submit):
            with patch.object(runtime.audit, "log_event") as audit_log_mock:
                runtime.submit_intent(intent, snapshot, source="tick")

        order = captured["managed_order"]
        self.assertTrue(order.reduce_only)
        self.assertEqual(order.side, "long")
        self.assertEqual(order.metadata.get("position_idx"), 1)
        self.assertEqual(order.metadata.get("cycle_role"), "long_reduce")
        self.assertIn(
            "fixed_cycle_long_reduce_intent_corrected",
            [call.args[0] for call in audit_log_mock.call_args_list if call.args],
        )

    def test_cycle_long_add_invalid_exchange_side_is_blocked(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=130.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=2.0,
            purpose="CYCLE_2_LONG_ADD",
            reduce_only=False,
            metadata={"cycle_role": "long_add"},
        )
        with patch.object(runtime, "_exchange_side", return_value="Buy"):
            with patch.object(runtime, "_submit_to_exchange") as submit_mock:
                with patch.object(runtime.audit, "log_event") as audit_log_mock:
                    result = runtime.submit_intent(intent, snapshot, source="tick")
        self.assertIsNone(result)
        submit_mock.assert_not_called()
        self.assertIn(
            "fixed_cycle_invalid_long_reduce_order_blocked",
            [call.args[0] for call in audit_log_mock.call_args_list if call.args],
        )

    def test_cycle_long_add_blocked_while_waiting_for_short_tp(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state["cycle_waiting_for_short_tp"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=150.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        long_add_intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose="CYCLE_2_LONG_ADD",
            reduce_only=False,
            metadata={"cycle_role": "long_add"},
        )
        long_add_intent_2 = StrategyIntent(
            side="long",
            qty=1.0,
            purpose="CYCLE_3_LONG_ADD",
            reduce_only=False,
        )
        short_reduce_intent = StrategyIntent(
            side="short",
            qty=1.5,
            purpose="CYCLE_2_SHORT_REDUCE",
            reduce_only=True,
        )
        long_tp_intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            reduce_only=True,
        )
        short_sl_intent = StrategyIntent(
            side="short",
            qty=0.5,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            reduce_only=True,
        )
        with patch.object(runtime, "_submit_to_exchange", return_value={"result": {"orderId": "ex-ok"}}) as submit_mock:
            with patch.object(runtime.audit, "log_event") as audit_log_mock:
                self.assertIsNone(runtime.submit_intent(long_add_intent, snapshot, source="tick"))
                self.assertIsNone(runtime.submit_intent(long_add_intent_2, snapshot, source="tick"))
                runtime.submit_intent(short_reduce_intent, snapshot, source="tick")
                runtime.submit_intent(long_tp_intent, snapshot, source="tick")
                runtime.submit_intent(short_sl_intent, snapshot, source="tick")
        self.assertEqual(submit_mock.call_count, 3)
        self.assertIn(
            "fixed_cycle_long_reduce_intent_blocked_phase",
            [call.args[0] for call in audit_log_mock.call_args_list if call.args],
        )

    def test_initial_long_entry_remains_buy_and_not_reduce_only(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=110.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_ENTRY_PURPOSE,
            reduce_only=False,
        )
        captured: dict[str, ManagedOrder] = {}

        def fake_submit(managed_order: ManagedOrder, *_: Any, force_market_fallback: bool = False) -> dict[str, Any]:
            captured["managed_order"] = managed_order
            return {"result": {"orderId": "ex-initial"}}

        with patch.object(runtime, "_submit_to_exchange", side_effect=fake_submit):
            runtime.submit_intent(intent, snapshot, source="tick")
        order = captured["managed_order"]
        self.assertFalse(order.reduce_only)
        self.assertEqual(order.side, "long")

    def test_refill_long_remains_buy_and_not_reduce_only(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=105.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=0.5,
            purpose="REFILL_LONG",
            reduce_only=False,
        )
        captured: dict[str, ManagedOrder] = {}

        def fake_submit(managed_order: ManagedOrder, *_: Any, force_market_fallback: bool = False) -> dict[str, Any]:
            captured["managed_order"] = managed_order
            return {"result": {"orderId": "ex-refill"}}

        with patch.object(runtime, "_submit_to_exchange", side_effect=fake_submit):
            runtime.submit_intent(intent, snapshot, source="tick")
        order = captured["managed_order"]
        self.assertFalse(order.reduce_only)
        self.assertEqual(order.side, "long")

    def test_cycle_long_add_restores_metadata_from_existing_order(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        existing = ManagedOrder(
            client_order_id="cycle-2-long",
            side="long",
            qty=1.0,
            purpose="CYCLE_2_LONG_ADD",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=1.0,
            metadata={"cycle_role": "long_reduce", "cycle_index": 2},
        )
        runtime.runtime_state.active_orders["cycle-2-long"] = existing
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=115.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose="CYCLE_2_LONG_ADD",
            metadata={"replace_open_purpose": "CYCLE_2_LONG_ADD", "cycle_role": "long_add"},
            reduce_only=False,
            position_idx=2,
        )
        captured: dict[str, ManagedOrder] = {}

        def fake_submit(managed_order: ManagedOrder, *_: Any) -> dict[str, Any]:
            captured["managed_order"] = managed_order
            return {"result": {"orderId": "ex-cycle-long-add"}}

        with patch.object(runtime, "_submit_to_exchange", side_effect=fake_submit):
            with patch.object(runtime.audit, "log_event") as audit_log_mock:
                runtime.submit_intent(intent, snapshot, source="tick")

        order = captured["managed_order"]
        self.assertEqual(order.metadata.get("cycle_role"), "long_reduce")
        self.assertEqual(order.metadata.get("cycle_index"), 2)
        self.assertEqual(order.metadata.get("position_idx"), 1)
        self.assertTrue(order.reduce_only)
        self.assertIn(
            "fixed_cycle_long_reduce_intent_metadata_restored",
            [call.args[0] for call in audit_log_mock.call_args_list if call.args],
        )

    def test_bootstrap_places_initial_long_and_short_entries(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)

        runtime.bootstrap()

        self.assertEqual(len(order_manager.market_orders), 2)
        sides = [order["side"] for order in order_manager.market_orders]
        self.assertIn("Buy", sides)
        self.assertIn("Sell", sides)

    def test_max_leverage_is_ensured_once_before_first_order_even_without_exchange_ready_bootstrap(self) -> None:
        order_manager = FakeOrderManager()
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                symbol="BTCUSDT",
                category="linear",
                rest_poll_after_fill_ms=0,
                order_refresh_cooldown_ms=0,
                max_cycles=3,
                price_tick_size=0.1,
                qty_step=0.001,
                reduction_pct_per_fill=15,
                long_fill_distance_pct=0.15,
            )
        )
        runtime = GenericHedgeRuntime(
            GenericRuntimeConfig(
                api_key="key",
                secret_key="secret",
                symbol="BTCUSDT",
                category="linear",
                min_order_value=1.0,
                ensure_exchange_ready=False,
                audit_log_file=None,
            ),
            strategy,
            logger=logging.getLogger("test.fixed_cycle"),
            order_manager=order_manager,
        )

        runtime.bootstrap()

        self.assertEqual(len(order_manager.ensure_max_leverage_calls), 1)
        self.assertEqual(order_manager.ensure_max_leverage_calls[0]["symbol"], "BTCUSDT")
        self.assertEqual(order_manager.leverage_calls, [])

    def test_bootstrap_with_existing_positions_prepares_downside_and_exits(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        order_manager.current_price = 97.0
        runtime = self.build_runtime(order_manager)

        snapshot = runtime.bootstrap()

        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price, snapshot, runtime.runtime_state)
        runtime.runtime_state.strategy_state["last_exit_signature"] = None
        self._seed_confirmed_cycle_state(runtime)
        self._seed_confirmed_cycle_state(runtime)
        self._seed_confirmed_cycle_state(runtime)
        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")
        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=runtime.runtime_state.strategy_state.get("current_effective_cycle", 0),
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
        )
        purposes = {intent.purpose for intent in intents}
        self.assertIn("CYCLE_1_LONG_ADD", {order.purpose for order in runtime.runtime_state.active_orders.values()})
        self.assertNotIn("CYCLE_1_SHORT_REDUCE", {order.purpose for order in runtime.runtime_state.active_orders.values()})
        self.assertIn("LONG_TP_EXIT", purposes)
        self.assertIn("SHORT_SL_EXIT", purposes)

    def test_bootstrap_cleans_stale_restart_state_before_fresh_entry(self) -> None:
        order_manager = FakeOrderManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "strategy_state.json"
            state_file.write_text(
                """
{
  "strategy_state": {
    "initial_entry_confirmed": true,
    "cycle_completed_count": 2,
    "cycle_waiting_for_short_tp": true,
    "short_tp_pending_cycle": 2,
    "pending_cycle_loss_usdt": 12.5,
    "cycle_state": {
      "symbol": "BTCUSDT",
      "trade_active": true,
      "long_add_pending": true,
      "cycle_waiting_for_short_tp": true,
      "short_tp_pending_cycle": 2
    }
  },
  "realized_long_pnl_total": 3.5,
  "realized_short_pnl_total": -1.25,
  "active_orders": []
}
""".strip(),
                encoding="utf-8",
            )
            strategy = FixedCycleHedgeStrategy(
                FixedCycleHedgeConfig(
                    symbol="BTCUSDT",
                    category="linear",
                    rest_poll_after_fill_ms=0,
                    order_refresh_cooldown_ms=0,
                    max_cycles=3,
                    price_tick_size=0.1,
                    qty_step=0.001,
                    reduction_pct_per_fill=15,
                    long_fill_distance_pct=0.15,
                )
            )
            runtime = GenericHedgeRuntime(
                GenericRuntimeConfig(
                    api_key="key",
                    secret_key="secret",
                    symbol="BTCUSDT",
                    category="linear",
                    min_order_value=1.0,
                    ensure_exchange_ready=False,
                    audit_log_file=None,
                    strategy_state_file=str(state_file),
                ),
                strategy,
                logger=logging.getLogger("test.fixed_cycle"),
                order_manager=order_manager,
            )

            runtime.bootstrap()

            state = runtime.runtime_state.strategy_state
            self.assertFalse(state["cycle_waiting_for_short_tp"])
            self.assertEqual(state["short_tp_pending_cycle"], 0)
            self.assertEqual(state["cycle_completed_count"], 0)
            self.assertEqual(state["pending_cycle_loss_usdt"], 0.0)
            self.assertFalse(state["initial_entry_confirmed"])
            self.assertEqual(runtime.runtime_state.realized_long_pnl_total, 0.0)
            self.assertEqual(runtime.runtime_state.realized_short_pnl_total, 0.0)
            self.assertEqual(len(runtime.runtime_state.active_orders), 2)
            self.assertEqual(len(order_manager.market_orders), 2)

    def test_bootstrap_prunes_persisted_active_orders_missing_on_exchange(self) -> None:
        order_manager = FakeOrderManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "strategy_state.json"
            state_file.write_text(
                """
{
  "strategy_state": {
    "cycle_waiting_for_short_tp": true,
    "short_tp_pending_cycle": 1,
    "cycle_state": {
      "symbol": "BTCUSDT",
      "trade_active": true,
      "long_add_pending": true,
      "cycle_waiting_for_short_tp": true,
      "short_tp_pending_cycle": 1
    }
  },
  "active_orders": [
    {
      "client_order_id": "fixed_cycle-cycle_1_long_add-stale",
      "side": "long",
      "qty": 0.25,
      "purpose": "CYCLE_1_LONG_ADD",
      "price": 99.8,
      "order_type": "Limit",
      "reduce_only": false,
      "exchange_order_id": "stale-exchange-order",
      "status": "OPEN",
      "filled_qty": 0.0,
      "remaining_qty": 0.25,
      "metadata": {},
      "trace": [],
      "created_at": "2026-04-30T00:00:00+00:00",
      "updated_at": "2026-04-30T00:00:00+00:00"
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            strategy = FixedCycleHedgeStrategy(
                FixedCycleHedgeConfig(
                    symbol="BTCUSDT",
                    category="linear",
                    rest_poll_after_fill_ms=0,
                    order_refresh_cooldown_ms=0,
                    max_cycles=3,
                    price_tick_size=0.1,
                    qty_step=0.001,
                    reduction_pct_per_fill=15,
                    long_fill_distance_pct=0.15,
                )
            )
            runtime = GenericHedgeRuntime(
                GenericRuntimeConfig(
                    api_key="key",
                    secret_key="secret",
                    symbol="BTCUSDT",
                    category="linear",
                    min_order_value=1.0,
                    ensure_exchange_ready=False,
                    audit_log_file=None,
                    strategy_state_file=str(state_file),
                ),
                strategy,
                logger=logging.getLogger("test.fixed_cycle"),
                order_manager=order_manager,
            )

            runtime.bootstrap()

            purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
            self.assertNotIn("CYCLE_1_LONG_ADD", purposes)
            self.assertEqual(len(runtime.runtime_state.active_orders), 2)
            self.assertEqual(len(order_manager.market_orders), 2)
            self.assertFalse(runtime.runtime_state.strategy_state["cycle_waiting_for_short_tp"])
            self.assertEqual(runtime.runtime_state.strategy_state["short_tp_pending_cycle"], 0)

    def test_dynamic_entry_no_hold_needed_logs_payload(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(
            order_manager,
            FixedCycleHedgeConfig(
                symbol="BTCUSDT",
                category="linear",
                dynamic_symbol_enabled=True,
                dynamic_symbol_hold_minutes=10,
                rest_poll_after_fill_ms=0,
                order_refresh_cooldown_ms=0,
                max_cycles=3,
                price_tick_size=0.1,
                qty_step=0.001,
                reduction_pct_per_fill=15,
                long_fill_distance_pct=0.15,
            ),
        )
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
        )
        future_scan = datetime.now(timezone.utc) + timedelta(minutes=20)

        with patch.object(runtime.strategy, "_compute_next_dynamic_scan_ready_at", return_value=future_scan):
            with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger.info") as mock_info:
                runtime.strategy._maybe_start_dynamic_symbol_hold_after_flat(
                    snapshot,
                    runtime.runtime_state,
                    runtime.context,
                    "unit_test_no_hold",
                )

        dynamic_logs = [call.args for call in mock_info.call_args_list if call.args and call.args[0] == "%s %s"]
        self.assertTrue(any(args[1] == "dynamic_entry_no_hold_needed" for args in dynamic_logs))
        payload = next(args[2] for args in dynamic_logs if args[1] == "dynamic_entry_no_hold_needed")
        self.assertEqual(payload["reason"], "unit_test_no_hold")
        self.assertIn("next_dynamic_scan_ready_at", payload)
        self.assertIn("minutes_until_ready", payload)

    def test_dynamic_entry_waiting_for_next_scan_result_logs_payload(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(
            order_manager,
            FixedCycleHedgeConfig(
                symbol="BTCUSDT",
                category="linear",
                dynamic_symbol_enabled=True,
                dynamic_symbol_hold_minutes=10,
                rest_poll_after_fill_ms=0,
                order_refresh_cooldown_ms=0,
                max_cycles=3,
                price_tick_size=0.1,
                qty_step=0.001,
                reduction_pct_per_fill=15,
                long_fill_distance_pct=0.15,
            ),
        )
        runtime.runtime_state.strategy_state["next_dynamic_entry_allowed_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=90)
        ).isoformat()

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger.info") as mock_info:
            allowed = runtime.strategy._dynamic_symbol_entry_gate_allows_entry(
                runtime.runtime_state,
                runtime.context,
                "unit_test_wait",
            )

        self.assertFalse(allowed)
        dynamic_logs = [call.args for call in mock_info.call_args_list if call.args and call.args[0] == "%s %s"]
        self.assertTrue(any(args[1] == "dynamic_entry_waiting_for_next_scan_result" for args in dynamic_logs))
        payload = next(
            args[2] for args in dynamic_logs if args[1] == "dynamic_entry_waiting_for_next_scan_result"
        )
        self.assertEqual(payload["reason"], "unit_test_wait")
        self.assertGreater(payload["seconds_remaining"], 0)

    def test_short_tp_fallback_normal_path_builds_conditional_short_reduce(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        order_manager.current_price = 150.0
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")
        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )
        long_cycle_order.qty = 0.25
        long_cycle_order.remaining_qty = 0.25
        long_cycle_order.reduce_only = True
        long_cycle_order.metadata.update(
            {
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "entry_price": 100.0,
            }
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]

        runtime.on_websocket_fill(
            long_cycle_order.exchange_order_id,
            qty=long_cycle_order.qty,
            price=99.8,
            cumulative_qty=long_cycle_order.qty,
        )

        short_tp_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_SHORT_REDUCE"
        )
        cycle_order_payload = next(
            payload
            for payload in order_manager.market_orders
            if payload.get("order_link_id") == short_tp_order.client_order_id
        )
        self.assertEqual(short_tp_order.order_type, "Market")
        self.assertIsNotNone(short_tp_order.metadata.get("trigger_price"))
        self.assertEqual(short_tp_order.metadata.get("trigger_direction"), 2)
        self.assertEqual(short_tp_order.metadata.get("close_on_trigger"), True)
        self.assertFalse(short_tp_order.metadata.get("market_fallback"))
        self.assertIsNone(short_tp_order.metadata.get("fallback_reason"))
        self.assertIn("trigger_price", cycle_order_payload)
        self.assertEqual(cycle_order_payload.get("trigger_direction"), 2)

    def test_fill_rebuilds_structure_and_advances_cycle_state(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        order_manager.current_price = 97.0
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")
        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )
        long_cycle_order.qty = 0.25
        long_cycle_order.remaining_qty = 0.25
        long_cycle_order.reduce_only = True
        long_cycle_order.metadata.update(
            {
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "entry_price": 100.0,
            }
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]

        recorded_intents: list[list] = []
        original_build_exit = runtime.strategy._build_exit_intents

        def capturing(
            self,
            snapshot,
            runtime_state,
            current_cycle,
            break_even_price,
            tp_price,
            hard_stop_active,
            context,
            *,
            force_exit_rebuild=False,
            pending_loss_old_signature=None,
        ):
            result = original_build_exit(
                snapshot,
                runtime_state,
                current_cycle,
                break_even_price,
                tp_price,
                hard_stop_active,
                context,
                force_exit_rebuild=force_exit_rebuild,
                pending_loss_old_signature=pending_loss_old_signature,
            )
            recorded_intents.append(result)
            return result

        with (
            patch.object(FixedCycleHedgeStrategy, "_build_exit_intents", new=capturing),
            patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger.info") as mock_info,
        ):
            runtime.on_websocket_fill(
                long_cycle_order.exchange_order_id,
                qty=long_cycle_order.qty,
                price=99.8,
                cumulative_qty=long_cycle_order.qty,
            )

        self.assertEqual(runtime.runtime_state.strategy_state["current_long_cycle_index"], 1)
        purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
        self.assertIn("CYCLE_1_SHORT_REDUCE", purposes)
        self.assertNotIn("CYCLE_2_LONG_ADD", purposes)
        self.assertEqual(runtime.runtime_state.strategy_state.get("short_tp_pending_cycle"), 1)
        short_tp_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_SHORT_REDUCE"
        )
        fallback_state = runtime.strategy._get_short_tp_fallback_state(runtime.runtime_state)
        cycle_order_payload = next(
            payload
            for payload in order_manager.market_orders
            if payload.get("order_link_id") == short_tp_order.client_order_id
        )
        latest_intents = recorded_intents[-1]
        expected_short_tp = runtime.runtime_state.strategy_state.get("last_short_tp_trigger_price")
        self.assertEqual(short_tp_order.order_type, "Market")
        self.assertIsNone(short_tp_order.metadata.get("trigger_price"))
        self.assertIsNone(short_tp_order.metadata.get("trigger_direction"))
        self.assertIsNone(short_tp_order.metadata.get("close_on_trigger"))
        self.assertEqual(short_tp_order.metadata.get("market_fallback"), True)
        self.assertEqual(
            short_tp_order.metadata.get("fallback_reason"), "short_tp_trigger_already_crossed"
        )
        self.assertEqual(short_tp_order.metadata.get("original_trigger_price"), expected_short_tp)
        self.assertEqual(short_tp_order.metadata.get("current_price"), 97.0)
        self.assertFalse(fallback_state.active)
        self.assertNotIn("trigger_price", cycle_order_payload)
        fallback_start_events = [
            call.args
            for call in mock_info.call_args_list
            if len(call.args) >= 2 and call.args[0] == "%s %s" and call.args[1] == "SHORT_TP_FALLBACK_START"
        ]
        self.assertEqual(fallback_start_events, [])
        self.assertTrue(recorded_intents)
        latest_intents = recorded_intents[-1]
        self.assertTrue(any(intent.purpose == "LONG_TP_EXIT" for intent in latest_intents))
        self.assertTrue(any(intent.purpose == "SHORT_SL_EXIT" for intent in latest_intents))

    def test_next_long_cycle_unlocks_only_after_short_follow_up_fill(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        order_manager.current_price = 97.0
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")
        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )
        long_cycle_order.qty = 0.25
        long_cycle_order.remaining_qty = 0.25
        long_cycle_order.reduce_only = True
        long_cycle_order.metadata.update(
            {
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "entry_price": 100.0,
            }
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        runtime.on_websocket_fill(
            long_cycle_order.exchange_order_id,
            qty=long_cycle_order.qty,
            price=99.8,
            cumulative_qty=long_cycle_order.qty,
        )
        ledger = runtime.runtime_state.strategy_state.setdefault(
            "audit_pnl_ledger",
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        ledger["cycle_long_reduce_pnl"]["1"] = -0.1
        ledger["cycle_pnl_entries"][f"cycle_long_reduce:1:{long_cycle_order.client_order_id}"] = {
            "pnl": -0.1,
            "source": "confirmed_closed_pnl",
            "is_confirmed": True,
        }

        short_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_SHORT_REDUCE"
        )
        short_cycle_order.qty = 0.075
        short_cycle_order.remaining_qty = 0.075
        self.assertEqual(short_cycle_order.metadata.get("market_fallback"), True)
        now = datetime.now(timezone.utc)
        order_manager.closed_pnl_rows = [
            {
                "orderId": short_cycle_order.exchange_order_id,
                "symbol": "BTCUSDT",
                "side": "Buy",
                "closedSize": short_cycle_order.qty,
                "avgExitPrice": 97.5,
                "closedPnl": 0.2,
                "createdTime": int(now.timestamp() * 1000),
                "updatedTime": int(now.timestamp() * 1000),
            }
        ]
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.425, "avgPrice": 100.0},
        ]
        order_manager.current_price = 100.0
        runtime.on_websocket_fill(
            short_cycle_order.exchange_order_id,
            qty=short_cycle_order.qty,
            price=97.5,
            cumulative_qty=short_cycle_order.qty,
        )

        purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
        self.assertIn("CYCLE_2_LONG_ADD", purposes)
        self.assertNotIn("CYCLE_1_SHORT_REDUCE", purposes)
        self.assertFalse(runtime.runtime_state.strategy_state.get("cycle_waiting_for_short_tp"))
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_completed_count"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_pair_count"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("current_short_cycle_index"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("short_tp_pending_cycle"), 0)

    def test_break_even_without_realized_loss_matches_profit_and_buffer_target(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        snapshot = runtime.runtime_state.last_snapshot
        snapshot.current_price = 97.0
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        initial_total_notional = float(
            runtime.runtime_state.strategy_state.get("initial_total_notional_usdt") or 0.0
        )
        fee_buffer = initial_total_notional * runtime.strategy._pct(runtime.strategy.config.fee_safety_buffer_pct)
        profit_target = initial_total_notional * runtime.strategy._pct(runtime.strategy.config.tp_profit_target_pct)
        expected_target_total = runtime.strategy.config.net_realized_pnl_target + fee_buffer + profit_target
        expected_break_even = runtime.strategy._normalize_price(
            (
                expected_target_total
                - (snapshot.short_avg * snapshot.short_qty)
                + (snapshot.long_avg * snapshot.long_qty)
            )
            / (snapshot.long_qty - snapshot.short_qty)
        )

        self.assertAlmostEqual(break_even_price, expected_break_even)

    def test_break_even_with_realized_long_loss_increases_exit_price(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        baseline_snapshot = runtime.runtime_state.last_snapshot
        baseline_break_even, _ = runtime.strategy._calculate_break_even(baseline_snapshot, runtime.runtime_state)

        loss_snapshot = HedgeSnapshot(
            symbol=baseline_snapshot.symbol,
            current_price=baseline_snapshot.current_price,
            long_qty=baseline_snapshot.long_qty,
            short_qty=baseline_snapshot.short_qty,
            long_avg=baseline_snapshot.long_avg,
            short_avg=baseline_snapshot.short_avg,
            realized_long_pnl_total=-1.0,
            realized_short_pnl_total=baseline_snapshot.realized_short_pnl_total,
            active_orders=baseline_snapshot.active_orders,
            source=baseline_snapshot.source,
            updated_at=baseline_snapshot.updated_at,
        )
        runtime.runtime_state.strategy_state["net_long_loss_balance"] = 1.0
        break_even_with_loss, traces = runtime.strategy._calculate_break_even(loss_snapshot, runtime.runtime_state)

        self.assertGreaterEqual(break_even_with_loss, baseline_break_even)
        self.assertGreaterEqual(traces[0].details.get("realized_long_loss", 0.0), 0.0)
        self.assertGreaterEqual(traces[0].details.get("loss_compensation", 0.0), 0.0)

    def test_tp_recovers_realized_long_loss_total_and_target_profit(self) -> None:
        order_manager = FakeOrderManager()
        config = FixedCycleHedgeConfig(
            symbol="BTCUSDT",
            category="linear",
            rest_poll_after_fill_ms=0,
            order_refresh_cooldown_ms=0,
            max_cycles=3,
            price_tick_size=0.1,
            qty_step=0.001,
            reduction_pct_per_fill=15,
            long_fill_distance_pct=0.15,
            target_profit_usdt=0.015,
        )
        runtime = self.build_runtime(order_manager, config=config)
        runtime.bootstrap()

        target_realized_long_loss = 0.145
        runtime.runtime_state.strategy_state["realized_long_loss_total"] = target_realized_long_loss
        runtime.strategy.realized_long_loss_total = target_realized_long_loss

        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=105.0,
            long_qty=1259.0,
            short_qty=629.0,
            long_avg=100.0,
            short_avg=98.0,
        )
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(
            break_even_price, snapshot, runtime.runtime_state
        )

        long_profit = max(tp_price - snapshot.long_avg, 0.0) * snapshot.long_qty
        short_loss = max(tp_price - snapshot.short_avg, 0.0) * snapshot.short_qty
        net_recovery = long_profit - short_loss - target_realized_long_loss

        self.assertGreaterEqual(
            net_recovery,
            float(runtime.strategy.config.target_profit_usdt or 0.0) - 1e-9,
            "basket TP should first recover realized long loss total before adding the target profit",
        )

    def test_required_remaining_profit_tracks_realized_net_pnl(self) -> None:
        order_manager = FakeOrderManager()
        config = FixedCycleHedgeConfig(
            symbol="BTCUSDT",
            category="linear",
            rest_poll_after_fill_ms=0,
            order_refresh_cooldown_ms=0,
            max_cycles=3,
            price_tick_size=0.1,
            qty_step=0.001,
            reduction_pct_per_fill=15,
            long_fill_distance_pct=0.15,
            target_profit_usdt=0.015,
        )
        runtime = self.build_runtime(order_manager, config=config)
        runtime.bootstrap()

        snapshot = runtime.runtime_state.last_snapshot
        runtime.runtime_state.realized_long_pnl_total = -0.145
        runtime.runtime_state.realized_short_pnl_total = 0.0
        required = runtime.strategy._required_remaining_profit(
            runtime.runtime_state, snapshot
        )
        self.assertAlmostEqual(
            required,
            float(config.target_profit_usdt) + 0.145,
            msg="Required profit must include accrued long loss",
        )

        runtime.runtime_state.realized_short_pnl_total = 0.1
        required_after_short_profit = runtime.strategy._required_remaining_profit(
            runtime.runtime_state, snapshot
        )
        self.assertAlmostEqual(
            required_after_short_profit,
            float(config.target_profit_usdt)
            - (runtime.runtime_state.realized_long_pnl_total + runtime.runtime_state.realized_short_pnl_total),
            msg="Short profit reduces remaining required profit",
        )

        runtime.runtime_state.realized_long_pnl_total = 0.02
        runtime.runtime_state.realized_short_pnl_total = 0.02
        required_when_target_met = runtime.strategy._required_remaining_profit(
            runtime.runtime_state, snapshot
        )
        self.assertEqual(
            required_when_target_met,
            0.0,
            "Remaining profit should clamp to zero once net PnL exceeds the target",
        )

    def test_adaptive_tp_price_moves_closer_for_healthy_structure(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                tp_buffer_pct=1.0,
                hedge_ratio_short=0.5,
                long_fill_distance_pct=0.5,
                price_tick_size=0.01,
            )
        )
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=100.0,
            short_avg=100.2,
        )

        break_even_price = 100.0
        adaptive_tp = strategy._calculate_tp_price(break_even_price, snapshot)
        baseline_tp = strategy._normalize_price(break_even_price * (1 + strategy._pct(strategy.config.tp_buffer_pct)))

        self.assertLess(adaptive_tp, baseline_tp)

    def test_adaptive_tp_price_moves_farther_for_unhealthy_structure(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                tp_buffer_pct=1.0,
                hedge_ratio_short=0.5,
                long_fill_distance_pct=0.5,
                price_tick_size=0.01,
            )
        )
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.2,
            long_avg=100.0,
            short_avg=105.0,
        )

        break_even_price = 100.0
        adaptive_tp = strategy._calculate_tp_price(break_even_price, snapshot)
        baseline_tp = strategy._normalize_price(break_even_price * (1 + strategy._pct(strategy.config.tp_buffer_pct)))

        self.assertGreater(adaptive_tp, baseline_tp)

    def test_short_sl_exit_uses_basket_metadata(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        snapshot = runtime.runtime_state.last_snapshot
        snapshot.current_price = 97.0
        runtime.runtime_state.strategy_state["last_exit_signature"] = None
        runtime.runtime_state.strategy_state["exit_rebuild_allowed"] = True
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price, snapshot, runtime.runtime_state)
        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=runtime.runtime_state.strategy_state.get("current_effective_cycle", 0),
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
            force_exit_rebuild=True,
        )
        short_intent = next(intent for intent in intents if intent.purpose == runtime.strategy.SHORT_SL_EXIT_PURPOSE)
        self.assertEqual(short_intent.metadata.get("basket_tp_price"), tp_price)
        self.assertEqual(short_intent.metadata.get("basket_break_even_price"), break_even_price)
        self.assertEqual(short_intent.metadata.get("replace_open_purpose"), ["SHORT_SL_EXIT"])

    def test_exit_signature_prevents_duplicate_exit_builds(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()
        snapshot = runtime.runtime_state.last_snapshot
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price)
        runtime.runtime_state.strategy_state["last_exit_signature"] = None

        intents_first = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=1,
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
        )
        intents_second = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=1,
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
        )
        self.assertGreater(len(intents_first), 0)
        self.assertEqual(intents_second, [])

    def test_break_even_trace_includes_loss_compensation_profit_target_and_fee_buffer(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        base_snapshot = runtime.runtime_state.last_snapshot
        snapshot = HedgeSnapshot(
            symbol=base_snapshot.symbol,
            current_price=base_snapshot.current_price,
            long_qty=base_snapshot.long_qty,
            short_qty=base_snapshot.short_qty,
            long_avg=base_snapshot.long_avg,
            short_avg=base_snapshot.short_avg,
            realized_long_pnl_total=-0.4,
            realized_short_pnl_total=-0.6,
            active_orders=base_snapshot.active_orders,
            source=base_snapshot.source,
            updated_at=base_snapshot.updated_at,
        )
        _, traces = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        trace = traces[0]
        details = trace.details

        initial_total_notional = float(
            runtime.runtime_state.strategy_state.get("initial_total_notional_usdt") or 0.0
        )
        expected_fee_buffer = initial_total_notional * runtime.strategy._pct(runtime.strategy.config.fee_safety_buffer_pct)
        expected_profit_target = initial_total_notional * runtime.strategy._pct(runtime.strategy.config.tp_profit_target_pct)
        expected_target_total = (
            runtime.strategy.config.net_realized_pnl_target
            + expected_fee_buffer
            + expected_profit_target
            + 1.0
        )

        self.assertAlmostEqual(details.get("fee_buffer", 0.0), expected_fee_buffer)
        self.assertAlmostEqual(details.get("profit_target", 0.0), expected_profit_target)
        self.assertAlmostEqual(details.get("realized_long_loss", 0.0), 0.4)
        self.assertAlmostEqual(details.get("realized_short_loss", 0.0), 0.6)
        self.assertAlmostEqual(details.get("loss_compensation", 0.0), 1.0)
        self.assertAlmostEqual(details.get("target_total", 0.0), expected_target_total)

    def test_adaptive_tp_price_preserves_loss_compensation_effect(self) -> None:
        config = FixedCycleHedgeConfig(
            symbol="BTCUSDT",
            category="linear",
            rest_poll_after_fill_ms=0,
            order_refresh_cooldown_ms=0,
            tp_buffer_pct=1.0,
            hedge_ratio_short=0.5,
            long_fill_distance_pct=0.5,
            price_tick_size=0.1,
            qty_step=0.001,
        )
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.2},
        ]
        runtime = self.build_runtime(order_manager, config)
        runtime.bootstrap()

        base_snapshot = runtime.runtime_state.last_snapshot
        break_even_without_loss, _ = runtime.strategy._calculate_break_even(base_snapshot, runtime.runtime_state)
        tp_without_loss = runtime.strategy._calculate_tp_price(break_even_without_loss, base_snapshot)

        loss_snapshot = HedgeSnapshot(
            symbol=base_snapshot.symbol,
            current_price=base_snapshot.current_price,
            long_qty=base_snapshot.long_qty,
            short_qty=base_snapshot.short_qty,
            long_avg=base_snapshot.long_avg,
            short_avg=base_snapshot.short_avg,
            realized_long_pnl_total=-1.0,
            realized_short_pnl_total=base_snapshot.realized_short_pnl_total,
            active_orders=base_snapshot.active_orders,
            source=base_snapshot.source,
            updated_at=base_snapshot.updated_at,
        )
        break_even_with_loss, _ = runtime.strategy._calculate_break_even(loss_snapshot, runtime.runtime_state)
        tp_with_loss = runtime.strategy._calculate_tp_price(break_even_with_loss, loss_snapshot)

        self.assertGreater(break_even_with_loss, break_even_without_loss)
        self.assertGreater(tp_with_loss, tp_without_loss)

    def test_exit_intents_replace_only_their_purpose(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        snapshot = runtime.runtime_state.last_snapshot
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price)
        runtime.runtime_state.strategy_state["last_exit_signature"] = None

        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=runtime.runtime_state.strategy_state.get("current_effective_cycle", 0),
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
        )

        long_intent = next(intent for intent in intents if intent.purpose == "LONG_TP_EXIT")
        short_intent = next(intent for intent in intents if intent.purpose == "SHORT_SL_EXIT")

        self.assertEqual(long_intent.metadata.get("replace_open_purpose"), ["LONG_TP_EXIT"])
        self.assertEqual(short_intent.metadata.get("replace_open_purpose"), ["SHORT_SL_EXIT"])

    def test_exit_prices_align_with_basket(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        snapshot = runtime.runtime_state.last_snapshot
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price)
        runtime.runtime_state.strategy_state["last_exit_signature"] = None

        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=runtime.runtime_state.strategy_state.get("current_effective_cycle", 0),
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
        )
        long_tp_intent = next(intent for intent in intents if intent.purpose == "LONG_TP_EXIT")
        short_sl_intent = next(intent for intent in intents if intent.purpose == "SHORT_SL_EXIT")
        self.assertAlmostEqual(long_tp_intent.price or 0.0, tp_price, places=8)
        self.assertAlmostEqual(short_sl_intent.price or 0.0, tp_price, places=8)

    def test_exit_intents_use_conditional_market_close(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        snapshot = runtime.runtime_state.last_snapshot
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price, snapshot)
        runtime.runtime_state.strategy_state["last_exit_signature"] = None
        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=runtime.runtime_state.strategy_state.get("current_effective_cycle", 0),
            break_even_price=break_even_price,
            tp_price=tp_price,
            hard_stop_active=False,
            context=runtime.context,
        )

        final_purposes = {
            runtime.strategy.LONG_TP_EXIT_PURPOSE,
            runtime.strategy.SHORT_SL_EXIT_PURPOSE,
        }
        final_intents = [intent for intent in intents if intent.purpose in final_purposes]
        self.assertTrue(final_intents)
        for intent in final_intents:
            self.assertEqual(intent.order_type, "Market")
            self.assertTrue(intent.reduce_only)
            self.assertTrue(intent.close_on_trigger)

    def test_exit_cancel_only_on_signature_change(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()
        order_manager.cancel_calls.clear()

        runtime.runtime_state.strategy_state["last_exit_signature"] = None
        snapshot = runtime.runtime_state.last_snapshot
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price)

        runtime.strategy._rebuild_structure(
            snapshot,
            runtime.runtime_state,
            runtime.context,
            reason="first-rebuild",
        )
        first_cancel_count = len(order_manager.cancel_calls)

        runtime.strategy._rebuild_structure(
            snapshot,
            runtime.runtime_state,
            runtime.context,
            reason="second-rebuild",
        )
        self.assertEqual(len(order_manager.cancel_calls), first_cancel_count)

    def test_fill_rebuild_recalculates_exit_and_keeps_realized_loss_compensation(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")
        runtime.runtime_state.realized_long_pnl_total = -1.0
        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]

        recorded_break_evens: list[tuple[HedgeSnapshot, float]] = []
        original_build_exit = runtime.strategy._build_exit_intents

        def capturing(
            self,
            snapshot,
            runtime_state,
            current_cycle,
            break_even_price,
            tp_price,
            hard_stop_active,
            context,
            *,
            force_exit_rebuild=False,
            pending_loss_old_signature=None,
        ):
            recorded_break_evens.append((snapshot, break_even_price))
            return original_build_exit(
                snapshot,
                runtime_state,
                current_cycle,
                break_even_price,
                tp_price,
                hard_stop_active,
                context,
                force_exit_rebuild=force_exit_rebuild,
                pending_loss_old_signature=pending_loss_old_signature,
            )

        with patch.object(FixedCycleHedgeStrategy, "_build_exit_intents", new=capturing):
            runtime.on_websocket_fill(
                long_cycle_order.exchange_order_id,
                qty=0.25,
                price=99.8,
                cumulative_qty=0.25,
            )

        self.assertTrue(recorded_break_evens)
        recalculated_snapshot, recalculated_break_even = recorded_break_evens[-1]
        expected_realized_loss = runtime.runtime_state.realized_long_pnl_total
        self.assertAlmostEqual(
            recalculated_snapshot.realized_long_pnl_total, expected_realized_loss, places=7
        )

        no_loss_snapshot = HedgeSnapshot(
            symbol=recalculated_snapshot.symbol,
            current_price=recalculated_snapshot.current_price,
            long_qty=recalculated_snapshot.long_qty,
            short_qty=recalculated_snapshot.short_qty,
            long_avg=recalculated_snapshot.long_avg,
            short_avg=recalculated_snapshot.short_avg,
            realized_long_pnl_total=0.0,
            realized_short_pnl_total=recalculated_snapshot.realized_short_pnl_total,
            active_orders=recalculated_snapshot.active_orders,
            source=recalculated_snapshot.source,
            updated_at=recalculated_snapshot.updated_at,
        )
        break_even_without_loss, _ = runtime.strategy._calculate_break_even(no_loss_snapshot, runtime.runtime_state)

        self.assertGreaterEqual(recalculated_break_even, break_even_without_loss)

    def test_on_fill_invalidates_exit_signature_before_rebuilding(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()
        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")

        state = runtime.runtime_state.strategy_state
        sentinel = {"legacy": "signature"}
        state["last_exit_signature"] = sentinel

        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )

        recorded_signatures = []
        original = runtime.strategy._build_exit_intents
        recorded_intents: list[list] = []

        def capturing(
            self,
            snapshot,
            runtime_state,
            current_cycle,
            break_even_price,
            tp_price,
            hard_stop_active,
            context,
            *,
            force_exit_rebuild=False,
            pending_loss_old_signature=None,
        ):
            recorded_signatures.append(runtime_state.strategy_state.get("last_exit_signature"))
            result = original(
                snapshot,
                runtime_state,
                current_cycle,
                break_even_price,
                tp_price,
                hard_stop_active,
                context,
                force_exit_rebuild=force_exit_rebuild,
                pending_loss_old_signature=pending_loss_old_signature,
            )
            recorded_intents.append(result)
            return result

        with patch.object(FixedCycleHedgeStrategy, "_build_exit_intents", new=capturing):
            runtime.on_websocket_fill(
                long_cycle_order.exchange_order_id,
                qty=long_cycle_order.qty,
                price=98.0,
                cumulative_qty=long_cycle_order.qty,
            )

        self.assertTrue(recorded_intents)
        latest_intents = recorded_intents[-1]
        self.assertTrue(any(intent.purpose == "LONG_TP_EXIT" for intent in latest_intents))
        self.assertTrue(any(intent.purpose == "SHORT_SL_EXIT" for intent in latest_intents))
        self.assertNotEqual(state["last_exit_signature"], sentinel)

    def test_exit_guard_blocks_orders_when_price_below_avgs(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=99.0,
            long_qty=1.0,
            short_qty=1.0,
            long_avg=100.0,
            short_avg=100.0,
        )
        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=1,
            break_even_price=100.0,
            tp_price=101.0,
            hard_stop_active=False,
            context=runtime.context,
        )
        self.assertEqual(intents, [])

    def test_cycle_completes_only_after_short_tp(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state.setdefault("cycle_completed_count", 0)
        runtime.runtime_state.strategy_state.setdefault("cycle_waiting_for_short_tp", False)
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_completed_count"), 0)

        state = runtime.runtime_state.strategy_state
        state["cycle_state"] = runtime.strategy._default_cycle_state()
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"1": -0.1},
            "cycle_short_tp_pnl": {"1": 0.2},
            "cycle_pnl_entries": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.1,
        }
        long_fill = FillEvent(
            exchange_order_id="long1",
            client_order_id="long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=0.5,
            exec_price=99.0,
            order_type="Limit",
            reduce_only=False,
            status="FILLED",
            exec_id="exec-long1",
            metadata={"cycle_index": 1, "cycle_role": "long_reduce", "confirmed_closed_pnl": -0.1},
        )
        runtime.runtime_state.strategy_state.setdefault("cycle_completed_count", 0)
        runtime.runtime_state.strategy_state.setdefault("cycle_waiting_for_short_tp", False)
        runtime.strategy._advance_cycle_from_fill(long_fill, runtime.runtime_state)
        self.assertTrue(runtime.runtime_state.strategy_state.get("cycle_waiting_for_short_tp"))
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_completed_count"), 0)
        self.assertEqual(runtime.runtime_state.strategy_state.get("short_tp_pending_cycle"), 1)
        self.assertFalse(runtime.runtime_state.strategy_state.get("long_add_pending"))

        short_fill = FillEvent(
            exchange_order_id="short1",
            client_order_id="short",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=0.5,
            exec_price=100.0,
            order_type="Limit",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-short1",
            metadata={"cycle_index": 1, "cycle_role": "short_reduce", "confirmed_closed_pnl": 0.2},
        )
        runtime.strategy._advance_cycle_from_fill(short_fill, runtime.runtime_state)
        self.assertFalse(runtime.runtime_state.strategy_state.get("cycle_waiting_for_short_tp"))
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_completed_count"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("current_short_cycle_index"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("short_tp_pending_cycle"), 0)

    def test_on_fill_blocks_cycle_short_reduce_until_closed_pnl_confirmed(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["cycle_state"] = runtime.strategy._default_cycle_state()
        state["cycle_completed_count"] = 1
        state["cycle_pair_count"] = 1
        state["cycle_long_add_filled"] = True
        state["cycle_short_tp_filled"] = False
        state["bot_state"] = runtime.strategy.STATE_RUNNING
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"2": -0.1123},
            "cycle_short_tp_pnl": {},
            "cycle_pnl_entries": {
                "cycle_long_reduce:2:cycle-long-2": {
                    "pnl": -0.1123,
                    "source": "confirmed_closed_pnl",
                    "is_confirmed": True,
                }
            },
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": -0.1123,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=99.0,
            short_avg=101.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        fill_event = FillEvent(
            exchange_order_id="cycle-short-2",
            client_order_id="cycle-short-2",
            side="short",
            purpose="CYCLE_2_SHORT_REDUCE",
            exec_qty=28.7,
            exec_price=0.429,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short-2",
            metadata={
                "cycle_index": 2,
                "cycle_role": "short_reduce",
                "runtime_calculated_pnl": 0.4240,
                "exec_pnl": 0.4240,
            },
        )

        with (
            patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock,
            patch.object(runtime.audit, "log_event") as audit_log_mock,
        ):
            intents = runtime.strategy.on_fill(fill_event, snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        ledger = state["audit_pnl_ledger"]
        self.assertNotIn("2", ledger["cycle_short_tp_pnl"])
        self.assertEqual(state.get("cycle_pair_count"), 1)
        self.assertEqual(state.get("cycle_completed_count"), 1)
        self.assertNotEqual(state.get("bot_state"), runtime.strategy.STATE_REFILL_PENDING)
        self.assertFalse(state.get("refill_pending", False))
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_short_reduce_pnl_waiting_for_closed_pnl", event_names)
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_short_reduce_pnl_waiting_for_closed_pnl"
                for call in audit_log_mock.call_args_list
            )
        )

    def test_on_fill_cycle_short_reduce_advances_only_after_closed_pnl_confirmed(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        now = datetime.now(timezone.utc)
        order_manager.closed_pnl_rows = [
            {
                "orderId": "cycle-short-2",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "closedSize": 28.7,
                "avgExitPrice": 0.429,
                "closedPnl": 0.4139141,
                "createdTime": int(now.timestamp() * 1000),
                "updatedTime": int(now.timestamp() * 1000),
            }
        ]
        state = runtime.runtime_state.strategy_state
        state["cycle_state"] = runtime.strategy._default_cycle_state()
        state["cycle_completed_count"] = 1
        state["cycle_pair_count"] = 1
        state["cycle_long_add_filled"] = True
        state["cycle_short_tp_filled"] = False
        state["bot_state"] = runtime.strategy.STATE_RUNNING
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"2": -0.1123},
            "cycle_short_tp_pnl": {},
            "cycle_pnl_entries": {
                "cycle_long_reduce:2:cycle-long-2": {
                    "pnl": -0.1123,
                    "source": "confirmed_closed_pnl",
                    "is_confirmed": True,
                }
            },
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": -0.1123,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=99.0,
            short_avg=101.0,
            source="websocket",
            updated_at=now,
        )
        runtime.runtime_state.last_snapshot = snapshot
        runtime.context.refresh_snapshot = lambda source: snapshot
        fill_event = FillEvent(
            exchange_order_id="cycle-short-2",
            client_order_id="cycle-short-2",
            side="short",
            purpose="CYCLE_2_SHORT_REDUCE",
            exec_qty=28.7,
            exec_price=0.429,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short-2",
            metadata={
                "cycle_index": 2,
                "cycle_role": "short_reduce",
                "runtime_calculated_pnl": 0.4240,
                "exec_pnl": 0.4240,
            },
            occurred_at=now,
        )

        with (
            patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock,
            patch.object(runtime.strategy, "_build_entry_intents", return_value=[]),
            patch.object(runtime.strategy, "_fast_path_second_order", return_value=[]),
            patch.object(runtime.strategy, "_rebuild_structure", return_value=[]),
        ):
            runtime.strategy.on_fill(fill_event, snapshot, runtime.runtime_state, runtime.context)

        ledger = state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_short_tp_pnl"]["2"], 0.4139141, places=7)
        self.assertEqual(state.get("cycle_pair_count"), 2)
        self.assertEqual(state.get("cycle_completed_count"), 2)
        self.assertEqual(state.get("bot_state"), runtime.strategy.STATE_REFILL_PENDING)
        self.assertTrue(state.get("refill_pending"))
        self.assertFalse(state.get("refill_in_progress"))
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_short_reduce_pnl_confirmed", event_names)
        self.assertIn("fixed_cycle_refill_triggered", event_names)

    def test_on_tick_builds_refill_intents_when_pending_without_refill_state(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["bot_state"] = runtime.strategy.STATE_REFILL_PENDING
        state["refill_pending"] = True
        state["refill_in_progress"] = False
        state["refill_state"] = {}
        state["initial_long_qty"] = 1.0
        state["initial_short_qty"] = 0.5
        state["cycle_completed_count"] = 2
        state["cycle_pair_count"] = 2
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.5,
            short_qty=0.25,
            long_avg=100.0,
            short_avg=100.0,
            source="tick",
        )

        intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual({intent.purpose for intent in intents}, {"REFILL_LONG", "REFILL_SHORT"})
        self.assertTrue(state.get("refill_in_progress"))
        self.assertEqual(state.get("refill_state", {}).get("expected_purposes"), ["REFILL_LONG", "REFILL_SHORT"])

    def test_on_tick_resets_stale_refill_flags_and_rebuilds_refill(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["bot_state"] = runtime.strategy.STATE_REFILL_PENDING
        state["refill_pending"] = True
        state["refill_in_progress"] = True
        state["refill_state"] = {
            "REQUESTED": True,
            "expected_purposes": ["REFILL_LONG", "REFILL_SHORT"],
            "created_at_ms": 123,
        }
        state["initial_long_qty"] = 1.0
        state["initial_short_qty"] = 0.5
        state["cycle_completed_count"] = 2
        state["cycle_pair_count"] = 2
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.5,
            short_qty=0.25,
            long_avg=100.0,
            short_avg=100.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event") as warning_log:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual({intent.purpose for intent in intents}, {"REFILL_LONG", "REFILL_SHORT"})
        self.assertTrue(state.get("refill_in_progress"))
        self.assertEqual(state.get("refill_state", {}).get("expected_purposes"), ["REFILL_LONG", "REFILL_SHORT"])
        warning_names = [call.args[0] for call in warning_log.call_args_list if call.args]
        self.assertIn("fixed_cycle_refill_stale_state_reset", warning_names)

    def test_rebuild_structure_skips_exit_fallback_while_refill_pending(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["bot_state"] = runtime.strategy.STATE_REFILL_PENDING
        state["refill_pending"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=100.0,
            short_avg=100.0,
            source="tick",
        )

        with (
            patch.object(runtime.strategy, "_build_downside_cycle_intents", return_value=[]),
            patch.object(runtime.strategy, "_build_exit_intents", return_value=[]) as build_exit_mock,
        ):
            intents = runtime.strategy._rebuild_structure(snapshot, runtime.runtime_state, runtime.context, reason="test_refill_pending")

        self.assertEqual(intents, [])
        build_exit_mock.assert_not_called()

    def test_rebuild_structure_defers_exit_until_second_pair_short_reduce_completes(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["bot_state"] = runtime.strategy.STATE_RUNNING
        state["initial_entry_confirmed"] = True
        state["refill_pending"] = False
        state["refill_in_progress"] = False
        state["cycle_pair_count"] = 1
        state["cycle_completed_count"] = 1
        state["current_long_cycle_index"] = 2
        state["current_short_cycle_index"] = 1
        state["pending_long_cycle_index"] = 2
        state["short_tp_pending_cycle"] = 2
        state["cycle_waiting_for_short_tp"] = True
        state["cycle_long_add_filled"] = True
        state["pending_cycle_loss_usdt"] = 1.2345
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=100.0,
            short_avg=100.0,
            source="tick",
        )
        expected_downside = [
            StrategyIntent(
                side="short",
                qty=0.5,
                purpose=runtime.strategy._cycle_purpose("short", 2),
            )
        ]

        with (
            patch.object(runtime.strategy, "_build_downside_cycle_intents", return_value=expected_downside),
            patch.object(runtime.strategy, "_build_exit_intents", return_value=[]) as build_exit_mock,
            patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock,
        ):
            intents = runtime.strategy._rebuild_structure(
                snapshot,
                runtime.runtime_state,
                runtime.context,
                reason="test_pending_second_pair_short_reduce",
            )

        self.assertEqual(intents, expected_downside)
        build_exit_mock.assert_not_called()
        self.assertIn(
            "fixed_cycle_exit_deferred_pending_second_pair_short_reduce",
            [call.args[0] for call in log_event_mock.call_args_list if call.args],
        )

    def test_on_order_update_clears_stale_refill_flags_for_terminal_refill_order(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["bot_state"] = runtime.strategy.STATE_REFILL_PENDING
        state["refill_pending"] = True
        state["refill_in_progress"] = True
        state["refill_state"] = {
            "REQUESTED": True,
            "expected_purposes": ["REFILL_LONG"],
            "created_at_ms": 123,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.5,
            short_qty=0.25,
            long_avg=100.0,
            short_avg=100.0,
            source="websocket",
        )

        runtime.strategy.on_order_update(
            {
                "orderStatus": "Rejected",
                "orderLinkId": "fixed_cycle-refill_long-test",
                "orderId": "ex-refill-long-test",
            },
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertFalse(state.get("refill_in_progress"))
        self.assertEqual(state.get("refill_state"), {})

    def test_exchange_cancelled_short_exit_triggers_immediate_close(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.0,
            short_qty=1.0,
            long_avg=100.0,
            short_avg=102.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-short_sl_exit-test",
            "orderId": "cancelled-short",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertEqual(intent.purpose, runtime.strategy.EMERGENCY_FLAT_SHORT_PURPOSE)
        self.assertEqual(intent.qty, snapshot.short_qty)
        self.assertTrue(runtime.runtime_state.strategy_state.get("emergency_flat_required"))
        self.assertEqual(runtime.runtime_state.strategy_state.get("emergency_exit_attempts"), 1)

    def test_ec_no_immediate_qty_to_fill_triggers_emergency_exit(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=2.0,
            short_qty=0.0,
            long_avg=99.5,
            short_avg=0.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Rejected",
            "orderLinkId": "fixed_cycle-long_tp_exit-test",
            "orderId": "rejected-long",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertEqual(intent.purpose, runtime.strategy.EMERGENCY_FLAT_LONG_PURPOSE)
        self.assertEqual(intent.qty, snapshot.long_qty)
        self.assertTrue(runtime.runtime_state.strategy_state.get("emergency_flat_required"))

    def test_long_exit_filled_short_exit_cancelled_closes_remaining_short(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=99.0,
            long_qty=0.0,
            short_qty=0.75,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Canceled",
            "orderLinkId": "fixed_cycle-short_sl_exit-other",
            "orderId": "cancelled-short-two",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_SHORT_PURPOSE)
        self.assertEqual(intents[0].qty, snapshot.short_qty)

    def test_fresh_restart_blocked_until_emergency_flat(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        state = runtime.runtime_state.strategy_state
        state["emergency_flat_required"] = True
        state["emergency_exit_attempts"] = 2
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="rest",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertIn(
            "fixed_cycle_fresh_entry_blocked_emergency_flat",
            [call.args[0] for call in log_event.call_args_list if call.args],
        )

    def test_emergency_market_close_has_no_trigger_fields(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.5,
            long_qty=1.0,
            short_qty=0.0,
            long_avg=99.0,
            short_avg=0.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-long_tp_exit-test",
            "orderId": "emergency-trigger",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertFalse(intent.close_on_trigger)
        self.assertIsNone(intent.trigger_price)
        self.assertIsNone(intent.trigger_direction)

    def test_emergency_close_qty_is_normalized_or_uses_safe_recovery_qty(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        strategy = runtime.strategy
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.5,
            long_qty=Decimal("1.2345"),
            short_qty=0.0,
            long_avg=99.0,
            short_avg=0.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-long_tp_exit-test-qty",
            "orderId": "emergency-qty",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        intents = strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        normalized_expected = strategy._safe_recovery_qty(snapshot.long_qty)
        self.assertEqual(intents[0].qty, normalized_expected)

    def test_duplicate_cancel_does_not_submit_duplicate_emergency_close(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.5,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-short_sl_exit-test-dupe",
            "orderId": "emergency-dupe",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        first_intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )
        second_intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(first_intents), 1)
        self.assertEqual(len(second_intents), 0)

    def test_reconciled_cancelled_short_exit_triggers_emergency_close(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.0,
            short_qty=0.75,
            long_avg=0.0,
            short_avg=100.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        order = ManagedOrder(
            client_order_id="fixed_cycle-short_sl_exit-reconcile",
            side="short",
            qty=0.75,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-reconcile-short",
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.75,
        )
        runtime.runtime_state.active_orders[order.client_order_id] = order
        history_order = {
            "orderId": order.exchange_order_id,
            "orderLinkId": order.client_order_id,
            "orderStatus": "Cancelled",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        with patch.object(runtime, "_dispatch") as dispatch_mock:
            runtime._dispatch_reconcile_terminal_cancel(
                order.client_order_id,
                order,
                history_order,
                "CANCELLED",
            )

        state = runtime.runtime_state.strategy_state
        self.assertTrue(state.get("emergency_flat_required"))
        dispatched_intents = dispatch_mock.call_args[0][1]
        self.assertEqual(len(dispatched_intents), 1)
        self.assertEqual(dispatched_intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_SHORT_PURPOSE)

    def test_final_exit_missing_opposite_short_exit_closes_short_immediately(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=102.0,
            long_qty=0.0,
            short_qty=0.6,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        state = runtime.runtime_state.strategy_state
        state["long_exit_filled"] = True
        state["short_exit_filled"] = False

        runtime.strategy._maybe_finalize_exit_after_leg_fill(
            runtime.runtime_state,
            runtime.context,
            runtime.strategy.LONG_TP_EXIT_PURPOSE,
        )

        self.assertTrue(state.get("emergency_flat_required"))
        intents = runtime.strategy._maybe_handle_emergency_exit_tick(
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )
        self.assertTrue(intents)

    def test_long_exit_filled_short_exit_reconcile_cancel_does_not_wait_for_ws(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.0,
            short_qty=0.5,
            long_avg=0.0,
            short_avg=100.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        order = ManagedOrder(
            client_order_id="fixed_cycle-short_sl_exit-reconcile-2",
            side="short",
            qty=0.5,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-reconcile-short-2",
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.5,
        )
        runtime.runtime_state.active_orders[order.client_order_id] = order
        history_order = {
            "orderId": order.exchange_order_id,
            "orderLinkId": order.client_order_id,
            "orderStatus": "Cancelled",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        with patch.object(runtime, "_dispatch") as dispatch_mock:
            runtime._dispatch_reconcile_terminal_cancel(
                order.client_order_id,
                order,
                history_order,
                "CANCELLED",
            )

        self.assertTrue(dispatch_mock.called)
        self.assertTrue(runtime.runtime_state.strategy_state.get("emergency_flat_required"))

    def test_reconcile_cancel_path_does_not_wait_for_on_tick(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.0,
            short_qty=0.5,
            long_avg=0.0,
            short_avg=100.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        order = ManagedOrder(
            client_order_id="fixed_cycle-short_sl_exit-reconcile-3",
            side="short",
            qty=0.5,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-reconcile-short-3",
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.5,
        )
        runtime.runtime_state.active_orders[order.client_order_id] = order
        history_order = {
            "orderId": order.exchange_order_id,
            "orderLinkId": order.client_order_id,
            "orderStatus": "Cancelled",
            "rejectReason": "EC_NoImmediateQtyToFill",
        }

        runtime._dispatch_reconcile_terminal_cancel(
            order.client_order_id,
            order,
            history_order,
            "CANCELLED",
        )

        state = runtime.runtime_state.strategy_state
        self.assertTrue(state.get("emergency_flat_required"))
        # Ensure duplicate events don't emit new intents (already handled)
        intents = runtime.strategy.on_order_update(
            history_order,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )
        self.assertEqual(intents, [])

    def test_manual_deactivated_short_exit_triggers_emergency_close(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.0,
            short_qty=0.5,
            long_avg=0.0,
            short_avg=100.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Deactivated",
            "orderLinkId": "fixed_cycle-short_sl_exit-test-manual",
            "orderId": "manual-short",
            "cancelType": "CancelByUser",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_SHORT_PURPOSE)

    def test_manual_deactivated_long_exit_triggers_emergency_close(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.25,
            short_qty=0.0,
            long_avg=99.5,
            short_avg=0.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Deactivated",
            "orderLinkId": "fixed_cycle-long_tp_exit-test-manual",
            "orderId": "manual-long",
            "cancelType": "CancelByUser",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_LONG_PURPOSE)

    def test_deactivated_exit_order_does_not_crash_source_order_link_id(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.5,
            short_qty=0.0,
            long_avg=100.0,
            short_avg=0.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Deactivated",
            "orderLinkId": "fixed_cycle-long_tp_exit-test-manual",
            "orderId": "manual-long",
            "cancelType": "CancelByUser",
            "metadata": {"order_link_id": "manual-link"},
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_LONG_PURPOSE)

    def test_deactivated_is_terminal_for_exit_purposes(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.75,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        for purpose in [
            runtime.strategy.LONG_TP_EXIT_PURPOSE,
            runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            runtime.strategy.LONG_TP_EXIT_RECOVERY_PURPOSE,
            runtime.strategy.SHORT_SL_EXIT_RECOVERY_PURPOSE,
        ]:
            payload = {
                "orderStatus": "Deactivated",
                "orderLinkId": f"fixed_cycle-{purpose.lower()}",
                "orderId": f"manual-{purpose.lower()}",
                "cancelType": "CancelByUser",
            }
            intents = runtime.strategy.on_order_update(
                payload,
                snapshot,
                runtime.runtime_state,
                runtime.context,
            )
            self.assertTrue(intents)

    def test_expected_rebuild_cancel_does_not_trigger_emergency(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-short_sl_exit-expected",
                "exchange_order_id": "expected-exchange-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": 1.0,
                "expires_at_monotonic": time.monotonic() + 2.0,
                "consumed": False,
            }
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.4,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-short_sl_exit-expected",
            "orderId": "expected-exchange-id",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(intents, [])
        self.assertFalse(runtime.runtime_state.strategy_state.get("emergency_flat_required"))
        self.assertTrue(runtime.runtime_state.strategy_state["expected_exit_cancels"][0]["consumed"])

    def test_bybit_cancel_without_expected_cancel_triggers_emergency(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.4,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-short_sl_exit-bybit",
            "orderId": "bybit-exchange-id",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_SHORT_PURPOSE)

    def test_expected_cancel_replacement_confirmed_clears_guard(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        now = time.monotonic()
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-short_sl_exit-old",
                "exchange_order_id": "old-exchange-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": now,
                "expires_at_monotonic": now + 2.0,
                "consumed": True,
            }
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.4,
            long_avg=0.0,
            short_avg=101.0,
            source="tick",
        )
        intent = StrategyIntent(
            side="short",
            qty=0.4,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            order_type="Market",
            reduce_only=True,
            position_idx=2,
        )

        runtime.submit_intent(intent, snapshot, source="tick")

        self.assertEqual(runtime.runtime_state.strategy_state.get("expected_exit_cancels"), [])

    def test_expected_cancel_replacement_prefers_consumed_entry(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        now = time.monotonic()
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-short_sl_exit-unconsumed",
                "exchange_order_id": "unconsumed-exchange-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": now,
                "expires_at_monotonic": now + 10.0,
                "consumed": False,
            },
            {
                "client_order_id": "fixed_cycle-short_sl_exit-consumed",
                "exchange_order_id": "consumed-exchange-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": now,
                "expires_at_monotonic": now + 10.0,
                "consumed": True,
            },
        ]
        intent = StrategyIntent(
            side="short",
            qty=0.4,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            order_type="Market",
            reduce_only=True,
            position_idx=2,
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._confirm_expected_exit_cancel_replacement(
                intent,
                client_id="replacement-client-id",
                exchange_order_id="replacement-exchange-id",
            )

        remaining = runtime.runtime_state.strategy_state.get("expected_exit_cancels")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["client_order_id"], "fixed_cycle-short_sl_exit-unconsumed")
        confirmation_calls = [
            call for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_expected_cancel_replacement_confirmed"
        ]
        self.assertEqual(len(confirmation_calls), 1)
        self.assertTrue(confirmation_calls[0].kwargs["confirmed_entry_consumed"])

    def test_expected_cancel_replacement_missing_triggers_emergency_after_timeout(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        now = time.monotonic()
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-short_sl_exit-old",
                "exchange_order_id": "old-exchange-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": now - 5.0,
                "expires_at_monotonic": now - 1.0,
                "consumed": True,
            }
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.4,
            long_avg=0.0,
            short_avg=101.0,
            source="tick",
        )

        intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertTrue(runtime.runtime_state.strategy_state.get("emergency_flat_required"))
        self.assertTrue(intents)

    def test_reconcile_cancel_respects_expected_cancel_guard(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        now = time.monotonic()
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-short_sl_exit-reconcile-guard",
                "exchange_order_id": "reconcile-guard-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": now,
                "expires_at_monotonic": now + 2.0,
                "consumed": False,
            }
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=0.0,
            short_qty=0.75,
            long_avg=0.0,
            short_avg=100.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        order = ManagedOrder(
            client_order_id="fixed_cycle-short_sl_exit-reconcile-guard",
            side="short",
            qty=0.75,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="reconcile-guard-id",
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.75,
        )
        history_order = {
            "orderId": "reconcile-guard-id",
            "orderLinkId": "fixed_cycle-short_sl_exit-reconcile-guard",
            "orderStatus": "Cancelled",
        }

        with patch.object(runtime, "_dispatch") as dispatch_mock:
            runtime._dispatch_reconcile_terminal_cancel(
                order.client_order_id,
                order,
                history_order,
                "CANCELLED",
            )

        dispatched_intents = dispatch_mock.call_args[0][1]
        self.assertEqual(dispatched_intents, [])
        self.assertFalse(runtime.runtime_state.strategy_state.get("emergency_flat_required"))

    def test_cycle_fill_exit_rebuild_does_not_emergency_flat(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-long_tp_exit-old",
                "exchange_order_id": "long-tp-old-id",
                "purpose": runtime.strategy.LONG_TP_EXIT_PURPOSE,
                "reason": "replace_open_purpose",
                "replacement_purpose": runtime.strategy.LONG_TP_EXIT_PURPOSE,
                "created_at_monotonic": time.monotonic(),
                "expires_at_monotonic": time.monotonic() + 2.0,
                "consumed": False,
            }
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.3,
            short_qty=0.3,
            long_avg=99.0,
            short_avg=101.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Deactivated",
            "orderLinkId": "fixed_cycle-long_tp_exit-old",
            "orderId": "long-tp-old-id",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(intents, [])
        self.assertFalse(runtime.runtime_state.strategy_state.get("emergency_flat_required"))

    def test_expected_cancel_guard_does_not_suppress_wrong_order_id(self) -> None:
        runtime = self.build_runtime(FakeOrderManager())
        runtime.runtime_state.strategy_state["expected_exit_cancels"] = [
            {
                "client_order_id": "fixed_cycle-short_sl_exit-expected",
                "exchange_order_id": "expected-exchange-id",
                "purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "reason": "exit_rebuild",
                "replacement_purpose": runtime.strategy.SHORT_SL_EXIT_PURPOSE,
                "created_at_monotonic": time.monotonic(),
                "expires_at_monotonic": time.monotonic() + 2.0,
                "consumed": False,
            }
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.4,
            long_avg=0.0,
            short_avg=101.0,
            source="websocket",
        )
        payload = {
            "orderStatus": "Cancelled",
            "orderLinkId": "fixed_cycle-short_sl_exit-other",
            "orderId": "other-exchange-id",
        }

        intents = runtime.strategy.on_order_update(
            payload,
            snapshot,
            runtime.runtime_state,
            runtime.context,
        )

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].purpose, runtime.strategy.EMERGENCY_FLAT_SHORT_PURPOSE)

    def test_complete_refill_clears_pending_cycle_loss_context(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["refill_pending"] = True
        state["refill_in_progress"] = True
        state["refill_state"] = {
            "REQUESTED": True,
            "expected_purposes": ["REFILL_LONG", "REFILL_SHORT"],
            "created_at_ms": 123,
        }
        state["bot_state"] = runtime.strategy.STATE_REFILL_PENDING
        state["cycle_completed_count"] = 2
        state["cycle_pair_count"] = 2
        state["pending_cycle_loss_usdt"] = 1.2345
        state["pending_short_cycle_index"] = 2

        with patch.object(runtime.context.audit, "log_event") as log_event_mock:
            runtime.strategy._complete_refill(runtime.runtime_state, runtime.context)

        self.assertEqual(state.get("pending_cycle_loss_usdt"), 0.0)
        self.assertEqual(state.get("pending_short_cycle_index"), 0)
        self.assertFalse(state.get("refill_pending"))
        self.assertFalse(state.get("refill_in_progress"))
        self.assertEqual(state.get("refill_state"), {})
        self.assertEqual(state.get("bot_state"), runtime.strategy.STATE_RUNNING)
        self.assertEqual(state.get("cycle_completed_count"), 0)
        self.assertEqual(state.get("cycle_pair_count"), 0)

    def test_complete_refill_clears_short_tp_fallback_state(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["refill_pending"] = True
        state["refill_in_progress"] = True
        state["refill_state"] = {
            "REQUESTED": True,
            "expected_purposes": ["REFILL_LONG", "REFILL_SHORT"],
            "created_at_ms": 123,
        }
        state["bot_state"] = runtime.strategy.STATE_REFILL_PENDING
        state["cycle_completed_count"] = 2
        state["cycle_pair_count"] = 2
        state["pending_cycle_loss_usdt"] = 1.2345
        state["short_tp_fallback_state"] = {
            "active": True,
            "original_trigger_price": 99.5,
            "qty": 0.25,
            "client_order_id": "fallback-order-1",
        }
        state["short_tp_fallback_order_context"] = {
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "cycle_index": 1,
        }
        state["last_short_tp_trigger_price"] = 98.5
        state["last_expected_short_tp_net"] = 12.34
        state["last_short_tp_qty"] = 0.25
        state["force_short_tp_rebuild"] = True
        state["pending_loss_updated_in_fill"] = True
        state["pending_loss_exit_old_signature"] = {"foo": "bar"}
        state["pending_loss_exit_rebuild_reason"] = "test_reason"

        with patch.object(runtime.context.audit, "log_event") as log_event_mock:
            runtime.strategy._complete_refill(runtime.runtime_state, runtime.context)

        self.assertIsNone(state.get("short_tp_fallback_state"))
        self.assertIsNone(state.get("short_tp_fallback_order_context"))
        self.assertEqual(state.get("last_short_tp_trigger_price"), 0.0)
        self.assertEqual(state.get("last_expected_short_tp_net"), 0.0)
        self.assertEqual(state.get("last_short_tp_qty"), 0.0)
        self.assertFalse(state.get("force_short_tp_rebuild"))
        self.assertFalse(state.get("pending_loss_updated_in_fill"))
        self.assertIsNone(state.get("pending_loss_exit_old_signature"))
        self.assertIsNone(state.get("pending_loss_exit_rebuild_reason"))

        fallback_reset_events = [
            call.kwargs
            for call in log_event_mock.call_args_list
            if call.args
            and call.args[0] == "fixed_cycle_refill_short_tp_fallback_state_cleared"
        ]
        self.assertEqual(len(fallback_reset_events), 1)
        self.assertEqual(fallback_reset_events[0]["symbol"], runtime.config.symbol)
        self.assertEqual(fallback_reset_events[0]["cycle_pair_count"], 0)
        self.assertEqual(fallback_reset_events[0]["cycle_completed_count"], 0)
        self.assertEqual(
            fallback_reset_events[0]["short_tp_fallback_state_before"],
            {
                "active": True,
                "original_trigger_price": 99.5,
                "qty": 0.25,
                "client_order_id": "fallback-order-1",
            },
        )
        self.assertTrue(
            fallback_reset_events[0]["short_tp_fallback_order_context_present_before"]
        )
        self.assertEqual(fallback_reset_events[0]["pending_cycle_loss_usdt"], 0.0)
        self.assertEqual(
            fallback_reset_events[0]["bot_state"], runtime.strategy.STATE_RUNNING
        )

    def test_on_fill_blocks_cycle_long_add_until_closed_pnl_confirmed(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["cycle_state"] = runtime.strategy._default_cycle_state()
        state["pending_cycle_loss_usdt"] = 0.0
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=1.0,
            short_qty=0.5,
            long_avg=99.0,
            short_avg=101.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        fill_event = FillEvent(
            exchange_order_id="cycle-long-2",
            client_order_id="cycle-long-2",
            side="long",
            purpose="CYCLE_2_LONG_ADD",
            exec_qty=10.0,
            exec_price=99.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-long-2",
            metadata={
                "cycle_index": 2,
                "cycle_role": "long_reduce",
                "runtime_calculated_pnl": -0.2123,
                "exec_pnl": -0.2123,
            },
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            runtime.strategy.on_fill(fill_event, snapshot, runtime.runtime_state, runtime.context)

        ledger = state["audit_pnl_ledger"]
        self.assertNotIn("2", ledger["cycle_long_reduce_pnl"])
        self.assertEqual(float(state.get("pending_cycle_loss_usdt") or 0.0), 0.0)
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_long_reduce_pnl_waiting_for_closed_pnl", event_names)

    def test_max_cycles_counts_pairs(self) -> None:
        order_manager = FakeOrderManager()
        config = FixedCycleHedgeConfig(
            symbol="BTCUSDT",
            category="linear",
            rest_poll_after_fill_ms=0,
            order_refresh_cooldown_ms=0,
            max_cycles=1,
            price_tick_size=0.1,
            qty_step=0.001,
        )
        runtime = self.build_runtime(order_manager, config)
        state = runtime.runtime_state.strategy_state
        state["cycle_state"] = runtime.strategy._default_cycle_state()
        long_fill = FillEvent(
            exchange_order_id="long1",
            client_order_id="long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=0.5,
            exec_price=99.0,
            order_type="Limit",
            reduce_only=False,
            status="FILLED",
            exec_id="exec-long1",
            metadata={"cycle_index": 1},
        )
        short_fill = FillEvent(
            exchange_order_id="short1",
            client_order_id="short",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=0.5,
            exec_price=100.0,
            order_type="Limit",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-short1",
            metadata={"cycle_index": 1},
        )
        runtime.strategy._advance_cycle_from_fill(long_fill, runtime.runtime_state)
        runtime.strategy._advance_cycle_from_fill(short_fill, runtime.runtime_state)

    def test_state_json_isolation_ignores_persisted_cycle_state(self) -> None:
        state_path = Path(__file__).resolve().parent / "state.json"
        state_path.write_text('{"long_cycle_index": 5, "short_cycle_index": 3}', encoding="utf-8")
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        runtime = self.build_runtime(order_manager)

        runtime.bootstrap()
        self._ensure_cycle_order(runtime, purpose="CYCLE_1_LONG_ADD", status="OPEN")

        purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
        self.assertIn("CYCLE_1_LONG_ADD", purposes)
        if state_path.exists():
            state_path.unlink()

    def test_on_tick_blocks_flat_restart_until_final_pnl_ready(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        state["final_trade_pnl_audited"] = False
        state["last_trade_pnl_complete"] = False
        state["last_trade_pnl_usdt"] = None
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        with (
            patch.object(
                runtime.strategy,
                "_emit_final_trade_pnl_if_complete_or_fetch",
                return_value=False,
            ) as emit_mock,
            patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock,
        ):
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(emit_mock.called)
        self.assertTrue(state["fresh_restart_required"])
        self.assertEqual(state["bot_state"], runtime.strategy.STATE_EXITED)
        self.assertTrue(
            any(call.args and call.args[0] == "fixed_cycle_flat_waiting_for_final_pnl" for call in log_event_mock.call_args_list)
        )

    def test_on_tick_allows_restart_when_final_pnl_ready(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["initial_entry_confirmed"] = False
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 1.23
        state["trade_block_id"] = "trade-1"
        state["last_trade_block_id"] = "trade-1"
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.87,
            "final_short_exit_pnl": -0.69,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertFalse(state["fresh_restart_required"])
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)

    def test_on_tick_forces_final_exit_cleanup_and_allows_restart_when_only_final_exit_orders_remain(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["initial_entry_confirmed"] = False
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 1.23
        state["trade_block_id"] = "trade-cleanup"
        state["last_trade_block_id"] = "trade-cleanup"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.87,
            "final_short_exit_pnl": -0.69,
            "total_realized_pnl": 0.0,
        }
        runtime.runtime_state.active_orders["long-exit"] = ManagedOrder(
            client_order_id="long-exit",
            exchange_order_id="ex-long-exit",
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.54,
            remaining_qty=0.46,
            metadata={},
        )
        runtime.runtime_state.active_orders["short-exit"] = ManagedOrder(
            client_order_id="short-exit",
            exchange_order_id="ex-short-exit",
            side="short",
            qty=0.5,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.5,
            metadata={},
        )
        runtime.runtime_state.exchange_to_client_id["ex-long-exit"] = "long-exit"
        runtime.runtime_state.exchange_to_client_id["ex-short-exit"] = "short-exit"
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertIn(runtime.strategy.LONG_ENTRY_PURPOSE, purposes)
        self.assertIn(runtime.strategy.SHORT_ENTRY_PURPOSE, purposes)
        self.assertFalse(state["fresh_restart_required"])
        self.assertEqual(runtime.runtime_state.active_orders, {})
        self.assertEqual(len(order_manager.cancel_calls), 2)
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_flat_final_exit_order_cleanup_forced"
                for call in log_event_mock.call_args_list
            )
        )

    def test_on_tick_keeps_restart_blocked_when_cycle_order_remains_active(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["initial_entry_confirmed"] = False
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 1.23
        state["trade_block_id"] = "trade-blocked"
        state["last_trade_block_id"] = "trade-blocked"
        runtime.runtime_state.active_orders["cycle-order"] = ManagedOrder(
            client_order_id="cycle-order",
            exchange_order_id="ex-cycle-order",
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            price=99.0,
            order_type="Limit",
            reduce_only=False,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=1.0,
            metadata={},
        )
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(state["fresh_restart_required"])
        self.assertIn("cycle-order", runtime.runtime_state.active_orders)
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_flat_waiting_for_order_cleanup"
                for call in log_event_mock.call_args_list
            )
        )

    def test_on_tick_true_fresh_start_does_not_wait_for_final_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = False
        state["fresh_restart_required"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertFalse(state["fresh_restart_required"])
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)

    def test_build_exit_intents_safely_unlocks_when_open_positions_unprotected(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        state["exit_locked"] = True
        state["exit_rebuild_allowed"] = False
        state["long_exit_filled"] = True
        state["short_exit_filled"] = True
        state["force_exit_rebuild"] = False
        snapshot = HedgeSnapshot(
            symbol="DASHUSDT",
            current_price=48.1,
            long_qty=2.11,
            short_qty=1.05,
            long_avg=47.42,
            short_avg=47.43,
            source="websocket",
        )

        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=1,
            break_even_price=47.41,
            tp_price=49.47,
            hard_stop_active=False,
            context=runtime.context,
        )

        purposes = {intent.purpose for intent in intents}
        self.assertIn("LONG_TP_EXIT", purposes)
        self.assertIn("SHORT_SL_EXIT", purposes)
        self.assertFalse(runtime.runtime_state.strategy_state["exit_locked"])
        self.assertFalse(runtime.runtime_state.strategy_state["exit_rebuild_allowed"])
        self.assertTrue(runtime.runtime_state.strategy_state["force_exit_rebuild"])
        self.assertFalse(runtime.runtime_state.strategy_state["long_exit_filled"])
        self.assertFalse(runtime.runtime_state.strategy_state["short_exit_filled"])

    def test_build_exit_intents_does_not_unlock_when_active_final_exit_orders_present(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        state["exit_locked"] = True
        state["exit_rebuild_allowed"] = False
        state["long_exit_filled"] = True
        state["short_exit_filled"] = True
        snapshot = HedgeSnapshot(
            symbol="DASHUSDT",
            current_price=48.1,
            long_qty=2.11,
            short_qty=1.05,
            long_avg=47.42,
            short_avg=47.43,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="long-exit",
                    exchange_order_id="ex-long",
                    side="long",
                    qty=2.11,
                    price=49.47,
                    purpose="LONG_TP_EXIT",
                    order_type="Market",
                    reduce_only=True,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=2.11,
                ),
                ActiveOrderSnapshot(
                    client_order_id="short-exit",
                    exchange_order_id="ex-short",
                    side="short",
                    qty=1.05,
                    price=49.47,
                    purpose="SHORT_SL_EXIT",
                    order_type="Market",
                    reduce_only=True,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.05,
                ),
            ),
            source="websocket",
        )

        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=1,
            break_even_price=47.41,
            tp_price=49.47,
            hard_stop_active=False,
            context=runtime.context,
        )

        self.assertEqual(intents, [])
        self.assertTrue(runtime.runtime_state.strategy_state["exit_locked"])
        self.assertFalse(runtime.runtime_state.strategy_state.get("force_exit_rebuild", False))

    def test_open_position_entry_guard_blocks_initial_when_non_refill(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["refill_pending"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.5,
            short_qty=0.5,
            long_avg=95.0,
            short_avg=95.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event") as warn_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_initial_entry_blocked_open_position"
                for call in warn_mock.call_args_list
            )
        )

    def test_unmatched_short_sl_exit_fill_recovers_final_short_exit_context(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.06177,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="websocket",
        )
        order_link_id = "fixed_cycle-short_sl_exit-test"
        order_manager.closed_pnl_rows = [
            {
                "orderId": "ex-short",
                "orderLinkId": order_link_id,
                "closedPnl": -0.2337,
                "closedSize": 90,
                "avgExitPrice": 0.06177,
                "symbol": "PNUTUSDT",
                "side": "Buy",
                "createdTime": 1,
                "updatedTime": 2,
            }
        ]
        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime.on_websocket_fill(
                exchange_order_id="ex-short-id",
                qty=90,
                price=0.06177,
                exec_id="exec-short",
                order_link_id=order_link_id,
            )
            runtime.strategy._ensure_final_exit_pnl_from_exchange(
                runtime.runtime_state, runtime.context, "test_unmatched_short_sl_exit"
            )

        self.assertTrue(state.get("final_short_exit_audited"))
        self.assertTrue(state.get("final_short_exit_order_context"))
        self.assertEqual(
            state.get("final_short_exit_order_context", {}).get("client_order_id"),
            order_link_id,
        )
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_unmatched_fill_recovered"
                for call in log_event_mock.call_args_list
            )
        )

    def test_unmatched_long_tp_exit_fill_recovers_final_long_exit_context(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.06177,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="websocket",
        )
        order_link_id = "fixed_cycle-long_tp_exit-test"
        order_manager.closed_pnl_rows = [
            {
                "orderId": "ex-long",
                "orderLinkId": order_link_id,
                "closedPnl": 0.1615,
                "closedSize": 95,
                "avgExitPrice": 0.0616,
                "symbol": "PNUTUSDT",
                "side": "Sell",
                "createdTime": 3,
                "updatedTime": 4,
            }
        ]
        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime.on_websocket_fill(
                exchange_order_id="ex-long-id",
                qty=95,
                price=0.0616,
                exec_id="exec-long",
                order_link_id=order_link_id,
            )
            runtime.strategy._ensure_final_exit_pnl_from_exchange(
                runtime.runtime_state, runtime.context, "test_unmatched_long_tp_exit"
            )

        self.assertTrue(state.get("final_long_exit_audited"))
        self.assertTrue(state.get("final_long_exit_order_context"))
        self.assertEqual(
            state.get("final_long_exit_order_context", {}).get("client_order_id"),
            order_link_id,
        )
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_unmatched_fill_recovered"
                for call in log_event_mock.call_args_list
            )
        )

    def test_unmatched_cycle_long_add_fill_does_not_record_unconfirmed_cycle_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.061,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="websocket",
        )
        with (
            patch.object(runtime.audit, "log_event") as log_event_mock,
            patch.object(runtime.strategy, "_rebuild_structure", return_value=[]),
        ):
            runtime.on_websocket_fill(
                exchange_order_id="ex-cycle",
                qty=50,
                price=0.0615,
                exec_id="exec-cycle",
                order_link_id="fixed_cycle-cycle_1_long_add-test",
            )

        ledger = state.get("audit_pnl_ledger") or {}
        self.assertNotIn("1", ledger.get("cycle_long_reduce_pnl", {}))
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_unmatched_fill_recovered"
                for call in log_event_mock.call_args_list
            )
        )

    def test_runtime_attaches_exec_pnl_for_reduce_only_cycle_long_fill(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=99.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        runtime.runtime_state.active_orders["cycle-long-fill"] = ManagedOrder(
            client_order_id="cycle-long-fill",
            exchange_order_id="ex-cycle-long-fill",
            side="long",
            qty=10.0,
            purpose="CYCLE_1_LONG_ADD",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=10.0,
            metadata={"cycle_index": 1, "cycle_role": "long_reduce", "entry_price": 100.0},
        )
        entry_price = 100.0
        exit_price = 99.0
        qty = 10.0
        fee_rate = runtime.strategy.config.order_fee_rate_pct / 100.0
        expected_pnl = calculate_pnl(entry_price, exit_price, qty, "long")
        entry_fee = abs(entry_price * qty) * fee_rate
        exit_fee = abs(exit_price * qty) * fee_rate
        expected_pnl -= entry_fee + exit_fee

        with (
            patch.object(runtime, "refresh_snapshot", return_value=snapshot),
            patch.object(runtime.strategy, "on_fill", return_value=[]) as on_fill_mock,
            patch.object(runtime.audit, "log_event") as log_event_mock,
        ):
            runtime._ingest_fill_event(
                exchange_order_id="ex-cycle-long-fill",
                client_id="cycle-long-fill",
                qty=10.0,
                price=99.0,
                exec_id="exec-cycle-long-fill",
                cumulative_qty=10.0,
                source="websocket",
            )

        fill_event = on_fill_mock.call_args[0][0]
        self.assertAlmostEqual(fill_event.metadata["exec_pnl"], expected_pnl)
        self.assertAlmostEqual(fill_event.metadata["runtime_calculated_pnl"], expected_pnl)
        self.assertEqual(fill_event.metadata["entry_price_for_pnl"], 100.0)
        self.assertEqual(fill_event.metadata["pnl_calc_source"], "runtime_calculate_pnl_with_fees")
        self.assertTrue(
            any(
                call.args and call.args[0] == "fill_runtime_calculated_pnl_attached"
                for call in log_event_mock.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_runtime_pnl_calculated_with_fees"
                for call in log_event_mock.call_args_list
            )
        )

    def test_runtime_calculated_pnl_with_fees_matches_bybit_case(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="websocket",
        )
        runtime.runtime_state.last_snapshot = snapshot
        runtime.runtime_state.active_orders["cycle-long-fee"] = ManagedOrder(
            client_order_id="cycle-long-fee",
            exchange_order_id="ex-cycle-long-fee",
            side="long",
            qty=42.0,
            purpose="CYCLE_2_LONG_ADD",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=42.0,
            metadata={
                "cycle_index": 2,
                "cycle_role": "long_reduce",
                "entry_price": 0.445,
            },
        )
        with (
            patch.object(runtime, "refresh_snapshot", return_value=snapshot),
            patch.object(runtime.strategy, "on_fill", return_value=[]) as on_fill_mock,
            patch.object(runtime.audit, "log_event") as log_event_mock,
        ):
            runtime._ingest_fill_event(
                exchange_order_id="ex-cycle-long-fee",
                client_id="cycle-long-fee",
                qty=42.0,
                price=0.437,
                exec_id="exec-cycle-long-fee",
                cumulative_qty=42.0,
                source="websocket",
            )

        fill_event = on_fill_mock.call_args[0][0]
        metadata = fill_event.metadata
        self.assertAlmostEqual(metadata["runtime_calculated_pnl"], -0.3563742, places=8)
        self.assertAlmostEqual(metadata["exec_pnl"], -0.3563742, places=8)
        self.assertAlmostEqual(metadata["runtime_gross_pnl"], -0.336, places=8)
        self.assertAlmostEqual(metadata["runtime_entry_fee"], 0.0102795, places=8)
        self.assertAlmostEqual(metadata["runtime_exit_fee"], 0.0100947, places=8)
        self.assertAlmostEqual(metadata["runtime_fee_rate"], 0.00055)
        self.assertEqual(metadata["pnl_calc_source"], "runtime_calculate_pnl_with_fees")
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_runtime_pnl_calculated_with_fees"
                for call in log_event_mock.call_args_list
            )
        )

    def test_strategy_records_cycle_long_add_pnl_into_ledger(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="ex-cycle-long",
            client_order_id="cycle-long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=10.0,
            exec_price=99.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-long",
            metadata={"cycle_index": 1, "cycle_role": "long_reduce", "confirmed_closed_pnl": -0.15},
        )

        runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_long_reduce_pnl"]["1"], -0.15)

    def test_strategy_records_cycle_short_tp_pnl_into_ledger(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="ex-cycle-short",
            client_order_id="cycle-short",
            side="short",
            purpose="CYCLE_1_SHORT_TP",
            exec_qty=10.0,
            exec_price=101.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short",
            metadata={"cycle_index": 1, "cycle_role": "short_reduce", "short_reduce_closed_pnl": 0.25},
        )

        runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], 0.25)

    def test_unmatched_unknown_fill_remains_unmatched(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=1.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="ws",
        )
        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime.on_websocket_fill(
                exchange_order_id="ex-unknown",
                qty=5,
                price=1.0,
                exec_id="exec-unknown",
                order_link_id="unknown-prefix",
            )

        self.assertFalse(
            any(
                call.args and call.args[0] == "fixed_cycle_unmatched_fill_recovered"
                for call in log_event_mock.call_args_list
            )
        )

    def test_on_websocket_fill_accepts_extra_kwargs(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)

        with patch.object(runtime, "_recover_fixed_cycle_unmatched_fill", return_value=None) as recover_mock:
            runtime.on_websocket_fill(
                "exchange-id",
                11860.0,
                0.0004779,
                exec_id="exec-test",
                order_link_id="fixed_cycle-long_tp_exit-test",
                order_side="Sell",
                some_future_kwarg="ignored",
            )

        recover_mock.assert_called_once()

    def test_recover_unmatched_fill_logs_with_keyword_args(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fake_audit = FakeAudit()
        runtime.audit = fake_audit

        client_id = runtime._recover_fixed_cycle_unmatched_fill(
            exchange_order_id="exchange-id",
            order_link_id="fixed_cycle-long_tp_exit-test",
            qty=11860.0,
            price=0.0004779,
            exec_id="exec-test",
        )

        self.assertEqual(client_id, "fixed_cycle-long_tp_exit-test")
        self.assertEqual(len(fake_audit.events), 1)
        event_name, payload = fake_audit.events[0]
        self.assertEqual(event_name, "fixed_cycle_unmatched_fill_recovered")
        self.assertEqual(payload["order_link_id"], "fixed_cycle-long_tp_exit-test")
        self.assertEqual(payload["exec_qty"], 11860.0)
        self.assertEqual(payload["inferred_side"], "long")

    def test_prepare_for_clean_startup_clears_final_exit_residual_state(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state.update(
            {
                "final_trade_pnl_audited": True,
                "final_long_exit_audited": True,
                "final_short_exit_audited": True,
                "final_long_exit_order_context": {"exchange_order_id": "old-long"},
                "final_short_exit_order_context": {"exchange_order_id": "old-short"},
                "final_exit_closed_pnl_signatures": ["sig-1"],
                "audit_processed_exit_fill_ids": ["fill-1"],
                "audit_completed_cycle_indices": ["1"],
                "processed_pnl_exec_ids": ["exec-1"],
                "processed_pnl_exec_ids_order": ["exec-1"],
                "trade_block_id": "trade-current",
                "last_trade_block_id": "trade-last",
                "last_trade_pnl_usdt": 1.23,
                "last_trade_pnl_finalized_at": "2026-05-13T00:00:00+00:00",
                "last_trade_symbol": "BTCUSDT",
                "last_trade_pnl_source": "audit_ledger",
                "last_trade_pnl_complete": True,
                "last_trade_pnl_breakdown": {"total_trade_pnl": 1.23},
                "post_exit_cleanup_required": True,
                "post_exit_cleanup_verified": True,
                "restart_delayed_pending_final_pnl_logged": True,
                "initial_entry_submitted": True,
                "initial_entry_confirmed": True,
                "audit_pnl_ledger": {
                    "cycle_long_reduce_pnl": {"1": -0.1},
                    "cycle_short_tp_pnl": {"1": 0.2},
                    "cycle_pnl_entries": {"1": {"cycle_net_pnl": 0.1}},
                    "final_long_exit_pnl": 0.8,
                    "final_short_exit_pnl": -0.5,
                    "total_realized_pnl": 0.3,
                },
            }
        )
        runtime.runtime_state.active_orders["stale"] = ManagedOrder(
            client_order_id="stale",
            exchange_order_id="ex-stale",
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            price=99.0,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
        )
        runtime.runtime_state.exchange_to_client_id["ex-stale"] = "stale"
        runtime.runtime_state.temporary_pnl_by_order["stale"] = 0.5
        runtime.runtime_state.confirmed_pnl_applied.add("stale")
        runtime.runtime_state.processed_fill_cumulative["stale"] = 1.0
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="startup",
        )

        cleaned = runtime.strategy.prepare_for_clean_startup(snapshot, runtime.runtime_state, runtime.context)

        self.assertTrue(cleaned)
        self.assertTrue(state["startup_flat_reset_applied"])
        self.assertFalse(state["initial_entry_submitted"])
        self.assertFalse(state["initial_entry_confirmed"])
        self.assertNotIn("final_long_exit_order_context", state)
        self.assertNotIn("final_short_exit_order_context", state)
        self.assertFalse(state.get("final_long_exit_audited", False))
        self.assertFalse(state.get("final_short_exit_audited", False))
        self.assertFalse(state.get("final_exit_closed_pnl_signatures"))
        self.assertNotIn("trade_block_id", state)
        self.assertNotIn("last_trade_block_id", state)
        self.assertFalse(any(key.startswith("last_trade_") for key in state))
        self.assertEqual(
            state["audit_pnl_ledger"],
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        self.assertEqual(runtime.runtime_state.active_orders, {})
        self.assertEqual(runtime.runtime_state.exchange_to_client_id, {})

    def test_submit_intent_blocks_stale_final_exit_and_cycle_intents_after_flat_restart(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state["startup_flat_reset_applied"] = True
        runtime.runtime_state.strategy_state["initial_entry_confirmed"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        blocked_intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE),
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy._cycle_purpose("long", 1)),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy._short_tp_pair_purpose(1)),
        ]

        with patch.object(runtime.audit, "log_event") as log_event_mock, patch.object(
            runtime, "_submit_to_exchange"
        ) as submit_mock:
            results = [runtime.submit_intent(intent, snapshot, source="tick") for intent in blocked_intents]

        self.assertEqual(results, [None, None, None, None])
        self.assertFalse(submit_mock.called)
        blocked_events = [
            call for call in log_event_mock.call_args_list if call.args and call.args[0] == "fixed_cycle_blocked_stale_intent_after_flat_restart"
        ]
        self.assertEqual(len(blocked_events), 4)

    def test_submit_intent_allows_fresh_initial_entries_after_flat_restart(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state["startup_flat_reset_applied"] = True
        runtime.runtime_state.strategy_state["initial_entry_confirmed"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        long_intent = StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE)
        short_intent = StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE)

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            long_result = runtime.submit_intent(long_intent, snapshot, source="tick")
            short_result = runtime.submit_intent(short_intent, snapshot, source="tick")

        self.assertIsNotNone(long_result)
        self.assertIsNotNone(short_result)
        blocked_events = [
            call for call in log_event_mock.call_args_list if call.args and call.args[0] == "fixed_cycle_blocked_stale_intent_after_flat_restart"
        ]
        self.assertEqual(blocked_events, [])

    def test_recover_fixed_cycle_unmatched_fill_blocks_stale_orders_after_flat_restart(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state["startup_flat_reset_applied"] = True

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            long_exit = runtime._recover_fixed_cycle_unmatched_fill(
                exchange_order_id="order-long-exit",
                order_link_id="fixed_cycle-long_tp_exit-test",
                qty=1.0,
                price=100.0,
                exec_id="exec-long-exit",
            )
            short_exit = runtime._recover_fixed_cycle_unmatched_fill(
                exchange_order_id="order-short-exit",
                order_link_id="fixed_cycle-short_sl_exit-test",
                qty=1.0,
                price=100.0,
                exec_id="exec-short-exit",
            )
            cycle_order = runtime._recover_fixed_cycle_unmatched_fill(
                exchange_order_id="order-cycle",
                order_link_id="fixed_cycle-cycle_1_long_add-test",
                qty=1.0,
                price=100.0,
                exec_id="exec-cycle",
            )

        self.assertIsNone(long_exit)
        self.assertIsNone(short_exit)
        self.assertIsNone(cycle_order)
        self.assertEqual(runtime.runtime_state.active_orders, {})
        self.assertEqual(runtime.runtime_state.exchange_to_client_id, {})
        blocked_events = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_blocked_stale_unmatched_fill_after_flat_restart"
        ]
        self.assertEqual(len(blocked_events), 3)

    def test_recover_unmatched_fill_blocked_during_bootstrap(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime._bootstrap_in_progress = True

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            blocked = runtime._recover_fixed_cycle_unmatched_fill(
                exchange_order_id="order-long-exit",
                order_link_id="fixed_cycle-long_tp_exit-test",
                qty=1.0,
                price=100.0,
                exec_id="exec-long-exit",
            )

        self.assertIsNone(blocked)
        event_calls = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_blocked_unmatched_fill_during_bootstrap"
        ]
        self.assertEqual(len(event_calls), 1)
        self.assertEqual(event_calls[0].kwargs["reason"], "bootstrap_in_progress")
        self.assertEqual(runtime.runtime_state.active_orders, {})
        self.assertEqual(runtime.runtime_state.exchange_to_client_id, {})

    def test_startup_sets_bootstrap_guard_before_websocket(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="startup",
        )
        websocket_guard_states: list[bool] = []

        def websocket_side_effect() -> None:
            websocket_guard_states.append(runtime._bootstrap_in_progress)

        def bootstrap_side_effect() -> HedgeSnapshot:
            self.assertTrue(runtime._bootstrap_in_progress)
            runtime._bootstrap_in_progress = False
            return snapshot

        with patch.object(runtime, "_start_websocket", side_effect=websocket_side_effect) as websocket_mock, patch.object(
            runtime, "bootstrap", side_effect=bootstrap_side_effect
        ) as bootstrap_mock, patch.object(runtime, "_start_price_loop") as price_loop_mock, patch.object(
            runtime, "_start_reconcile_loop"
        ) as reconcile_loop_mock:
            runtime.start()

        websocket_mock.assert_called_once()
        bootstrap_mock.assert_called_once()
        price_loop_mock.assert_called_once()
        reconcile_loop_mock.assert_called_once()
        self.assertEqual(websocket_guard_states, [True])
        self.assertFalse(runtime._bootstrap_in_progress)

    def test_confirm_startup_flat_rejects_when_open_orders_exist(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="startup",
        )
        order_manager.open_orders = [{"orderId": "open-1", "orderLinkId": "link-1"}]

        with patch("fixed_cycle_hedge_bot.runtime.time.sleep", return_value=None), patch.object(
            runtime, "refresh_snapshot", side_effect=[snapshot, snapshot]
        ), patch.object(runtime.audit, "log_event") as log_event_mock:
            confirm_snapshot, confirmed, reason = runtime._confirm_startup_flat_snapshot(snapshot)

        self.assertFalse(confirmed)
        self.assertEqual(reason, "open_orders_found")
        self.assertEqual(confirm_snapshot, snapshot)
        rejection_events = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_startup_flat_confirmation_rejected_open_orders_found"
        ]
        self.assertTrue(rejection_events)
        payload = rejection_events[0].kwargs
        self.assertEqual(payload["symbol"], runtime.config.symbol)
        self.assertEqual(payload["open_order_count"], 1)

    def test_confirm_startup_flat_fails_when_open_order_check_errors(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="startup",
        )

        def raising_fetch(*args, **kwargs):
            raise RuntimeError("boom")

        with patch("fixed_cycle_hedge_bot.runtime.time.sleep", return_value=None), patch.object(
            runtime, "refresh_snapshot", side_effect=[snapshot, snapshot]
        ), patch.object(order_manager, "fetch_open_orders", side_effect=raising_fetch):
            confirm_snapshot, confirmed, reason = runtime._confirm_startup_flat_snapshot(snapshot)

        self.assertFalse(confirmed)
        self.assertEqual(reason, "open_order_check_failed")
        self.assertEqual(confirm_snapshot, snapshot)

    def test_bootstrap_blocks_on_start_when_flat_confirmation_fails(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="startup",
        )

        with patch.object(runtime, "refresh_snapshot", return_value=snapshot), patch.object(
            runtime,
            "_confirm_startup_flat_snapshot",
            return_value=(snapshot, False, "open_orders_found"),
        ) as confirm_mock, patch.object(runtime.strategy, "prepare_for_clean_startup") as clean_mock, patch.object(
            runtime.strategy, "on_start"
        ) as on_start_mock, patch.object(runtime, "_dispatch") as dispatch_mock, patch.object(
            runtime, "submit_intent"
        ) as submit_mock, patch.object(runtime.audit, "log_event") as log_event_mock:
            result = runtime.bootstrap()

        self.assertEqual(result, snapshot)
        confirm_mock.assert_called_once_with(snapshot)
        clean_mock.assert_not_called()
        on_start_mock.assert_not_called()
        dispatch_mock.assert_not_called()
        submit_mock.assert_not_called()
        blocked_events = [
            call
            for call in log_event_mock.call_args_list
            if call.args and call.args[0] == "fixed_cycle_startup_flat_confirmation_failed_start_blocked"
        ]
        self.assertEqual(len(blocked_events), 1)
        self.assertEqual(blocked_events[0].kwargs["reason"], "open_orders_found")
        self.assertEqual(blocked_events[0].kwargs["symbol"], runtime.config.symbol)

    def test_update_initial_entry_confirmation_lifts_startup_flat_reset_guard(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["startup_flat_reset_applied"] = True
        state["initial_entry_confirmed"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=2.0,
            short_qty=1.0,
            long_avg=99.5,
            short_avg=100.5,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            confirmed = runtime.strategy._update_initial_entry_confirmation(snapshot, runtime.runtime_state)

        self.assertTrue(confirmed)
        self.assertTrue(state["initial_entry_confirmed"])
        self.assertFalse(state["startup_flat_reset_applied"])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_startup_flat_reset_guard_lifted_after_initial_entry"
                for call in log_event_mock.call_args_list
            )
        )

    def test_dispatch_blocks_initial_entries_with_unsettled_runtime_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"] = ManagedOrder(
            client_order_id="fixed_cycle-long_tp_exit-test",
            side="long",
            qty=210830.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=0.0004779,
            order_type="Market",
            reduce_only=True,
            status="PARTIAL",
            filled_qty=198970.0,
            remaining_qty=11860.0,
        )
        snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.0004779,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intents = [
            StrategyIntent(side="long", qty=100.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=50.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]

        with (
            patch.object(runtime, "submit_intent") as submit_mock,
            patch.object(runtime.audit, "log_event") as log_event_mock,
        ):
            runtime._dispatch("tick", intents, snapshot)

        submit_mock.assert_not_called()
        self.assertTrue(
            any(
                call.args and call.args[0] == "strategy_initial_entry_blocked_unsettled_runtime_orders"
                for call in log_event_mock.call_args_list
            )
        )

    def test_filled_with_remaining_qty_stays_blocking_for_initial_entries(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.0004779,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="websocket",
        )
        runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"] = ManagedOrder(
            client_order_id="fixed_cycle-long_tp_exit-test",
            exchange_order_id="exchange-id",
            side="long",
            qty=210830.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=0.0004779,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=210830.0,
        )
        runtime.runtime_state.exchange_to_client_id["exchange-id"] = "fixed_cycle-long_tp_exit-test"
        snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.0004779,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        intents = [
            StrategyIntent(side="long", qty=100.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=50.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime.handle_websocket_event(
                "order",
                {
                    "orderId": "exchange-id",
                    "orderStatus": "Filled",
                    "cumExecQty": 198970.0,
                    "qty": 210830.0,
                },
            )
            runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"].status = "OPEN"
            runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"].filled_qty = 0.0
            runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"].remaining_qty = 210830.0

            self.assertIn("fixed_cycle-long_tp_exit-test", runtime.runtime_state.active_orders)
            managed_order = runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"]
            self.assertEqual(managed_order.status, "FILLED")
            self.assertEqual(managed_order.remaining_qty, 11860.0)

            with patch.object(runtime, "submit_intent") as submit_mock:
                runtime._dispatch("tick", intents, snapshot)

            submit_mock.assert_not_called()

        self.assertTrue(
            any(
                call.args and call.args[0] == "order_terminal_but_remaining_qty_wait"
                for call in log_event_mock.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.args and call.args[0] == "strategy_initial_entry_blocked_unsettled_runtime_orders"
                for call in log_event_mock.call_args_list
            )
        )

    def test_rebuild_structure_waits_for_unsettled_strategy_orders_when_flat(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_long_qty"] = 100.0
        state["initial_short_qty"] = 50.0
        runtime.runtime_state.active_orders["fixed_cycle-long_tp_exit-test"] = ManagedOrder(
            client_order_id="fixed_cycle-long_tp_exit-test",
            side="long",
            qty=210830.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=0.0004779,
            order_type="Market",
            reduce_only=True,
            status="PARTIAL",
            filled_qty=198970.0,
            remaining_qty=11860.0,
        )
        snapshot = HedgeSnapshot(
            symbol="PNUTUSDT",
            current_price=0.0004779,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch.object(runtime.context.audit, "log_event") as log_event_mock:
            intents = runtime.strategy._rebuild_structure(
                snapshot,
                runtime.runtime_state,
                runtime.context,
                reason="test_flat_unsettled_orders",
            )

        self.assertEqual(intents, [])
        self.assertNotEqual(state.get("bot_state"), runtime.strategy.STATE_EXITED)
        self.assertFalse(bool(state.get("fresh_restart_required")))
        self.assertFalse(
            any(call.args and call.args[0] == "fixed_cycle_exited" for call in log_event_mock.call_args_list)
        )
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_flat_waiting_unsettled_strategy_orders"
                for call in log_event_mock.call_args_list
            )
        )

    def test_final_pnl_blocks_restart_without_final_long_closed_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        state["final_short_exit_audited"] = True
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"1": -0.1620646},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": -0.72342272,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        runtime.runtime_state.last_snapshot = snapshot

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            blocked = runtime.strategy._block_flat_restart_until_final_pnl(
                snapshot,
                runtime.runtime_state,
                runtime.context,
                reason="test_cycle_pnl_long_side_satisfied",
            )

        self.assertTrue(blocked)
        self.assertFalse(state.get("final_long_exit_audited", False))
        self.assertFalse(state.get("final_trade_pnl_audited", False))
        self.assertIsNone(state.get("last_trade_pnl_usdt"))
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_final_exit_pnl_fetch_missing"
                for call in log_event_mock.call_args_list
            )
        )

    def test_final_pnl_missing_long_context_without_cycle_pnl_stays_blocked(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        state["final_short_exit_audited"] = True
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": -0.72342272,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        runtime.runtime_state.last_snapshot = snapshot

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event") as warning_mock:
            blocked = runtime.strategy._block_flat_restart_until_final_pnl(
                snapshot,
                runtime.runtime_state,
                runtime.context,
                reason="test_missing_context_without_cycle_pnl",
            )

        self.assertTrue(blocked)
        self.assertFalse(state.get("final_long_exit_audited", False))
        self.assertFalse(state.get("final_trade_pnl_audited", False))
        self.assertIsNone(state.get("last_trade_pnl_usdt"))
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_final_pnl_context_missing"
                for call in warning_mock.call_args_list
            )
        )

    def test_final_pnl_ignores_runtime_calculated_pnl_without_closed_data(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.realized_long_pnl_total = -0.25
        state = runtime.runtime_state.strategy_state
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        runtime.runtime_state.last_snapshot = snapshot

        finalized = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
            runtime.runtime_state,
            runtime.context,
            "test_runtime_realized_without_trade_evidence",
        )

        self.assertFalse(finalized)
        self.assertFalse(state.get("final_long_exit_audited", False))
        self.assertFalse(state.get("final_trade_pnl_audited", False))
        self.assertIsNone(state.get("last_trade_pnl_usdt"))

    def test_final_pnl_persists_closed_pnl_over_runtime_calculation(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.80787566,
            "final_short_exit_pnl": -0.53106832,
            "total_realized_pnl": 0.0,
        }
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "short-exit"}
        state["trade_block_id"] = "trade-1"
        runtime.runtime_state.realized_long_pnl_total = 0.91838995
        runtime.runtime_state.realized_short_pnl_total = -0.47586

        finalized = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
            runtime.runtime_state,
            runtime.context,
            "test_double_count_prevention",
        )

        self.assertTrue(finalized)
        self.assertAlmostEqual(state["last_trade_pnl_usdt"], 0.27680734)
        breakdown = state["last_trade_pnl_breakdown"]
        self.assertEqual(breakdown["cycle_long_reduce_pnl_total"], 0.0)
        self.assertEqual(breakdown["cycle_short_tp_pnl_total"], 0.0)
        self.assertEqual(breakdown["cycle_net_pnl"], 0.0)
        self.assertAlmostEqual(breakdown["final_exit_net_pnl"], 0.27680734)
        self.assertAlmostEqual(breakdown["total_trade_pnl"], 0.27680734)
        self.assertEqual(state["last_trade_pnl_source"], "bybit_closed_pnl")
        self.assertTrue(state["final_trade_pnl_audited"])

    def test_final_pnl_includes_real_cycle_entries_without_runtime_fallback(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"1": -0.10},
            "cycle_short_tp_pnl": {"1": 0.20},
            "final_long_exit_pnl": 0.80787566,
            "final_short_exit_pnl": -0.53106832,
            "total_realized_pnl": 0.0,
        }
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "short-exit"}
        state["trade_block_id"] = "trade-1"
        runtime.runtime_state.realized_long_pnl_total = 0.91838995
        runtime.runtime_state.realized_short_pnl_total = -0.47586

        finalized = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
            runtime.runtime_state,
            runtime.context,
            "test_cycle_entries_still_count",
        )

        self.assertTrue(finalized)
        breakdown = state["last_trade_pnl_breakdown"]
        self.assertAlmostEqual(breakdown["cycle_net_pnl"], 0.10)
        self.assertAlmostEqual(breakdown["final_exit_net_pnl"], 0.27680734)
        self.assertAlmostEqual(breakdown["total_trade_pnl"], 0.37680734)

    def test_total_trade_pnl_includes_real_cycle_pair_and_final_exits(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"1": -0.15},
            "cycle_short_tp_pnl": {"1": 0.25},
            "final_long_exit_pnl": 0.80,
            "final_short_exit_pnl": -0.50,
            "total_realized_pnl": 0.0,
        }
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "short-exit"}
        state["trade_block_id"] = "trade-cycle-1"

        finalized = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
            runtime.runtime_state,
            runtime.context,
            "test_cycle_pair_and_final_exit_total",
        )

        self.assertTrue(finalized)
        breakdown = state["last_trade_pnl_breakdown"]
        self.assertAlmostEqual(breakdown["cycle_net_pnl"], 0.10)
        self.assertAlmostEqual(breakdown["final_exit_net_pnl"], 0.30)
        self.assertAlmostEqual(breakdown["total_trade_pnl"], 0.40)
        self.assertEqual(state["last_trade_block_id"], "trade-cycle-1")

    def test_final_exit_pnl_ledger_updates_from_closed_pnl_history(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit", "symbol": "BTCUSDT"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "short-exit", "symbol": "BTCUSDT"}
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        order_manager.closed_pnl_rows = [
            {"orderId": "long-exit", "symbol": "BTCUSDT", "orderLinkId": "long-link", "closedPnl": "0.6", "side": "Sell"},
            {"orderId": "short-exit", "symbol": "BTCUSDT", "orderLinkId": "short-link", "closedPnl": "-0.4", "side": "Buy"},
        ]

        confirmed = runtime.strategy._ensure_final_exit_pnl_from_exchange(
            runtime.runtime_state,
            runtime.context,
            "test_final_exit_pnl_fetch",
        )

        self.assertTrue(confirmed)
        self.assertAlmostEqual(state["audit_pnl_ledger"]["final_long_exit_pnl"], 0.6)
        self.assertAlmostEqual(state["audit_pnl_ledger"]["final_short_exit_pnl"], -0.4)
        self.assertTrue(state["final_long_exit_audited"])
        self.assertTrue(state["final_short_exit_audited"])

        order_manager.closed_pnl_rows = []
        confirmed_again = runtime.strategy._ensure_final_exit_pnl_from_exchange(
            runtime.runtime_state,
            runtime.context,
            "test_final_exit_pnl_fetch_sticky",
        )

        self.assertTrue(confirmed_again)
        self.assertAlmostEqual(state["audit_pnl_ledger"]["final_long_exit_pnl"], 0.6)

    def test_long_exit_waits_for_closed_pnl_before_finalizing(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_short_exit_audited"] = True
        state["final_short_exit_order_context"] = {
            "exchange_order_id": "short-exit",
            "symbol": "BTCUSDT",
        }
        state["final_long_exit_order_context"] = {
            "exchange_order_id": "long-exit",
            "symbol": "BTCUSDT",
        }
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": -1.69407931,
            "total_realized_pnl": 0.0,
        }
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        order_manager.closed_pnl_rows = [
            {"orderId": "short-exit", "symbol": "BTCUSDT", "closedPnl": "-1.69407931", "side": "Buy"},
        ]

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            finalized_before = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
                runtime.runtime_state,
                runtime.context,
                "test_long_waits_for_closed_pnl",
            )
            blocked = runtime.strategy._block_flat_restart_until_final_pnl(
                runtime.runtime_state.last_snapshot,
                runtime.runtime_state,
                runtime.context,
                reason="test_long_waits_for_closed_pnl",
            )

        self.assertFalse(finalized_before)
        self.assertTrue(blocked)
        self.assertIsNone(state["audit_pnl_ledger"]["final_long_exit_pnl"])
        self.assertFalse(state.get("last_trade_pnl_complete", False))
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertNotIn("fixed_cycle_last_trade_pnl_persisted", event_names)
        self.assertIn("fixed_cycle_flat_waiting_for_final_pnl", event_names)

        order_manager.closed_pnl_rows.append(
            {"orderId": "long-exit", "symbol": "BTCUSDT", "closedPnl": "0.0", "side": "Sell"}
        )
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_final:
            finalized_after = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
                runtime.runtime_state,
                runtime.context,
                "test_long_waits_for_closed_pnl",
            )

        self.assertTrue(finalized_after)
        self.assertAlmostEqual(state["audit_pnl_ledger"]["final_long_exit_pnl"], 0.0)
        event_names_final = [call.args[0] for call in log_event_final.call_args_list if call.args]
        self.assertIn("fixed_cycle_last_trade_pnl_persisted", event_names_final)
        self.assertIn("fixed_cycle_trade_pnl_finalized", event_names_final)
        self.assertTrue(state.get("last_trade_pnl_complete", False))

    def test_fresh_entry_blocked_pending_final_exit_settlement(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 0.18
        state["trade_block_id"] = "trade-block"
        state["last_trade_block_id"] = "trade-block"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.9,
            "final_short_exit_pnl": -0.7,
            "total_realized_pnl": 0.0,
        }
        runtime.runtime_state.active_orders["final-short-sl"] = ManagedOrder(
            client_order_id="short-final",
            side="short",
            qty=1.0,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="NEW",
        )
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(
                snapshot, runtime.runtime_state, runtime.context
            )

        self.assertEqual(intents, [])
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_fresh_entry_blocked_active_strategy_orders", event_names)

    def test_fresh_entry_blocked_when_pnl_incomplete(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = False
        state["last_trade_pnl_usdt"] = None
        state["trade_block_id"] = "trade-block"
        state["last_trade_block_id"] = "trade-block"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.87,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        state["current_trade_pnl_state_reset_for_entry"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(
                snapshot, runtime.runtime_state, runtime.context
            )

        self.assertEqual(intents, [])
        self.assertFalse(state["current_trade_pnl_state_reset_for_entry"])
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertTrue(
            any(
                name in event_names
                for name in [
                    "fixed_cycle_fresh_entry_blocked_pending_final_exit_settlement",
                    "fixed_cycle_flat_waiting_for_final_pnl",
                ]
            )
        )

    def test_fresh_entry_allowed_after_reset_even_if_ledger_empty(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 0.18
        state["trade_block_id"] = "trade-finalized"
        state["last_trade_block_id"] = "trade-finalized"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)

    def test_fresh_entry_allowed_after_reset_with_new_trade_id(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 0.21
        state["trade_block_id"] = "new-trade-id"
        state["last_trade_block_id"] = "old-trade-id"
        state["current_trade_pnl_state_reset_for_entry"] = True
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)

    def test_fresh_entry_blocked_when_previous_trade_not_finalized(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = False
        state["trade_block_id"] = "trade-incomplete"
        state["last_trade_block_id"] = "trade-other"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": None,
            "final_short_exit_pnl": None,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_fresh_entry_blocked_pending_final_exit_settlement"
                for call in log_event_mock.call_args_list
                if call.args
            )
        )

    def test_fresh_entry_blocked_when_final_exit_orders_active(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 0.5
        state["trade_block_id"] = "trade-finalized"
        state["last_trade_block_id"] = "trade-finalized"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.8,
            "final_short_exit_pnl": -0.3,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="pending-long-exit",
                    exchange_order_id="ex-long-exit",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
                    order_type="Market",
                    reduce_only=True,
                    status="OPEN",
                ),
            ),
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(
            any(
                call.args
                and call.args[0] == "fixed_cycle_fresh_entry_blocked_active_strategy_orders"
                for call in log_event_mock.call_args_list
                if call.args
            )
        )

    def test_post_exit_cleanup_blocks_fresh_entry_until_orders_canceled(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.open_orders = [{"orderLinkId": "fixed_cycle-blocked"}]
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["post_exit_cleanup_required"] = True
        state["post_exit_cleanup_verified"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_post_exit_cleanup_waiting", event_names)

    def test_post_exit_cleanup_waits_when_snapshot_has_cycle_order(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["post_exit_cleanup_required"] = True
        state["post_exit_cleanup_verified"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="cycle-long",
                    exchange_order_id="ex-cycle-long",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="CYCLE_1_LONG_ADD",
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_post_exit_cleanup_waiting", event_names)

    def test_post_exit_cleanup_allows_entry_after_verified_clean(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["post_exit_cleanup_required"] = True
        state["post_exit_cleanup_verified"] = False
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)
        self.assertTrue(state.get("post_exit_cleanup_verified"))
        self.assertFalse(state.get("post_exit_cleanup_required"))

    def test_post_exit_cleanup_retries_until_snapshot_clean(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.open_orders = [{"orderLinkId": "fixed_cycle-blocked"}]
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["post_exit_cleanup_required"] = True
        state["post_exit_cleanup_verified"] = False
        snapshot_first = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event"):
            first_intents = runtime.strategy._build_entry_intents(
                snapshot_first, runtime.runtime_state, runtime.context
            )
        self.assertEqual(first_intents, [])
        order_manager.open_orders = []
        snapshot_second = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        second_intents = runtime.strategy._build_entry_intents(
            snapshot_second, runtime.runtime_state, runtime.context
        )

        purposes = {intent.purpose for intent in second_intents}
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)

    def test_on_tick_fresh_restart_blocked_post_exit_cleanup_pending(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["post_exit_cleanup_required"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        with patch.object(runtime.strategy, "_final_pnl_ready_for_restart", return_value=True), patch.object(
            runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch"
        ), patch.object(
            runtime.strategy, "_dynamic_symbol_entry_gate_allows_entry", return_value=True
        ), patch.object(
            runtime.strategy, "_attempt_post_exit_cleanup", return_value=False
        ), patch.object(
            runtime.strategy, "_reset_cycle_state"
        ) as reset_mock, patch.object(
            runtime.strategy, "_force_fresh_start_reset"
        ) as force_mock, patch.object(
            runtime.strategy, "_build_entry_intents"
        ) as build_mock:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)
        self.assertEqual(intents, [])
        self.assertFalse(reset_mock.called)
        self.assertFalse(force_mock.called)
        self.assertFalse(build_mock.called)

    def test_on_tick_fresh_restart_blocked_post_exit_cleanup_not_clean(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["post_exit_cleanup_required"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        active_order = ManagedOrder(
            client_order_id="cycle-block",
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            price=None,
            order_type="Market",
            reduce_only=False,
            status="OPEN",
        )
        def fake_attempt(snapshot_arg, runtime_state_arg, context_arg):
            state["post_exit_cleanup_required"] = False
            state["post_exit_cleanup_verified"] = True
            runtime_state_arg.last_snapshot = HedgeSnapshot(
                symbol="BTCUSDT",
                current_price=100.0,
                long_qty=0.0,
                short_qty=0.0,
                long_avg=0.0,
                short_avg=0.0,
                active_orders=(active_order,),
                source="tick",
            )
            return True

        with patch.object(runtime.strategy, "_final_pnl_ready_for_restart", return_value=True), patch.object(
            runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch"
        ), patch.object(
            runtime.strategy, "_dynamic_symbol_entry_gate_allows_entry", return_value=True
        ), patch.object(runtime.strategy, "_attempt_post_exit_cleanup", side_effect=fake_attempt), patch.object(
            runtime.strategy, "_reset_cycle_state"
        ) as reset_mock, patch.object(
            runtime.strategy, "_force_fresh_start_reset"
        ) as force_mock, patch.object(
            runtime.strategy, "_build_entry_intents"
        ) as build_mock:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertFalse(reset_mock.called)
        self.assertFalse(force_mock.called)
        self.assertFalse(build_mock.called)

    def test_on_tick_fresh_restart_resets_after_cleanup_verified(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["post_exit_cleanup_required"] = True

        def fake_attempt(snapshot_arg, runtime_state_arg, context_arg):
            state["post_exit_cleanup_required"] = False
            state["post_exit_cleanup_verified"] = True
            runtime_state_arg.last_snapshot = snapshot_arg
            return True

        with patch.object(runtime.strategy, "_final_pnl_ready_for_restart", return_value=True), patch.object(
            runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch"
        ), patch.object(
            runtime.strategy, "_dynamic_symbol_entry_gate_allows_entry", return_value=True
        ), patch.object(runtime.strategy, "_attempt_post_exit_cleanup", side_effect=fake_attempt), patch.object(
            runtime.strategy, "_reset_cycle_state"
        ) as reset_mock, patch.object(runtime.strategy, "_force_fresh_start_reset") as force_mock, patch.object(
            runtime.strategy, "_build_entry_intents", return_value=[StrategyIntent(side="long", qty=1.0, purpose="INITIAL_LONG_ENTRY")]
        ) as build_mock:
            intents = runtime.strategy.on_tick(
                HedgeSnapshot(
                    symbol="BTCUSDT",
                    current_price=100.0,
                    long_qty=0.0,
                    short_qty=0.0,
                    long_avg=0.0,
                    short_avg=0.0,
                    source="tick",
                ),
                runtime.runtime_state,
                runtime.context,
            )

        self.assertTrue(reset_mock.called)
        self.assertTrue(force_mock.called)
        self.assertTrue(build_mock.called)
        self.assertEqual(intents[0].purpose, "INITIAL_LONG_ENTRY")
        self.assertFalse(state["fresh_restart_required"])

    def test_on_start_fresh_restart_blocks_cleanup_pending(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["post_exit_cleanup_required"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        with patch.object(runtime.strategy, "_final_pnl_ready_for_restart", return_value=True), patch.object(
            runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch"
        ), patch.object(
            runtime.strategy, "_dynamic_symbol_entry_gate_allows_entry", return_value=True
        ), patch.object(runtime.strategy, "_attempt_post_exit_cleanup", return_value=False), patch.object(
            runtime.strategy, "_reset_cycle_state"
        ) as reset_mock, patch.object(
            runtime.strategy, "_force_fresh_start_reset"
        ) as force_mock, patch.object(
            runtime.strategy, "_build_entry_intents"
        ) as build_mock, patch.object(
            runtime.strategy, "_load_best_coin_symbol_from_file", return_value=None
        ), patch.object(
            runtime.strategy, "_trigger_restart_script_after_full_exit", return_value=False
        ):
            intents = runtime.strategy.on_start(snapshot, runtime.runtime_state, runtime.context)
        self.assertEqual(intents, [])
        self.assertFalse(reset_mock.called)
        self.assertFalse(force_mock.called)
        self.assertFalse(build_mock.called)

    def test_on_start_fresh_restart_blocks_cleanup_not_clean(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["post_exit_cleanup_required"] = True
        active_order = ManagedOrder(
            client_order_id="cycle-block",
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            price=None,
            order_type="Market",
            reduce_only=False,
            status="OPEN",
        )
        def fake_attempt(snapshot_arg, runtime_state_arg, context_arg):
            state["post_exit_cleanup_required"] = False
            state["post_exit_cleanup_verified"] = True
            runtime_state_arg.last_snapshot = HedgeSnapshot(
                symbol="BTCUSDT",
                current_price=100.0,
                long_qty=0.0,
                short_qty=0.0,
                long_avg=0.0,
                short_avg=0.0,
                active_orders=(active_order,),
                source="tick",
            )
            return True
        with patch.object(runtime.strategy, "_final_pnl_ready_for_restart", return_value=True), patch.object(
            runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch"
        ), patch.object(
            runtime.strategy, "_dynamic_symbol_entry_gate_allows_entry", return_value=True
        ), patch.object(runtime.strategy, "_attempt_post_exit_cleanup", side_effect=fake_attempt), patch.object(
            runtime.strategy, "_reset_cycle_state"
        ) as reset_mock, patch.object(
            runtime.strategy, "_force_fresh_start_reset"
        ) as force_mock, patch.object(
            runtime.strategy, "_build_entry_intents"
        ) as build_mock, patch.object(
            runtime.strategy, "_load_best_coin_symbol_from_file", return_value=None
        ), patch.object(
            runtime.strategy, "_trigger_restart_script_after_full_exit", return_value=False
        ):
            intents = runtime.strategy.on_start(
                HedgeSnapshot(
                    symbol="BTCUSDT",
                    current_price=100.0,
                    long_qty=0.0,
                    short_qty=0.0,
                    long_avg=0.0,
                    short_avg=0.0,
                    source="tick",
                ),
                runtime.runtime_state,
                runtime.context,
            )
        self.assertEqual(intents, [])
        self.assertFalse(reset_mock.called)
        self.assertFalse(force_mock.called)
        self.assertFalse(build_mock.called)

    def test_on_start_fresh_restart_resets_after_cleanup_verified(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["fresh_restart_required"] = True
        state["post_exit_cleanup_required"] = True
        def fake_attempt(snapshot_arg, runtime_state_arg, context_arg):
            state["post_exit_cleanup_required"] = False
            state["post_exit_cleanup_verified"] = True
            runtime_state_arg.last_snapshot = snapshot_arg
            return True
        with patch.object(runtime.strategy, "_final_pnl_ready_for_restart", return_value=True), patch.object(
            runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch"
        ), patch.object(
            runtime.strategy, "_dynamic_symbol_entry_gate_allows_entry", return_value=True
        ), patch.object(runtime.strategy, "_attempt_post_exit_cleanup", side_effect=fake_attempt), patch.object(
            runtime.strategy, "_reset_cycle_state"
        ) as reset_mock, patch.object(runtime.strategy, "_force_fresh_start_reset") as force_mock, patch.object(
            runtime.strategy,
            "_build_entry_intents",
            return_value=[StrategyIntent(side="long", qty=1.0, purpose="INITIAL_LONG_ENTRY")],
        ) as build_mock, patch.object(
            runtime.strategy, "_load_best_coin_symbol_from_file", return_value=None
        ), patch.object(
            runtime.strategy, "_trigger_restart_script_after_full_exit", return_value=False
        ):
            intents = runtime.strategy.on_start(
                HedgeSnapshot(
                    symbol="BTCUSDT",
                    current_price=100.0,
                    long_qty=0.0,
                    short_qty=0.0,
                    long_avg=0.0,
                    short_avg=0.0,
                    source="tick",
                ),
                runtime.runtime_state,
                runtime.context,
            )
        self.assertTrue(reset_mock.called)
        self.assertTrue(force_mock.called)
        self.assertTrue(build_mock.called)
        self.assertEqual(intents[0].purpose, "INITIAL_LONG_ENTRY")

    def test_dispatch_blocks_entry_intents_when_unsettled_orders_exist(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["pending"] = ManagedOrder(
            client_order_id="pending",
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_ENTRY_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=False,
            status="PARTIAL",
            remaining_qty=0.5,
        )
        intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", intents, snapshot)

        logged_events = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("strategy_initial_entry_blocked_unsettled_runtime_orders", logged_events)

    def test_dispatch_blocks_initial_entries_with_unsettled_snapshot_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="cycle-long-add",
                    exchange_order_id="ex-cycle-long-add",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="CYCLE_1_LONG_ADD",
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
            source="tick",
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", intents, snapshot)

        logged_events = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("strategy_initial_entry_blocked_unsettled_snapshot_orders", logged_events)

    def test_dispatch_ignores_stale_snapshot_after_cleanup_verification(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["post_exit_cleanup_verified"] = True
        state["post_exit_cleanup_required"] = False
        verified_ts = datetime.now(timezone.utc)
        state["post_exit_cleanup_verified_snapshot_updated_at"] = verified_ts.isoformat()
        intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]
        snapshot_time = verified_ts - timedelta(seconds=5)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            updated_at=snapshot_time,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="cycle-long-add",
                    exchange_order_id="ex-cycle-long-add",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="CYCLE_1_LONG_ADD",
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", intents, snapshot)

        logged_events = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("strategy_initial_entry_snapshot_orders_ignored_after_verified_cleanup", logged_events)
        self.assertNotIn("strategy_initial_entry_blocked_unsettled_snapshot_orders", logged_events)

    def test_dispatch_still_blocks_fresh_snapshot_after_cleanup_verification(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["post_exit_cleanup_verified"] = True
        state["post_exit_cleanup_required"] = False
        verified_ts = datetime.now(timezone.utc)
        state["post_exit_cleanup_verified_snapshot_updated_at"] = verified_ts.isoformat()
        intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]
        snapshot_time = verified_ts + timedelta(seconds=5)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            updated_at=snapshot_time,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="cycle-long-add",
                    exchange_order_id="ex-cycle-long-add",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="CYCLE_1_LONG_ADD",
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", intents, snapshot)

        logged_events = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("strategy_initial_entry_blocked_unsettled_snapshot_orders", logged_events)
        self.assertNotIn("strategy_initial_entry_snapshot_orders_ignored_after_verified_cleanup", logged_events)

    def test_dispatch_blocks_initial_entries_during_post_exit_cleanup(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state["post_exit_cleanup_required"] = True
        runtime.runtime_state.strategy_state["post_exit_cleanup_verified"] = False
        intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
        )

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", intents, snapshot)

        logged_events = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("strategy_initial_entry_blocked_post_exit_cleanup_pending", logged_events)

    def test_runtime_unsettled_strategy_orders_ignores_terminal_remaining_qty(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["terminal"] = ManagedOrder(
            client_order_id="terminal",
            side="short",
            qty=1.0,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            filled_qty=1.0,
            remaining_qty=1000.0,
        )
        unsettled = runtime._runtime_unsettled_strategy_orders()
        self.assertEqual(unsettled, [])

    def test_runtime_unsettled_strategy_orders_blocks_open_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["open-exit"] = ManagedOrder(
            client_order_id="open-exit",
            side="short",
            qty=1.0,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            remaining_qty=0.5,
        )
        unsettled = runtime._runtime_unsettled_strategy_orders()
        self.assertEqual(len(unsettled), 1)

    def test_dispatch_allows_entries_when_terminal_order_has_stale_remaining_qty(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["terminal"] = ManagedOrder(
            client_order_id="terminal",
            side="short",
            qty=1.0,
            purpose=runtime.strategy.SHORT_SL_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            remaining_qty=1000.0,
        )
        intents = [
            StrategyIntent(side="long", qty=1.0, purpose=runtime.strategy.LONG_ENTRY_PURPOSE),
            StrategyIntent(side="short", qty=1.0, purpose=runtime.strategy.SHORT_ENTRY_PURPOSE),
        ]
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
        )
        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._dispatch("tick", intents, snapshot)
        logged_events = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertNotIn("strategy_initial_entry_blocked_unsettled_runtime_orders", logged_events)

    def test_reconcile_skips_inference_for_terminal_stale_order(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        managed_order = ManagedOrder(
            client_order_id="terminal-order",
            side="long",
            qty=1.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=10.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            filled_qty=0.5,
            remaining_qty=0.5,
        )
        runtime.runtime_state.active_orders["terminal-order"] = managed_order
        def history_fetch(*args, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"orderStatus": "FILLED", "cumExecQty": 1.0, "avgPrice": 10.0}]
        order_manager.fetch_open_orders = lambda *args, **kwargs: []
        order_manager.fetch_order_history = history_fetch
        with patch.object(runtime, "_ingest_fill_event") as ingest_mock, patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._reconcile_active_orders()
        self.assertFalse(ingest_mock.called)
        self.assertTrue(
            any(
                call.args and call.args[0] == "reconcile_terminal_order_skip_stale_fill_inference"
                for call in log_event_mock.call_args_list
                if call.args
            )
        )
    def test_stale_previous_trade_pnl_does_not_satisfy_current_trade(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["trade_block_id"] = "trade-2"
        state["last_trade_block_id"] = "trade-1"
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 0.27680734
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.80787566,
            "final_short_exit_pnl": -0.53106832,
            "total_realized_pnl": 0.0,
        }
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["final_long_exit_order_context"] = {"exchange_order_id": "long-exit"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "short-exit"}

        self.assertFalse(runtime.strategy._final_pnl_ready_for_restart(runtime.runtime_state))
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            finalized = runtime.strategy._emit_final_trade_pnl_if_complete_or_fetch(
                runtime.runtime_state,
                runtime.context,
                "test_stale_previous_trade_pnl",
            )

        self.assertTrue(finalized)
        self.assertEqual(state["last_trade_block_id"], "trade-2")
        self.assertAlmostEqual(state["last_trade_pnl_usdt"], 0.27680734)
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_stale_final_pnl_state_ignored"
                for call in log_event_mock.call_args_list
            )
        )

    def test_build_entry_intents_resets_current_trade_pnl_state_but_preserves_last_trade_history(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["last_trade_pnl_usdt"] = 0.27680734
        state["last_trade_pnl_finalized_at"] = "2026-05-06T08:00:00+00:00"
        state["last_trade_symbol"] = "FILUSDT"
        state["last_trade_block_id"] = "trade-1"
        state["last_trade_pnl_source"] = "audit_ledger"
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_breakdown"] = {"total_trade_pnl": 0.27680734}
        state["trade_block_id"] = "trade-1"
        state["final_trade_pnl_audited"] = True
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["final_long_exit_order_context"] = {"exchange_order_id": "old-long"}
        state["final_short_exit_order_context"] = {"exchange_order_id": "old-short"}
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {"1": -0.10},
            "cycle_short_tp_pnl": {"1": 0.20},
            "final_long_exit_pnl": 0.80787566,
            "final_short_exit_pnl": -0.53106832,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertTrue(intents)
        self.assertEqual(state["last_trade_pnl_usdt"], 0.27680734)
        self.assertEqual(state["last_trade_block_id"], "trade-1")
        self.assertFalse(state.get("final_trade_pnl_audited", False))
        self.assertFalse(state.get("final_long_exit_audited", False))
        self.assertFalse(state.get("final_short_exit_audited", False))
        self.assertNotIn("final_long_exit_order_context", state)
        self.assertNotIn("final_short_exit_order_context", state)
        self.assertEqual(
            state["audit_pnl_ledger"],
            {
                "cycle_long_reduce_pnl": {},
                "cycle_short_tp_pnl": {},
                "cycle_pnl_entries": {},
                "final_long_exit_pnl": None,
                "final_short_exit_pnl": None,
                "total_realized_pnl": 0.0,
            },
        )
        self.assertTrue(state.get("trade_block_id"))
        self.assertNotEqual(state.get("trade_block_id"), "trade-1")

    def test_cycle_fill_without_usable_pnl_logs_missing_cycle_pnl_warning(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="ex-cycle-missing",
            client_order_id="cycle-missing",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=10.0,
            exec_price=99.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-missing",
            metadata={"cycle_index": 1, "cycle_role": "long_reduce"},
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event") as warning_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_cycle_pnl_missing_confirmed_closed_pnl"
                for call in warning_mock.call_args_list
            )
        )

    def test_cycle_long_reduce_refresh_applies_metadata_before_extract(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        cycle_state = runtime.runtime_state.strategy_state.setdefault(
            "cycle_state", runtime.strategy._default_cycle_state()
        )
        cycle_state.setdefault("long_fills", {})["2"] = {
            "order_id": "47e41fd3-6f37-4d11-85bf-a09212ae547e",
            "client_order_id": "fixed_cycle-cycle_2_long_add-b25e10f85a",
            "exec_id": "a39ef420-ff57-58c4-b133-297dc9e5227e",
            "qty": 42.0,
            "price": 0.437,
        }
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        order_manager.closed_pnl_rows = [
            {
                "orderId": "47e41fd3-6f37-4d11-85bf-a09212ae547e",
                "symbol": "BTCUSDT",
                "side": "Sell",
                "closedSize": 42.0,
                "avgEntryPrice": 0.48,
                "avgExitPrice": 0.437,
                "closedPnl": -0.3563742,
                "updatedTime": now_ms,
                "createdTime": now_ms,
            }
        ]
        fill_event = FillEvent(
            exchange_order_id="47e41fd3-6f37-4d11-85bf-a09212ae547e",
            client_order_id="fixed_cycle-cycle_2_long_add-b25e10f85a",
            purpose="CYCLE_2_LONG_ADD",
            side="long",
            exec_qty=42.0,
            exec_price=0.437,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="a39ef420-ff57-58c4-b133-297dc9e5227e",
            metadata={"cycle_index": 2, "cycle_role": "long_reduce"},
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock, patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event"
        ) as warning_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_long_reduce_pnl"]["2"], -0.3563742, places=8)
        metadata = fill_event.metadata or {}
        self.assertAlmostEqual(metadata["confirmed_closed_pnl"], -0.3563742, places=8)
        self.assertAlmostEqual(metadata["confirmed_closed_qty"], 42.0, places=8)
        self.assertAlmostEqual(metadata["confirmed_closed_avg_price"], 0.437, places=8)
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_cycle_pnl_refreshed_metadata_applied", event_names)
        self.assertIn("fixed_cycle_cycle_pnl_confirmed_seen_after_refresh", event_names)
        self.assertFalse(
            any(
                call.args and call.args[0] == "fixed_cycle_cycle_pnl_missing_confirmed_closed_pnl"
                for call in warning_mock.call_args_list
            )
        )

    def test_cycle_long_reduce_uses_provisional_runtime_pnl_when_confirmed_missing(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="cycle-long",
            client_order_id="cycle-long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=10.0,
            exec_price=1.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-provisional",
            metadata={
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "runtime_calculated_pnl": -0.25,
                "exec_pnl": -0.25,
            },
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertNotIn("1", ledger["cycle_long_reduce_pnl"])
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_cycle_pnl_using_provisional_runtime_pnl", event_names)

    def test_cycle_short_reduce_ignores_source_long_confirmed_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="ex-cycle-short",
            client_order_id="cycle-short",
            side="short",
            purpose="CYCLE_1_SHORT_TP",
            exec_qty=103.0,
            exec_price=0.11919,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short",
            metadata={
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "confirmed_closed_pnl": -0.14475136,
                "short_reduce_closed_pnl": 0.17510,
                "exec_pnl": 0.17510,
                "runtime_calculated_pnl": 0.17510,
            },
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._audit_calc") as audit_calc_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], 0.17510, places=5)
        self.assertNotAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], -0.14475136, places=5)
        exit_summary_payload = next(
            call.args[1]
            for call in audit_calc_mock.call_args_list
            if call.args and call.args[0] == "exit_pnl_summary"
        )
        self.assertAlmostEqual(exit_summary_payload["expected_vs_actual"]["actual_fill_pnl"], 0.17510, places=5)
        self.assertEqual(
            exit_summary_payload["expected_vs_actual"]["actual_fill_pnl_source"],
            "short_reduce_closed_pnl",
        )

    def test_refresh_short_reduce_closed_pnl_called_once_per_audit(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="ex-cycle-short",
            client_order_id="cycle-short",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=28.7,
            exec_price=0.429,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short-1",
            metadata={"cycle_index": 1, "cycle_role": "short_reduce"},
        )
        with patch.object(
            runtime.strategy, "_refresh_short_reduce_closed_pnl", return_value=False
        ) as refresh_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)
        self.assertEqual(refresh_mock.call_count, 1)

    def test_cycle_short_reduce_delayed_closed_pnl_replace_provisional(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        fill_event = FillEvent(
            exchange_order_id="cycle-short",
            client_order_id="cycle-short",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=28.7,
            exec_price=0.429,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short-reduce",
            metadata={
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "runtime_calculated_pnl": 0.15211,
                "exec_pnl": 0.15211,
            },
        )

        runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)
        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertNotIn("1", ledger["cycle_short_tp_pnl"])

        now = datetime.now(timezone.utc)
        order_manager.closed_pnl_rows = [
            {
                "orderId": "cycle-short",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "closedSize": 28.7,
                "avgExitPrice": 0.429,
                "closedPnl": 0.1384,
                "createdTime": int(now.timestamp() * 1000),
                "updatedTime": int(now.timestamp() * 1000),
            }
        ]

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], 0.1384, places=6)
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_short_reduce_pnl_confirmed", event_names)

    def test_cycle_short_reduce_replaces_runtime_pnl_with_confirmed_closed_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        now = datetime.now(timezone.utc)
        order_manager.closed_pnl_rows = [
            {
                "orderId": "cycle-short",
                "symbol": "BTCUSDT",
                "side": "Buy",
                "closedSize": 28.7,
                "avgExitPrice": 0.429,
                "closedPnl": 0.1384,
                "createdTime": int(now.timestamp() * 1000),
                "updatedTime": int(now.timestamp() * 1000),
            }
        ]
        fill_event = FillEvent(
            exchange_order_id="cycle-short",
            client_order_id="cycle-short",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=28.7,
            exec_price=0.429,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short-reduce",
            metadata={"cycle_index": 1, "cycle_role": "short_reduce"},
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            runtime.strategy._audit_exit_pnl_summary(fill_event, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(0.1384, ledger["cycle_short_tp_pnl"]["1"], places=6)
        self.assertNotAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], 0.15211, places=6)
        event_names = [call.args[0] for call in log_event_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_short_reduce_pnl_confirmed", event_names)

    def test_cycle_long_confirmed_replaces_provisional_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        provisional_fill = FillEvent(
            exchange_order_id="ex-cycle-long",
            client_order_id="cycle-long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=206.0,
            exec_price=0.12033,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-long-provisional",
            metadata={
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "runtime_calculated_pnl": -0.11742,
                "exec_pnl": -0.11742,
            },
        )
        confirmed_fill = FillEvent(
            exchange_order_id="ex-cycle-long",
            client_order_id="cycle-long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=206.0,
            exec_price=0.12033,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-long-confirmed",
            metadata={
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "confirmed_closed_pnl": -0.14475136,
            },
        )

        runtime.strategy._audit_exit_pnl_summary(provisional_fill, runtime.runtime_state, runtime.context)
        runtime.strategy._audit_exit_pnl_summary(confirmed_fill, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_long_reduce_pnl"]["1"], -0.14475136, places=8)
        self.assertNotAlmostEqual(ledger["cycle_long_reduce_pnl"]["1"], -0.26217136, places=8)

    def test_cycle_net_uses_long_confirmed_and_short_runtime_pnl(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        long_fill = FillEvent(
            exchange_order_id="ex-cycle-long",
            client_order_id="cycle-long",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=206.0,
            exec_price=0.12033,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-long-confirmed",
            metadata={
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "confirmed_closed_pnl": -0.14475136,
            },
        )
        short_fill = FillEvent(
            exchange_order_id="ex-cycle-short",
            client_order_id="cycle-short",
            side="short",
            purpose="CYCLE_1_SHORT_TP",
            exec_qty=103.0,
            exec_price=0.11919,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-cycle-short",
            metadata={
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "short_reduce_closed_pnl": 0.17510,
                "exec_pnl": 0.17510,
                "runtime_calculated_pnl": 0.17510,
            },
        )

        runtime.strategy._audit_exit_pnl_summary(long_fill, runtime.runtime_state, runtime.context)
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._audit_calc") as audit_calc_mock:
            runtime.strategy._audit_exit_pnl_summary(short_fill, runtime.runtime_state, runtime.context)

        ledger = runtime.runtime_state.strategy_state["audit_pnl_ledger"]
        self.assertAlmostEqual(ledger["cycle_long_reduce_pnl"]["1"], -0.14475136, places=8)
        self.assertAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], 0.17510, places=5)
        self.assertAlmostEqual(
            ledger["cycle_long_reduce_pnl"]["1"] + ledger["cycle_short_tp_pnl"]["1"],
            0.03034864,
            places=8,
        )
        exit_summary_payload = next(
            call.args[1]
            for call in audit_calc_mock.call_args_list
            if call.args and call.args[0] == "exit_pnl_summary"
        )
        self.assertAlmostEqual(exit_summary_payload["totals"]["cycle_net_pnl"], 0.03034864, places=8)
        self.assertEqual(
            exit_summary_payload["expected_vs_actual"]["actual_fill_pnl_source"],
            "short_reduce_closed_pnl",
        )

    def test_on_tick_blocks_flat_restart_when_snapshot_has_stale_exit_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="long-exit",
                    exchange_order_id="ex-long-exit",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="LONG_TP_EXIT",
                    order_type="Market",
                    reduce_only=True,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
            source="tick",
        )
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(state["fresh_restart_required"])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_flat_waiting_for_order_cleanup"
                for call in log_event_mock.call_args_list
            )
        )

    def test_on_tick_blocks_flat_restart_when_runtime_has_stale_exit_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["initial_entry_confirmed"] = True
        stale_order = ManagedOrder(
            client_order_id="short-exit",
            exchange_order_id="ex-short-exit",
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.5,
            metadata={},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        runtime.runtime_state.active_orders[stale_order.client_order_id] = stale_order
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy.on_tick(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(state["fresh_restart_required"])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_flat_waiting_for_order_cleanup"
                for call in log_event_mock.call_args_list
            )
        )

    def test_reconcile_skips_invalid_open_order_payload_without_crashing(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.active_orders["short-exit"] = ManagedOrder(
            client_order_id="short-exit",
            exchange_order_id="ex-short-exit",
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.5,
            metadata={},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        order_manager.open_orders = [None]

        with patch.object(runtime.audit, "log_event") as log_event_mock:
            runtime._reconcile_active_orders()

        self.assertTrue(
            any(
                call.args and call.args[0] == "reconcile_order_skip_invalid_payload"
                for call in log_event_mock.call_args_list
            )
        )

    def test_build_entry_intents_blocks_flat_with_stale_strategy_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="long-exit",
                    exchange_order_id="ex-long-exit",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="LONG_TP_EXIT",
                    order_type="Market",
                    reduce_only=True,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
            source="tick",
        )
        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_fresh_entry_blocked_active_strategy_orders"
                for call in log_event_mock.call_args_list
            )
        )

    def test_fresh_entry_blocked_when_snapshot_has_cycle_order(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        state = runtime.runtime_state.strategy_state
        state["final_trade_pnl_audited"] = True
        state["last_trade_pnl_complete"] = True
        state["last_trade_pnl_usdt"] = 0.5
        state["trade_block_id"] = "trade-cycle"
        state["last_trade_block_id"] = "trade-cycle"
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.1,
            "final_short_exit_pnl": -0.1,
            "total_realized_pnl": 0.0,
        }
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id="cycle-long-add",
                    exchange_order_id="ex-cycle-long-add",
                    side="long",
                    qty=1.0,
                    price=None,
                    purpose="CYCLE_1_LONG_ADD",
                    order_type="Market",
                    reduce_only=False,
                    status="OPEN",
                    filled_qty=0.0,
                    remaining_qty=1.0,
                    metadata={},
                ),
            ),
        )

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_event_mock:
            intents = runtime.strategy._build_entry_intents(snapshot, runtime.runtime_state, runtime.context)

        self.assertEqual(intents, [])
        self.assertTrue(
            any(
                call.args and call.args[0] == "fixed_cycle_fresh_entry_blocked_active_strategy_orders"
                for call in log_event_mock.call_args_list
            )
        )

    def test_build_exit_intents_skips_flat_snapshot(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        runtime.runtime_state.strategy_state["initial_entry_confirmed"] = True
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="tick",
        )

        intents = runtime.strategy._build_exit_intents(
            snapshot,
            runtime.runtime_state,
            current_cycle=0,
            break_even_price=100.0,
            tp_price=101.0,
            hard_stop_active=False,
            context=runtime.context,
        )

        self.assertEqual(intents, [])

    def test_on_start_true_fresh_start_still_builds_initial_entries(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            source="bootstrap",
        )

        intents = runtime.strategy.on_start(snapshot, runtime.runtime_state, runtime.context)

        purposes = {intent.purpose for intent in intents}
        self.assertIn("INITIAL_LONG_ENTRY", purposes)
        self.assertIn("INITIAL_SHORT_ENTRY", purposes)

    def test_final_exit_cleanup_waits_for_basket_completion_when_short_sl_fills_first(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        now = datetime.now(timezone.utc)

        long_exit = ManagedOrder(
            client_order_id="long-exit",
            exchange_order_id="ex-long-exit",
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=1.0,
            metadata={
                "trigger_price": 101.0,
                "basket_tp_price": 101.0,
                "basket_break_even_price": 100.0,
                "position_idx": 1,
            },
            created_at=now,
            updated_at=now,
        )
        short_exit = ManagedOrder(
            client_order_id="short-exit",
            exchange_order_id="ex-short-exit",
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            price=None,
            order_type="Market",
            reduce_only=True,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=0.5,
            metadata={
                "trigger_price": 100.99,
                "basket_tp_price": 101.0,
                "basket_break_even_price": 100.0,
                "position_idx": 2,
            },
            created_at=now,
            updated_at=now,
        )
        runtime.runtime_state.active_orders[long_exit.client_order_id] = long_exit
        runtime.runtime_state.active_orders[short_exit.client_order_id] = short_exit
        runtime.runtime_state.exchange_to_client_id[long_exit.exchange_order_id] = long_exit.client_order_id
        runtime.runtime_state.exchange_to_client_id[short_exit.exchange_order_id] = short_exit.client_order_id
        runtime.runtime_state.strategy_state["initial_entry_confirmed"] = True

        post_short_fill_snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.0,
            long_qty=1.0,
            short_qty=0.0,
            long_avg=100.0,
            short_avg=100.0,
            active_orders=(
                ActiveOrderSnapshot(
                    client_order_id=long_exit.client_order_id,
                    exchange_order_id=long_exit.exchange_order_id,
                    side=long_exit.side,
                    qty=long_exit.qty,
                    price=long_exit.price,
                    purpose=long_exit.purpose,
                    order_type=long_exit.order_type,
                    reduce_only=long_exit.reduce_only,
                    status=long_exit.status,
                    filled_qty=long_exit.filled_qty,
                    remaining_qty=long_exit.remaining_qty,
                    metadata=dict(long_exit.metadata),
                ),
            ),
            source="websocket",
            updated_at=now,
        )
        runtime.runtime_state.last_snapshot = post_short_fill_snapshot
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.0, "avgPrice": 0.0},
        ]

        short_fill = FillEvent(
            exchange_order_id="ex-short-exit",
            client_order_id="short-exit",
            side="short",
            purpose="SHORT_SL_EXIT",
            exec_qty=0.5,
            exec_price=101.0,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-short-exit",
            metadata={
                "trigger_price": 100.99,
                "basket_tp_price": 101.0,
                "basket_break_even_price": 100.0,
                "position_idx": 2,
            },
            occurred_at=now,
        )
        runtime.strategy.on_fill(short_fill, post_short_fill_snapshot, runtime.runtime_state, runtime.context)
        runtime.runtime_state.active_orders.pop("short-exit", None)
        runtime.runtime_state.exchange_to_client_id.pop("ex-short-exit", None)

        self.assertIn("long-exit", runtime.runtime_state.active_orders)
        self.assertEqual(order_manager.cancel_all_orders_calls, [])
        self.assertFalse(runtime.runtime_state.strategy_state.get("exit_locked"))

        flat_snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.01,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(),
            source="websocket",
            updated_at=now,
        )
        runtime.runtime_state.last_snapshot = flat_snapshot
        order_manager.positions = []
        runtime.runtime_state.active_orders["long-exit"].status = "FILLED"
        runtime.runtime_state.active_orders["long-exit"].filled_qty = 1.0
        runtime.runtime_state.active_orders["long-exit"].remaining_qty = 0.0

        long_fill = FillEvent(
            exchange_order_id="ex-long-exit",
            client_order_id="long-exit",
            side="long",
            purpose="LONG_TP_EXIT",
            exec_qty=1.0,
            exec_price=101.01,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            exec_id="exec-long-exit",
            metadata={
                "trigger_price": 101.0,
                "basket_tp_price": 101.0,
                "basket_break_even_price": 100.0,
                "position_idx": 1,
            },
            occurred_at=now,
        )
        runtime.strategy.on_fill(long_fill, flat_snapshot, runtime.runtime_state, runtime.context)

        self.assertGreaterEqual(len(order_manager.cancel_all_orders_calls), 1)
        self.assertNotIn("long-exit", runtime.runtime_state.active_orders)

    def test_final_exit_cleanup_delayed_when_unsettled_final_exit_order_remains(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        now = datetime.now(timezone.utc)
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=101.01,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            active_orders=(),
            source="websocket",
            updated_at=now,
        )
        runtime.runtime_state.active_orders["long-exit"] = ManagedOrder(
            client_order_id="long-exit",
            exchange_order_id="ex-long-exit",
            side="long",
            qty=210830.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="PARTIAL",
            filled_qty=198970.0,
            remaining_qty=11860.0,
            created_at=now,
            updated_at=now,
        )
        runtime.runtime_state.exchange_to_client_id["ex-long-exit"] = "long-exit"
        runtime.runtime_state.strategy_state["long_exit_filled"] = True
        runtime.runtime_state.strategy_state["short_exit_filled"] = True

        with (
            patch.object(runtime.context.audit, "log_event") as log_event_mock,
            patch.object(runtime.strategy, "_emit_final_trade_pnl_if_complete_or_fetch") as emit_pnl_mock,
        ):
            runtime.strategy._maybe_finalize_exit_after_leg_fill(
                runtime.runtime_state,
                runtime.context,
                runtime.strategy.LONG_TP_EXIT_PURPOSE,
            )

        self.assertEqual(order_manager.cancel_all_orders_calls, [])
        self.assertIn("long-exit", runtime.runtime_state.active_orders)
        self.assertIn("ex-long-exit", runtime.runtime_state.exchange_to_client_id)
        self.assertFalse(runtime.runtime_state.strategy_state.get("exit_locked"))
        emit_pnl_mock.assert_called_once()
        self.assertTrue(
            any(
                call.args and call.args[0] == "final_exit_cleanup_delayed_unsettled_final_orders"
                for call in log_event_mock.call_args_list
            )
        )

    def test_purge_active_orders_skips_unsettled_exit_orders(self) -> None:
        order_manager = FakeOrderManager()
        runtime = self.build_runtime(order_manager)
        now = datetime.now(timezone.utc)
        runtime.runtime_state.active_orders["long-exit"] = ManagedOrder(
            client_order_id="long-exit",
            exchange_order_id="ex-long-exit",
            side="long",
            qty=210830.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=None,
            order_type="Market",
            reduce_only=True,
            status="PARTIAL",
            filled_qty=198970.0,
            remaining_qty=11860.0,
            created_at=now,
            updated_at=now,
        )
        runtime.runtime_state.exchange_to_client_id["ex-long-exit"] = "long-exit"
        runtime.runtime_state.active_orders["cycle-order"] = ManagedOrder(
            client_order_id="cycle-order",
            exchange_order_id="ex-cycle-order",
            side="long",
            qty=100.0,
            purpose="CYCLE_1_LONG_ADD",
            price=100.0,
            order_type="Limit",
            reduce_only=False,
            status="OPEN",
            filled_qty=0.0,
            remaining_qty=100.0,
            created_at=now,
            updated_at=now,
        )
        runtime.runtime_state.exchange_to_client_id["ex-cycle-order"] = "cycle-order"

        with patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger.info") as info_mock:
            runtime.strategy._purge_active_orders(
                runtime.runtime_state,
                runtime.strategy._all_cycle_purposes() + runtime.strategy._exit_purposes(),
            )

        self.assertIn("long-exit", runtime.runtime_state.active_orders)
        self.assertNotIn("cycle-order", runtime.runtime_state.active_orders)
        self.assertIn("ex-long-exit", runtime.runtime_state.exchange_to_client_id)
        self.assertNotIn("ex-cycle-order", runtime.runtime_state.exchange_to_client_id)
        self.assertTrue(
            any(
                call.args and call.args[0] == "purge_active_orders_skipped_unsettled_exit %s"
                for call in info_mock.call_args_list
            )
        )

    def test_config_loader_enforces_expected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "fixed_cycle.json"
            config_file.write_text('{"base_notional_usdt": 150.0}', encoding="utf-8")
            with self.assertRaises(ValueError):
                FixedCycleHedgeConfig.from_json_file(config_file)

    def test_config_loader_allows_override_with_enforce_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "fixed_cycle.json"
            config_file.write_text('{"base_notional_usdt": 250.0, "hard_stop_cycle": 6}', encoding="utf-8")

            config = FixedCycleHedgeConfig.from_json_file(config_file, enforce_expected_path=False)

            self.assertEqual(config.base_notional_usdt, 250.0)
            self.assertEqual(config.hard_stop_cycle, 6)

    def test_cleanup_retries_and_verifies_empty(self) -> None:
        class FakeCleanupManager:
            def __init__(self) -> None:
                self.fetch_calls = 0

            def cancel_all_orders(self, *, symbol: str, category: str) -> bool:
                return True

            def fetch_open_orders(self, *, symbol: str, category: str):
                self.fetch_calls += 1
                if self.fetch_calls == 1:
                    return [{"orderLinkId": "fixed_cycle-test"}]
                return []

        manager = FakeCleanupManager()
        with patch.object(cleanup.logger, "info") as info_mock, patch(
            "fixed_cycle_hedge_bot.cleanup.time.sleep"
        ):
            success = cleanup.cleanup_all_strategy_orders_and_verify(
                "BTCUSDT",
                "linear",
                order_manager=manager,
                max_attempts=2,
                sleep_seconds=0.0,
            )

        self.assertTrue(success)
        event_names = [call.args[0] for call in info_mock.call_args_list if call.args]
        self.assertIn("fixed_cycle_exchange_cleanup_verified_empty", event_names)

    def test_cleanup_fails_when_orders_persist(self) -> None:
        class FakeCleanupManager:
            def cancel_all_orders(self, *, symbol: str, category: str) -> bool:
                return True

            def fetch_open_orders(self, *, symbol: str, category: str):
                return [{"clientOrderId": "fixed_cycle-fail"}]

        manager = FakeCleanupManager()
        with patch.object(cleanup.logger, "error") as error_mock, patch(
            "fixed_cycle_hedge_bot.cleanup.time.sleep"
        ):
            success = cleanup.cleanup_all_strategy_orders_and_verify(
                "BTCUSDT",
                "linear",
                order_manager=manager,
                max_attempts=2,
                sleep_seconds=0.0,
            )

        self.assertFalse(success)
        self.assertTrue(
            any(call.args and call.args[0] == "fixed_cycle_exchange_cleanup_failed" for call in error_mock.call_args_list)
        )




if __name__ == "__main__":
    unittest.main()

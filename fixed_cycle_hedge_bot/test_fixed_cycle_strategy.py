import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import ActiveOrderSnapshot, FillEvent, HedgeSnapshot, ManagedOrder
from fixed_cycle_hedge_bot.runtime import GenericHedgeRuntime, GenericRuntimeConfig


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

    def test_safe_float_helper(self) -> None:
        strategy = FixedCycleHedgeStrategy()
        self.assertIsNone(strategy._safe_float(None, None))
        self.assertEqual(strategy._safe_float("", 5.5), 5.5)
        self.assertAlmostEqual(strategy._safe_float("123.45", 0.0), 123.45)
        self.assertEqual(strategy._safe_float("invalid", 2.5), 2.5)

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
        runtime = self.build_runtime(order_manager)

        snapshot = runtime.bootstrap()

        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price, snapshot, runtime.runtime_state)
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
        purposes = {intent.purpose for intent in intents}
        self.assertIn("CYCLE_1_LONG_ADD", {order.purpose for order in runtime.runtime_state.active_orders.values()})
        self.assertNotIn("CYCLE_1_SHORT_REDUCE", {order.purpose for order in runtime.runtime_state.active_orders.values()})
        self.assertIn("LONG_TP_EXIT", purposes)
        self.assertIn("LONG_SL_EXIT", purposes)
        self.assertIn("SHORT_TP_EXIT", purposes)
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

    def test_fill_rebuilds_structure_and_advances_cycle_state(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
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
            force_exit_rebuild=False,
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
            )
            recorded_intents.append(result)
            return result

        with patch.object(FixedCycleHedgeStrategy, "_build_exit_intents", new=capturing):
            runtime.on_websocket_fill(
                long_cycle_order.exchange_order_id,
                qty=0.25,
                price=99.8,
                cumulative_qty=0.25,
            )

        self.assertEqual(runtime.runtime_state.strategy_state["current_long_cycle_index"], 1)
        purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
        self.assertIn("CYCLE_1_SHORT_REDUCE", purposes)
        self.assertNotIn("CYCLE_2_LONG_ADD", purposes)
        self.assertEqual(runtime.runtime_state.strategy_state.get("short_tp_pending_cycle"), 1)
        short_tp_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_SHORT_REDUCE"
        )
        latest_intents = recorded_intents[-1]
        tp_intent = next(intent for intent in latest_intents if intent.purpose == "LONG_TP_EXIT")
        expected_short_tp = tp_intent.trigger_price
        self.assertEqual(short_tp_order.price, expected_short_tp)
        self.assertEqual(short_tp_order.metadata.get("trigger_price"), expected_short_tp)
        self.assertGreaterEqual(len(order_manager.cancel_calls), 2)
        self.assertTrue(recorded_intents)
        latest_intents = recorded_intents[-1]
        self.assertTrue(any(intent.purpose == "LONG_TP_EXIT" for intent in latest_intents))
        self.assertTrue(any(intent.purpose == "LONG_SL_EXIT" for intent in latest_intents))
        self.assertTrue(any(intent.purpose == "SHORT_TP_EXIT" for intent in latest_intents))
        self.assertTrue(any(intent.purpose == "SHORT_SL_EXIT" for intent in latest_intents))

    def test_next_long_cycle_unlocks_only_after_short_follow_up_fill(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        runtime.on_websocket_fill(
            long_cycle_order.exchange_order_id,
            qty=0.25,
            price=99.8,
            cumulative_qty=0.25,
        )

        short_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_SHORT_REDUCE"
        )
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.75, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.425, "avgPrice": 100.0},
        ]
        runtime.on_websocket_fill(
            short_cycle_order.exchange_order_id,
            qty=0.075,
            price=101.7,
            cumulative_qty=0.075,
        )

        purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
        self.assertIn("CYCLE_2_LONG_ADD", purposes)
        self.assertNotIn("CYCLE_1_SHORT_REDUCE", purposes)
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_completed_count"), 1)
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

        self.assertGreater(break_even_with_loss, baseline_break_even)
        self.assertAlmostEqual(traces[0].details.get("realized_long_loss", 0.0), 1.0)
        self.assertAlmostEqual(traces[0].details.get("loss_compensation", 0.0), 1.0)

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

        short_exit = next(order for order in runtime.runtime_state.active_orders.values() if order.purpose == "SHORT_SL_EXIT")
        snapshot = runtime.runtime_state.last_snapshot
        break_even_price, _ = runtime.strategy._calculate_break_even(snapshot, runtime.runtime_state)
        tp_price = runtime.strategy._calculate_tp_price(break_even_price, snapshot, runtime.runtime_state)
        self.assertEqual(short_exit.metadata.get("basket_tp_price"), tp_price)
        self.assertEqual(short_exit.metadata.get("basket_break_even_price"), break_even_price)
        self.assertEqual(short_exit.metadata.get("replace_open_purpose"), ["SHORT_SL_EXIT"])

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
        long_sl_intent = next(intent for intent in intents if intent.purpose == "LONG_SL_EXIT")
        short_tp_intent = next(intent for intent in intents if intent.purpose == "SHORT_TP_EXIT")
        short_intent = next(intent for intent in intents if intent.purpose == "SHORT_SL_EXIT")

        self.assertEqual(long_intent.metadata.get("replace_open_purpose"), ["LONG_TP_EXIT"])
        self.assertEqual(long_sl_intent.metadata.get("replace_open_purpose"), ["LONG_SL_EXIT"])
        self.assertEqual(short_intent.metadata.get("replace_open_purpose"), ["SHORT_SL_EXIT"])
        self.assertEqual(short_tp_intent.metadata.get("replace_open_purpose"), ["SHORT_TP_EXIT"])

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
        long_sl_intent = next(intent for intent in intents if intent.purpose == "LONG_SL_EXIT")
        short_tp_intent = next(intent for intent in intents if intent.purpose == "SHORT_TP_EXIT")
        short_sl_intent = next(intent for intent in intents if intent.purpose == "SHORT_SL_EXIT")
        self.assertAlmostEqual(long_tp_intent.price or 0.0, tp_price, places=8)
        self.assertAlmostEqual(short_sl_intent.price or 0.0, tp_price, places=8)
        self.assertAlmostEqual(long_sl_intent.price or 0.0, break_even_price, places=8)
        self.assertAlmostEqual(short_tp_intent.price or 0.0, break_even_price, places=8)

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

        for intent in intents:
            if intent.purpose.endswith("_EXIT"):
                self.assertEqual(intent.order_type, "Market")
                self.assertTrue(intent.close_on_trigger)
                self.assertIsNotNone(intent.trigger_price)

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

        def capturing(self, snapshot, runtime_state, current_cycle, break_even_price, tp_price, hard_stop_active, context):
            recorded_break_evens.append((snapshot, break_even_price))
            return original_build_exit(snapshot, runtime_state, current_cycle, break_even_price, tp_price, hard_stop_active, context)

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

        self.assertGreater(recalculated_break_even, break_even_without_loss)

    def test_on_fill_invalidates_exit_signature_before_rebuilding(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 98.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 98.0},
        ]
        runtime = self.build_runtime(order_manager)
        runtime.bootstrap()

        state = runtime.runtime_state.strategy_state
        sentinel = {"legacy": "signature"}
        state["last_exit_signature"] = sentinel

        long_cycle_order = next(
            order for order in runtime.runtime_state.active_orders.values() if order.purpose == "CYCLE_1_LONG_ADD"
        )

        recorded_signatures = []
        original = runtime.strategy._build_exit_intents
        recorded_intents: list[list] = []

        def capturing(self, snapshot, runtime_state, current_cycle, break_even_price, tp_price, hard_stop_active, context):
            recorded_signatures.append(runtime_state.strategy_state.get("last_exit_signature"))
            result = original(snapshot, runtime_state, current_cycle, break_even_price, tp_price, hard_stop_active, context)
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
        self.assertTrue(any(intent.purpose == "LONG_SL_EXIT" for intent in latest_intents))
        self.assertTrue(any(intent.purpose == "SHORT_TP_EXIT" for intent in latest_intents))
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
            metadata={"cycle_index": 1},
        )
        runtime.strategy._advance_cycle_from_fill(short_fill, runtime.runtime_state)
        self.assertFalse(runtime.runtime_state.strategy_state.get("cycle_waiting_for_short_tp"))
        self.assertEqual(runtime.runtime_state.strategy_state.get("cycle_completed_count"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("current_short_cycle_index"), 1)
        self.assertEqual(runtime.runtime_state.strategy_state.get("short_tp_pending_cycle"), 0)

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

        purposes = {order.purpose for order in runtime.runtime_state.active_orders.values()}
        self.assertIn("CYCLE_1_LONG_ADD", purposes)
        if state_path.exists():
            state_path.unlink()

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

        self.assertEqual(len(order_manager.cancel_all_orders_calls), 1)
        self.assertNotIn("long-exit", runtime.runtime_state.active_orders)

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




if __name__ == "__main__":
    unittest.main()

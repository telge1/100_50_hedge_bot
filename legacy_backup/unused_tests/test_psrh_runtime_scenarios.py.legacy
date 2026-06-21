import logging
import threading
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone

from strategy.config import StrategyConfig
from strategy.execution.order_executor import OrderExecutor, OrderIntent
from strategy.position_manager import PositionManager
from strategy.psrh_strategy import PSRHStrategy
from strategy.risk_manager import RiskManager
from strategy.state_machine import StateMachine, StrategyState


class FakeOrderManager:
    def __init__(self) -> None:
        self.open_orders = []
        self.positions = []
        self.cancel_calls = []
        self.reduce_market_orders = []
        self.limit_orders = []
        self.market_orders = []
        self.trading_stop_calls = []
        self.trading_stop_clear_calls = []
        self.limit_order_response = True
        self.ensure_hedge_mode_calls = []
        self.set_leverage_calls = []
        self.ensure_max_leverage_calls = []

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def fetch_instrument_info(self, symbol: str, category: str) -> dict:
        return {
            "lotSizeFilter": {"qtyStep": "0.001"},
            "leverageFilter": {"maxLeverage": "25"},
        }

    def fetch_open_orders(self, symbol: str, category: str):
        if self.open_orders is None:
            return None
        return list(self.open_orders)

    def fetch_positions(
        self, symbol: str | None, category: str, settle_coin: str | None = None
    ):
        return list(self.positions)

    def ensure_hedge_mode(self, symbol: str, category: str = "linear") -> bool:
        self.ensure_hedge_mode_calls.append({"symbol": symbol, "category": category})
        return True

    def set_leverage(
        self,
        symbol: str,
        buy_leverage: int | float | str,
        sell_leverage: int | float | str,
        category: str = "linear",
    ) -> bool:
        self.set_leverage_calls.append(
            {
                "symbol": symbol,
                "buy_leverage": buy_leverage,
                "sell_leverage": sell_leverage,
                "category": category,
            }
        )
        return True

    def ensure_max_leverage(self, symbol: str, category: str = "linear") -> bool:
        self.ensure_max_leverage_calls.append(
            {"symbol": symbol, "category": category}
        )
        return True

    def _update_position(self, side: str, qty: float, price: float | None) -> None:
        if qty <= 0 or price is None:
            return
        pos = next(
            (
                p
                for p in self.positions
                if (p.get("side") or p.get("positionSide") or "").lower() in {side, side.capitalize()}
            ),
            None,
        )
        if not pos:
            pos = {"symbol": "BTCUSDT", "side": side, "size": 0.0, "avgPrice": 0.0}
            self.positions.append(pos)
        existing_size = float(pos.get("size") or pos.get("positionQty") or 0.0)
        existing_avg = float(pos.get("avgPrice") or pos.get("entryPrice") or 0.0)
        total_cost = existing_avg * existing_size + price * qty
        new_size = existing_size + qty
        pos["size"] = new_size
        pos["avgPrice"] = total_cost / new_size if new_size else 0.0

    def place_limit_order(self, payload) -> dict:
        self.limit_orders.append(payload)
        if self.limit_order_response is None:
            return None
        return {"result": {"orderId": f"ex-{payload.order_link_id}"}}

    def place_market_order(self, **kwargs) -> dict:
        self.market_orders.append(kwargs)
        price = kwargs.get("price")
        side = kwargs["side"]
        qty = float(kwargs["qty"])
        if side == "Buy":
            self._update_position("buy", qty, price)
        else:
            self._update_position("sell", qty, price)
        return {"result": {"orderId": f"ex-{kwargs['order_link_id']}"}}

    def place_reduce_market_order(self, **kwargs) -> dict:
        self.reduce_market_orders.append(kwargs)
        return {"result": {"orderId": f"ex-{kwargs.get('order_link_id', 'market')}"}}

    def cancel_order(
        self,
        order_id: str,
        *,
        symbol: str | None = None,
        category: str = "linear",
    ) -> bool:
        self.cancel_calls.append(
            {"order_id": order_id, "symbol": symbol, "category": category}
        )
        return True

    def set_long_take_profit(
        self,
        *,
        symbol: str,
        tp_price: float,
        position_size: float,
        position_idx: int = 1,
        category: str = "linear",
    ) -> dict:
        self.trading_stop_calls.append(
            {
                "symbol": symbol,
                "position_idx": position_idx,
                "position_size": position_size,
                "take_profit": tp_price,
                "stop_loss": None,
                "category": category,
            }
        )
        return {"result": {"symbol": symbol, "positionIdx": position_idx}}

    def set_short_stop_loss(
        self,
        *,
        symbol: str,
        sl_price: float,
        position_size: float,
        position_idx: int = 2,
        category: str = "linear",
    ) -> dict:
        self.trading_stop_calls.append(
            {
                "symbol": symbol,
                "position_idx": position_idx,
                "position_size": position_size,
                "take_profit": None,
                "stop_loss": sl_price,
                "category": category,
            }
        )
        return {"result": {"symbol": symbol, "positionIdx": position_idx}}

    def clear_long_take_profit(
        self,
        *,
        symbol: str,
        position_idx: int = 1,
        category: str = "linear",
    ) -> dict:
        self.trading_stop_clear_calls.append(
            {
                "symbol": symbol,
                "position_idx": position_idx,
                "field": "takeProfit",
                "category": category,
            }
        )
        return {"result": {"symbol": symbol, "positionIdx": position_idx}}

    def clear_short_stop_loss(
        self,
        *,
        symbol: str,
        position_idx: int = 2,
        category: str = "linear",
    ) -> dict:
        self.trading_stop_clear_calls.append(
            {
                "symbol": symbol,
                "position_idx": position_idx,
                "field": "stopLoss",
                "category": category,
            }
        )
        return {"result": {"symbol": symbol, "positionIdx": position_idx}}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_strategy(order_manager: FakeOrderManager | None = None) -> PSRHStrategy:
    strategy = PSRHStrategy.__new__(PSRHStrategy)
    config = StrategyConfig(
        api_key="",
        secret_key="",
        min_order_value=1.0,
        fast_poll_interval_seconds=0.05,
        order_sync_interval_seconds=0.05,
    )
    logger = logging.getLogger(f"psrh-test-{id(strategy)}")
    logger.handlers = []
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)

    strategy.config = config
    strategy.position_manager = PositionManager()
    strategy.state_machine = StateMachine()
    strategy.risk_manager = RiskManager(config)
    strategy.orders = []
    strategy.dca_steps = 0
    strategy.last_price = 100.0
    strategy.last_rebuy_time = None
    strategy.initialized = True
    strategy.order_manager = order_manager or FakeOrderManager()
    strategy._exchange_ready = True
    strategy.logger = logger
    strategy.last_rebuy_price = None
    strategy._extend_requested = False
    strategy._submitted_orders = set()
    strategy.active_orders = {}
    strategy._order_lock = threading.Lock()
    strategy._recent_orders = deque(maxlen=20)
    strategy._reconcile_thread = None
    strategy._fast_poll_thread = None
    strategy._position_sync_queue = deque()
    strategy._reconcile_stop = threading.Event()
    strategy._fast_poll_stop = threading.Event()
    strategy._exchange_lock = threading.Lock()
    strategy._position_sync_lock = threading.Lock()
    strategy._init_lock = threading.Lock()
    strategy._recovery_lock = threading.RLock()
    strategy._has_recovered = True
    strategy._last_status_log = None
    strategy._last_mismatch_log = None
    strategy._last_hedge_time = None
    strategy._startup_waiting_logged = False
    strategy._exchange_to_client_id = {}
    strategy._initial_hedge_checked = True
    strategy._post_rebuy_exit_target = None
    strategy._last_short_add_pre_spread_pct = None
    strategy._record_realized_pnl_by_side = lambda side, pnl: None
    strategy._long_heal_adds = 0
    strategy._short_heal_adds = 0
    strategy._spread_healing_active = False
    strategy._realized_long_pnl_total = 0.0
    strategy._realized_short_pnl_total = 0.0
    strategy._long_adds_in_cycle = 0
    strategy._short_adds_in_cycle = 0
    strategy._last_relevant_high = None
    strategy._last_relevant_low = None
    strategy._pending_rebuild_side = None
    strategy._pending_failover_side = None
    strategy._wait_reference_price = None
    strategy._last_structure_event = None
    strategy._preplaced_heal_orders_armed = False
    strategy._preplaced_heal_generation = 0
    strategy._active_preplaced_heal_long_client_id = None
    strategy._active_preplaced_heal_short_client_id = None
    strategy._preplaced_heal_rearm_in_progress = False

    strategy.executor = OrderExecutor(
        config=strategy.config,
        logger=strategy.logger,
        order_manager=strategy.order_manager,
        position_manager=strategy.position_manager,
        risk_manager=strategy.risk_manager,
        state_machine=strategy.state_machine,
        active_orders=strategy.active_orders,
        submitted_orders=strategy._submitted_orders,
        recent_orders=strategy._recent_orders,
        exchange_to_client_id=strategy._exchange_to_client_id,
        order_lock=strategy._order_lock,
        normalize_order_qty=strategy._normalize_order_qty,
        current_qty_step=strategy._current_qty_step,
        has_active_intent=strategy._has_active_intent,
        generate_client_order_id=strategy._generate_client_order_id,
        log_slippage_check=strategy._log_slippage_check,
        safe_update_order=strategy.safe_update_order,
        mark_order_filled=strategy.mark_order_filled,
        record_realized_pnl_by_side=strategy._record_realized_pnl_by_side,
        handle_order_finalized_locked=strategy._handle_order_finalized_locked,
        sync_positions_with_exchange=strategy.sync_positions_with_exchange,
        get_position_snapshot=strategy._get_position_snapshot,
        verify_order_on_exchange=strategy.verify_order_on_exchange,
        get_last_price=strategy._get_last_price,
        set_dca_steps=strategy._set_dca_steps,
        on_intent_executed=strategy._on_intent_executed,
    )
    strategy.state_machine.transition(StrategyState.WAIT_FOR_HEDGE)
    return strategy


def seed_active_order(
    strategy: PSRHStrategy,
    *,
    client_id: str = "cid-1",
    exchange_order_id: str = "ex-1",
    side: str = "long",
    purpose: str = "LONG_REBUY",
    status: str = "OPEN",
    size: float = 1.0,
    price: float = 100.0,
    exchange_confirmed: bool = True,
    created_at: datetime | None = None,
) -> str:
    created_at = created_at or utcnow()
    order = {
        "side": side,
        "purpose": purpose,
        "price": price,
        "size": size,
        "qty": size,
        "status": status,
        "created_at": created_at,
        "verify_attempts": 0,
        "remaining_qty": size,
        "partial_handled": False,
        "metadata": {},
        "retry_count": 0,
    }
    if exchange_confirmed:
        order["exchange_confirmed"] = True
    if exchange_order_id:
        order["exchange_order_id"] = exchange_order_id
        strategy._exchange_to_client_id[exchange_order_id] = client_id

    strategy.active_orders[client_id] = order
    strategy._recent_orders.append(client_id)
    strategy._submitted_orders.add((side, round(size, 4), purpose))
    return client_id


class PSRHRuntimeScenarioTests(unittest.TestCase):
    def test_verify_fetch_failed_does_not_imply_empty_snapshot(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(
            strategy,
            status="PENDING_SUBMIT",
            exchange_order_id="ex-fetch-failed",
            exchange_confirmed=False,
        )
        strategy.order_manager.open_orders = None

        verified = strategy.verify_order_on_exchange(
            client_id,
            source="test",
            retries=1,
            delay=0.0,
            log_missing=True,
        )

        self.assertFalse(verified)
        self.assertEqual(strategy.active_orders[client_id]["status"], "PENDING_SUBMIT")
        self.assertFalse(strategy.active_orders[client_id].get("exchange_confirmed", False))
        self.assertEqual(strategy.active_orders[client_id]["verify_attempts"], 1)
        self.assertEqual(
            strategy.active_orders[client_id]["metadata"]["verification_last_result"],
            "fetch_failed",
        )
        self.assertNotIn("missing_from_exchange_count", strategy.active_orders[client_id]["metadata"])

    def test_unknown_requires_repeated_missing_snapshots(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(strategy, exchange_order_id="ex-missing")
        strategy.order_manager.open_orders = []

        strategy.sync_orders_with_exchange(event_source="reconcile")
        self.assertEqual(strategy.active_orders[client_id]["status"], "OPEN")
        self.assertEqual(
            strategy.active_orders[client_id]["metadata"]["missing_from_exchange_count"],
            1,
        )

        strategy.sync_orders_with_exchange(event_source="reconcile")
        self.assertEqual(strategy.active_orders[client_id]["status"], "UNKNOWN")
        self.assertEqual(
            strategy.active_orders[client_id]["metadata"]["missing_from_exchange_count"],
            2,
        )

    def test_unknown_recovers_to_open_when_order_reappears_as_new(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(
            strategy,
            exchange_order_id="ex-unknown-recover",
            status="UNKNOWN",
        )
        strategy.active_orders[client_id]["metadata"]["missing_from_exchange_count"] = 2
        strategy.active_orders[client_id]["metadata"]["missing_since"] = utcnow().isoformat()
        strategy.order_manager.open_orders = [
            {
                "orderLinkId": client_id,
                "orderId": "ex-unknown-recover",
                "orderStatus": "New",
                "cumExecQty": "0.0",
                "qty": "1.0",
            }
        ]

        strategy.sync_orders_with_exchange(event_source="reconcile")

        self.assertEqual(strategy.active_orders[client_id]["status"], "OPEN")
        self.assertEqual(
            strategy.active_orders[client_id]["metadata"]["missing_from_exchange_count"],
            0,
        )
        self.assertNotIn("missing_since", strategy.active_orders[client_id]["metadata"])

    def test_visible_stale_open_does_not_force_reduce_market_fallback(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(
            strategy,
            purpose="SHORT_REBALANCE",
            exchange_order_id="ex-visible-open",
            created_at=utcnow() - timedelta(seconds=10),
        )
        strategy.order_manager.open_orders = [
            {
                "orderLinkId": client_id,
                "orderId": "ex-visible-open",
                "orderStatus": "New",
                "cumExecQty": "0.0",
                "qty": "1.0",
            }
        ]

        strategy.sync_orders_with_exchange(event_source="reconcile")

        self.assertEqual(len(strategy.order_manager.cancel_calls), 1)
        self.assertEqual(strategy.order_manager.cancel_calls[0]["order_id"], "ex-visible-open")
        self.assertEqual(strategy.order_manager.reduce_market_orders, [])
        self.assertEqual(strategy.active_orders[client_id]["status"], "OPEN")

    def test_filled_handled_is_not_reactivated_by_later_rest_or_ws(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(strategy, exchange_order_id="ex-final-guard")

        strategy.on_order_fill_event(client_id, source="reconcile")
        strategy.order_manager.open_orders = [
            {
                "orderLinkId": client_id,
                "orderId": "ex-final-guard",
                "orderStatus": "Filled",
                "cumExecQty": "1.0",
                "qty": "1.0",
            }
        ]

        strategy.sync_orders_with_exchange(event_source="reconcile")
        strategy.on_websocket_fill("ex-final-guard", qty=1.0, price=100.0)

        self.assertNotIn(client_id, strategy.active_orders)
        self.assertNotIn(client_id, strategy._recent_orders)
        self.assertNotIn(("long", 1.0, "LONG_REBUY"), strategy._submitted_orders)
        self.assertNotIn("ex-final-guard", strategy._exchange_to_client_id)

    def test_ws_final_fill_before_rest_sync_has_no_double_fill_handling(self) -> None:
        strategy = build_strategy()
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            exchange_order_id="ex-ws-first",
        )

        fill_calls = []
        follow_ups = []
        original = strategy.on_order_fill_event

        def counted_fill(client_order_id: str, source: str = "reconcile") -> None:
            fill_calls.append((client_order_id, source))
            original(client_order_id, source)

        strategy.on_order_fill_event = counted_fill
        strategy.adjust_short_hedge = lambda price, spread=0.0, long_size_override=None: OrderIntent(
            side="short",
            qty=0.5,
            price=price,
            purpose="SHORT_REBALANCE",
        )
        strategy._execute_intents = lambda intents: follow_ups.extend(intents)

        strategy.on_websocket_fill("ex-ws-first", qty=1.0, price=100.0)
        strategy.sync_orders_with_exchange(event_source="reconcile")

        self.assertEqual(len(fill_calls), 1)
        self.assertEqual(fill_calls[0][1], "websocket")
        self.assertEqual([intent.purpose for intent in follow_ups], ["SHORT_REBALANCE"])
        self.assertNotIn("cid-1", strategy.active_orders)

    def test_ws_final_fill_with_cumulative_only_still_triggers_follow_up(self) -> None:
        strategy = build_strategy()
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            exchange_order_id="ex-ws-cum-only",
        )

        fill_calls = []
        follow_ups = []
        original = strategy.on_order_fill_event

        def counted_fill(client_order_id: str, source: str = "reconcile") -> None:
            fill_calls.append((client_order_id, source))
            original(client_order_id, source)

        strategy.on_order_fill_event = counted_fill
        strategy.adjust_short_hedge = lambda price, spread=0.0, long_size_override=None: OrderIntent(
            side="short",
            qty=0.5,
            price=price,
            purpose="SHORT_REBALANCE",
        )
        strategy._execute_intents = lambda intents: follow_ups.extend(intents)

        strategy.on_websocket_fill(
            "ex-ws-cum-only",
            qty=0.0,
            price=100.0,
            cumulative_qty=1.0,
        )

        self.assertEqual(fill_calls, [("cid-1", "websocket")])
        self.assertEqual([intent.purpose for intent in follow_ups], ["SHORT_REBALANCE"])
        self.assertNotIn("cid-1", strategy.active_orders)

    def test_rest_final_fill_without_ws_handles_once_and_cleans_up(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(strategy, exchange_order_id="ex-rest-fill")

        strategy.order_manager.open_orders = [
            {
                "orderLinkId": client_id,
                "orderId": "ex-rest-fill",
                "orderStatus": "Filled",
                "cumExecQty": "1.0",
                "qty": "1.0",
            }
        ]

        fill_calls = []
        original = strategy.on_order_fill_event

        def counted_fill(client_order_id: str, source: str = "reconcile") -> None:
            fill_calls.append((client_order_id, source))
            original(client_order_id, source)

        strategy.on_order_fill_event = counted_fill

        strategy.sync_orders_with_exchange(event_source="reconcile")

        self.assertEqual(fill_calls, [(client_id, "reconcile")])
        self.assertNotIn(client_id, strategy.active_orders)
        self.assertNotIn(client_id, strategy._recent_orders)
        self.assertNotIn(("long", 1.0, "LONG_REBUY"), strategy._submitted_orders)
        self.assertNotIn("ex-rest-fill", strategy._exchange_to_client_id)

    def test_long_rebuy_hedge_final_fill_triggers_exactly_one_short_rebalance(self) -> None:
        strategy = build_strategy()
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            exchange_order_id="ex-follow-up",
        )

        follow_ups = []
        strategy.adjust_short_hedge = lambda price, spread=0.0, long_size_override=None: OrderIntent(
            side="short",
            qty=0.5,
            price=price,
            purpose="SHORT_REBALANCE",
        )
        strategy._execute_intents = lambda intents: follow_ups.extend(intents)

        strategy.on_websocket_fill("ex-follow-up", qty=1.0, price=100.0)
        strategy.on_websocket_fill("ex-follow-up", qty=1.0, price=100.0)

        self.assertEqual([intent.purpose for intent in follow_ups], ["SHORT_REBALANCE"])

    def test_hedge_recover_fill_sets_exit_orders_before_rebuy(self) -> None:
        strategy = build_strategy()
        strategy.position_manager.sync_positions(1000.0, 100.0, 500.0, 98.0)
        strategy.order_manager.positions = [
            {"side": "Buy", "size": "1000.0", "avgPrice": "100.0"},
            {"side": "Sell", "size": "500.0", "avgPrice": "98.0"},
        ]
        seed_active_order(
            strategy,
            side="short",
            purpose="HEDGE_RECOVER",
            exchange_order_id="ex-hedge-ready",
            price=98.0,
        )

        follow_ups = []
        strategy.place_long_rebuy = (
            lambda price, spread, allow_bypass_recovery_low=False: OrderIntent(
                side="long",
                qty=1.0,
                price=97.35,
                purpose="LONG_REBUY",
            )
        )
        strategy._execute_intents = lambda intents: follow_ups.extend(intents)

        strategy.on_websocket_fill("ex-hedge-ready", qty=1.0, price=98.0)

        self.assertEqual([intent.purpose for intent in follow_ups], ["LONG_REBUY"])
        self.assertEqual(
            [call["field"] for call in strategy.order_manager.trading_stop_clear_calls],
            ["takeProfit", "stopLoss"],
        )
        self.assertEqual(len(strategy.order_manager.trading_stop_calls), 2)
        self.assertEqual(strategy.order_manager.trading_stop_calls[0]["position_idx"], 1)
        self.assertEqual(strategy.order_manager.trading_stop_calls[1]["position_idx"], 2)
        self.assertIsNotNone(strategy._post_rebuy_exit_target)

    def test_short_rebalance_final_fill_submits_next_rebuy(self) -> None:
        strategy = build_strategy()
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.position_manager.sync_positions(1000.0, 100.0, 500.0, 98.0)
        strategy.order_manager.positions = [
            {"side": "Buy", "size": "1000.0", "avgPrice": "100.0"},
            {"side": "Sell", "size": "500.0", "avgPrice": "98.0"},
        ]
        seed_active_order(
            strategy,
            side="short",
            purpose="SHORT_REBALANCE",
            exchange_order_id="ex-short-rebalance",
        )

        follow_ups = []
        strategy.place_long_rebuy = (
            lambda price, spread, allow_bypass_recovery_low=False: OrderIntent(
                side="long",
                qty=1.25,
                price=97.35,
                purpose="LONG_REBUY",
            )
        )
        strategy._execute_intents = lambda intents: follow_ups.extend(intents)

        strategy.on_websocket_fill("ex-short-rebalance", qty=1.0, price=98.0)

        self.assertEqual([intent.purpose for intent in follow_ups], ["LONG_REBUY"])
        self.assertIsNotNone(strategy._post_rebuy_exit_target)
        self.assertEqual(
            [call["field"] for call in strategy.order_manager.trading_stop_clear_calls],
            ["takeProfit", "stopLoss"],
        )
        self.assertEqual(len(strategy.order_manager.trading_stop_calls), 2)

    def test_short_rebalance_fill_submits_rebuy_before_exit_refresh(self) -> None:
        strategy = build_strategy()
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.position_manager.sync_positions(1000.0, 100.0, 500.0, 98.0)
        seed_active_order(
            strategy,
            side="short",
            purpose="SHORT_REBALANCE",
            exchange_order_id="ex-short-rebalance-ordering",
        )

        call_order: list[str] = []
        strategy._submit_next_short_based_rebuy = lambda reference_price: call_order.append("rebuy") or True
        strategy._set_cycle_profit_exit_orders = lambda: call_order.append("exits") or True

        strategy.on_websocket_fill("ex-short-rebalance-ordering", qty=1.0, price=98.0)

        self.assertEqual(call_order, ["rebuy", "exits"])

    def test_place_long_rebuy_uses_quote_notional_base(self) -> None:
        strategy = build_strategy()
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.config.long_entry_size = 70.0
        strategy.config.user.rebuy_size_multiplier_base = 0.50
        strategy.config.user.rebuy_size_multiplier_increment = 0.025
        strategy.config.user.rebuy_size_multiplier_span = 0.005
        strategy.config.max_total_notional = 20_000.0
        strategy.position_manager.sync_positions(100.0, 100.0, 50.0, 102.0)
        strategy.last_price = 102.0
        strategy.last_rebuy_price = 101.0

        intent = strategy.place_long_rebuy(102.0, spread=0.02)

        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertAlmostEqual(intent.qty * intent.price, 35.0, places=6)
        self.assertGreater(intent.qty, 0.3)

    def test_place_long_rebuy_uses_positive_long_minus_short_spread(self) -> None:
        strategy = build_strategy()
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.config.long_entry_size = 70.0
        strategy.config.user.rebuy_size_multiplier_base = 0.50
        strategy.config.max_total_notional = 20_000.0
        strategy.position_manager.sync_positions(100.0, 100.0, 50.0, 98.0)
        strategy.last_price = 98.0
        strategy.last_rebuy_price = 97.0

        intent = strategy.place_long_rebuy(98.0, spread=0.02)

        self.assertIsNotNone(intent)
        assert intent is not None
        expected_level = 98.0 * (1 - (0.02 / 3))
        self.assertAlmostEqual(intent.price, expected_level, places=6)

    def test_place_long_rebuy_restart_without_active_rebuy_does_not_skip_identical_price(self) -> None:
        strategy = build_strategy()
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.config.long_entry_size = 70.0
        strategy.config.user.rebuy_size_multiplier_base = 0.50
        strategy.config.max_total_notional = 20_000.0
        strategy.position_manager.sync_positions(100.0, 100.0, 50.0, 98.0)
        strategy.last_price = 97.35
        strategy.last_rebuy_price = 97.35

        intent = strategy.place_long_rebuy(97.35, spread=0.02)

        self.assertIsNotNone(intent)

    def test_place_long_rebuy_with_active_rebuy_still_skips_identical_price(self) -> None:
        strategy = build_strategy()
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.config.long_entry_size = 70.0
        strategy.config.user.rebuy_size_multiplier_base = 0.50
        strategy.config.max_total_notional = 20_000.0
        strategy.position_manager.sync_positions(100.0, 100.0, 50.0, 98.0)
        strategy.last_price = 97.35
        strategy.last_rebuy_price = 97.35
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            status="OPEN",
            size=1.0,
            price=97.35,
        )

        intent = strategy.place_long_rebuy(97.35, spread=0.02)

        self.assertIsNone(intent)

    def test_short_rebalance_executes_as_market_order(self) -> None:
        strategy = build_strategy()
        strategy.position_manager.sync_positions(986.0, 0.08876958, 398.0, 0.08752)
        strategy.order_manager.positions = [
            {"side": "Buy", "size": "986.0", "avgPrice": "0.08876958"},
            {"side": "Sell", "size": "398.0", "avgPrice": "0.08752"},
        ]
        intent = OrderIntent(
            side="short",
            qty=95.0,
            price=0.0870824,
            purpose="SHORT_REBALANCE",
        )

        executed = strategy.executor.execute_intent(intent)

        self.assertTrue(executed)
        self.assertEqual(len(strategy.order_manager.market_orders), 1)
        self.assertEqual(strategy.order_manager.market_orders[0]["side"], "Sell")
        self.assertEqual(strategy.order_manager.limit_orders, [])

    def test_failed_limit_rebuy_submission_falls_back_to_market_when_price_lower(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.limit_order_response = None
        strategy = build_strategy(order_manager)
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.config.max_total_notional = 20_000.0
        strategy.position_manager.sync_positions(100.0, 100.0, 50.0, 98.0)
        strategy.last_price = 97.0
        strategy.last_rebuy_price = 96.0

        intent = strategy.place_long_rebuy(98.0, spread=0.02)

        self.assertIsNotNone(intent)
        assert intent is not None
        executed = strategy.executor.execute_intent(intent)

        self.assertTrue(executed)
        self.assertEqual(strategy._submitted_orders, set())
        self.assertGreaterEqual(len(strategy.order_manager.market_orders), 1)
        self.assertEqual(strategy.order_manager.market_orders[0]["side"], "Buy")
        self.assertGreaterEqual(len(strategy.order_manager.limit_orders), 1)

    def test_failed_limit_rebuy_submission_cleans_up_when_price_not_lower(self) -> None:
        order_manager = FakeOrderManager()
        order_manager.limit_order_response = None
        strategy = build_strategy(order_manager)
        strategy.state_machine.transition(StrategyState.RECOVERY)
        strategy.config.max_total_notional = 20_000.0
        strategy.position_manager.sync_positions(100.0, 100.0, 50.0, 98.0)
        strategy.last_price = 98.5
        strategy.last_rebuy_price = 96.0

        intent = strategy.place_long_rebuy(98.0, spread=0.02)

        self.assertIsNotNone(intent)
        assert intent is not None
        executed = strategy.executor.execute_intent(intent)

        self.assertFalse(executed)
        self.assertEqual(strategy._submitted_orders, set())
        self.assertEqual(strategy.active_orders, {})
        self.assertEqual(strategy.order_manager.market_orders, [])

    def test_ws_partials_do_not_follow_up_until_final_fill(self) -> None:
        strategy = build_strategy()
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            exchange_order_id="ex-partial",
            size=3.0,
        )

        follow_ups = []
        strategy.adjust_short_hedge = lambda price, spread=0.0, long_size_override=None: OrderIntent(
            side="short",
            qty=0.5,
            price=price,
            purpose="SHORT_REBALANCE",
        )
        strategy._execute_intents = lambda intents: follow_ups.extend(intents)

        strategy.on_websocket_fill("ex-partial", qty=1.0, price=100.0)
        self.assertEqual(follow_ups, [])
        self.assertEqual(strategy.active_orders["cid-1"]["status"], "PARTIAL")

        strategy.on_websocket_fill("ex-partial", qty=1.0, price=100.0)
        self.assertEqual(follow_ups, [])
        self.assertEqual(strategy.active_orders["cid-1"]["status"], "PARTIAL")

        strategy.on_websocket_fill("ex-partial", qty=1.0, price=100.0)
        self.assertEqual([intent.purpose for intent in follow_ups], ["SHORT_REBALANCE"])
        self.assertNotIn("cid-1", strategy.active_orders)

    def test_ws_duplicate_exec_id_is_ignored(self) -> None:
        strategy = build_strategy()
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            exchange_order_id="ex-dup-exec",
            size=2.0,
        )

        strategy.on_websocket_fill(
            "ex-dup-exec",
            qty=1.0,
            price=100.0,
            exec_id="exec-1",
            cumulative_qty=1.0,
        )
        strategy.on_websocket_fill(
            "ex-dup-exec",
            qty=1.0,
            price=100.0,
            exec_id="exec-1",
            cumulative_qty=1.0,
        )

        self.assertEqual(strategy.active_orders["cid-1"]["status"], "PARTIAL")
        self.assertEqual(strategy.active_orders["cid-1"]["filled_qty"], 1.0)
        self.assertEqual(strategy.active_orders["cid-1"]["remaining_qty"], 1.0)

    def test_ws_stale_cumulative_fill_is_ignored(self) -> None:
        strategy = build_strategy()
        seed_active_order(
            strategy,
            purpose="LONG_REBUY_HEDGE",
            exchange_order_id="ex-stale-cum",
            size=3.0,
        )

        strategy.on_websocket_fill(
            "ex-stale-cum",
            qty=1.0,
            price=100.0,
            exec_id="exec-1",
            cumulative_qty=2.0,
        )
        strategy.on_websocket_fill(
            "ex-stale-cum",
            qty=1.0,
            price=100.0,
            exec_id="exec-2",
            cumulative_qty=2.0,
        )

        self.assertEqual(strategy.active_orders["cid-1"]["status"], "PARTIAL")
        self.assertEqual(strategy.active_orders["cid-1"]["filled_qty"], 2.0)
        self.assertEqual(strategy.active_orders["cid-1"]["remaining_qty"], 1.0)

    def test_open_stale_missing_snapshot_defers_market_fallback(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(
            strategy,
            exchange_order_id="ex-stale-open",
            created_at=utcnow() - timedelta(seconds=10),
        )
        strategy.order_manager.open_orders = []

        strategy.sync_orders_with_exchange(event_source="reconcile")

        self.assertEqual(strategy.order_manager.cancel_calls, [])
        self.assertEqual(strategy.order_manager.reduce_market_orders, [])
        self.assertEqual(strategy.active_orders[client_id]["status"], "OPEN")
        self.assertEqual(
            strategy.active_orders[client_id]["metadata"]["missing_from_exchange_count"],
            1,
        )

    def test_pending_submit_with_exchange_id_does_not_resubmit(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(
            strategy,
            status="PENDING_SUBMIT",
            exchange_order_id="ex-pending",
            exchange_confirmed=False,
            created_at=utcnow() - timedelta(seconds=10),
        )
        strategy.order_manager.open_orders = []

        resubmits = []
        strategy.executor._place_order_on_exchange = (
            lambda *args, **kwargs: resubmits.append((args, kwargs)) or True
        )

        strategy.sync_orders_with_exchange(event_source="reconcile")

        self.assertEqual(resubmits, [])
        self.assertIn(client_id, strategy.active_orders)
        self.assertEqual(strategy.active_orders[client_id]["status"], "PENDING_SUBMIT")

    def test_finalized_order_cleanup_removes_all_tracking_entries(self) -> None:
        strategy = build_strategy()
        client_id = seed_active_order(strategy, exchange_order_id="ex-cleanup")

        strategy.on_order_fill_event(client_id, source="reconcile")

        self.assertNotIn(client_id, strategy.active_orders)
        self.assertNotIn(client_id, strategy._recent_orders)
        self.assertNotIn(("long", 1.0, "LONG_REBUY"), strategy._submitted_orders)
        self.assertNotIn("ex-cleanup", strategy._exchange_to_client_id)

    def test_find_active_symbol_from_positions_prefers_long(self) -> None:
        strategy = build_strategy()

        symbol = strategy._find_active_symbol_from_positions(
            [
                {"symbol": "BTCUSDT", "side": "Sell", "size": "10"},
                {"symbol": "ETHUSDT", "side": "Buy", "size": "5"},
            ]
        )

        self.assertEqual(symbol, "ETHUSDT")

    def test_ensure_exchange_ready_uses_max_leverage_helper(self) -> None:
        order_manager = FakeOrderManager()
        strategy = build_strategy(order_manager)
        strategy._exchange_ready = False
        strategy._has_recovered = True
        strategy._recover_state_from_exchange = lambda: None

        strategy._ensure_exchange_ready()

        self.assertEqual(
            order_manager.ensure_hedge_mode_calls,
            [{"symbol": strategy.config.default_symbol, "category": strategy.config.category}],
        )
        self.assertEqual(
            order_manager.ensure_max_leverage_calls,
            [{"symbol": strategy.config.default_symbol, "category": strategy.config.category}],
        )
        self.assertEqual(order_manager.set_leverage_calls, [])


if __name__ == "__main__":
    unittest.main()

import logging
import threading
from collections import deque
from datetime import datetime, timezone

from emergency_100.final_hedge_strategy import PSRHStrategy
from strategy.config import StrategyConfig
from strategy.execution.order_executor import OrderExecutor
from strategy.position_manager import PositionManager
from strategy.risk_manager import RiskManager
from strategy.state_machine import StateMachine, StrategyState


class FakeOrderManager:
    def __init__(self) -> None:
        self.positions = []
        self.open_orders = []
        self.limit_orders = []
        self.market_orders = []
        self.reduce_market_orders = []
        self.cancel_calls = []

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def fetch_instrument_info(self, symbol: str, category: str) -> dict:
        return {
            "lotSizeFilter": {"qtyStep": "0.001"},
            "leverageFilter": {"maxLeverage": "25"},
        }

    def fetch_positions(
        self, symbol: str | None, category: str, settle_coin: str | None = None
    ):
        return list(self.positions)

    def fetch_open_orders(self, symbol: str | None = None, category: str = "linear"):
        return list(self.open_orders)

    def place_limit_order(self, payload) -> dict:
        self.limit_orders.append(payload)
        return {"result": {"orderId": f"ex-{payload.order_link_id}"}}

    def place_market_order(self, **kwargs) -> dict:
        self.market_orders.append(kwargs)
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_strategy() -> PSRHStrategy:
    strategy = PSRHStrategy.__new__(PSRHStrategy)
    strategy.config = StrategyConfig()
    strategy.config.min_order_value = 1.0
    strategy.config.default_symbol = "BTCUSDT"
    strategy.config.category = "linear"
    strategy.position_manager = PositionManager()
    strategy.state_machine = StateMachine()
    strategy.state_machine.transition(StrategyState.WAIT_NO_ACTION)
    strategy.risk_manager = RiskManager(strategy.config)
    strategy.orders = []
    strategy.dca_steps = 0
    strategy.last_price = 100.0
    strategy.last_rebuy_time = None
    strategy.initialized = True
    strategy.order_manager = FakeOrderManager()
    strategy._exchange_ready = True
    strategy.logger = logging.getLogger(f"test.final.paired-close.{id(strategy)}")
    strategy.logger.handlers = []
    strategy.logger.addHandler(logging.NullHandler())
    strategy.logger.propagate = False
    strategy.logger.setLevel(logging.CRITICAL)
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
    strategy._aggressive_down_heal_initial_short_size = None
    strategy._aggressive_down_heal_reference_price = None
    strategy._aggressive_down_heal_phase_completed = False
    strategy._phase2_short_profit_budget_reserved = 0.0
    strategy._phase3_long_target_reference_size = None
    strategy._phase4_short_target_reference_size = None
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
    return strategy


def seed_active_order(
    strategy: PSRHStrategy,
    *,
    client_id: str,
    exchange_order_id: str,
    side: str,
    purpose: str,
    size: float,
    price: float,
) -> str:
    order = {
        "side": side,
        "purpose": purpose,
        "price": price,
        "size": size,
        "qty": size,
        "status": "OPEN",
        "created_at": utcnow(),
        "verify_attempts": 0,
        "remaining_qty": size,
        "partial_handled": False,
        "metadata": {},
        "retry_count": 0,
        "exchange_confirmed": True,
        "exchange_order_id": exchange_order_id,
    }
    strategy.active_orders[client_id] = order
    strategy._recent_orders.append(client_id)
    strategy._submitted_orders.add((side, round(size, 4), purpose))
    strategy._exchange_to_client_id[exchange_order_id] = client_id
    return client_id


def test_spread_heal_short_fill_creates_paired_partial_sl_long() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(10.0, 100.0, 10.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 10.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 10.0, "avgPrice": 98.0},
    ]
    client_id = seed_active_order(
        strategy,
        client_id="cid-short-heal-1",
        exchange_order_id="ex-short-heal-1",
        side="short",
        purpose="spread_heal_short",
        size=1.0,
        price=101.0,
    )

    strategy.on_order_fill_event(client_id, source="reconcile")

    paired_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "paired_partial_sl_long"
    ]
    assert len(paired_orders) == 1
    expected_trigger = 101.0 * (1 + 0.02 + 0.002)
    assert abs(paired_orders[0]["size"] - 1.0) < 1e-9
    assert abs(paired_orders[0]["price"] - expected_trigger) < 1e-9
    assert strategy.order_manager.limit_orders[-1].reduce_only is True
    assert strategy.order_manager.limit_orders[-1].side == "Sell"
    assert abs(strategy.order_manager.limit_orders[-1].price - expected_trigger) < 1e-9


def test_spread_heal_long_fill_creates_paired_partial_sl_short() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(10.0, 100.0, 10.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 10.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 10.0, "avgPrice": 98.0},
    ]
    client_id = seed_active_order(
        strategy,
        client_id="cid-long-heal-1",
        exchange_order_id="ex-long-heal-1",
        side="long",
        purpose="spread_heal_long",
        size=1.0,
        price=95.0,
    )

    strategy.on_order_fill_event(client_id, source="reconcile")

    paired_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "paired_partial_sl_short"
    ]
    assert len(paired_orders) == 1
    expected_trigger = 95.0 * (1 - 0.02 - 0.002)
    assert abs(paired_orders[0]["size"] - 1.0) < 1e-9
    assert abs(paired_orders[0]["price"] - expected_trigger) < 1e-9
    assert strategy.order_manager.limit_orders[-1].reduce_only is True
    assert strategy.order_manager.limit_orders[-1].side == "Buy"
    assert abs(strategy.order_manager.limit_orders[-1].price - expected_trigger) < 1e-9


def test_phase2_budget_is_consumed_on_fill_not_on_intent_build() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(10.0, 100.0, 0.0, 0.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 5.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 0.0, "avgPrice": 0.0},
    ]
    strategy._realized_short_pnl_total = 10.0
    strategy._aggressive_down_heal_phase_completed = True
    strategy.config.enable_phase2_short_profit_long_reduce = True

    intent = strategy._build_phase2_long_reduce_from_short_profit_intent(price=98.0)

    assert intent is not None
    assert strategy._phase2_short_profit_budget_reserved == 0.0

    client_id = seed_active_order(
        strategy,
        client_id="cid-phase2-long-reduce",
        exchange_order_id="ex-phase2-long-reduce",
        side="long",
        purpose="phase2_long_reduce_from_short_profit",
        size=intent.qty,
        price=98.0,
    )
    strategy.active_orders[client_id]["metadata"] = dict(intent.metadata or {})

    strategy.on_order_fill_event(client_id, source="reconcile")

    assert abs(strategy._phase2_short_profit_budget_reserved - 10.0) < 1e-9


def test_spread_heal_short_fill_does_not_latch_flag_when_no_paired_order_built() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(0.0, 0.0, 10.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 0.0, "avgPrice": 0.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 10.0, "avgPrice": 98.0},
    ]
    client_id = seed_active_order(
        strategy,
        client_id="cid-short-heal-no-paired",
        exchange_order_id="ex-short-heal-no-paired",
        side="short",
        purpose="spread_heal_short",
        size=1.0,
        price=101.0,
    )

    strategy.on_order_fill_event(client_id, source="reconcile")

    metadata = strategy.active_orders.get(client_id, {}).get("metadata", {})
    assert metadata.get("paired_partial_sl_long_created") is not True
    assert strategy.order_manager.limit_orders == []


def test_second_spread_heal_short_fill_keeps_existing_paired_partial_sl_long() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(10.0, 100.0, 10.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 10.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 10.0, "avgPrice": 98.0},
    ]
    first_id = seed_active_order(
        strategy,
        client_id="cid-short-heal-a",
        exchange_order_id="ex-short-heal-a",
        side="short",
        purpose="spread_heal_short",
        size=1.0,
        price=101.0,
    )
    strategy.on_order_fill_event(first_id, source="reconcile")

    second_id = seed_active_order(
        strategy,
        client_id="cid-short-heal-b",
        exchange_order_id="ex-short-heal-b",
        side="short",
        purpose="spread_heal_short",
        size=1.0,
        price=102.0,
    )
    strategy.on_order_fill_event(second_id, source="reconcile")

    paired_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "paired_partial_sl_long"
    ]
    assert len(paired_orders) == 2
    assert len(strategy.order_manager.limit_orders) == 2


def test_same_spread_heal_short_fill_does_not_create_second_paired_partial_sl_long() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(10.0, 100.0, 10.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 10.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 10.0, "avgPrice": 98.0},
    ]
    client_id = seed_active_order(
        strategy,
        client_id="cid-short-heal-once",
        exchange_order_id="ex-short-heal-once",
        side="short",
        purpose="spread_heal_short",
        size=1.0,
        price=101.0,
    )

    strategy.on_order_fill_event(client_id, source="reconcile")
    first_limit_count = len(strategy.order_manager.limit_orders)
    order = strategy.active_orders.get(client_id)
    if order:
        order.setdefault("metadata", {})["paired_partial_sl_long_created"] = True
    strategy._handle_filled_spread_heal_short(
        client_id,
        order or {},
        "reconcile",
    )

    assert len(strategy.order_manager.limit_orders) == first_limit_count


def test_paired_partial_sl_long_fill_cancels_open_short_heals_and_rebuilds_from_current_short_size() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    seed_active_order(
        strategy,
        client_id="cid-future-short-1",
        exchange_order_id="ex-future-short-1",
        side="short",
        purpose="spread_heal_short",
        size=0.6,
        price=101.0,
    )
    seed_active_order(
        strategy,
        client_id="cid-future-short-2",
        exchange_order_id="ex-future-short-2",
        side="short",
        purpose="spread_heal_short",
        size=0.7,
        price=102.0,
    )
    strategy.active_orders["cid-future-short-1"]["metadata"]["future_short_heal"] = True
    strategy.active_orders["cid-future-short-2"]["metadata"]["future_short_heal"] = True
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close",
        exchange_order_id="ex-paired-long-close",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    cancelled_order_ids = {call["order_id"] for call in strategy.order_manager.cancel_calls}
    assert cancelled_order_ids == {"ex-future-short-1", "ex-future-short-2"}
    rebuilt_short_orders = [
        order for order in strategy.active_orders.values() if order["purpose"] == "spread_heal_short"
    ]
    assert len(rebuilt_short_orders) == 1
    assert rebuilt_short_orders[0]["size"] == 0.5
    assert strategy.order_manager.limit_orders[-1].side == "Buy"
    assert strategy.order_manager.limit_orders[-1].reduce_only is True


def test_paired_partial_sl_short_fill_cancels_open_long_heals_and_rebuilds_from_current_long_size() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(5.0, 100.0, 8.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 5.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 8.0, "avgPrice": 98.0},
    ]
    seed_active_order(
        strategy,
        client_id="cid-future-long-1",
        exchange_order_id="ex-future-long-1",
        side="long",
        purpose="spread_heal_long",
        size=0.6,
        price=97.0,
    )
    seed_active_order(
        strategy,
        client_id="cid-future-long-2",
        exchange_order_id="ex-future-long-2",
        side="long",
        purpose="spread_heal_long",
        size=0.7,
        price=96.0,
    )
    strategy.active_orders["cid-future-long-1"]["metadata"]["future_long_heal"] = True
    strategy.active_orders["cid-future-long-2"]["metadata"]["future_long_heal"] = True
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-short-close",
        exchange_order_id="ex-paired-short-close",
        side="short",
        purpose="paired_partial_sl_short",
        size=1.0,
        price=94.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    cancelled_order_ids = {call["order_id"] for call in strategy.order_manager.cancel_calls}
    assert cancelled_order_ids == {"ex-future-long-1", "ex-future-long-2"}
    rebuilt_long_orders = [
        order for order in strategy.active_orders.values() if order["purpose"] == "spread_heal_long"
    ]
    assert len(rebuilt_long_orders) == 1
    assert rebuilt_long_orders[0]["size"] == 0.5
    assert strategy.order_manager.limit_orders[-1].side == "Buy"
    assert strategy.order_manager.limit_orders[-1].reduce_only is False


def test_future_short_heal_rebuild_uses_current_short_size_with_default_fine_heal_size_pct() -> None:
    strategy = build_strategy()
    strategy.config.enable_fine_heal_phase = True
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close-default-fine",
        exchange_order_id="ex-paired-long-close-default-fine",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    rebuilt_short_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "spread_heal_short"
        and (order.get("metadata") or {}).get("future_short_heal", False)
    ]
    assert len(rebuilt_short_orders) == 1
    assert rebuilt_short_orders[0]["size"] == 0.5


def test_future_short_heal_rebuild_uses_current_short_size_with_custom_fine_heal_size_pct() -> None:
    strategy = build_strategy()
    strategy.config.enable_fine_heal_phase = True
    strategy.config.fine_heal_size_pct = 0.15
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close-custom-fine",
        exchange_order_id="ex-paired-long-close-custom-fine",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    rebuilt_short_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "spread_heal_short"
        and (order.get("metadata") or {}).get("future_short_heal", False)
    ]
    assert len(rebuilt_short_orders) == 1
    assert rebuilt_short_orders[0]["size"] == 0.75


def test_future_short_heal_rebuild_uses_current_short_size_when_fine_phase_disabled() -> None:
    strategy = build_strategy()
    strategy.config.enable_fine_heal_phase = False
    strategy.config.action_size_pct = 0.10
    strategy.config.fine_heal_size_pct = 0.15
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close-fine-disabled",
        exchange_order_id="ex-paired-long-close-fine-disabled",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    rebuilt_short_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "spread_heal_short"
        and (order.get("metadata") or {}).get("future_short_heal", False)
    ]
    assert len(rebuilt_short_orders) == 1
    assert rebuilt_short_orders[0]["size"] == 0.5


def test_paired_partial_sl_long_fill_cancels_only_future_short_heals() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    non_future_id = seed_active_order(
        strategy,
        client_id="cid-live-short-heal",
        exchange_order_id="ex-live-short-heal",
        side="short",
        purpose="spread_heal_short",
        size=0.6,
        price=101.0,
    )
    future_id = seed_active_order(
        strategy,
        client_id="cid-future-short-heal",
        exchange_order_id="ex-future-short-heal",
        side="short",
        purpose="spread_heal_short",
        size=0.7,
        price=102.0,
    )
    strategy.active_orders[future_id]["metadata"]["future_short_heal"] = True
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close-2",
        exchange_order_id="ex-paired-long-close-2",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    cancelled_order_ids = {call["order_id"] for call in strategy.order_manager.cancel_calls}
    assert cancelled_order_ids == {"ex-future-short-heal"}
    assert strategy.active_orders[non_future_id]["status"] == "OPEN"


def test_paired_partial_sl_long_fill_rebuild_runs_only_once() -> None:
    strategy = build_strategy()
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close-once",
        exchange_order_id="ex-paired-long-close-once",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )
    order = strategy.active_orders[paired_client_id]

    strategy._handle_filled_paired_long_close(paired_client_id, order, "reconcile")
    first_limit_count = len(strategy.order_manager.limit_orders)
    strategy._handle_filled_paired_long_close(paired_client_id, order, "reconcile")

    assert len(strategy.order_manager.limit_orders) == first_limit_count


def test_preplaced_heal_fill_logic_still_runs() -> None:
    strategy = build_strategy()
    client_id = seed_active_order(
        strategy,
        client_id="cid-preplaced-short",
        exchange_order_id="ex-preplaced-short",
        side="short",
        purpose="preplaced_heal_short_limit",
        size=1.0,
        price=101.0,
    )
    called = []

    def record_preplaced_fill(client_order_id: str, purpose: str | None, source: str) -> None:
        called.append((client_order_id, purpose, source))

    strategy._handle_preplaced_heal_fill = record_preplaced_fill

    strategy.on_order_fill_event(client_id, source="reconcile")

    assert called == [(client_id, "preplaced_heal_short_limit", "reconcile")]


def test_spread_heal_short_fill_still_creates_paired_partial_sl_long_when_preplaced_flag_is_enabled() -> None:
    strategy = build_strategy()
    strategy.config.preplaced_heal_enabled = True
    strategy.position_manager.sync_positions(10.0, 100.0, 10.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 10.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 10.0, "avgPrice": 98.0},
    ]
    client_id = seed_active_order(
        strategy,
        client_id="cid-short-heal-preplaced-on",
        exchange_order_id="ex-short-heal-preplaced-on",
        side="short",
        purpose="spread_heal_short",
        size=1.0,
        price=101.0,
    )

    strategy.on_order_fill_event(client_id, source="reconcile")

    paired_orders = [
        order
        for order in strategy.active_orders.values()
        if order["purpose"] == "paired_partial_sl_long"
    ]
    assert len(paired_orders) == 1


def test_paired_partial_sl_long_fill_still_rebuilds_when_preplaced_flag_is_enabled() -> None:
    strategy = build_strategy()
    strategy.config.preplaced_heal_enabled = True
    strategy.position_manager.sync_positions(8.0, 100.0, 5.0, 98.0)
    strategy.order_manager.positions = [
        {"symbol": "BTCUSDT", "side": "Buy", "size": 8.0, "avgPrice": 100.0},
        {"symbol": "BTCUSDT", "side": "Sell", "size": 5.0, "avgPrice": 98.0},
    ]
    seed_active_order(
        strategy,
        client_id="cid-future-short-preplaced",
        exchange_order_id="ex-future-short-preplaced",
        side="short",
        purpose="spread_heal_short",
        size=0.7,
        price=102.0,
    )
    strategy.active_orders["cid-future-short-preplaced"]["metadata"]["future_short_heal"] = True
    paired_client_id = seed_active_order(
        strategy,
        client_id="cid-paired-long-close-preplaced",
        exchange_order_id="ex-paired-long-close-preplaced",
        side="long",
        purpose="paired_partial_sl_long",
        size=1.0,
        price=104.0,
    )

    strategy.on_order_fill_event(paired_client_id, source="reconcile")

    assert strategy.order_manager.cancel_calls != []

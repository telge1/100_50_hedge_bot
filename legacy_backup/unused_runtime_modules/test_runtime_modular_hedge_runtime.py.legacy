import logging
import math
import os
import sys
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import (
    HedgeSnapshot as FixedCycleHedgeSnapshot,
    StrategyIntent as FixedCycleStrategyIntent,
)
from fixed_cycle_hedge_bot.runtime import (
    GenericHedgeRuntime as FixedCycleRuntime,
    GenericRuntimeConfig as FixedCycleRuntimeConfig,
)
from modular_hedge_runtime.audit_logger import AuditLogger
from modular_hedge_runtime.base import StrategyContext
from modular_hedge_runtime.models import (
    HedgeSnapshot,
    ManagedOrder,
    RuntimeState,
)
from modular_hedge_runtime.runtime import GenericHedgeRuntime, GenericRuntimeConfig
from unittest.mock import patch


class DummyOrderManager:
    def __init__(self):
        self.positions = []
        self.open_orders = []
        self.order_history = {}
        self.last_reduce_kwargs: dict[str, Any] | None = None
        self.reduce_call_count = 0
        self.last_post_error: dict[str, Any] | None = None

    def fetch_positions(self, *args, **kwargs):
        return list(self.positions)

    def fetch_mark_price(self, *args, **kwargs):
        return 1.0

    def fetch_open_orders(self, *args, **kwargs):
        return list(self.open_orders)

    def fetch_order_history(self, *args, **kwargs):
        order_id = kwargs.get("order_id")
        order_link_id = kwargs.get("order_link_id")
        if order_id and order_id in self.order_history:
            return list(self.order_history[order_id])
        if order_link_id and order_link_id in self.order_history:
            return list(self.order_history[order_link_id])
        return []

    def normalize_qty(self, *args, **kwargs):
        if "qty" in kwargs:
            return kwargs["qty"]
        if len(args) > 1:
            return args[1]
        return 0.0

    def place_limit_order(self, payload):
        order_link_id = getattr(payload, "order_link_id", None)
        if isinstance(payload, dict):
            order_link_id = payload.get("order_link_id", order_link_id)
        return {"result": {"orderId": f"limit-{order_link_id or 'limit'}"}}

    def place_reduce_market_order(self, **kwargs):
        self.reduce_call_count += 1
        self.last_reduce_kwargs = kwargs
        self.last_post_error = None
        return {"result": {"orderId": f"reduce-{kwargs.get('order_link_id', 'reduce')}"}}

    def place_market_order(self, **kwargs):
        return {"result": {"orderId": f"market-{kwargs.get('order_link_id', 'market')}"}}

    def ensure_hedge_mode(self, *args, **kwargs):
        return True

    def ensure_max_leverage(self, *args, **kwargs):
        return True

    def get_cached_instrument_rules(self, *args, **kwargs):
        return {}


class RejectingReduceOrderManager(DummyOrderManager):
    def __init__(self, fail_fallback: bool = False):
        super().__init__()
        self.fail_fallback = fail_fallback

    def place_reduce_market_order(self, **kwargs):
        self.reduce_call_count += 1
        self.last_reduce_kwargs = kwargs
        if self.reduce_call_count == 1:
            self.last_post_error = {"retCode": 110093, "retMsg": "expect Falling"}
            return None
        if self.fail_fallback:
            self.last_post_error = {"retCode": 110093, "retMsg": "expect Falling"}
            return None
        self.last_post_error = None
        return {
            "result": {
                "orderId": f"reduce-{kwargs.get('order_link_id', 'reduce')}-{self.reduce_call_count}"
            }
        }


def build_runtime(order_manager: DummyOrderManager | None = None) -> GenericHedgeRuntime:
    config = GenericRuntimeConfig(
        api_key="test",
        secret_key="test",
        ensure_exchange_ready=False,
    )
    runtime = GenericHedgeRuntime(
        config,
        FixedCycleHedgeStrategy(),
        order_manager=order_manager or DummyOrderManager(),
    )
    runtime.context.refresh_snapshot = runtime.refresh_snapshot
    return runtime


def build_fixed_cycle_runtime(order_manager: DummyOrderManager | None = None) -> FixedCycleRuntime:
    config = FixedCycleRuntimeConfig(
        api_key="test",
        secret_key="test",
        ensure_exchange_ready=False,
    )
    runtime = FixedCycleRuntime(
        config,
        FixedCycleHedgeStrategy(),
        order_manager=order_manager or DummyOrderManager(),
    )
    runtime.context.refresh_snapshot = runtime.refresh_snapshot
    return runtime


def _build_long_add_intent(
    *,
    qty: float,
    trigger_price: float,
    purpose: str = "CYCLE_1_LONG_ADD",
) -> FixedCycleStrategyIntent:
    return FixedCycleStrategyIntent(
        side="long",
        qty=qty,
        purpose=purpose,
        order_type="Market",
        reduce_only=True,
        trigger_price=trigger_price,
        trigger_direction=2,
        trigger_by="LastPrice",
        close_on_trigger=True,
        position_idx=1,
        metadata={
            "cycle_role": "long_reduce",
        },
    )


def _create_managed_order(
    *,
    runtime: GenericHedgeRuntime,
    client_order_id: str,
    side: str,
    purpose: str,
    qty: float,
    reduce_only: bool,
    exchange_order_id: str | None = None,
) -> ManagedOrder:
    order = ManagedOrder(
        client_order_id=client_order_id,
        side=side,
        qty=qty,
        purpose=purpose,
        price=1.0,
        order_type="Limit",
        reduce_only=reduce_only,
        status="OPEN",
        remaining_qty=qty,
        exchange_order_id=exchange_order_id,
    )
    runtime.runtime_state.active_orders[client_order_id] = order
    if exchange_order_id:
        runtime.runtime_state.exchange_to_client_id[exchange_order_id] = client_order_id
    return order


def test_sell_fill_matches_long_tp_exit():
    runtime = build_runtime()
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-long-tp",
        side="long",
        purpose="LONG_TP_EXIT",
        qty=95.6,
        reduce_only=True,
        exchange_order_id="original-long-stop",
    )

    runtime.on_websocket_fill(
        "fill-exchange-long",
        95.6,
        1.306,
        order_side="Sell",
    )

    assert order.exchange_order_id == "fill-exchange-long"


def test_buy_fill_matches_short_sl_exit():
    runtime = build_runtime()
    logged_events: list[tuple[str, dict]] = []

    def capture(event: str, **kwargs):
        logged_events.append((event, kwargs))

    runtime.audit.log_event = capture
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-short-sl",
        side="short",
        purpose="SHORT_SL_EXIT",
        qty=38.2,
        reduce_only=True,
    )

    runtime.on_websocket_fill(
        "fill-exchange-short",
        38.2,
        1.306,
        order_side="Buy",
    )

    assert order.exchange_order_id == "fill-exchange-short"
    assert runtime.runtime_state.exchange_to_client_id.get("fill-exchange-short") == order.client_order_id
    assert any(event == "unmatched_fill_matched" for event, _ in logged_events)
    assert not any(event == "unmatched_fill" for event, _ in logged_events)


def test_initial_entry_not_matched_by_fallback():
    runtime = build_runtime()
    _create_managed_order(
        runtime=runtime,
        client_order_id="test-initial-entry",
        side="long",
        purpose="INITIAL_LONG_ENTRY",
        qty=76.0,
        reduce_only=False,
    )

    runtime.on_websocket_fill(
        "fill-initial",
        76.0,
        1.306,
        order_side="Sell",
    )

    assert "fill-initial" not in runtime.runtime_state.exchange_to_client_id


def test_partial_sell_fill_matches_long_tp_exit():
    runtime = build_runtime()
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-long-tp-partial",
        side="long",
        purpose="LONG_TP_EXIT",
        qty=95.6,
        reduce_only=True,
    )

    runtime.on_websocket_fill(
        "partial-long",
        20.0,
        1.306,
        order_side="Sell",
    )

    assert runtime.runtime_state.exchange_to_client_id.get("partial-long") == order.client_order_id
    assert order.exchange_order_id == "partial-long"


def test_partial_buy_fill_matches_short_sl_exit():
    runtime = build_runtime()
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-short-sl-partial",
        side="short",
        purpose="SHORT_SL_EXIT",
        qty=38.2,
        reduce_only=True,
    )

    runtime.on_websocket_fill(
        "partial-short",
        5.8,
        1.306,
        order_side="Buy",
    )

    assert runtime.runtime_state.exchange_to_client_id.get("partial-short") == order.client_order_id
    assert order.exchange_order_id == "partial-short"


def test_untracked_order_link_id_triggers_fallback():
    runtime = build_runtime()
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-long-redirect",
        side="long",
        purpose="LONG_TP_EXIT",
        qty=95.6,
        reduce_only=True,
    )

    runtime.on_websocket_fill(
        "link-order",
        95.6,
        1.306,
        order_side="Sell",
        order_link_id="missing-link",
    )

    assert order.exchange_order_id == "link-order"


def test_late_fill_for_finalized_reduce_order_is_not_unmatched():
    runtime = build_runtime()
    logged_events: list[tuple[str, dict]] = []

    def capture(event: str, **kwargs):
        logged_events.append((event, kwargs))

    runtime.audit.log_event = capture
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="cycle-1-long-reduce",
        side="long",
        purpose="CYCLE_1_LONG_ADD",
        qty=50.0,
        reduce_only=True,
        exchange_order_id="reduce-order-1",
    )
    order.status = "FILLED"
    order.filled_qty = 50.0
    order.remaining_qty = 0.0
    runtime._finalize_managed_order(order.client_order_id, order)

    runtime.on_websocket_fill(
        "reduce-order-1",
        12.0,
        1.204,
        exec_id="late-exec-1",
        order_link_id=order.client_order_id,
        order_side="Sell",
    )

    assert runtime.runtime_state.exchange_to_client_id.get("reduce-order-1") == order.client_order_id
    assert runtime.runtime_state.finalized_orders.get(order.client_order_id) is order
    assert any(event == "late_fill_ignored" for event, _ in logged_events)
    assert not any(event == "unmatched_fill" for event, _ in logged_events)


def test_short_tp_fill_maps_order_id():
    runtime = build_runtime()
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="cycle-1-short-tp",
        side="short",
        purpose="CYCLE_1_SHORT_TP",
        qty=4.6,
        reduce_only=True,
    )

    with patch.object(runtime, "_finalize_managed_order") as finalize:
        runtime.on_websocket_fill(
            "short-tp-exec",
            4.6,
            1.3482,
            order_side="Buy",
            order_link_id=order.client_order_id,
        )
        finalize.assert_called_once_with(order.client_order_id, order)

    assert runtime.runtime_state.exchange_to_client_id.get("short-tp-exec") == order.client_order_id
    assert order.exchange_order_id == "short-tp-exec"
    assert order.status == "FILLED"


def test_sell_fill_matches_initial_short_entry_with_position_idx_fallback():
    runtime = build_runtime()
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-initial-short",
        side="short",
        purpose="INITIAL_SHORT_ENTRY",
        qty=12.0,
        reduce_only=False,
    )

    runtime.on_websocket_fill(
        "short-entry-exec",
        12.0,
        1.204,
        order_side="Sell",
        position_idx=2,
    )

    assert runtime.runtime_state.exchange_to_client_id.get("short-entry-exec") == order.client_order_id
    assert order.exchange_order_id == "short-entry-exec"
    assert order.status == "FILLED"


def test_reconcile_finalizes_initial_short_entry_when_position_matches():
    order_manager = DummyOrderManager()
    order_manager.positions = [
        {"side": "Sell", "size": "12", "avgPrice": "1.205"},
    ]
    runtime = build_runtime(order_manager=order_manager)
    order = _create_managed_order(
        runtime=runtime,
        client_order_id="test-reconcile-initial-short",
        side="short",
        purpose="INITIAL_SHORT_ENTRY",
        qty=12.0,
        reduce_only=False,
        exchange_order_id="short-entry-order",
    )

    runtime._reconcile_active_orders()

    assert order.client_order_id not in runtime.runtime_state.active_orders
    assert runtime.runtime_state.finalized_orders.get(order.client_order_id) is order
    assert order.status == "FILLED"
    assert order.filled_qty == 12.0


def test_initial_entry_confirmed_requires_no_open_initial_orders():
    strategy = FixedCycleHedgeStrategy()
    runtime_state = RuntimeState()
    runtime_state.active_orders["initial-short"] = ManagedOrder(
        client_order_id="initial-short",
        side="short",
        qty=12.0,
        purpose=strategy.SHORT_ENTRY_PURPOSE,
        price=None,
        order_type="Market",
        reduce_only=False,
        status="OPEN",
        remaining_qty=12.0,
    )
    snapshot = HedgeSnapshot(
        symbol="XRPUSDT",
        current_price=1.2,
        long_qty=20.0,
        short_qty=12.0,
        long_avg=1.19,
        short_avg=1.21,
        active_orders=(runtime_state.active_orders["initial-short"].to_snapshot(),),
    )
    context = StrategyContext(
        audit=AuditLogger(logging.getLogger("initial-entry-confirmed"), "logs/tests_audit.jsonl"),
        runtime_name=strategy.name,
        symbol="XRPUSDT",
        category="linear",
        min_order_value=1.0,
    )

    strategy.on_tick(snapshot, runtime_state, context)

    assert runtime_state.strategy_state["initial_entry_confirmed"] is False


def test_downside_long_intent_is_reduce_only():
    strategy = FixedCycleHedgeStrategy()
    runtime_state = RuntimeState()
    runtime_state.strategy_state.update(
        {
            "initial_entry_confirmed": True,
            "entry_reference_price": 1.305,
            "initial_long_qty": 76.5,
            "initial_short_qty": 38.2,
            "long_add_pending": False,
            "cycle_state": {},
            "cycle_waiting_for_short_tp": False,
            "current_long_cycle_index": 0,
            "current_short_cycle_index": 0,
            "current_effective_cycle": 0,
        }
    )
    context = StrategyContext(
        audit=AuditLogger(logging.getLogger("downside-reduce"), "logs/tests_audit.jsonl"),
        runtime_name=strategy.name,
        symbol="XRPUSDT",
        category="linear",
        min_order_value=1.0,
    )
    snapshot = HedgeSnapshot(
        symbol="XRPUSDT",
        current_price=1.303,
        long_qty=76.6,
        short_qty=38.3,
        long_avg=1.305,
        short_avg=1.3049,
    )

    with patch.object(
        FixedCycleHedgeStrategy, "_fixed_long_cycle_qty", return_value=5.0
    ):
        intents = strategy._build_downside_cycle_intents(snapshot, runtime_state, context)
    long_reduce_intents = [intent for intent in intents if intent.side == "long"]
    assert long_reduce_intents, "Kein Long-Intent erzeugt"
    intent = long_reduce_intents[0]
    assert intent.reduce_only is True
    assert intent.metadata.get("cycle_role") == "long_reduce"


def test_long_reduce_uses_config_percent_distance():
    strategy = FixedCycleHedgeStrategy()
    strategy.config.long_fill_distance_pct = 0.5
    runtime_state = RuntimeState()
    runtime_state.strategy_state.update(
        {
            "initial_entry_confirmed": True,
            "entry_reference_price": 1.305,
            "initial_long_qty": 76.5,
            "initial_short_qty": 38.2,
            "long_add_pending": False,
            "cycle_state": {},
            "cycle_waiting_for_short_tp": False,
            "current_long_cycle_index": 0,
            "current_short_cycle_index": 0,
            "current_effective_cycle": 0,
        }
    )
    context = StrategyContext(
        audit=AuditLogger(logging.getLogger("downside-reduce"), "logs/tests_audit.jsonl"),
        runtime_name=strategy.name,
        symbol="XRPUSDT",
        category="linear",
        min_order_value=1.0,
    )
    logged: dict[str, dict] = {}

    def capture(event: str, **kwargs: dict) -> None:
        logged[event] = kwargs

    context.audit.log_event = capture
    snapshot = HedgeSnapshot(
        symbol="XRPUSDT",
        current_price=1.303,
        long_qty=76.6,
        short_qty=38.3,
        long_avg=1.305,
        short_avg=1.3049,
    )

    with patch.object(
        FixedCycleHedgeStrategy, "_fixed_long_cycle_qty", return_value=5.0
    ):
        intents = strategy._build_downside_cycle_intents(snapshot, runtime_state, context)
    long_intents = [intent for intent in intents if intent.side == "long"]
    assert long_intents, "Kein Long-Intent erzeugt"
    intent = long_intents[0]
    event = logged.get("fixed_cycle_long_reduce_planned") or {}
    expected_distance_pct = strategy._clamp_pct_fraction(
        strategy._pct(strategy.config.long_fill_distance_pct)
    )
    actual_distance_pct = event.get("distance_pct_used")
    assert math.isclose(actual_distance_pct, expected_distance_pct, rel_tol=1e-9)
    assert math.isclose(
        intent.trigger_price,
        event.get("trigger_price_normalized") or 0.0,
        rel_tol=1e-9,
        abs_tol=0.0,
    )


def test_long_reduce_plans_short_tp_pair():
    strategy = FixedCycleHedgeStrategy()
    runtime_state = RuntimeState()
    runtime_state.strategy_state.update(
        {
            "initial_entry_confirmed": True,
            "entry_reference_price": 1.305,
            "initial_long_qty": 76.5,
            "initial_short_qty": 38.2,
            "long_add_pending": False,
            "cycle_state": {},
            "cycle_waiting_for_short_tp": False,
            "current_long_cycle_index": 0,
            "current_short_cycle_index": 0,
            "current_effective_cycle": 0,
        }
    )
    context = StrategyContext(
        audit=AuditLogger(logging.getLogger("downside-reduce"), "logs/tests_audit.jsonl"),
        runtime_name=strategy.name,
        symbol="XRPUSDT",
        category="linear",
        min_order_value=1.0,
    )
    logged: dict[str, dict] = {}

    def capture(event: str, **kwargs: dict) -> None:
        logged[event] = kwargs

    context.audit.log_event = capture
    snapshot = HedgeSnapshot(
        symbol="XRPUSDT",
        current_price=1.303,
        long_qty=76.6,
        short_qty=38.3,
        long_avg=1.305,
        short_avg=1.3049,
    )

    with patch.object(
        FixedCycleHedgeStrategy, "_fixed_long_cycle_qty", return_value=5.0
    ), patch.object(
        FixedCycleHedgeStrategy, "_fixed_short_cycle_qty", return_value=3.0
    ):
        intents = strategy._build_downside_cycle_intents(snapshot, runtime_state, context)

    short_tp_purpose = strategy._short_tp_pair_purpose(1)
    short_tp_intents = [intent for intent in intents if intent.purpose == short_tp_purpose]
    assert short_tp_intents, "Die gekoppelte Short-TP-Order wurde nicht erstellt"
    short_tp_intent = short_tp_intents[0]
    assert short_tp_intent.trigger_direction == 2
    audit_event = logged.get("fixed_cycle_short_tp_pair_planned") or {}
    assert math.isclose(
        short_tp_intent.trigger_price,
        audit_event.get("trigger_price_normalized") or 0.0,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("reduction_multiplier") or 0.0,
        0.5,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("reduction_pct_used") or 0.0,
        strategy.config.reduction_pct_per_fill * 0.5,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("qty_normalized") or 0.0,
        3.0,
        rel_tol=1e-9,
    )


def test_short_reduce_intent_stays_below_long_fill():
    strategy = FixedCycleHedgeStrategy()
    strategy.config.min_notional_usdt = 0.0
    strategy.config.short_fill_distance_pct = 0.12
    runtime_state = RuntimeState()
    state = runtime_state.strategy_state
    long_fill_price = 1.3507
    cycle_state = strategy._default_cycle_state()
    cycle_state.update(
        {
            "long_fills": {"1": {"price": long_fill_price}},
            "short_tp_pending_cycle": 1,
            "cycle_waiting_for_short_tp": True,
        }
    )
    state["cycle_state"] = cycle_state
    state.update(
        {
            "short_tp_pending_cycle": 1,
            "cycle_waiting_for_short_tp": True,
            "initial_short_qty": 40.0,
            "entry_reference_price": 1.34,
        }
    )
    snapshot = HedgeSnapshot(
        symbol="XRPUSDT",
        current_price=1.34,
        long_qty=50.0,
        short_qty=25.0,
        long_avg=1.33,
        short_avg=1.32,
    )
    context = StrategyContext(
        audit=AuditLogger(logging.getLogger("short-reduce-price"), "logs/tests_audit.jsonl"),
        runtime_name=strategy.name,
        symbol="XRPUSDT",
        category="linear",
        min_order_value=1.0,
    )
    logged: dict[str, dict] = {}

    def capture(event: str, **kwargs: dict) -> None:
        logged[event] = kwargs

    context.audit.log_event = capture

    intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)
    assert intents, "Kein Short-Reduce-Intent erstellt"
    intent = intents[0]
    assert intent.trigger_direction == 2
    assert intent.trigger_price < long_fill_price
    assert intent.price < long_fill_price
    assert intent.metadata.get("long_fill_price") == long_fill_price
    assert intent.metadata.get("short_reduce_reference") == long_fill_price
    assert intent.metadata.get("cycle_role") == "short_reduce"
    audit_event = logged.get("fixed_cycle_short_cycle_planned") or {}
    expected_distance_pct = strategy._clamp_pct_fraction(
        strategy._pct(strategy.config.short_fill_distance_pct)
    )
    actual_distance_pct = audit_event.get("distance_pct_used")
    assert math.isclose(actual_distance_pct, expected_distance_pct, rel_tol=1e-9)
    assert math.isclose(
        intent.trigger_price,
        audit_event.get("trigger_price_normalized") or 0.0,
        rel_tol=1e-9,
        abs_tol=0.0,
    )
    reduction_multiplier = 0.5
    expected_reduction_pct = strategy.config.reduction_pct_per_fill * reduction_multiplier
    expected_qty_raw = snapshot.short_qty * strategy._pct(expected_reduction_pct)
    expected_qty_normalized = strategy._normalize_qty(
        min(expected_qty_raw, snapshot.short_qty)
    )
    assert math.isclose(
        audit_event.get("reduction_multiplier") or 0.0,
        reduction_multiplier,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("reduction_pct_used") or 0.0,
        expected_reduction_pct,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("qty_raw") or 0.0,
        expected_qty_raw,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("qty_normalized") or 0.0,
        expected_qty_normalized,
        rel_tol=1e-9,
    )


def test_short_tp_pair_falls_back_to_initial_short_qty():
    strategy = FixedCycleHedgeStrategy()
    runtime_state = RuntimeState()
    state = runtime_state.strategy_state
    state.update(
        {
            "initial_entry_confirmed": True,
            "entry_reference_price": 1.305,
            "initial_long_qty": 76.5,
            "initial_short_qty": 38.2,
            "long_add_pending": False,
            "cycle_state": {},
            "cycle_waiting_for_short_tp": False,
            "current_long_cycle_index": 0,
            "current_short_cycle_index": 0,
            "current_effective_cycle": 0,
        }
    )
    context = StrategyContext(
        audit=AuditLogger(logging.getLogger("short-tp-fallback"), "logs/tests_audit.jsonl"),
        runtime_name=strategy.name,
        symbol="XRPUSDT",
        category="linear",
        min_order_value=1.0,
    )
    logged: dict[str, dict] = {}

    def capture(event: str, **kwargs: dict) -> None:
        logged[event] = kwargs

    context.audit.log_event = capture
    snapshot = HedgeSnapshot(
        symbol="XRPUSDT",
        current_price=1.303,
        long_qty=76.6,
        short_qty=0.0,
        long_avg=1.305,
        short_avg=0.0,
    )

    with patch.object(
        FixedCycleHedgeStrategy, "_fixed_long_cycle_qty", return_value=5.0
    ), patch.object(
        FixedCycleHedgeStrategy, "_fixed_short_cycle_qty", return_value=3.0
    ):
        intents = strategy._build_downside_cycle_intents(snapshot, runtime_state, context)

    short_tp_purpose = strategy._short_tp_pair_purpose(1)
    short_tp_intents = [intent for intent in intents if intent.purpose == short_tp_purpose]
    assert short_tp_intents, "Keine Short-TP-Paar-Order geplant"
    audit_event = logged.get("fixed_cycle_short_tp_pair_planned") or {}
    assert math.isclose(
        audit_event.get("current_short_qty") or 0.0,
        state["initial_short_qty"],
        rel_tol=1e-9,
    )
    expected_reduction_pct = strategy.config.reduction_pct_per_fill * 0.5
    assert math.isclose(
        audit_event.get("reduction_pct_used") or 0.0,
        expected_reduction_pct,
        rel_tol=1e-9,
    )
    assert math.isclose(
        audit_event.get("qty_normalized") or 0.0,
        3.0,
        rel_tol=1e-9,
    )


def test_long_add_stale_trigger_market_fallback():
    manager = DummyOrderManager()
    runtime = build_fixed_cycle_runtime(order_manager=manager)
    logs: list[tuple[str, dict]] = []

    def capture(event: str, **kwargs):
        logs.append((event, kwargs))

    runtime.audit.log_event = capture
    snapshot = FixedCycleHedgeSnapshot(
        symbol="CHIPUSDT",
        current_price=9.0,
        long_qty=3.0,
        short_qty=0.0,
        long_avg=9.5,
        short_avg=0.0,
    )
    intent = _build_long_add_intent(qty=5.0, trigger_price=10.0)

    client_id = runtime.submit_intent(intent, snapshot, source="test")
    assert client_id
    assert manager.last_reduce_kwargs
    assert manager.last_reduce_kwargs["qty"] == 3.0
    assert manager.last_reduce_kwargs.get("trigger_price") is None
    assert any(event == "pre_long_add_trigger_validation" for event, _ in logs)
    assert any(event == "stale_long_add_trigger_market_fallback" for event, _ in logs)
    assert any(event == "long_add_market_fallback_submitted" for event, _ in logs)
    active = runtime.runtime_state.active_orders.get(client_id)
    assert active
    assert active.purpose == "CYCLE_1_LONG_ADD"


def test_long_add_conditional_rejection_triggers_market_fallback():
    manager = RejectingReduceOrderManager()
    runtime = build_fixed_cycle_runtime(order_manager=manager)
    logs: list[tuple[str, dict]] = []

    def capture(event: str, **kwargs):
        logs.append((event, kwargs))

    runtime.audit.log_event = capture
    snapshot = FixedCycleHedgeSnapshot(
        symbol="CHIPUSDT",
        current_price=110.0,
        long_qty=10.0,
        short_qty=0.0,
        long_avg=110.0,
        short_avg=0.0,
    )
    intent = _build_long_add_intent(qty=5.0, trigger_price=100.0)

    client_id = runtime.submit_intent(intent, snapshot, source="test")
    assert client_id
    assert manager.reduce_call_count == 2
    assert manager.last_reduce_kwargs
    assert manager.last_reduce_kwargs["qty"] == 5.0
    assert manager.last_reduce_kwargs.get("trigger_price") is None
    assert any(event == "long_add_conditional_rejected_market_fallback" for event, _ in logs)
    assert any(event == "long_add_market_fallback_submitted" for event, _ in logs)


def test_long_add_market_fallback_failure_logged():
    manager = RejectingReduceOrderManager(fail_fallback=True)
    runtime = build_fixed_cycle_runtime(order_manager=manager)
    logs: list[tuple[str, dict]] = []

    def capture(event: str, **kwargs):
        logs.append((event, kwargs))

    runtime.audit.log_event = capture
    snapshot = FixedCycleHedgeSnapshot(
        symbol="CHIPUSDT",
        current_price=110.0,
        long_qty=5.0,
        short_qty=0.0,
        long_avg=110.0,
        short_avg=0.0,
    )
    intent = _build_long_add_intent(qty=3.0, trigger_price=100.0)

    assert runtime.submit_intent(intent, snapshot, source="test") is None
    assert manager.reduce_call_count == 2
    assert any(event == "long_add_market_fallback_failed" for event, _ in logs)

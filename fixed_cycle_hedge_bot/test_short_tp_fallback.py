import logging

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, RuntimeState
from fixed_cycle_hedge_bot.trailing_fallback import (
    ShortTpFallbackState,
    start_short_tp_fallback,
    update_short_tp_fallback,
)


class DummyOrderManager:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.post_calls: list[tuple[str, str]] = []

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def normalize_price(self, symbol: str, price: float, category: str) -> float:
        return price

    def _post(self, path: str, body: str):
        self.post_calls.append((path, body))
        if self.responses:
            return self.responses.pop(0)
        return None


def build_strategy() -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            symbol="BTCUSDT",
            category="linear",
            restart=False,
            rest_poll_after_fill_ms=0,
            order_refresh_cooldown_ms=0,
            price_tick_size=0.1,
            qty_step=0.001,
            min_order_qty=0.001,
            min_notional_usdt=0.0,
        )
    )


def build_context(order_manager=None) -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test.short_tp_fallback"), None),
        runtime_name="fixed_cycle",
        symbol="BTCUSDT",
        category="linear",
        min_order_value=0.0,
        order_manager=order_manager,
    )


def prepare_short_tp_follow_up_state() -> RuntimeState:
    runtime_state = RuntimeState()
    runtime_state.strategy_state.update(
        {
            "short_tp_pending_cycle": 1,
            "cycle_waiting_for_short_tp": True,
            "initial_short_qty": 1.0,
            "entry_reference_price": 100.0,
            "short_avg": 100.0,
        }
    )
    runtime_state.strategy_state["cycle_state"] = {
        "symbol": "BTCUSDT",
        "long_fills": {
            "1": {
                "price": 100.0,
                "qty": 0.5,
                "closed_pnl_ready": True,
                "confirmed_closed_pnl": -1.0,
                "closed_qty": 0.5,
                "closed_avg_price": 99.5,
                "closed_cost": 49.75,
                "last_long_add_loss_usdt": 1.0,
            }
        },
        "short_fills": {},
        "cycle_waiting_for_short_tp": True,
        "short_tp_pending_cycle": 1,
    }
    return runtime_state


def test_short_tp_normal_flow_builds_regular_intent_without_fallback(caplog, monkeypatch):
    strategy = build_strategy()
    runtime_state = prepare_short_tp_follow_up_state()
    context = build_context()
    snapshot = HedgeSnapshot(
        symbol="BTCUSDT",
        current_price=150.0,
        long_qty=1.5,
        short_qty=1.0,
        long_avg=100.0,
        short_avg=100.0,
    )

    monkeypatch.setattr(strategy, "_fixed_short_cycle_qty", lambda *args, **kwargs: 1.0)
    caplog.set_level(logging.INFO)

    intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

    assert len(intents) == 1
    assert intents[0].side == "short"
    assert snapshot.current_price > intents[0].trigger_price
    fallback_state = runtime_state.strategy_state.get("short_tp_fallback_state") or {}
    assert not fallback_state
    assert "SHORT_TP_FALLBACK_START" not in caplog.text


def test_short_tp_fallback_flow_starts_and_submits_exactly_once(caplog, monkeypatch):
    strategy = build_strategy()
    runtime_state = prepare_short_tp_follow_up_state()
    context = build_context()
    snapshot = HedgeSnapshot(
        symbol="BTCUSDT",
        current_price=90.0,
        long_qty=1.5,
        short_qty=1.0,
        long_avg=100.0,
        short_avg=100.0,
    )
    order_manager = DummyOrderManager(
        responses=[{"retCode": 0, "result": {"orderId": "fallback-1"}}]
    )

    monkeypatch.setattr(strategy, "_fixed_short_cycle_qty", lambda *args, **kwargs: 1.0)
    caplog.set_level(logging.INFO)

    intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

    assert intents == []
    assert "SHORT_TP_FALLBACK_START" in caplog.text
    fallback_state = ShortTpFallbackState.from_dict(
        runtime_state.strategy_state.get("short_tp_fallback_state")
    )
    assert fallback_state.active is True
    assert fallback_state.submitted is False

    submitted, response = update_short_tp_fallback(
        fallback_state,
        order_manager=order_manager,
        symbol="BTCUSDT",
        category="linear",
        current_price=snapshot.current_price,
        activation_drop_pct=0.001,
        stop_offset_pct=0.0025,
    )
    assert submitted is True
    assert response == {"retCode": 0, "result": {"orderId": "fallback-1"}}
    assert fallback_state.submitted is True
    assert fallback_state.submit_failed is False
    assert len(order_manager.post_calls) == 1

    submitted_again, response_again = update_short_tp_fallback(
        fallback_state,
        order_manager=order_manager,
        symbol="BTCUSDT",
        category="linear",
        current_price=snapshot.current_price,
        activation_drop_pct=0.001,
        stop_offset_pct=0.0025,
    )
    assert submitted_again is False
    assert response_again is None
    assert len(order_manager.post_calls) == 1


def test_short_tp_fallback_submit_failure_blocks_second_submit():
    state = ShortTpFallbackState()
    started = start_short_tp_fallback(
        state,
        qty=1.0,
        original_trigger_price=99.0,
        current_price=95.0,
        activation_drop_pct=0.001,
        stop_offset_pct=0.0025,
    )
    order_manager = DummyOrderManager(responses=[None])

    assert started is True

    submitted, response = update_short_tp_fallback(
        state,
        order_manager=order_manager,
        symbol="BTCUSDT",
        category="linear",
        current_price=95.0,
        activation_drop_pct=0.001,
        stop_offset_pct=0.0025,
    )
    assert submitted is False
    assert response is None
    assert state.submit_failed is True
    assert state.submitted is False
    assert len(order_manager.post_calls) == 1

    submitted_again, response_again = update_short_tp_fallback(
        state,
        order_manager=order_manager,
        symbol="BTCUSDT",
        category="linear",
        current_price=95.0,
        activation_drop_pct=0.001,
        stop_offset_pct=0.0025,
    )
    assert submitted_again is False
    assert response_again is None
    assert len(order_manager.post_calls) == 1


def test_short_tp_fallback_resets_and_rebuilds_normal_tp_when_price_recovers(monkeypatch):
    strategy = build_strategy()
    runtime_state = prepare_short_tp_follow_up_state()
    context = build_context()
    runtime_state.strategy_state["short_tp_fallback_state"] = ShortTpFallbackState(
        active=True,
        purpose="SHORT_TP_FALLBACK",
        position_idx=2,
        qty=1.0,
        original_trigger_price=99.0,
        activation_price=95.0,
        trailing_distance=0.2375,
        lowest_price=94.0,
        submitted=False,
    ).to_dict()
    snapshot = HedgeSnapshot(
        symbol="BTCUSDT",
        current_price=105.0,
        long_qty=1.5,
        short_qty=1.0,
        long_avg=100.0,
        short_avg=100.0,
    )

    monkeypatch.setattr(strategy, "_fixed_short_cycle_qty", lambda *args, **kwargs: 1.0)

    intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

    assert len(intents) == 1
    assert intents[0].side == "short"
    reset_state = ShortTpFallbackState.from_dict(
        runtime_state.strategy_state.get("short_tp_fallback_state")
    )
    assert reset_state.active is False
    assert reset_state.submitted is False


def test_short_tp_fallback_registers_runtime_order_and_resets_on_fill():
    strategy = build_strategy()
    runtime_state = prepare_short_tp_follow_up_state()
    context = build_context()
    fallback_state = ShortTpFallbackState(
        active=True,
        purpose="SHORT_TP_FALLBACK",
        position_idx=2,
        qty=1.0,
        original_trigger_price=99.0,
        activation_price=95.0,
        trailing_distance=0.2375,
        lowest_price=94.0,
        submitted=True,
        client_order_id="short-fallback-1",
        exchange_order_id="ex-short-fallback-1",
    )
    runtime_state.strategy_state["short_tp_fallback_state"] = fallback_state.to_dict()
    runtime_state.strategy_state["short_tp_fallback_order_context"] = {
        "purpose": "CYCLE_1_SHORT_REDUCE",
        "cycle_index": 1,
    }
    runtime_state.strategy_state["cycle_long_add_filled"] = True

    strategy._register_short_tp_fallback_order(runtime_state)

    assert "short-fallback-1" in runtime_state.active_orders
    assert runtime_state.exchange_to_client_id["ex-short-fallback-1"] == "short-fallback-1"
    assert runtime_state.active_orders["short-fallback-1"].purpose == "CYCLE_1_SHORT_REDUCE"
    assert runtime_state.active_orders["short-fallback-1"].metadata["short_tp_fallback"] is True

    fill_event = FillEvent(
        exchange_order_id="ex-short-fallback-1",
        client_order_id="short-fallback-1",
        side="short",
        purpose="CYCLE_1_SHORT_REDUCE",
        exec_qty=1.0,
        exec_price=94.0,
        order_type="Market",
        reduce_only=True,
        status="FILLED",
        metadata={"short_tp_fallback": True, "cycle_index": 1, "cycle_role": "short_reduce"},
    )
    snapshot = HedgeSnapshot(
        symbol="BTCUSDT",
        current_price=94.0,
        long_qty=1.5,
        short_qty=0.0,
        long_avg=100.0,
        short_avg=100.0,
    )

    strategy.on_fill(fill_event, snapshot, runtime_state, context)

    reset_state = ShortTpFallbackState.from_dict(runtime_state.strategy_state.get("short_tp_fallback_state"))
    assert reset_state.active is False
    assert runtime_state.strategy_state.get("short_tp_fallback_order_context") is None
    assert runtime_state.strategy_state["cycle_waiting_for_short_tp"] is False

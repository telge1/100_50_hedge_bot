import logging
import threading
from collections import deque
from unittest import mock

import pytest

from strategy.config import StrategyConfig
from strategy.execution.order_executor import OrderExecutor, OrderIntent
from strategy.position_manager import PositionManager
from strategy.risk_manager import RiskManager
from strategy.state_machine import StateMachine


class FakeReduceOrderManager:
    def __init__(self) -> None:
        self.reduce_calls: list[dict] = []
        self.active_orders_ref: dict | None = None
        self._pending_reductions: deque[dict[str, float | str]] = deque()

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def place_reduce_market_order(self, **kwargs):
        self.reduce_calls.append(dict(kwargs))
        order_link_id = kwargs.get("order_link_id")
        if self.active_orders_ref is not None and order_link_id in self.active_orders_ref:
            tracked = dict(self.active_orders_ref[order_link_id])
            self.reduce_calls[-1]["tracked_order_size"] = tracked.get("size")
            self.reduce_calls[-1]["tracked_order_qty"] = tracked.get("qty")
            self.reduce_calls[-1]["tracked_reduce_only"] = (
                tracked.get("metadata") or {}
            ).get("reduce_only")
        self._pending_reductions.append(
            {
                "side": "short" if kwargs["side"] == "Buy" else "long",
                "qty": float(kwargs["qty"]),
            }
        )
        return {"result": {"orderId": f"ex-{len(self.reduce_calls)}"}}


class ExecutorHarness:
    def __init__(
        self,
        *,
        long_size: float,
        long_avg: float,
        short_size: float,
        short_avg: float,
    ) -> None:
        self.config = StrategyConfig()
        self.config.min_order_value = 1.0
        self.config.default_symbol = "BTCUSDT"
        self.config.category = "linear"
        self.order_manager = FakeReduceOrderManager()
        self.position_manager = PositionManager()
        self.position_manager.sync_positions(long_size, long_avg, short_size, short_avg)
        self.risk_manager = RiskManager(self.config)
        self.state_machine = StateMachine()
        self.active_orders: dict[str, dict] = {}
        self.submitted_orders: set[tuple[str, float, str]] = set()
        self.recent_orders: deque[str] = deque(maxlen=20)
        self.exchange_to_client_id: dict[str, str] = {}
        self.order_lock = threading.Lock()
        self.recorded_realized_by_side: list[tuple[str, float]] = []
        self.finalized_orders: list[tuple[str, dict]] = []
        self.order_manager.active_orders_ref = self.active_orders
        self._id_counter = 0
        self.executor = OrderExecutor(
            config=self.config,
            logger=logging.getLogger("test.order_executor.partial_reduce"),
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            state_machine=self.state_machine,
            active_orders=self.active_orders,
            submitted_orders=self.submitted_orders,
            recent_orders=self.recent_orders,
            exchange_to_client_id=self.exchange_to_client_id,
            order_lock=self.order_lock,
            normalize_order_qty=lambda qty, purpose: qty,
            current_qty_step=lambda: 0.001,
            has_active_intent=lambda side, purpose, price, qty: False,
            generate_client_order_id=self._generate_client_order_id,
            log_slippage_check=lambda side: None,
            safe_update_order=self._safe_update_order,
            mark_order_filled=self._mark_order_filled,
            record_realized_pnl_by_side=self._record_realized_pnl_by_side,
            handle_order_finalized_locked=self._handle_order_finalized_locked,
            sync_positions_with_exchange=self._sync_positions_with_exchange,
            get_position_snapshot=self._get_position_snapshot,
            verify_order_on_exchange=lambda *args, **kwargs: True,
            get_last_price=lambda: None,
            set_dca_steps=lambda value: None,
            on_intent_executed=lambda intent, submit_price: [],
        )

    def _generate_client_order_id(self, purpose: str) -> str:
        self._id_counter += 1
        return f"{purpose}-{self._id_counter}"

    def _safe_update_order(self, client_id: str, updates: dict) -> None:
        order = self.active_orders.get(client_id)
        if order:
            order.update(updates)

    def _mark_order_filled(self, client_id: str) -> None:
        order = self.active_orders.get(client_id)
        if order:
            order["status"] = "FILLED"

    def _record_realized_pnl_by_side(self, side: str, pnl: float) -> None:
        self.recorded_realized_by_side.append((side, pnl))

    def _handle_order_finalized_locked(self, client_id: str, order: dict) -> None:
        self.finalized_orders.append((client_id, dict(order)))

    def _sync_positions_with_exchange(self) -> None:
        if not self.order_manager._pending_reductions:
            return
        reduction = self.order_manager._pending_reductions.popleft()
        side = str(reduction["side"])
        qty = float(reduction["qty"])
        if side == "long":
            new_long = max(self.position_manager.long_size - qty, 0.0)
            self.position_manager.sync_positions(
                new_long,
                self.position_manager.long_avg,
                self.position_manager.short_size,
                self.position_manager.short_avg,
            )
        else:
            new_short = max(self.position_manager.short_size - qty, 0.0)
            self.position_manager.sync_positions(
                self.position_manager.long_size,
                self.position_manager.long_avg,
                new_short,
                self.position_manager.short_avg,
            )

    def _get_position_snapshot(self) -> tuple[float, float, float, float]:
        return (
            self.position_manager.long_size,
            self.position_manager.short_size,
            self.position_manager.long_avg,
            self.position_manager.short_avg,
        )


@pytest.mark.parametrize(
    ("purpose", "side", "price", "qty", "expected_pnl"),
    [
        ("normal_reduce_long", "long", 105.0, 2.0, 10.0),
        ("normal_reduce_short", "short", 95.0, 3.0, 15.0),
        ("failover_reduce_long", "long", 103.0, 1.5, 4.5),
        ("failover_reduce_short", "short", 94.0, 2.5, 15.0),
    ],
)
def test_partial_reduce_market_intents_use_exact_intent_qty(
    purpose: str,
    side: str,
    price: float,
    qty: float,
    expected_pnl: float,
) -> None:
    harness = ExecutorHarness(long_size=10.0, long_avg=100.0, short_size=10.0, short_avg=100.0)
    intent = OrderIntent(
        side=side,
        qty=qty,
        price=price,
        purpose=purpose,
        order_type="Market",
        reduce_only=True,
    )

    with mock.patch.object(
        harness.executor,
        "close_position",
        side_effect=AssertionError("partial reduce must not use full-close path"),
    ) as close_mock, mock.patch.object(
        harness.executor,
        "_force_reduce_market_exit",
        side_effect=AssertionError("partial reduce must not use forced full exit"),
    ) as force_mock, mock.patch.object(
        harness.executor,
        "_submit_partial_reduce_market",
        wraps=harness.executor._submit_partial_reduce_market,
    ) as partial_mock:
        result = harness.executor.execute_intent(intent)

    assert result is True
    assert close_mock.call_count == 0
    assert force_mock.call_count == 0
    assert partial_mock.call_count == 1
    assert len(harness.order_manager.reduce_calls) == 1
    assert harness.order_manager.reduce_calls[0]["qty"] == qty
    assert harness.order_manager.reduce_calls[0]["tracked_order_size"] == qty
    assert harness.order_manager.reduce_calls[0]["tracked_order_qty"] == qty
    assert harness.order_manager.reduce_calls[0]["tracked_reduce_only"] is True
    assert harness.recorded_realized_by_side == [(side, expected_pnl)]
    assert harness.risk_manager.realized_pnl == pytest.approx(expected_pnl)
    if side == "long":
        assert harness.position_manager.long_size == pytest.approx(10.0 - qty)
        assert harness.position_manager.short_size == pytest.approx(10.0)
    else:
        assert harness.position_manager.short_size == pytest.approx(10.0 - qty)
        assert harness.position_manager.long_size == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("purpose", "side", "intent_qty", "expected_reduce_qty"),
    [
        ("basket_exit", "long", 1.0, 10.0),
        ("DD_EXIT", "long", 1.0, 10.0),
        ("EMERGENCY", "long", 1.0, 10.0),
        ("TP_LONG", "long", 1.0, 10.0),
        ("TP_SHORT", "short", 1.0, 8.0),
        ("CLOSE_LONG", "long", 1.0, 10.0),
    ],
)
def test_explicit_full_close_purposes_ignore_intent_qty_and_close_live_side(
    purpose: str,
    side: str,
    intent_qty: float,
    expected_reduce_qty: float,
) -> None:
    harness = ExecutorHarness(long_size=10.0, long_avg=100.0, short_size=8.0, short_avg=100.0)
    price = 105.0 if side == "long" else 95.0
    intent = OrderIntent(
        side=side,
        qty=intent_qty,
        price=price,
        purpose=purpose,
        order_type="Market",
        reduce_only=True,
    )

    with mock.patch.object(
        harness.executor,
        "_submit_partial_reduce_market",
        side_effect=AssertionError("explicit full-close purpose must not use partial path"),
    ) as partial_mock, mock.patch.object(
        harness.executor,
        "close_position",
        wraps=harness.executor.close_position,
    ) as close_mock:
        result = harness.executor.execute_intent(intent)

    assert result is True
    assert partial_mock.call_count == 0
    assert close_mock.call_count == 1
    assert len(harness.order_manager.reduce_calls) == 1
    assert harness.order_manager.reduce_calls[0]["qty"] == expected_reduce_qty
    assert harness.order_manager.reduce_calls[0]["tracked_order_size"] == expected_reduce_qty
    if side == "long":
        assert harness.position_manager.long_size == 0.0
    else:
        assert harness.position_manager.short_size == 0.0


def test_reduce_only_market_non_full_close_purpose_stays_partial() -> None:
    harness = ExecutorHarness(long_size=12.0, long_avg=100.0, short_size=9.0, short_avg=100.0)
    intent = OrderIntent(
        side="long",
        qty=2.25,
        price=104.0,
        purpose="normal_reduce_long",
        order_type="Market",
        reduce_only=True,
    )

    with mock.patch.object(
        harness.executor,
        "close_position",
        side_effect=AssertionError("reduce_only alone must not imply full close"),
    ), mock.patch.object(
        harness.executor,
        "_submit_partial_reduce_market",
        wraps=harness.executor._submit_partial_reduce_market,
    ) as partial_mock:
        result = harness.executor.execute_intent(intent)

    assert result is True
    assert partial_mock.call_count == 1
    assert harness.order_manager.reduce_calls[0]["qty"] == pytest.approx(2.25)
    assert harness.position_manager.long_size == pytest.approx(9.75)

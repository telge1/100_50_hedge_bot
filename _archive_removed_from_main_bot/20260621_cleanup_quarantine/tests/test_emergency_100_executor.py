import logging
import unittest

from emergency_100.executor import Emergency100Executor
from emergency_100.state import HedgeSnapshot
from emergency_100.strategy import ActionKind, StrategyAction
from strategy.config import StrategyConfig


class FakeOrderManager:
    def __init__(self) -> None:
        self.market_orders = []
        self.reduce_market_orders = []

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def place_market_order(self, **kwargs):
        self.market_orders.append(kwargs)
        return {"result": {"orderId": "market-1"}}

    def place_reduce_market_order(self, **kwargs):
        self.reduce_market_orders.append(kwargs)
        return {"result": {"orderId": "reduce-1"}}


class Emergency100ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = StrategyConfig()
        self.config.min_order_value = 5.0
        self.order_manager = FakeOrderManager()
        self.executor = Emergency100Executor(
            self.config,
            self.order_manager,
            logging.getLogger("test.emergency_100.executor"),
        )
        self.snapshot = HedgeSnapshot(
            symbol="BTCUSDT",
            current_price=100.0,
            long_size_usdt=100.0,
            short_size_usdt=100.0,
            long_avg=100.0,
            short_avg=97.0,
        )

    def test_add_long_submits_buy_market_order(self) -> None:
        result = self.executor.execute_actions(
            snapshot=self.snapshot,
            actions=[StrategyAction(kind=ActionKind.ADD_LONG, size_usdt=20.0)],
            cycle_id="cycle-1",
            decision_id="cycle-1-d0001",
            execute_live=True,
        )

        self.assertEqual(result[0].status, "submitted")
        self.assertEqual(len(self.order_manager.market_orders), 1)
        self.assertEqual(self.order_manager.market_orders[0]["side"], "Buy")
        self.assertEqual(self.order_manager.market_orders[0]["position_idx"], 1)

    def test_add_short_submits_sell_market_order(self) -> None:
        result = self.executor.execute_actions(
            snapshot=self.snapshot,
            actions=[StrategyAction(kind=ActionKind.ADD_SHORT, size_usdt=20.0)],
            cycle_id="cycle-1",
            decision_id="cycle-1-d0001",
            execute_live=True,
        )

        self.assertEqual(result[0].status, "submitted")
        self.assertEqual(len(self.order_manager.market_orders), 1)
        self.assertEqual(self.order_manager.market_orders[0]["side"], "Sell")
        self.assertEqual(self.order_manager.market_orders[0]["position_idx"], 2)

    def test_reduce_short_submits_reduce_only_buy(self) -> None:
        result = self.executor.execute_actions(
            snapshot=self.snapshot,
            actions=[StrategyAction(kind=ActionKind.REDUCE_SHORT, size_usdt=20.0)],
            cycle_id="cycle-1",
            decision_id="cycle-1-d0001",
            execute_live=True,
        )

        self.assertEqual(result[0].status, "submitted")
        self.assertEqual(len(self.order_manager.reduce_market_orders), 1)
        self.assertEqual(self.order_manager.reduce_market_orders[0]["side"], "Buy")
        self.assertEqual(self.order_manager.reduce_market_orders[0]["position_idx"], 2)

    def test_dry_run_marks_action_as_planned(self) -> None:
        result = self.executor.execute_actions(
            snapshot=self.snapshot,
            actions=[StrategyAction(kind=ActionKind.ADD_SHORT, size_usdt=20.0)],
            cycle_id="cycle-1",
            decision_id="cycle-1-d0001",
            execute_live=False,
        )

        self.assertEqual(result[0].status, "planned")
        self.assertEqual(len(self.order_manager.market_orders), 0)

    def test_below_min_order_value_is_skipped(self) -> None:
        result = self.executor.execute_actions(
            snapshot=self.snapshot,
            actions=[StrategyAction(kind=ActionKind.ADD_SHORT, size_usdt=2.0)],
            cycle_id="cycle-1",
            decision_id="cycle-1-d0001",
            execute_live=True,
        )

        self.assertEqual(result[0].status, "skipped")
        self.assertEqual(len(self.order_manager.market_orders), 0)


if __name__ == "__main__":
    unittest.main()

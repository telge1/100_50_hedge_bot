import logging
import unittest

from fixed_cycle_hedge_bot.basket_exit_strategy import BasketExitConfig, BasketExitHedgeStrategy
from fixed_cycle_hedge_bot.runtime import GenericHedgeRuntime, GenericRuntimeConfig


class FakeOrderManager:
    def __init__(self) -> None:
        self.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 1.0, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        self.current_price = 100.0
        self.reduce_market_orders = []
        self.limit_orders = []
        self.market_orders = []
        self.open_orders = []
        self.order_histories = {}

    def normalize_qty(self, symbol: str, qty: float, category: str) -> float:
        return qty

    def fetch_positions(self, symbol: str | None = None, category: str = "linear", settle_coin: str | None = None):
        return list(self.positions)

    def fetch_mark_price(self, symbol: str, category: str = "linear") -> float:
        return self.current_price

    def place_reduce_market_order(self, **kwargs):
        self.reduce_market_orders.append(kwargs)
        return {"result": {"orderId": f"ex-{kwargs['order_link_id']}"}}

    def place_limit_order(self, payload):
        self.limit_orders.append(payload)
        return {"result": {"orderId": f"ex-{payload.order_link_id}"}}

    def place_market_order(self, **kwargs):
        self.market_orders.append(kwargs)
        return {"result": {"orderId": f"ex-{kwargs['order_link_id']}"}}

    def ensure_hedge_mode(self, symbol: str, category: str = "linear") -> bool:
        return True

    def ensure_max_leverage(self, symbol: str, category: str = "linear") -> bool:
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


class BasketExitRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.order_manager = FakeOrderManager()
        self.strategy = BasketExitHedgeStrategy(BasketExitConfig(target_basket_pnl=0.0))
        self.runtime = GenericHedgeRuntime(
            GenericRuntimeConfig(
                api_key="key",
                secret_key="secret",
                symbol="BTCUSDT",
                category="linear",
                min_order_value=1.0,
                ensure_exchange_ready=False,
                audit_log_file=None,
            ),
            self.strategy,
            logger=logging.getLogger("test.basket_exit"),
            order_manager=self.order_manager,
        )
        self.runtime.bootstrap()

    def test_tick_closes_both_sides_when_basket_at_breakeven(self) -> None:
        self.runtime.process_tick()

        self.assertEqual(len(self.order_manager.reduce_market_orders), 2)
        sides = [order["side"] for order in self.order_manager.reduce_market_orders]
        self.assertIn("Sell", sides)
        self.assertIn("Buy", sides)

    def test_bootstrap_classifies_unknown_basket_exit_orders_from_position_idx(self) -> None:
        self.order_manager.open_orders = [
            {
                "orderId": "ex-basket-long",
                "orderStatus": "New",
                "qty": "1.0",
                "side": "Sell",
                "positionIdx": 1,
                "orderType": "Limit",
                "reduceOnly": True,
                "price": "100.5",
                "cumExecQty": "0",
            },
            {
                "orderId": "ex-basket-short",
                "orderStatus": "New",
                "qty": "0.5",
                "side": "Buy",
                "positionIdx": 2,
                "orderType": "Limit",
                "reduceOnly": True,
                "price": "99.5",
                "cumExecQty": "0",
            },
        ]
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
            BasketExitHedgeStrategy(BasketExitConfig(target_basket_pnl=0.0)),
            logger=logging.getLogger("test.basket_exit.classify"),
            order_manager=self.order_manager,
        )

        runtime.bootstrap()

        long_id = runtime.runtime_state.exchange_to_client_id["ex-basket-long"]
        short_id = runtime.runtime_state.exchange_to_client_id["ex-basket-short"]
        self.assertEqual(runtime.runtime_state.active_orders[long_id].purpose, "BASKET_EXIT_LONG")
        self.assertEqual(runtime.runtime_state.active_orders[long_id].side, "long")
        self.assertEqual(runtime.runtime_state.active_orders[short_id].purpose, "BASKET_EXIT_SHORT")
        self.assertEqual(runtime.runtime_state.active_orders[short_id].side, "short")


if __name__ == "__main__":
    unittest.main()

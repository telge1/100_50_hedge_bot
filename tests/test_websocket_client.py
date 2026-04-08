import unittest

from core.websocket_client import BybitWebSocketClient


class TestBybitWebSocketClient(unittest.TestCase):
    def test_partial_fill_triggers_fill_callback(self) -> None:
        client = BybitWebSocketClient("key", "secret")
        fills = []
        client.set_fill_callback(
            lambda order_id, qty, price, **kwargs: fills.append(
                (order_id, qty, price, kwargs.get("exec_id"), kwargs.get("cumulative_qty"))
            )
        )

        client.process_order_message(
            {
                "orderId": "ex-partial-live",
                "symbol": "BTCUSDT",
                "orderStatus": "PartiallyFilled",
                "cancelType": "UNKNOWN",
                "execId": "exec-1",
                "execQty": "0.25",
                "execPrice": "100.5",
                "cumExecQty": "0.25",
                "qty": "1.0",
                "price": "100.5",
                "side": "Buy",
                "orderType": "Limit",
            }
        )

        self.assertEqual(fills, [("ex-partial-live", 0.25, 100.5, "exec-1", 0.25)])

    def test_filled_without_exec_qty_uses_cumulative_fallback(self) -> None:
        client = BybitWebSocketClient("key", "secret")
        fills = []
        client.set_fill_callback(
            lambda order_id, qty, price, **kwargs: fills.append(
                (order_id, qty, price, kwargs.get("exec_id"), kwargs.get("cumulative_qty"))
            )
        )

        client.process_order_message(
            {
                "orderId": "ex-filled-cum-only",
                "symbol": "BTCUSDT",
                "orderStatus": "Filled",
                "cancelType": "UNKNOWN",
                "avgPrice": "101.25",
                "cumExecQty": "1.0",
                "qty": "1.0",
                "price": "101.0",
                "side": "Buy",
                "orderType": "Limit",
            }
        )

        self.assertEqual(fills, [("ex-filled-cum-only", 0.0, 101.25, None, 1.0)])

    def test_cancelled_order_does_not_trigger_fill_callback(self) -> None:
        client = BybitWebSocketClient("key", "secret")
        fills = []
        client.set_fill_callback(lambda order_id, qty, price: fills.append((order_id, qty, price)))

        client.process_order_message(
            {
                "orderId": "ex-cancelled",
                "symbol": "BTCUSDT",
                "orderStatus": "Filled",
                "cancelType": "CancelByUser",
                "execQty": "1.0",
                "execPrice": "100.0",
                "qty": "1.0",
                "price": "100.0",
                "side": "Buy",
                "orderType": "Market",
            }
        )

        self.assertEqual(fills, [])


if __name__ == "__main__":
    unittest.main()

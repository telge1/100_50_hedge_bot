import json
import logging
import tempfile
import unittest
from pathlib import Path

from fixed_cycle_hedge_bot.dynamic_breakeven_strategy import (
    DynamicBreakevenConfig,
    DynamicBreakevenHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import ManagedOrder
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
        self.cancel_calls = []
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

    def cancel_order(self, order_id: str, *, symbol: str | None = None, category: str = "linear") -> bool:
        self.cancel_calls.append({"order_id": order_id, "symbol": symbol, "category": category})
        return True

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


class DynamicBreakevenRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.order_manager = FakeOrderManager()
        self.strategy = DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig())
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
            logger=logging.getLogger("test.dynamic_breakeven.folder"),
            order_manager=self.order_manager,
        )
        self.runtime.bootstrap()

    def test_tick_places_long_reduce_when_price_drops_to_trigger(self) -> None:
        self.order_manager.current_price = 99.0
        self.runtime.process_tick()
        self.assertEqual(len(self.order_manager.reduce_market_orders), 1)
        order = self.order_manager.reduce_market_orders[0]
        self.assertEqual(order["side"], "Sell")
        self.assertAlmostEqual(float(order["qty"]), 0.33)

    def test_reconcile_converts_filled_long_reduce_into_short_compensation(self) -> None:
        self.order_manager.current_price = 99.0
        self.runtime.process_tick()

        client_id, managed_order = next(iter(self.runtime.runtime_state.active_orders.items()))
        exchange_order_id = managed_order.exchange_order_id
        self.order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.67, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        self.order_manager.order_histories[client_id] = [
            {
                "orderId": exchange_order_id,
                "orderLinkId": client_id,
                "orderStatus": "Filled",
                "cumExecQty": "0.33",
                "avgPrice": "99.0",
                "price": "99.0",
            }
        ]

        self.runtime.reconcile_once()

        self.assertEqual(len(self.order_manager.limit_orders), 1)
        self.assertAlmostEqual(float(self.order_manager.limit_orders[0].price), 97.95, places=6)

    def test_reconcile_keeps_open_order_when_exchange_reports_new(self) -> None:
        self.order_manager.current_price = 99.0
        self.runtime.process_tick()

        client_id, managed_order = next(iter(self.runtime.runtime_state.active_orders.items()))
        self.order_manager.open_orders = [
            {
                "orderId": managed_order.exchange_order_id,
                "orderLinkId": client_id,
                "orderStatus": "New",
                "cumExecQty": "0",
            }
        ]

        self.runtime.reconcile_once()

        self.assertIn(client_id, self.runtime.runtime_state.active_orders)
        self.assertEqual(self.runtime.runtime_state.active_orders[client_id].status, "OPEN")

    def test_reconcile_logs_open_order_match_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audit_file = Path(tmp_dir) / "audit.jsonl"
            runtime = GenericHedgeRuntime(
                GenericRuntimeConfig(
                    api_key="key",
                    secret_key="secret",
                    symbol="BTCUSDT",
                    category="linear",
                    min_order_value=1.0,
                    ensure_exchange_ready=False,
                    audit_log_file=str(audit_file),
                ),
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.reconcile.audit.open"),
                order_manager=self.order_manager,
            )
            runtime.bootstrap()
            self.order_manager.current_price = 99.0
            runtime.process_tick()

            client_id, managed_order = next(iter(runtime.runtime_state.active_orders.items()))
            self.order_manager.open_orders = [
                {
                    "orderId": managed_order.exchange_order_id,
                    "orderLinkId": client_id,
                    "orderStatus": "New",
                    "cumExecQty": "0",
                    "qty": str(managed_order.qty),
                    "side": "Sell",
                    "orderType": "Market",
                }
            ]

            runtime.reconcile_once()

            records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
            record = next(record for record in records if record["event"] == "order_reconciled_open")
            self.assertEqual(record["reconcile_source"], "open_orders")
            self.assertEqual(record["managed_order"]["client_order_id"], client_id)
            self.assertEqual(record["exchange_order"]["order_id"], managed_order.exchange_order_id)

    def test_reconcile_marks_partial_order_and_keeps_it_active(self) -> None:
        self.order_manager.current_price = 99.0
        self.runtime.process_tick()

        client_id, managed_order = next(iter(self.runtime.runtime_state.active_orders.items()))
        self.order_manager.positions = [
            {"symbol": "BTCUSDT", "side": "Buy", "size": 0.90, "avgPrice": 100.0},
            {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
        ]
        self.order_manager.order_histories[client_id] = [
            {
                "orderId": managed_order.exchange_order_id,
                "orderLinkId": client_id,
                "orderStatus": "PartiallyFilled",
                "cumExecQty": "0.10",
                "avgPrice": "99.0",
                "price": "99.0",
            }
        ]

        self.runtime.reconcile_once()

        self.assertIn(client_id, self.runtime.runtime_state.active_orders)
        self.assertEqual(self.runtime.runtime_state.active_orders[client_id].status, "PARTIAL")
        self.assertAlmostEqual(self.runtime.runtime_state.active_orders[client_id].filled_qty, 0.10)

    def test_reconcile_logs_history_fill_inference_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audit_file = Path(tmp_dir) / "audit.jsonl"
            runtime = GenericHedgeRuntime(
                GenericRuntimeConfig(
                    api_key="key",
                    secret_key="secret",
                    symbol="BTCUSDT",
                    category="linear",
                    min_order_value=1.0,
                    ensure_exchange_ready=False,
                    audit_log_file=str(audit_file),
                ),
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.reconcile.audit.history"),
                order_manager=self.order_manager,
            )
            runtime.bootstrap()
            self.order_manager.current_price = 99.0
            runtime.process_tick()

            client_id, managed_order = next(iter(runtime.runtime_state.active_orders.items()))
            self.order_manager.positions = [
                {"symbol": "BTCUSDT", "side": "Buy", "size": 0.67, "avgPrice": 100.0},
                {"symbol": "BTCUSDT", "side": "Sell", "size": 0.5, "avgPrice": 100.0},
            ]
            self.order_manager.order_histories[client_id] = [
                {
                    "orderId": managed_order.exchange_order_id,
                    "orderLinkId": client_id,
                    "orderStatus": "Filled",
                    "cumExecQty": "0.33",
                    "avgPrice": "99.0",
                    "price": "99.0",
                }
            ]

            runtime.reconcile_once()

            records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
            history_record = next(record for record in records if record["event"] == "reconcile_history_found")
            self.assertEqual(history_record["normalized_history_status"], "FILLED")
            self.assertEqual(history_record["history_order"]["order_link_id"], client_id)
            fill_record = next(record for record in records if record["event"] == "reconcile_fill_inferred")
            self.assertAlmostEqual(fill_record["incremental_qty"], 0.33)
            self.assertAlmostEqual(fill_record["exec_price"], 99.0)

    def test_reconcile_removes_canceled_order(self) -> None:
        self.order_manager.current_price = 99.0
        self.runtime.process_tick()

        client_id, managed_order = next(iter(self.runtime.runtime_state.active_orders.items()))
        self.order_manager.order_histories[client_id] = [
            {
                "orderId": managed_order.exchange_order_id,
                "orderLinkId": client_id,
                "orderStatus": "Cancelled",
                "cumExecQty": "0",
            }
        ]

        self.runtime.reconcile_once()

        self.assertNotIn(client_id, self.runtime.runtime_state.active_orders)

    def test_reconcile_removes_rejected_order(self) -> None:
        self.order_manager.current_price = 99.0
        self.runtime.process_tick()

        client_id, managed_order = next(iter(self.runtime.runtime_state.active_orders.items()))
        self.order_manager.order_histories[client_id] = [
            {
                "orderId": managed_order.exchange_order_id,
                "orderLinkId": client_id,
                "orderStatus": "Rejected",
                "cumExecQty": "0",
            }
        ]

        self.runtime.reconcile_once()

        self.assertNotIn(client_id, self.runtime.runtime_state.active_orders)

    def test_bootstrap_loads_persisted_strategy_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "runtime_state.json"
            first_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.persistence.first"),
                order_manager=self.order_manager,
            )
            first_runtime.bootstrap()
            first_runtime.runtime_state.strategy_state["awaiting_short_fill"] = True
            first_runtime.runtime_state.strategy_state["custom_value"] = 7
            first_runtime._save_strategy_state()

            second_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.persistence.second"),
                order_manager=self.order_manager,
            )
            second_runtime.bootstrap()

            self.assertTrue(second_runtime.runtime_state.strategy_state["awaiting_short_fill"])
            self.assertEqual(second_runtime.runtime_state.strategy_state["custom_value"], 7)

    def test_bootstrap_restores_active_order_from_persisted_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "runtime_state.json"
            first_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.persist.first"),
                order_manager=self.order_manager,
            )
            first_runtime.bootstrap()
            first_runtime.runtime_state.active_orders["cid-persist"] = ManagedOrder(
                client_order_id="cid-persist",
                side="short",
                qty=0.25,
                purpose="DYN_SHORT_COMPENSATE",
                price=97.5,
                order_type="Limit",
                reduce_only=True,
                exchange_order_id="ex-cid-persist",
                status="OPEN",
                remaining_qty=0.25,
                metadata={"manual_test": True},
            )
            first_runtime.runtime_state.exchange_to_client_id["ex-cid-persist"] = "cid-persist"
            self.order_manager.open_orders = [
                {
                    "orderId": "ex-cid-persist",
                    "orderLinkId": "cid-persist",
                    "orderStatus": "New",
                    "qty": "0.25",
                    "price": "97.5",
                    "side": "Buy",
                    "orderType": "Limit",
                    "reduceOnly": True,
                    "cumExecQty": "0",
                }
            ]
            first_runtime._save_strategy_state()

            second_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.persist.second"),
                order_manager=self.order_manager,
            )
            second_runtime.bootstrap()

            self.assertIn("cid-persist", second_runtime.runtime_state.active_orders)
            self.assertEqual(second_runtime.runtime_state.exchange_to_client_id["ex-cid-persist"], "cid-persist")
            self.assertEqual(second_runtime.runtime_state.active_orders["cid-persist"].status, "OPEN")

    def test_bootstrap_recovers_open_exchange_order_without_persisted_state(self) -> None:
        self.order_manager.open_orders = [
            {
                "orderId": "ex-recovered-1",
                "orderLinkId": "dynamic_breakeven_hedge-dyn_short_compensate-abc123",
                "orderStatus": "New",
                "qty": "0.15",
                "price": "97.4",
                "side": "Buy",
                "orderType": "Limit",
                "reduceOnly": True,
                "cumExecQty": "0",
            }
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
            DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
            logger=logging.getLogger("test.dynamic.recovery.exchange"),
            order_manager=self.order_manager,
        )
        runtime.bootstrap()

        recovered_id = "dynamic_breakeven_hedge-dyn_short_compensate-abc123"
        self.assertIn(recovered_id, runtime.runtime_state.active_orders)
        recovered = runtime.runtime_state.active_orders[recovered_id]
        self.assertEqual(recovered.purpose, "DYN_SHORT_COMPENSATE")
        self.assertEqual(recovered.status, "OPEN")
        self.assertEqual(runtime.runtime_state.exchange_to_client_id["ex-recovered-1"], recovered_id)

    def test_bootstrap_matches_open_exchange_order_without_link_id_to_persisted_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "runtime_state.json"
            first_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.nolink.first"),
                order_manager=self.order_manager,
            )
            first_runtime.bootstrap()
            first_runtime.runtime_state.active_orders["cid-known"] = ManagedOrder(
                client_order_id="cid-known",
                side="short",
                qty=0.25,
                purpose="DYN_SHORT_COMPENSATE",
                price=97.5,
                order_type="Limit",
                reduce_only=True,
                status="OPEN",
                remaining_qty=0.25,
                metadata={"persisted": True},
            )
            first_runtime._save_strategy_state()

            self.order_manager.open_orders = [
                {
                    "orderId": "ex-no-link-1",
                    "orderStatus": "New",
                    "qty": "0.25",
                    "price": "97.5",
                    "side": "Buy",
                    "orderType": "Limit",
                    "reduceOnly": True,
                    "cumExecQty": "0",
                }
            ]

            second_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.nolink.second"),
                order_manager=self.order_manager,
            )
            second_runtime.bootstrap()

            self.assertIn("cid-known", second_runtime.runtime_state.active_orders)
            recovered = second_runtime.runtime_state.active_orders["cid-known"]
            self.assertEqual(recovered.exchange_order_id, "ex-no-link-1")
            self.assertEqual(recovered.purpose, "DYN_SHORT_COMPENSATE")
            self.assertEqual(second_runtime.runtime_state.exchange_to_client_id["ex-no-link-1"], "cid-known")

    def test_bootstrap_does_not_mismatch_without_link_id_when_candidates_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "runtime_state.json"
            first_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.ambiguous.first"),
                order_manager=self.order_manager,
            )
            first_runtime.bootstrap()
            first_runtime.runtime_state.active_orders["cid-a"] = ManagedOrder(
                client_order_id="cid-a",
                side="short",
                qty=0.25,
                purpose="DYN_SHORT_COMPENSATE",
                price=97.5,
                order_type="Limit",
                reduce_only=True,
                status="OPEN",
                remaining_qty=0.25,
            )
            first_runtime.runtime_state.active_orders["cid-b"] = ManagedOrder(
                client_order_id="cid-b",
                side="short",
                qty=0.25,
                purpose="OTHER_SHORT_EXIT",
                price=97.5,
                order_type="Limit",
                reduce_only=True,
                status="OPEN",
                remaining_qty=0.25,
            )
            first_runtime._save_strategy_state()

            self.order_manager.open_orders = [
                {
                    "orderId": "ex-no-link-ambiguous",
                    "orderStatus": "New",
                    "qty": "0.25",
                    "price": "97.5",
                    "side": "Buy",
                    "orderType": "Limit",
                    "reduceOnly": True,
                    "cumExecQty": "0",
                }
            ]

            second_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.ambiguous.second"),
                order_manager=self.order_manager,
            )
            second_runtime.bootstrap()

            self.assertNotEqual(second_runtime.runtime_state.exchange_to_client_id["ex-no-link-ambiguous"], "cid-a")
            self.assertNotEqual(second_runtime.runtime_state.exchange_to_client_id["ex-no-link-ambiguous"], "cid-b")

    def test_bootstrap_classifies_unknown_dynamic_short_compensation_without_link_id(self) -> None:
        self.order_manager.open_orders = [
            {
                "orderId": "ex-classify-dyn-1",
                "orderStatus": "New",
                "qty": "0.15",
                "price": "97.4",
                "side": "Buy",
                "positionIdx": 2,
                "orderType": "Limit",
                "reduceOnly": True,
                "cumExecQty": "0",
            }
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
            DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
            logger=logging.getLogger("test.dynamic.recovery.classify"),
            order_manager=self.order_manager,
        )
        runtime.bootstrap()

        recovered_id = runtime.runtime_state.exchange_to_client_id["ex-classify-dyn-1"]
        recovered = runtime.runtime_state.active_orders[recovered_id]
        self.assertEqual(recovered.side, "short")
        self.assertEqual(recovered.purpose, "DYN_SHORT_COMPENSATE")
        self.assertIn("dyn_short_compensate", recovered.client_order_id)

    def test_bootstrap_logs_heuristic_classification_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audit_file = Path(tmp_dir) / "audit.jsonl"
            self.order_manager.open_orders = [
                {
                    "orderId": "ex-audit-classify-1",
                    "orderStatus": "New",
                    "qty": "0.15",
                    "price": "97.4",
                    "side": "Buy",
                    "positionIdx": 2,
                    "orderType": "Limit",
                    "reduceOnly": True,
                    "cumExecQty": "0",
                }
            ]

            runtime = GenericHedgeRuntime(
                GenericRuntimeConfig(
                    api_key="key",
                    secret_key="secret",
                    symbol="BTCUSDT",
                    category="linear",
                    min_order_value=1.0,
                    ensure_exchange_ready=False,
                    audit_log_file=str(audit_file),
                ),
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.audit.classify"),
                order_manager=self.order_manager,
            )
            runtime.bootstrap()

            records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
            classification_record = next(record for record in records if record["event"] == "startup_order_recovery_classified")
            self.assertEqual(classification_record["classified_purpose"], "DYN_SHORT_COMPENSATE")
            self.assertEqual(classification_record["classification_inputs"]["runtime_side"], "short")
            attach_record = next(record for record in records if record["event"] == "startup_order_recovery_attached")
            self.assertEqual(attach_record["recovery_source"], "heuristic_classification")

    def test_bootstrap_logs_ambiguous_match_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "runtime_state.json"
            audit_file = Path(tmp_dir) / "audit.jsonl"
            first_runtime = GenericHedgeRuntime(
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
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.audit.ambiguous.first"),
                order_manager=self.order_manager,
            )
            first_runtime.bootstrap()
            first_runtime.runtime_state.active_orders["cid-a"] = ManagedOrder(
                client_order_id="cid-a",
                side="short",
                qty=0.25,
                purpose="DYN_SHORT_COMPENSATE",
                price=97.5,
                order_type="Limit",
                reduce_only=True,
                status="OPEN",
                remaining_qty=0.25,
            )
            first_runtime.runtime_state.active_orders["cid-b"] = ManagedOrder(
                client_order_id="cid-b",
                side="short",
                qty=0.25,
                purpose="OTHER_SHORT_EXIT",
                price=97.5,
                order_type="Limit",
                reduce_only=True,
                status="OPEN",
                remaining_qty=0.25,
            )
            first_runtime._save_strategy_state()
            self.order_manager.open_orders = [
                {
                    "orderId": "ex-audit-ambiguous-1",
                    "orderStatus": "New",
                    "qty": "0.25",
                    "price": "97.5",
                    "side": "Buy",
                    "orderType": "Limit",
                    "reduceOnly": True,
                    "cumExecQty": "0",
                }
            ]

            second_runtime = GenericHedgeRuntime(
                GenericRuntimeConfig(
                    api_key="key",
                    secret_key="secret",
                    symbol="BTCUSDT",
                    category="linear",
                    min_order_value=1.0,
                    ensure_exchange_ready=False,
                    audit_log_file=str(audit_file),
                    strategy_state_file=str(state_file),
                ),
                DynamicBreakevenHedgeStrategy(DynamicBreakevenConfig()),
                logger=logging.getLogger("test.dynamic.recovery.audit.ambiguous.second"),
                order_manager=self.order_manager,
            )
            second_runtime.bootstrap()

            records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
            skip_record = next(record for record in records if record["event"] == "startup_order_recovery_match_skipped")
            self.assertEqual(skip_record["reason"], "ambiguous_candidates")
            self.assertGreaterEqual(len(skip_record["candidate_scores"]), 2)


if __name__ == "__main__":
    unittest.main()

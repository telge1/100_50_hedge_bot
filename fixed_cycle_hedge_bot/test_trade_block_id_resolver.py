#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    configure_confirmed_order_pnl_history_file,
    set_default_bot_name,
)
from fixed_cycle_hedge_bot.models import RuntimeState
from fixed_cycle_hedge_bot.trade_block_id_resolver import (
    preserve_last_trade_block_id_before_clear,
    resolve_active_trade_block_id,
)

LAST_TBID = "11111111-1111-1111-1111-111111111111"
CURRENT_TBID = "22222222-2222-2222-2222-222222222222"
CTX_TBID = "33333333-3333-3333-3333-333333333333"


class ResolveActiveTradeBlockIdTests(unittest.TestCase):
    def test_prefers_trade_block_id_over_last(self) -> None:
        state = {
            "trade_block_id": CURRENT_TBID,
            "last_trade_block_id": LAST_TBID,
        }
        self.assertEqual(resolve_active_trade_block_id(state), CURRENT_TBID)

    def test_falls_back_to_last_trade_block_id(self) -> None:
        state = {"last_trade_block_id": LAST_TBID}
        self.assertEqual(resolve_active_trade_block_id(state), LAST_TBID)

    def test_falls_back_to_final_exit_contexts(self) -> None:
        state = {
            "final_exit_trading_stop_context": {"trade_block_id": CTX_TBID},
            "final_long_exit_order_context": {"trade_block_id": "ignored-if-trading-stop-set"},
        }
        self.assertEqual(resolve_active_trade_block_id(state), CTX_TBID)

    def test_falls_back_to_final_long_exit_order_context(self) -> None:
        state = {
            "final_long_exit_order_context": {"trade_block_id": CTX_TBID},
        }
        self.assertEqual(resolve_active_trade_block_id(state), CTX_TBID)

    def test_returns_none_when_missing(self) -> None:
        self.assertIsNone(resolve_active_trade_block_id({}))


class PreserveLastTradeBlockIdTests(unittest.TestCase):
    def test_copies_current_trade_block_id_to_last(self) -> None:
        state = {
            "trade_block_id": CURRENT_TBID,
            "last_trade_block_id": "old-value",
        }
        preserve_last_trade_block_id_before_clear(state)
        self.assertEqual(state["last_trade_block_id"], CURRENT_TBID)
        self.assertEqual(state["trade_block_id"], CURRENT_TBID)

    def test_leaves_last_unchanged_when_current_missing(self) -> None:
        state = {"last_trade_block_id": LAST_TBID, "trade_block_id": None}
        preserve_last_trade_block_id_before_clear(state)
        self.assertEqual(state["last_trade_block_id"], LAST_TBID)


class ConfirmedWriterTradeBlockIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "confirmed_order_pnl_history.jsonl"
        configure_confirmed_order_pnl_history_file(self.history_path)
        set_default_bot_name("long_bot_1")
        self.strategy = FixedCycleHedgeStrategy()
        self.runtime_state = RuntimeState(strategy_state={})

    def tearDown(self) -> None:
        configure_confirmed_order_pnl_history_file(None)
        set_default_bot_name("long_bot_1")
        self.temp_dir.cleanup()

    def _base_payload(self) -> dict:
        return {
            "exchange_order_id": "order-1",
            "purpose": "CYCLE_1_LONG_REDUCE",
            "closed_pnl": 1.23,
            "side": "long",
        }

    def test_writes_with_last_trade_block_id_when_payload_missing(self) -> None:
        self.runtime_state.strategy_state["last_trade_block_id"] = LAST_TBID
        self.strategy._write_confirmed_order_pnl_history(
            self._base_payload(),
            runtime_state=self.runtime_state,
        )
        lines = self.history_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["trade_block_id"], LAST_TBID)

    def test_writes_with_unknown_trade_block_id_when_missing(self) -> None:
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event"
        ) as warning_mock:
            self.strategy._write_confirmed_order_pnl_history(
                self._base_payload(),
                runtime_state=self.runtime_state,
            )
            warning_mock.assert_called()
            event_name = warning_mock.call_args[0][0]
            self.assertEqual(event_name, "confirmed_order_pnl_trade_block_id_missing_wrote_anyway")
        lines = self.history_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["trade_block_id"], "unknown")
        self.assertEqual(row["trade_block_id_source"], "missing_fallback")


class FinalizeTradeBlockIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = FixedCycleHedgeStrategy()
        self.runtime_state = RuntimeState(strategy_state={})

    def _ledger_ready_state(self, *, last_trade_block_id: str | None) -> None:
        state = self.runtime_state.strategy_state
        state["audit_pnl_ledger"] = {
            "cycle_long_reduce_pnl": {},
            "cycle_short_tp_pnl": {},
            "final_long_exit_pnl": 0.5,
            "final_short_exit_pnl": 0.25,
            "total_realized_pnl": 0.75,
        }
        state["final_long_exit_audited"] = True
        state["final_short_exit_audited"] = True
        state["trade_block_id"] = None
        if last_trade_block_id:
            state["last_trade_block_id"] = last_trade_block_id

    def test_finalize_uses_last_trade_block_id_without_uuid4(self) -> None:
        self._ledger_ready_state(last_trade_block_id=LAST_TBID)
        captured: dict[str, str] = {}

        def _capture_persist(
            runtime_state: RuntimeState,
            *,
            total_trade_pnl: float,
            breakdown: dict,
            source: str,
            pnl_complete: bool,
            trade_block_id: str,
            finalized_at: str,
        ) -> None:
            del runtime_state, total_trade_pnl, breakdown, source, pnl_complete, finalized_at
            captured["trade_block_id"] = trade_block_id

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.uuid4",
            side_effect=AssertionError("uuid4 must not be called during finalize"),
        ):
            with mock.patch.object(
                self.strategy,
                "_persist_last_trade_pnl_summary",
                side_effect=_capture_persist,
            ):
                result = self.strategy._emit_final_trade_pnl_if_complete_or_fetch(
                    self.runtime_state,
                    None,
                    "test_finalize",
                )
        self.assertTrue(result)
        self.assertEqual(captured["trade_block_id"], LAST_TBID)

    def test_finalize_skips_without_any_trade_block_id(self) -> None:
        self._ledger_ready_state(last_trade_block_id=None)
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.uuid4",
            side_effect=AssertionError("uuid4 must not be called during finalize"),
        ):
            with mock.patch(
                "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event"
            ) as warning_mock:
                with mock.patch.object(self.strategy, "_persist_last_trade_pnl_summary") as persist_mock:
                    result = self.strategy._emit_final_trade_pnl_if_complete_or_fetch(
                        self.runtime_state,
                        None,
                        "test_finalize_missing",
                    )
        self.assertFalse(result)
        persist_mock.assert_not_called()
        warning_mock.assert_called()
        self.assertEqual(
            warning_mock.call_args[0][0],
            "fixed_cycle_finalize_missing_trade_block_id",
        )


class ConfirmedPnlOwnerRoutingTests(unittest.TestCase):
    SHORT_OWNER_PURPOSES = (
        ("CYCLE_1_LONG_REDUCE", "long-c1"),
        ("LONG_SL_EXIT", "long-sl"),
        ("SHORT_TP_EXIT", "short-tp"),
    )
    LONG_OWNER_PURPOSES = (
        ("CYCLE_1_SHORT_REDUCE", "short-c1"),
        ("SHORT_SL_EXIT", "short-sl"),
        ("LONG_TP_EXIT", "long-tp"),
    )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "confirmed_order_pnl_history.jsonl"
        configure_confirmed_order_pnl_history_file(self.history_path)
        self.runtime_state = RuntimeState(strategy_state={"trade_block_id": LAST_TBID})

    def tearDown(self) -> None:
        configure_confirmed_order_pnl_history_file(None)
        set_default_bot_name("long_bot_1")
        self.temp_dir.cleanup()

    def _read_rows(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        return [json.loads(line) for line in self.history_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write(
        self,
        *,
        bot_name: str,
        purpose: str,
        order_id: str,
        pnl: float = 0.1,
    ) -> None:
        set_default_bot_name(bot_name)
        strategy = FixedCycleHedgeStrategy()
        strategy._write_confirmed_order_pnl_history(
            {
                "exchange_order_id": order_id,
                "purpose": purpose,
                "closed_pnl": pnl,
                "trade_block_id": LAST_TBID,
            },
            runtime_state=self.runtime_state,
        )

    def _route_payload(self, log_mock: mock.Mock) -> dict:
        route_calls = [
            call
            for call in log_mock.call_args_list
            if call.args and call.args[0] == "confirmed_pnl_history_route"
        ]
        self.assertEqual(len(route_calls), 1)
        return route_calls[0].args[1]

    def test_short_owner_writes_each_counter_leg_and_final_exit_to_short_file(self) -> None:
        for purpose, order_id in self.SHORT_OWNER_PURPOSES:
            with self.subTest(owner="short_bot_1", purpose=purpose):
                self.history_path.unlink(missing_ok=True)
                self._write(bot_name="short_bot_1", purpose=purpose, order_id=order_id)
                rows = self._read_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["purpose"], purpose)
                self.assertEqual(rows[0]["bot_name"], "short_bot_1")
                self.assertEqual(str(rows[0]["trade_block_id"]), LAST_TBID)

    def test_long_owner_writes_each_counter_leg_and_final_exit_to_long_file(self) -> None:
        for purpose, order_id in self.LONG_OWNER_PURPOSES:
            with self.subTest(owner="long_bot_1", purpose=purpose):
                self.history_path.unlink(missing_ok=True)
                self._write(bot_name="long_bot_1", purpose=purpose, order_id=order_id)
                rows = self._read_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["purpose"], purpose)
                self.assertEqual(rows[0]["bot_name"], "long_bot_1")
                self.assertEqual(str(rows[0]["trade_block_id"]), LAST_TBID)

    def test_short_owner_each_purpose_logs_route_reason_owner_bot(self) -> None:
        for purpose, order_id in self.SHORT_OWNER_PURPOSES:
            with self.subTest(owner="short_bot_1", purpose=purpose):
                set_default_bot_name("short_bot_1")
                strategy = FixedCycleHedgeStrategy()
                with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_mock:
                    strategy._write_confirmed_order_pnl_history(
                        {
                            "exchange_order_id": order_id,
                            "purpose": purpose,
                            "closed_pnl": 0.05,
                            "trade_block_id": LAST_TBID,
                        },
                        runtime_state=self.runtime_state,
                    )
                payload = self._route_payload(log_mock)
                self.assertEqual(payload["route_reason"], "owner_bot")
                self.assertEqual(payload["target_bot"], "short_bot_1")
                self.assertEqual(payload["purpose"], purpose)
                self.assertEqual(payload["source_bot_name"], "short_bot_1")

    def test_long_owner_each_purpose_logs_route_reason_owner_bot(self) -> None:
        for purpose, order_id in self.LONG_OWNER_PURPOSES:
            with self.subTest(owner="long_bot_1", purpose=purpose):
                set_default_bot_name("long_bot_1")
                strategy = FixedCycleHedgeStrategy()
                with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_mock:
                    strategy._write_confirmed_order_pnl_history(
                        {
                            "exchange_order_id": order_id,
                            "purpose": purpose,
                            "closed_pnl": 0.05,
                            "trade_block_id": LAST_TBID,
                        },
                        runtime_state=self.runtime_state,
                    )
                payload = self._route_payload(log_mock)
                self.assertEqual(payload["route_reason"], "owner_bot")
                self.assertEqual(payload["target_bot"], "long_bot_1")
                self.assertEqual(payload["purpose"], purpose)
                self.assertEqual(payload["source_bot_name"], "long_bot_1")

    def test_owner_bot_does_not_call_purpose_side_resolve_target(self) -> None:
        cases = (
            ("short_bot_1", "CYCLE_1_LONG_REDUCE", "long-c1"),
            ("long_bot_1", "CYCLE_1_SHORT_REDUCE", "short-c1"),
        )
        for bot_name, purpose, order_id in cases:
            with self.subTest(owner=bot_name, purpose=purpose):
                set_default_bot_name(bot_name)
                strategy = FixedCycleHedgeStrategy()
                with mock.patch(
                    "fixed_cycle_hedge_bot.fixed_cycle_strategy._resolve_confirmed_pnl_history_target",
                ) as resolve_mock:
                    with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event"):
                        strategy._write_confirmed_order_pnl_history(
                            {
                                "exchange_order_id": order_id,
                                "purpose": purpose,
                                "closed_pnl": 0.05,
                                "trade_block_id": LAST_TBID,
                            },
                            runtime_state=self.runtime_state,
                        )
                resolve_mock.assert_not_called()

    def test_short_strategy_writes_counter_leg_and_final_exits_to_short_file(self) -> None:
        cases = [
            ("short-c1", "CYCLE_1_SHORT_REDUCE", -0.1),
            *[(order_id, purpose, 0.1) for purpose, order_id in self.SHORT_OWNER_PURPOSES],
        ]
        for order_id, purpose, pnl in cases:
            self._write(bot_name="short_bot_1", purpose=purpose, order_id=order_id, pnl=pnl)
        rows = self._read_rows()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["bot_name"], "short_bot_1")
            self.assertEqual(row["trade_block_id"], LAST_TBID)

    def test_long_strategy_writes_all_legs_and_final_exits_to_long_file(self) -> None:
        self._write(bot_name="long_bot_1", purpose="CYCLE_1_LONG_REDUCE", order_id="long-c1")
        for purpose, order_id in self.LONG_OWNER_PURPOSES:
            self._write(bot_name="long_bot_1", purpose=purpose, order_id=order_id, pnl=-0.04)
        rows = self._read_rows()
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["bot_name"], "long_bot_1")

    def test_fallback_routing_used_when_owner_bot_unknown(self) -> None:
        strategy = FixedCycleHedgeStrategy()
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.default_bot_name",
            "unknown_bot",
        ):
            with mock.patch(
                "fixed_cycle_hedge_bot.fixed_cycle_strategy._resolve_confirmed_pnl_history_target",
                return_value=("short_bot_1", self.history_path),
            ) as resolve_mock:
                with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_mock:
                    strategy._write_confirmed_order_pnl_history(
                        {
                            "exchange_order_id": "short-c1",
                            "purpose": "CYCLE_1_SHORT_REDUCE",
                            "closed_pnl": -0.1,
                            "trade_block_id": LAST_TBID,
                        },
                        runtime_state=self.runtime_state,
                    )
                    payload = self._route_payload(log_mock)
        resolve_mock.assert_called_once()
        self.assertEqual(payload["route_reason"], "fallback_purpose_side")
        row = self._read_rows()[0]
        self.assertEqual(row["bot_name"], "short_bot_1")


class ResetCurrentTradePnlStateTests(unittest.TestCase):
    def test_before_initial_entry_creates_new_trade_block_id(self) -> None:
        strategy = FixedCycleHedgeStrategy()
        runtime_state = RuntimeState(
            strategy_state={
                "trade_block_id": CURRENT_TBID,
                "last_trade_block_id": CURRENT_TBID,
            }
        )
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.uuid4",
            return_value=mock.Mock(__str__=lambda _self: "new-trade-block-id"),
        ):
            strategy._reset_current_trade_pnl_state(
                runtime_state,
                reason="before_initial_entry",
            )
        self.assertEqual(runtime_state.strategy_state["trade_block_id"], "new-trade-block-id")


if __name__ == "__main__":
    unittest.main()

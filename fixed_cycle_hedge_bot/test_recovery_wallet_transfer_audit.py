#!/usr/bin/env python3
from __future__ import annotations

import logging
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState

TBID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RELOAD_ID = f"{TBID}:recovery_reload:2"


def _snapshot() -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="JTOUSDT",
        current_price=0.77,
        long_qty=100.0,
        short_qty=50.0,
        long_avg=0.771,
        short_avg=0.770,
    )


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_recovery_wallet_transfer_audit")),
        runtime_name="test_runtime",
        symbol="JTOUSDT",
        category="linear",
        min_order_value=5.0,
    )


def _baseline() -> dict:
    return {
        "symbol": "JTOUSDT",
        "bot_name": "long_bot_1",
        "strategy_side": "long",
        "trade_block_id": TBID,
        "initial_wallet_balance_usdt": 8.77388512,
        "allocated_wallet_balance_usdt": 8.77388512,
        "total_transferred_recovery_usdt": 0.0,
        "pending_recovery_transfer_usdt": 0.0,
        "recovery_transfers": [],
    }


def _runtime_state(*, strategy_side: str = "long", bot_name: str = "long_bot_1") -> RuntimeState:
    return RuntimeState(
        strategy_state={
            "trade_block_id": TBID,
            "recovery_reload_id": RELOAD_ID,
            "recovery_reference_cycle_index": 2,
            "recovery_activation_reason": "time_distance_refill",
            "recovery_activation_timing": "after_first_leg_reduce_fill",
        },
        last_snapshot=_snapshot(),
    )


class RecoveryWalletTransferAuditHelperTests(unittest.TestCase):
    def test_audit_log_event_duplicate_key_safe(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state()
        context = _context()
        captured: list[dict] = []

        def _capture(event: str, **payload: object) -> None:
            captured.append({"event": event, **payload})

        context.audit.log_event = _capture  # type: ignore[method-assign]
        payload = strategy._log_recovery_wallet_transfer_audit(
            context,
            "planned",
            runtime_state,
            baseline=_baseline(),
            cycle_index=2,
            transfer_amount_usdt=8.77388512,
            transfer_required=True,
            extra={
                "symbol": "SHOULD_NOT_OVERRIDE",
                "transfer_amount_usdt": 8.77388512,
                "requested_amount_usdt": 8.77388512,
            },
        )
        self.assertEqual(payload["symbol"], "JTOUSDT")
        self.assertEqual(len(captured), 1)
        self.assertNotIn("TypeError", str(captured[0]))


class EnsureRecoveryWalletTransferAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.baseline_path = Path(self.temp_dir.name) / "recovery_wallet_baseline.json"
        self.saved_baselines: list[dict] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _capture_logs(self, strategy: FixedCycleHedgeStrategy, context: StrategyContext):
        strategy_logs: list[tuple[str, dict]] = []
        audit_logs: list[tuple[str, dict]] = []

        def _strategy_log(event: str, payload: dict) -> None:
            strategy_logs.append((event, dict(payload)))

        def _audit_log(event: str, **payload: object) -> None:
            audit_logs.append((event, dict(payload)))

        patcher = mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=_strategy_log,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        context.audit.log_event = _audit_log  # type: ignore[method-assign]
        return strategy_logs, audit_logs

    def _lock_side_effect(self):
        @contextmanager
        def _cm():
            yield

        return _cm()

    def _install_common_success_mocks(
        self,
        strategy: FixedCycleHedgeStrategy,
        *,
        bot_name: str,
    ) -> None:
        transfer_context = {
            "ok": True,
            "executor_path": Path("/tmp/wallet_transfer_executor.py"),
            "log_path": Path("/tmp/wallet_transfer_executor.jsonl"),
            "config_path": Path("/tmp/config.yaml"),
            "source_account_label": "master",
            "target_account_label": "Long_bot_1" if bot_name.startswith("long") else "Short_bot_1",
            "target_profile": "bot_1",
            "source_config_origin": "current_bot_group:group_config",
            "payload_base": {
                "symbol": "JTOUSDT",
                "bot_name": bot_name,
                "cycle_index": 2,
                "transfer_amount_usdt": 8.77388512,
            },
        }
        mock.patch.object(
            strategy,
            "_recovery_wallet_baseline_path",
            return_value=self.baseline_path,
        ).start()
        mock.patch.object(
            strategy,
            "_ensure_recovery_wallet_baseline",
            return_value=_baseline(),
        ).start()
        mock.patch.object(
            strategy,
            "_recovery_wallet_transfer_already_completed",
            return_value=False,
        ).start()
        mock.patch.object(
            strategy,
            "_resolve_recovery_wallet_transfer_context",
            return_value=transfer_context,
        ).start()
        mock.patch.object(
            strategy,
            "_recovery_wallet_transfer_lock",
            side_effect=self._lock_side_effect,
        ).start()
        mock.patch.object(
            strategy,
            "_build_recovery_wallet_transfer_signature",
            return_value="sig-test",
        ).start()
        mock.patch.object(
            strategy,
            "_build_recovery_wallet_transfer_id",
            return_value="transfer-id-test",
        ).start()
        mock.patch.object(
            strategy,
            "_reserve_recovery_wallet_and_watcher_baselines",
            return_value=True,
        ).start()
        mock.patch.object(
            strategy,
            "_set_recovery_wallet_transfer_tracking",
            side_effect=lambda baseline, state, **fields: baseline.update(fields),
        ).start()
        mock.patch.object(
            strategy,
            "_set_recovery_lifecycle_state",
            return_value=None,
        ).start()
        mock.patch.object(
            strategy,
            "_save_recovery_wallet_baseline",
            side_effect=self._save_baseline,
        ).start()

    def _save_baseline(self, path: Path, payload: dict) -> None:
        self.saved_baselines.append(dict(payload))
        path.write_text("{}", encoding="utf-8")

    def test_success_path_emits_planned_started_success_and_baseline_updated(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state(strategy_side="long", bot_name="long_bot_1")
        context = _context()
        strategy_logs, audit_logs = self._capture_logs(strategy, context)
        self._install_common_success_mocks(strategy, bot_name="long_bot_1")

        executor_event = {
            "bot_name": "long_bot_1",
            "direction": "refill",
            "transfer_id": "transfer-id-test",
            "requested_amount": 8.77388512,
            "final_amount": 8.77,
            "from_account_type": "FUND",
            "to_account_type": "UNIFIED",
            "status": "SUCCESS",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        completed = mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.subprocess.run",
            return_value=completed,
        ), mock.patch.object(
            strategy,
            "_find_latest_wallet_transfer_success_event",
            return_value=executor_event,
        ):
            ok = strategy._ensure_recovery_wallet_transfer(
                _snapshot(), runtime_state, context, cycle_index=2
            )
        self.assertTrue(ok)

        strategy_events = [event for event, _ in strategy_logs]
        self.assertIn("fixed_cycle_recovery_wallet_transfer_planned", strategy_events)
        self.assertIn("fixed_cycle_recovery_wallet_transfer_started", strategy_events)
        self.assertIn("fixed_cycle_recovery_wallet_transfer_success", strategy_events)
        self.assertIn("fixed_cycle_recovery_wallet_baseline_updated_after_transfer", strategy_events)
        audit_events = [event for event, _ in audit_logs]
        self.assertIn("fixed_cycle_recovery_wallet_transfer_planned", audit_events)
        self.assertIn("fixed_cycle_recovery_wallet_transfer_started", audit_events)
        self.assertIn("fixed_cycle_recovery_wallet_transfer_success", audit_events)
        self.assertIn(
            "fixed_cycle_recovery_wallet_baseline_updated_after_transfer",
            audit_events,
        )
        self.assertTrue(self.saved_baselines)
        transfer_record = (self.saved_baselines[-1].get("recovery_transfers") or [])[0]
        self.assertEqual(transfer_record.get("requested_amount_usdt"), 8.77388512)
        self.assertEqual(transfer_record.get("rounded_amount_usdt"), 8.77)
        self.assertEqual(transfer_record.get("from_account_type"), "FUND")
        self.assertEqual(transfer_record.get("to_account_type"), "UNIFIED")
        self.assertEqual(transfer_record.get("recovery_reload_id"), RELOAD_ID)

    def test_execute_success_logs_started_and_success(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state()
        context = _context()
        strategy_logs, audit_logs = self._capture_logs(strategy, context)
        transfer_context = {
            "ok": True,
            "executor_path": Path("/tmp/wallet_transfer_executor.py"),
            "log_path": Path("/tmp/wallet_transfer_executor.jsonl"),
            "config_path": Path("/tmp/config.yaml"),
            "source_account_label": "master",
            "target_account_label": "Long_bot_1",
            "payload_base": {
                "symbol": "JTOUSDT",
                "bot_name": "long_bot_1",
                "cycle_index": 2,
                "transfer_amount_usdt": 8.77388512,
            },
        }
        completed = mock.Mock(returncode=0, stderr="", stdout="")
        executor_event = {
            "bot_name": "long_bot_1",
            "direction": "refill",
            "transfer_id": "transfer-id-test",
            "requested_amount": 8.77388512,
            "final_amount": 8.77,
            "from_account_type": "FUND",
            "to_account_type": "UNIFIED",
            "status": "SUCCESS",
        }
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.subprocess.run",
            return_value=completed,
        ) as run_mock, mock.patch.object(
            strategy,
            "_find_latest_wallet_transfer_success_event",
            return_value=executor_event,
        ):
            success, transfer_id, result_code, event = strategy._execute_recovery_wallet_transfer(
                runtime_state,
                transfer_context,
                "transfer-id-test",
                context=context,
                baseline=_baseline(),
                cycle_index=2,
            )
        self.assertTrue(success)
        self.assertEqual(result_code, "completed")
        self.assertEqual(transfer_id, "transfer-id-test")
        self.assertEqual(event, executor_event)
        run_mock.assert_called_once()
        strategy_events = [name for name, _ in strategy_logs]
        self.assertIn("fixed_cycle_recovery_wallet_transfer_started", strategy_events)
        self.assertIn("fixed_cycle_recovery_wallet_transfer_success", strategy_events)
        audit_events = [name for name, _ in audit_logs]
        self.assertIn("fixed_cycle_recovery_wallet_transfer_started", audit_events)
        self.assertIn("fixed_cycle_recovery_wallet_transfer_success", audit_events)

    def test_already_completed_emits_skipped(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state()
        context = _context()
        strategy_logs, audit_logs = self._capture_logs(strategy, context)
        mock.patch.object(
            strategy,
            "_ensure_recovery_wallet_baseline",
            return_value=_baseline(),
        ).start()
        mock.patch.object(
            strategy,
            "_recovery_wallet_transfer_already_completed",
            return_value=True,
        ).start()
        mock.patch.object(strategy, "_set_recovery_lifecycle_state", return_value=None).start()

        ok = strategy._ensure_recovery_wallet_transfer(
            _snapshot(), runtime_state, context, cycle_index=2
        )
        self.assertTrue(ok)
        skipped = [
            payload
            for event, payload in strategy_logs
            if event == "fixed_cycle_recovery_wallet_transfer_skipped"
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].get("skip_reason"), "already_completed")
        audit_skipped = [
            payload
            for event, payload in audit_logs
            if event == "fixed_cycle_recovery_wallet_transfer_skipped"
        ]
        self.assertEqual(len(audit_skipped), 1)

    def test_subprocess_failure_emits_failed_and_not_success(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state()
        context = _context()
        strategy_logs, audit_logs = self._capture_logs(strategy, context)
        transfer_context = {
            "ok": True,
            "executor_path": Path("/tmp/wallet_transfer_executor.py"),
            "log_path": Path("/tmp/wallet_transfer_executor.jsonl"),
            "config_path": Path("/tmp/config.yaml"),
            "source_account_label": "master",
            "target_account_label": "Long_bot_1",
            "payload_base": {
                "symbol": "JTOUSDT",
                "bot_name": "long_bot_1",
                "cycle_index": 2,
                "transfer_amount_usdt": 8.77388512,
            },
        }
        failed = mock.Mock(returncode=1, stderr="Transfer failed", stdout="")
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy.subprocess.run",
            return_value=failed,
        ), mock.patch.object(
            strategy,
            "_find_latest_wallet_transfer_success_event",
            return_value=None,
        ):
            success, _, result_code, event = strategy._execute_recovery_wallet_transfer(
                runtime_state,
                transfer_context,
                "transfer-id-test",
                context=context,
                baseline=_baseline(),
                cycle_index=2,
            )
        self.assertFalse(success)
        self.assertEqual(result_code, "check_required")
        self.assertIsNone(event)
        strategy_events = [name for name, _ in strategy_logs]
        self.assertIn("fixed_cycle_recovery_wallet_transfer_failed", strategy_events)
        self.assertNotIn("fixed_cycle_recovery_wallet_transfer_success", strategy_events)
        audit_events = [name for name, _ in audit_logs]
        self.assertIn("fixed_cycle_recovery_wallet_transfer_failed", audit_events)

    def test_short_strategy_envelope_contains_strategy_side(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="short_bot_1", strategy_side="short", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state(strategy_side="short", bot_name="short_bot_1")
        payload = strategy._build_recovery_wallet_transfer_envelope(
            runtime_state,
            baseline=_baseline(),
            cycle_index=2,
            transfer_amount_usdt=8.77,
            transfer_required=True,
        )
        self.assertEqual(payload.get("strategy_side"), "short")
        self.assertEqual(payload.get("bot_name"), "short_bot_1")

    def test_long_strategy_envelope_contains_strategy_side(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="JTOUSDT")
        )
        runtime_state = _runtime_state(strategy_side="long", bot_name="long_bot_1")
        payload = strategy._build_recovery_wallet_transfer_envelope(
            runtime_state,
            baseline=_baseline(),
            cycle_index=2,
            transfer_amount_usdt=8.77,
            transfer_required=True,
        )
        self.assertEqual(payload.get("strategy_side"), "long")


if __name__ == "__main__":
    unittest.main()

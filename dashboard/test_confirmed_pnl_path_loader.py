#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fixed_cycle_hedge_bot.confirmed_pnl_path_logic import (
    confirmed_row_dedupe_key,
    load_valid_confirmed_pnl_rows_from_paths,
    path_bot_name_from_logs_path,
    purpose_allowed_for_path_bot,
    purpose_implies_bot_side,
    row_bot_name_matches_path,
    should_skip_foreign_confirmed_pnl_write,
    validate_confirmed_pnl_row_for_path,
)

LONG_PATH = Path(
    "live_bots/100_50_hedge_bot/long_bot_1/logs/confirmed_order_pnl_history.jsonl"
)
SHORT_PATH = Path(
    "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
)
TBID = "2b3e3f30-3224-4b6b-8be1-4118197d1d55"


class ConfirmedPnlReadOwnershipTests(unittest.TestCase):
    """Dashboard read membership: bot file + row bot_name, never purpose side."""

    def _row(self, *, bot_name: str, purpose: str) -> dict:
        return {
            "trade_block_id": TBID,
            "bot_name": bot_name,
            "purpose": purpose,
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "closed_pnl": 0.04107762,
        }

    def test_long_bot_file_accepts_own_short_leg_purpose(self) -> None:
        ok, event, _ = validate_confirmed_pnl_row_for_path(
            self._row(bot_name="long_bot_1", purpose="CYCLE_1_SHORT_REDUCE"),
            LONG_PATH,
        )
        self.assertTrue(ok)
        self.assertIsNone(event)

    def test_long_bot_file_accepts_own_long_leg_purpose(self) -> None:
        ok, event, _ = validate_confirmed_pnl_row_for_path(
            self._row(bot_name="long_bot_1", purpose="CYCLE_1_LONG_ADD"),
            LONG_PATH,
        )
        self.assertTrue(ok)
        self.assertIsNone(event)

    def test_short_bot_file_accepts_own_long_leg_purpose(self) -> None:
        ok, event, _ = validate_confirmed_pnl_row_for_path(
            self._row(bot_name="short_bot_1", purpose="CYCLE_1_LONG_REDUCE"),
            SHORT_PATH,
        )
        self.assertTrue(ok)
        self.assertIsNone(event)

    def test_short_bot_file_accepts_own_short_leg_purpose(self) -> None:
        ok, event, _ = validate_confirmed_pnl_row_for_path(
            self._row(bot_name="short_bot_1", purpose="CYCLE_1_SHORT_REDUCE"),
            SHORT_PATH,
        )
        self.assertTrue(ok)
        self.assertIsNone(event)

    def test_long_bot_file_rejects_foreign_short_bot_row(self) -> None:
        ok, event, payload = validate_confirmed_pnl_row_for_path(
            self._row(bot_name="short_bot_1", purpose="CYCLE_1_SHORT_REDUCE"),
            LONG_PATH,
        )
        self.assertFalse(ok)
        self.assertEqual(event, "confirmed_pnl_history_path_bot_mismatch_skipped")
        self.assertEqual(payload.get("path_bot_name"), "long_bot_1")
        self.assertEqual(payload.get("row_bot_name"), "short_bot_1")

    def test_short_bot_file_rejects_foreign_long_bot_row(self) -> None:
        ok, event, payload = validate_confirmed_pnl_row_for_path(
            self._row(bot_name="long_bot_1", purpose="CYCLE_1_LONG_REDUCE"),
            SHORT_PATH,
        )
        self.assertFalse(ok)
        self.assertEqual(event, "confirmed_pnl_history_path_bot_mismatch_skipped")
        self.assertEqual(payload.get("path_bot_name"), "short_bot_1")
        self.assertEqual(payload.get("row_bot_name"), "long_bot_1")


class ConfirmedPnlPathLogicTests(unittest.TestCase):
    def test_path_bot_name_from_logs_path_short_active_root(self) -> None:
        self.assertEqual(path_bot_name_from_logs_path(SHORT_PATH), "short_bot_1")

    def test_path_bot_name_from_logs_path_long_active_root(self) -> None:
        self.assertEqual(path_bot_name_from_logs_path(LONG_PATH), "long_bot_1")

    def test_purpose_implies_bot_side_is_classification_only(self) -> None:
        self.assertEqual(purpose_implies_bot_side("CYCLE_1_SHORT_REDUCE"), "short")
        self.assertEqual(purpose_implies_bot_side("CYCLE_1_LONG_ADD"), "long")

    def test_purpose_allowed_for_path_bot_write_guard_only(self) -> None:
        """Write guard may reject counter-leg purposes; read path does not use this."""
        self.assertFalse(purpose_allowed_for_path_bot("long_bot_1", "CYCLE_1_SHORT_REDUCE"))
        self.assertFalse(purpose_allowed_for_path_bot("short_bot_1", "CYCLE_1_LONG_ADD"))
        self.assertTrue(purpose_allowed_for_path_bot("short_bot_1", "CYCLE_1_SHORT_REDUCE"))
        self.assertTrue(purpose_allowed_for_path_bot("long_bot_1", "CYCLE_1_LONG_ADD"))

        ok, event, _ = validate_confirmed_pnl_row_for_path(
            {
                "bot_name": "long_bot_1",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "trade_block_id": TBID,
            },
            LONG_PATH,
        )
        self.assertTrue(ok)
        self.assertIsNone(event)

    def test_row_bot_name_matches_path(self) -> None:
        row = {"bot_name": "long_bot_1", "purpose": "CYCLE_1_SHORT_REDUCE"}
        self.assertFalse(row_bot_name_matches_path("short_bot_1", row))
        self.assertTrue(row_bot_name_matches_path("long_bot_1", row))

    def test_dedupe_key_keeps_long_and_short_rows_separate(self) -> None:
        base = {
            "trade_block_id": TBID,
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "closed_pnl": 0.04107762,
            "timestamp": "2026-06-25T00:00:00+00:00",
            "dedupe_key": "7813a42a-f12d-4db1-92f2-b17ffa50b103:CYCLE_1_SHORT_REDUCE",
        }
        long_row = dict(base, bot_name="long_bot_1")
        short_row = dict(base, bot_name="short_bot_1")
        self.assertNotEqual(confirmed_row_dedupe_key(long_row), confirmed_row_dedupe_key(short_row))


class ConfirmedPnlLoaderIntegrationTests(unittest.TestCase):
    def test_loader_reads_only_configured_short_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            short_path = (
                root
                / "live_bots"
                / "short_hedge_bot"
                / "short_bot_1"
                / "logs"
                / "confirmed_order_pnl_history.jsonl"
            )
            legacy_copy = (
                root
                / "live_bots"
                / "100_50_hedge_bot"
                / "short_bot_1"
                / "logs"
                / "confirmed_order_pnl_history.jsonl"
            )
            long_path = (
                root
                / "live_bots"
                / "100_50_hedge_bot"
                / "long_bot_1"
                / "logs"
                / "confirmed_order_pnl_history.jsonl"
            )
            row = {
                "trade_block_id": TBID,
                "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "closed_pnl": 0.04107762,
                "bot_name": "short_bot_1",
                "source": "bot_confirmed_pnl",
            }
            long_row = dict(row, bot_name="long_bot_1")
            for path, payload in (
                (short_path, row),
                (legacy_copy, row),
                (long_path, long_row),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            loaded = load_valid_confirmed_pnl_rows_from_paths([short_path])
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["bot_name"], "short_bot_1")
            self.assertEqual(loaded[0]["closed_pnl"], 0.04107762)

            long_loaded = load_valid_confirmed_pnl_rows_from_paths([long_path])
            self.assertEqual(len(long_loaded), 1)
            self.assertEqual(long_loaded[0]["bot_name"], "long_bot_1")
            self.assertEqual(long_loaded[0]["purpose"], "CYCLE_1_SHORT_REDUCE")

            legacy_loaded = load_valid_confirmed_pnl_rows_from_paths([legacy_copy])
            self.assertEqual(len(legacy_loaded), 1)

            foreign_short_row = dict(row, bot_name="short_bot_1")
            ok, event, _payload = validate_confirmed_pnl_row_for_path(foreign_short_row, long_path)
            self.assertFalse(ok)
            self.assertEqual(event, "confirmed_pnl_history_path_bot_mismatch_skipped")

    def test_active_short_path_not_mixed_with_legacy_copy(self) -> None:
        short_active = Path(
            "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        legacy_short = Path(
            "live_bots/100_50_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        self.assertNotEqual(str(short_active), str(legacy_short))


class ConfirmedPnlWriterSkipTests(unittest.TestCase):
    def test_long_runtime_short_reduce_without_source_path_is_not_skipped(self) -> None:
        payload = {
            "bot_name": "long_bot_1",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "trade_block_id": TBID,
        }
        target_path = SHORT_PATH
        skip, reason, _context = should_skip_foreign_confirmed_pnl_write(
            payload=payload,
            default_bot_name="long_bot_1",
            target_bot_name="short_bot_1",
            target_path=target_path,
            purpose="CYCLE_1_SHORT_REDUCE",
        )
        self.assertFalse(skip)
        self.assertIsNone(reason)

    def test_foreign_row_with_source_path_is_skipped(self) -> None:
        payload = {
            "bot_name": "long_bot_1",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "trade_block_id": TBID,
            "source_path": str(LONG_PATH),
        }
        skip, reason, context = should_skip_foreign_confirmed_pnl_write(
            payload=payload,
            default_bot_name="long_bot_1",
            target_bot_name="short_bot_1",
            target_path=SHORT_PATH,
            purpose="CYCLE_1_SHORT_REDUCE",
        )
        self.assertTrue(skip)
        self.assertEqual(reason, "source_path_foreign_bot")
        self.assertEqual(context.get("source_path"), payload["source_path"])

    def test_owner_long_bot_accepts_counter_leg_short_reduce_write(self) -> None:
        payload = {
            "bot_name": "long_bot_1",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
        }
        skip, reason, _context = should_skip_foreign_confirmed_pnl_write(
            payload=payload,
            default_bot_name="long_bot_1",
            target_bot_name="long_bot_1",
            target_path=LONG_PATH,
            purpose="CYCLE_1_SHORT_REDUCE",
        )
        self.assertFalse(skip)
        self.assertIsNone(reason)

    def test_owner_short_bot_accepts_counter_leg_long_reduce_write(self) -> None:
        payload = {
            "bot_name": "short_bot_1",
            "purpose": "CYCLE_1_LONG_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
        }
        skip, reason, _context = should_skip_foreign_confirmed_pnl_write(
            payload=payload,
            default_bot_name="short_bot_1",
            target_bot_name="short_bot_1",
            target_path=SHORT_PATH,
            purpose="CYCLE_1_LONG_REDUCE",
        )
        self.assertFalse(skip)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fixed_cycle_hedge_bot.confirmed_pnl_path_logic import (
    build_confirmed_pnl_index_from_rows,
    confirmed_row_dedupe_key,
    filter_confirmed_rows_for_trade_block,
    finalize_confirmed_rows,
    load_valid_confirmed_pnl_rows_from_paths,
    path_bot_name_from_logs_path,
    purpose_allowed_for_path_bot,
    purpose_implies_bot_side,
    row_bot_name_matches_path,
    should_skip_foreign_confirmed_pnl_write,
    validate_confirmed_pnl_row_for_path,
)


class ConfirmedPnlPathLogicTests(unittest.TestCase):
    def test_path_bot_name_from_logs_path_short_active_root(self) -> None:
        path = Path(
            "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        self.assertEqual(path_bot_name_from_logs_path(path), "short_bot_1")

    def test_path_bot_name_from_logs_path_long_active_root(self) -> None:
        path = Path(
            "live_bots/100_50_hedge_bot/long_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        self.assertEqual(path_bot_name_from_logs_path(path), "long_bot_1")

    def test_purpose_implies_bot_side_cycle_purposes(self) -> None:
        self.assertEqual(purpose_implies_bot_side("CYCLE_1_SHORT_REDUCE"), "short")
        self.assertEqual(purpose_implies_bot_side("CYCLE_1_LONG_ADD"), "long")

    def test_purpose_allowed_for_path_bot(self) -> None:
        self.assertFalse(
            purpose_allowed_for_path_bot("long_bot_1", "CYCLE_1_SHORT_REDUCE")
        )
        self.assertFalse(purpose_allowed_for_path_bot("short_bot_1", "CYCLE_1_LONG_ADD"))
        self.assertTrue(purpose_allowed_for_path_bot("short_bot_1", "CYCLE_1_SHORT_REDUCE"))
        self.assertTrue(purpose_allowed_for_path_bot("long_bot_1", "CYCLE_1_LONG_ADD"))

    def test_row_bot_name_matches_path(self) -> None:
        row = {"bot_name": "long_bot_1", "purpose": "CYCLE_1_SHORT_REDUCE"}
        self.assertFalse(row_bot_name_matches_path("short_bot_1", row))
        self.assertTrue(row_bot_name_matches_path("long_bot_1", row))

    def test_validate_accepts_cross_purpose_when_bot_name_matches(self) -> None:
        long_path = Path(
            "live_bots/100_50_hedge_bot/long_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        row = {
            "bot_name": "long_bot_1",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
        }
        ok, event, _payload = validate_confirmed_pnl_row_for_path(row, long_path)
        self.assertTrue(ok)
        self.assertIsNone(event)

    def test_short_bot_file_accepts_hedge_cross_purpose_rows_when_bot_name_matches(self) -> None:
        path = Path(
            "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        base = {
            "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
            "bot_name": "short_bot_1",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "closed_pnl": 0.04107762,
        }
        for purpose in (
            "CYCLE_1_SHORT_REDUCE",
            "SHORT_SL_EXIT",
            "CYCLE_1_LONG_REDUCE",
            "CYCLE_2_LONG_REDUCE",
            "REFILL_LONG",
        ):
            with self.subTest(purpose=purpose):
                row = dict(base, purpose=purpose)
                ok, event, _payload = validate_confirmed_pnl_row_for_path(row, path)
                self.assertTrue(ok, purpose)
                self.assertIsNone(event)

    def test_foreign_bot_name_row_is_skipped(self) -> None:
        path = Path(
            "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        row = {
            "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
            "bot_name": "long_bot_1",
            "purpose": "CYCLE_1_LONG_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "closed_pnl": 0.04107762,
        }
        ok, event, payload = validate_confirmed_pnl_row_for_path(row, path)
        self.assertFalse(ok)
        self.assertEqual(event, "confirmed_pnl_history_path_bot_mismatch_skipped")
        self.assertEqual(payload.get("path_bot_name"), "short_bot_1")
        self.assertEqual(payload.get("row_bot_name"), "long_bot_1")

    def test_dedupe_key_keeps_long_and_short_rows_separate(self) -> None:
        base = {
            "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            "closed_pnl": 0.04107762,
            "timestamp": "2026-06-25T00:00:00+00:00",
            "dedupe_key": "7813a42a-f12d-4db1-92f2-b17ffa50b103:CYCLE_1_SHORT_REDUCE",
        }
        long_row = dict(base, bot_name="long_bot_1")
        short_row = dict(base, bot_name="short_bot_1")
        self.assertNotEqual(confirmed_row_dedupe_key(long_row), confirmed_row_dedupe_key(short_row))


class ConfirmedPnlIndexTests(unittest.TestCase):
    TBID = "2b3e3f30-3224-4b6b-8be1-4118197d1d55"

    def test_build_confirmed_pnl_index_groups_and_dedupes_by_trade_block(self) -> None:
        rows = [
            {
                "trade_block_id": self.TBID,
                "bot_name": "short_bot_1",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "closed_pnl": 0.04107762,
                "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            },
            {
                "trade_block_id": self.TBID,
                "bot_name": "short_bot_1",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "closed_pnl": 0.04107762,
                "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
            },
            {
                "trade_block_id": "other-tbid",
                "bot_name": "short_bot_1",
                "purpose": "CYCLE_1_LONG_REDUCE",
                "closed_pnl": 1.0,
            },
        ]
        index = build_confirmed_pnl_index_from_rows(rows)
        self.assertEqual(len(index[self.TBID]), 1)
        self.assertEqual(index[self.TBID][0]["closed_pnl"], 0.04107762)
        self.assertEqual(len(index["other-tbid"]), 1)

    def test_index_lookup_matches_filter_for_tbid(self) -> None:
        rows = [
            {
                "trade_block_id": self.TBID,
                "bot_name": "short_bot_1",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "closed_pnl": 0.04107762,
                "symbol": "NEARUSDT",
            },
            {
                "trade_block_id": "other-tbid",
                "bot_name": "short_bot_1",
                "purpose": "REFILL_LONG",
                "closed_pnl": 1.0,
            },
        ]
        index = build_confirmed_pnl_index_from_rows(rows)
        filtered = filter_confirmed_rows_for_trade_block(rows, self.TBID)
        self.assertEqual(index.get(self.TBID), filtered)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["symbol"], "NEARUSDT")
        self.assertEqual(filtered[0]["closed_pnl"], 0.04107762)

    def test_page_build_loads_confirmed_history_exactly_once(self) -> None:
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
            closed_path = (
                root
                / "live_bots"
                / "short_hedge_bot"
                / "short_bot_1"
                / "logs"
                / "dashboard_closed_pnl_history.jsonl"
            )
            confirmed_rows_payload = [
                {
                    "trade_block_id": self.TBID,
                    "bot_name": "short_bot_1",
                    "purpose": "CYCLE_1_SHORT_REDUCE",
                    "closed_pnl": 0.04107762,
                    "symbol": "NEARUSDT",
                    "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
                },
                {
                    "trade_block_id": "tbid-1",
                    "bot_name": "short_bot_1",
                    "purpose": "CYCLE_1_LONG_REDUCE",
                    "closed_pnl": 1.0,
                    "exchange_order_id": "order-1",
                },
            ]
            closed_rows_payload = [
                {
                    "trade_block_id": "tbid-closed",
                    "bot_name": "short_bot_1",
                    "symbol": "BTCUSDT",
                    "total_trade_pnl": 2.0,
                    "timestamp": "2026-06-25T00:00:00+00:00",
                }
            ]
            short_path.parent.mkdir(parents=True, exist_ok=True)
            short_path.write_text(
                "\n".join(json.dumps(row) for row in confirmed_rows_payload) + "\n",
                encoding="utf-8",
            )
            closed_path.write_text(
                "\n".join(json.dumps(row) for row in closed_rows_payload) + "\n",
                encoding="utf-8",
            )

            confirmed_load_calls = 0
            closed_load_calls = 0

            def _load_confirmed_once(paths):
                nonlocal confirmed_load_calls
                confirmed_load_calls += 1
                return load_valid_confirmed_pnl_rows_from_paths(paths)

            def _load_closed_once(paths):
                nonlocal closed_load_calls
                closed_load_calls += 1
                entries = []
                for path in paths:
                    with Path(path).open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if line:
                                entries.append(json.loads(line))
                return entries

            # Mirrors _build_profit_trade_filtered_rows: index first, then trade load reuses rows.
            all_confirmed_rows = _load_confirmed_once([short_path])
            confirmed_index = build_confirmed_pnl_index_from_rows(all_confirmed_rows)
            confirmed_rows_flat = [row for rows in confirmed_index.values() for row in rows]

            closed_entries = _load_closed_once([closed_path])
            history_entries = closed_entries + confirmed_rows_flat
            self.assertEqual(len(history_entries), 3)

            trade_tbids = {entry["trade_block_id"] for entry in history_entries}
            self.assertIn(self.TBID, trade_tbids)
            self.assertIn("tbid-closed", trade_tbids)

            for _ in range(50):
                tbid = self.TBID
                enriched = confirmed_index.get(tbid, [])
                if enriched:
                    self.assertEqual(enriched[0]["closed_pnl"], 0.04107762)
                    self.assertEqual(len(enriched), 1)

            self.assertEqual(confirmed_load_calls, 1)
            self.assertEqual(closed_load_calls, 1)

    def test_page_enrichment_pattern_reads_confirmed_history_once(self) -> None:
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
            rows = []
            for idx in range(5):
                rows.append(
                    {
                        "trade_block_id": f"tbid-{idx}",
                        "bot_name": "short_bot_1",
                        "purpose": "CYCLE_1_SHORT_REDUCE" if idx % 2 == 0 else "CYCLE_1_LONG_REDUCE",
                        "closed_pnl": float(idx),
                        "exchange_order_id": f"order-{idx}",
                    }
                )
            rows.append(
                {
                    "trade_block_id": self.TBID,
                    "bot_name": "short_bot_1",
                    "purpose": "CYCLE_1_SHORT_REDUCE",
                    "closed_pnl": 0.04107762,
                    "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
                }
            )
            short_path.parent.mkdir(parents=True, exist_ok=True)
            short_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            load_calls = 0

            def _load_once(paths):
                nonlocal load_calls
                load_calls += 1
                return load_valid_confirmed_pnl_rows_from_paths(paths)

            all_rows = _load_once([short_path])
            confirmed_index = build_confirmed_pnl_index_from_rows(all_rows)

            for trade in rows:
                tbid = trade["trade_block_id"]
                confirmed = confirmed_index.get(tbid, [])
                if tbid == self.TBID:
                    self.assertEqual(len(confirmed), 1)
                    self.assertEqual(confirmed[0]["closed_pnl"], 0.04107762)
                    self.assertEqual(confirmed[0]["purpose"], "CYCLE_1_SHORT_REDUCE")

            self.assertEqual(load_calls, 1)

    def test_finalize_confirmed_rows_keeps_cross_purpose_rows_for_matching_bot(self) -> None:
        rows = [
            {
                "bot_name": "short_bot_1",
                "purpose": "SHORT_SL_EXIT",
                "trade_block_id": self.TBID,
            },
            {
                "bot_name": "short_bot_1",
                "purpose": "CYCLE_1_LONG_REDUCE",
                "trade_block_id": self.TBID,
            },
            {
                "bot_name": "short_bot_1",
                "purpose": "REFILL_LONG",
                "trade_block_id": self.TBID,
            },
        ]
        finalized = finalize_confirmed_rows(rows)
        self.assertEqual(len(finalized), 3)


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
                "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
                "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "closed_pnl": 0.04107762,
                "bot_name": "short_bot_1",
                "source": "bot_confirmed_pnl",
            }
            wrong_long_row = dict(row, bot_name="long_bot_1")
            for path, payload in (
                (short_path, row),
                (legacy_copy, row),
                (long_path, wrong_long_row),
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

    def test_trade_block_filter_returns_nearusdt_short_reduce_row(self) -> None:
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
            tbid = "2b3e3f30-3224-4b6b-8be1-4118197d1d55"
            target_row = {
                "trade_block_id": tbid,
                "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "closed_pnl": 0.04107762,
                "bot_name": "short_bot_1",
                "symbol": "NEARUSDT",
            }
            other_row = {
                "trade_block_id": "other-tbid",
                "purpose": "CYCLE_1_LONG_REDUCE",
                "closed_pnl": 1.0,
                "bot_name": "short_bot_1",
            }
            short_path.parent.mkdir(parents=True, exist_ok=True)
            short_path.write_text(
                "\n".join(json.dumps(row) for row in (target_row, other_row)) + "\n",
                encoding="utf-8",
            )
            loaded = load_valid_confirmed_pnl_rows_from_paths([short_path])
            filtered = [
                row
                for row in loaded
                if str(row.get("trade_block_id") or "").strip() == tbid
            ]
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]["purpose"], "CYCLE_1_SHORT_REDUCE")
            self.assertEqual(filtered[0]["closed_pnl"], 0.04107762)
            self.assertEqual(filtered[0]["symbol"], "NEARUSDT")

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
            "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
        }
        target_path = Path(
            "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
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
            "trade_block_id": "2b3e3f30-3224-4b6b-8be1-4118197d1d55",
            "source_path": (
                "live_bots/100_50_hedge_bot/long_bot_1/logs/confirmed_order_pnl_history.jsonl"
            ),
        }
        target_path = Path(
            "live_bots/short_hedge_bot/short_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        skip, reason, context = should_skip_foreign_confirmed_pnl_write(
            payload=payload,
            default_bot_name="long_bot_1",
            target_bot_name="short_bot_1",
            target_path=target_path,
            purpose="CYCLE_1_SHORT_REDUCE",
        )
        self.assertTrue(skip)
        self.assertEqual(reason, "source_path_foreign_bot")
        self.assertEqual(context.get("source_path"), payload["source_path"])

    def test_purpose_path_mismatch_blocks_write_to_long_file(self) -> None:
        payload = {
            "bot_name": "long_bot_1",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "exchange_order_id": "7813a42a-f12d-4db1-92f2-b17ffa50b103",
        }
        target_path = Path(
            "live_bots/100_50_hedge_bot/long_bot_1/logs/confirmed_order_pnl_history.jsonl"
        )
        skip, reason, _context = should_skip_foreign_confirmed_pnl_write(
            payload=payload,
            default_bot_name="long_bot_1",
            target_bot_name="long_bot_1",
            target_path=target_path,
            purpose="CYCLE_1_SHORT_REDUCE",
        )
        self.assertTrue(skip)
        self.assertEqual(reason, "purpose_path_mismatch")


if __name__ == "__main__":
    unittest.main()

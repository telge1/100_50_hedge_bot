#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pair_symbol_resolve import archive_stale_pair_state, resolve_pair_symbol


class PairSymbolResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pair_state = self.root / "pair_symbol_bot_1.json"
        self.long_pid = self.root / "long.pid"
        self.short_pid = self.root / "short.pid"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_pair_state(self, symbol: str, *, long_running: bool, short_running: bool) -> None:
        self.pair_state.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "long_running": long_running,
                    "short_running": short_running,
                }
            ),
            encoding="utf-8",
        )

    def test_stale_jtousdt_both_stopped_yields_empty_symbol(self) -> None:
        self._write_pair_state("JTOUSDT", long_running=False, short_running=False)
        result = resolve_pair_symbol(self.pair_state, self.long_pid, self.short_pid)
        self.assertEqual(result["pair_symbol"], "")
        self.assertTrue(result["stale_cleared"])
        self.assertEqual(result["old_symbol"], "JTOUSDT")

    def test_jtousdt_with_long_running_flag_keeps_pair_symbol(self) -> None:
        self._write_pair_state("JTOUSDT", long_running=True, short_running=False)
        result = resolve_pair_symbol(self.pair_state, self.long_pid, self.short_pid)
        self.assertEqual(result["pair_symbol"], "JTOUSDT")
        self.assertFalse(result["stale_cleared"])

    def test_missing_pair_state_yields_empty_symbol(self) -> None:
        result = resolve_pair_symbol(self.pair_state, self.long_pid, self.short_pid)
        self.assertEqual(result["pair_symbol"], "")
        self.assertFalse(result["stale_cleared"])

    def test_jtousdt_with_short_pid_alive_keeps_pair_symbol(self) -> None:
        self._write_pair_state("JTOUSDT", long_running=False, short_running=False)
        self.short_pid.write_text(f"{__import__('os').getpid()}\n", encoding="utf-8")
        result = resolve_pair_symbol(self.pair_state, self.long_pid, self.short_pid)
        self.assertEqual(result["pair_symbol"], "JTOUSDT")
        self.assertFalse(result["stale_cleared"])

    def test_archive_moves_stale_file(self) -> None:
        self._write_pair_state("JTOUSDT", long_running=False, short_running=False)
        archive = archive_stale_pair_state(self.pair_state)
        self.assertIsNotNone(archive)
        self.assertFalse(self.pair_state.exists())
        self.assertTrue(archive.exists())


if __name__ == "__main__":
    unittest.main()

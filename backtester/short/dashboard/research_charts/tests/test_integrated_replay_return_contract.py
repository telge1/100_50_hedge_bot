"""Regression: run_segment_price / integrated run_segment return triple."""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[5]
_CTX = (
    _REPO
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_7d_context"
)
for p in (_REPO / "backtester/short/dashboard", _REPO, _CTX):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import phase6xt_90d_integrated_price_pool_memory_v1 as integrated  # noqa: E402
import phase6xt_market_memory_price_stream_recovery_v1 as rec  # noqa: E402
from market_memory_state import MarketMemoryState  # noqa: E402


class TestReturnContract(unittest.TestCase):
    def test_run_segment_price_annotation_is_triple(self):
        sig = inspect.signature(rec.run_segment_price)
        ret = sig.return_annotation
        self.assertIn("tuple", str(ret))

    def test_run_segment_price_returns_three_values(self):
        fake_meta = {"birth_ledger": {1: {}, 10: {}}, "decision_closes_processed": 1}
        with patch.object(rec, "walk_ssot", return_value=(None, fake_meta, [])):
            with patch.object(rec, "unique_dcs", return_value=[datetime(2026, 7, 1, tzinfo=timezone.utc)]):
                out = rec.run_segment_price(
                    datetime(2026, 7, 1, tzinfo=timezone.utc),
                    datetime(2026, 7, 2, tzinfo=timezone.utc),
                    MagicMock(),
                    "db",
                    {},
                    MarketMemoryState("2026-07-01T00:00:00Z"),
                    {},
                    None,
                    with_price=False,
                )
        self.assertEqual(len(out), 3)
        mem, meta, proc = out
        self.assertIsInstance(meta, dict)
        self.assertIn("birth_ledger", meta)
        self.assertIsNone(proc)

    def test_integrated_runner_reference_unpack_ast(self):
        path = _CTX / "phase6xt_90d_integrated_price_pool_memory_v1.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Tuple):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "run_segment":
                continue
            n = len(node.targets[0].elts)
            if n != 3:
                bad.append((node.lineno, n))
        self.assertEqual(bad, [], f"run_segment unpack not triple: {bad}")

    def test_parity_inputs_from_reference_meta(self):
        fake_meta = {"birth_ledger": {1: {"oid": {}}, 10: {}}}
        with patch.object(integrated, "run_segment", return_value=(None, fake_meta, None)):
            _, ref_meta, _ = integrated.run_segment()
        from market_memory_bootstrap_engine import parity_diff

        inc = {1: {}, 10: {}}
        pr = parity_diff(fake_meta["birth_ledger"], inc)
        self.assertIn("match", pr)


if __name__ == "__main__":
    unittest.main()

"""Phase 4R tests (coverage + snapshot helpers)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "tools/phase4p_fixed_notional_pnl_v1"))

from coverage import assess_window  # noqa: E402
from notional_pnl import POSITION_NOTIONAL_USDT, compute_trade_pnl  # noqa: E402
from snapshot_index import intervals_from_snapshot, load_snapshot_index  # noqa: E402


JSONL = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.jsonl"


def test_28d_window_blocked():
    row = assess_window("main_28d", "2026-09-01T00:00:00Z", "2026-09-29T00:00:00Z", JSONL)
    assert row["coverage_status"] == "COVERAGE_BLOCKED"


def test_snapshot_intervals_depth2():
    idx = load_snapshot_index(JSONL)
    assert idx
    snap = next(iter(idx.values()))
    ivs = intervals_from_snapshot(snap)
    assert any(i.get("depth") == 2 for i in ivs)


def test_pnl_unchanged_from_4p():
    pnl = compute_trade_pnl("SHORT", 0.1, 0.09, 0.102, 0.09, ambiguous=False, open_at_end=False)
    assert pnl.net_pnl_usdt is not None


def test_notional_1000():
    assert POSITION_NOTIONAL_USDT == 1000.0


def test_pilot_coverage_may_run():
    row = assess_window("pilot_24h", "2026-09-11T12:00:00Z", "2026-09-12T12:00:00Z", JSONL)
    assert row["timeline_covers_window"] is True

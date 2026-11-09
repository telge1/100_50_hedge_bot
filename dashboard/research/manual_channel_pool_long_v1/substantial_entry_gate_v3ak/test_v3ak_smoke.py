"""Smoke tests — substantial entry gate + reference trade."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

V3AK = Path(__file__).resolve().parent
PROBE = V3AK.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3W = PROBE / "tp_research_v3w"

for p in (V3AK, V3AH, V3W, V3AB, PROBE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_v3w import TP2_GRID
from substantial_entry_gate import evaluate_substantial_near_zone
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, build_context_from_snapshot


def test_reference_has_substantial_near_zone_e1():
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    row = next(p for p in frozen["pending"] if p["touch_event_id"] == "te-d-c0791ea0c65e-0003")
    dc = datetime.strptime(row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    entry = float(row["entry_price"])
    r1 = evaluate_substantial_near_zone(row["_pool_members"], dc, entry, TP2_GRID[0])
    assert r1.pass_gate, r1.fail_detail
    assert r1.qualifying_zone is not None
    assert r1.qualifying_zone.dist_from_entry_pct <= 2.5


if __name__ == "__main__":
    test_reference_has_substantial_near_zone_e1()
    print("ok")

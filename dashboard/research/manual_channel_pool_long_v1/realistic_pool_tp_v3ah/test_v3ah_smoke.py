"""Smoke tests for V3AH zone selection (no backtest)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

V3AH = Path(__file__).resolve().parent
PROBE = V3AH.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3W = PROBE / "tp_research_v3w"

for p in (V3AH, V3W, V3AB, PROBE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1  # noqa: E402

l1._bootstrap_import_paths()

from v3ab_context import build_context_from_snapshot  # noqa: E402
from v3ah_context import DEFAULT_SNAPSHOT, FROZEN_SHA256  # noqa: E402
from zone_density import resolve_realistic_tp  # noqa: E402


def test_reference_trade_cap6_d20_selects_violet():
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    row = next(
        p
        for p in frozen["pending"]
        if p.get("touch_event_id") == "te-d-c0791ea0c65e-0003"
    )
    entry = float(row["entry_price"])
    dc = datetime.strptime(row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=timezone.utc
    )
    members = row["_pool_members"]
    r6 = resolve_realistic_tp(members, dc, entry, cap_dist_pct=6.0, dominance="D20")
    r5 = resolve_realistic_tp(members, dc, entry, cap_dist_pct=5.0, dominance="D20")
    assert r5.ok is False and r5.reason == "NO_QUALIFIED_TP"
    assert r6.ok is True
    assert abs(r6.tp_bottom - 0.08929) < 1e-4


def test_density_rank_before_cap():
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    row = next(p for p in frozen["pending"] if p.get("touch_event_id") == "te-d-c0791ea0c65e-0003")
    entry = float(row["entry_price"])
    dc = datetime.strptime(row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=timezone.utc
    )
    r = resolve_realistic_tp(row["_pool_members"], dc, entry, cap_dist_pct=6.0, dominance="D25")
    assert r.density_rank is not None and r.density_rank <= 4


if __name__ == "__main__":
    test_reference_trade_cap6_d20_selects_violet()
    test_density_rank_before_cap()
    print("ok")

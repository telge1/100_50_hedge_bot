"""Guards for the C1a single-blocker recovery case study (research-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.inventory_mtm_freeze import inventory_mtm_usdt
from research.backtests.run_c1a_single_blocker_recovery_case_study import (
    BASELINE_TRADE_ID,
    COIN,
    TRADE_START_INDEX,
    select_case,
)

HYBRID = Path(
    "research/backtests/results/blocker_recovery_trigger_and_hybrid_audit_20260720"
)
CASE = Path(
    "research/backtests/results/c1a_single_blocker_recovery_case_study_20260720"
)


def test_inventory_mtm_formula_matches_trigger_snapshot() -> None:
    # APTUSDT C1a trigger snapshot (from reproduced run).
    mtm = inventory_mtm_usdt(
        realized=0.014987768369999432,
        long_qty=38.147000000000006,
        long_avg=1.9661,
        short_qty=19.093,
        short_avg=1.9661,
        mark=1.9369,
    )
    assert mtm == pytest.approx(-0.5413890316299986, abs=1e-9)


def test_select_case_picks_apt_trade_3() -> None:
    if not (HYBRID / "original_blocker_outcomes.csv").exists():
        pytest.skip("hybrid audit artifacts not present")
    selection = select_case(HYBRID)
    assert selection["selected"]["coin"] == COIN
    assert int(float(selection["selected"]["baseline_trade_id"])) == BASELINE_TRADE_ID
    assert selection["n_recovered"] == 18


def test_case_study_artifacts_and_guards() -> None:
    if not (CASE / "selected_case.json").exists():
        pytest.skip("case study not generated yet")
    import json

    payload = json.loads((CASE / "selected_case.json").read_text(encoding="utf-8"))
    guards = payload["guards"]
    assert guards["reproduces_audit_trigger_candle"] is True
    assert guards["cycle_at_trigger_is_2"] is True
    assert guards["absolute_flat_matches_audit"] is True
    assert guards["final_pnl_matches"] is True
    assert guards["long_short_flat"] is True
    assert guards["no_active_orders"] is True
    assert guards["pnl_recon_closes"] is True
    assert guards["baseline_still_open_blocker"] is True
    assert payload["trade_start_index"] == TRADE_START_INDEX
    required = [
        "REPORT.md",
        "selected_case.json",
        "event_timeline.csv",
        "candle_path.csv",
        "position_state_transitions.csv",
        "order_lifecycle.csv",
        "pnl_reconciliation.csv",
        "baseline_vs_c1a.csv",
        "counterfactual_summary.csv",
        "code_path_map.md",
    ]
    for name in required:
        assert (CASE / name).exists(), name

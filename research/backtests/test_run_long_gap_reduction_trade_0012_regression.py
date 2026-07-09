from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.run_long_gap_reduction_trade_0012_audit import (
    BASE_RESULTS_DIR,
    run_long_gap_reduction_trade_0012_audit,
)


_APT_CANDLES_AVAILABLE = (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists()
_ADDON_BASELINE_PATH = (
    BASE_RESULTS_DIR
    / "addon_recovery_trade_0012_full_audit"
    / "run_20260708T174903.535501_0000"
    / "trade_0012_result.json"
)
_ADDON_BASELINE_AVAILABLE = _ADDON_BASELINE_PATH.exists()


@pytest.mark.skipif(
    not _APT_CANDLES_AVAILABLE or not _ADDON_BASELINE_AVAILABLE,
    reason="APTUSDT candle data or addon-recovery baseline for Trade-0012 unavailable",
)
def test_trade_0012_long_gap_reduction_regression_runs_and_produces_summary() -> None:
    """
    Execute the real Trade-0012 long-gap-reduction audit when APTUSDT candles
    are available and assert that the summary JSON is produced and consistent.
    """
    outputs = run_long_gap_reduction_trade_0012_audit()
    summary_path = Path(outputs["summary_path"])
    assert summary_path.exists()

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    long_only = payload["long_gap_reduction"]

    # Basic structural sanity checks, not exact numeric expectations:
    assert long_only["start_state"]["cycle3_candle_index"] >= 0
    assert long_only["final_long_qty"] == pytest.approx(long_only["final_short_qty"])
    assert long_only["remaining_gap"] == pytest.approx(0.0, abs=1e-9)


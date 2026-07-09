from __future__ import annotations

from pathlib import Path

import json
import pytest

from research.backtests.run_addon_recovery_trade_0012_audit import (
    run_addon_recovery_trade_0012_audit,
    START_INDEX,
    END_INDEX,
    EXPECTED_START_TIME,
    EXPECTED_END_TIME,
)


def test_runner_produces_metadata_and_core_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Smoke test: ensure the runner completes and writes reproduction_metadata.json
    and trade_0012_result.json in a fresh environment with real candle data.

    This test does not validate full economic correctness; it only checks that
    the main orchestration path runs without raising and produces key artifacts.
    """

    # Run in the real project root; outputs are timestamped and do not overwrite
    # existing files. We do not monkeypatch candle loading to keep behavior close
    # to production, but we keep the assertions lightweight.
    outputs = run_addon_recovery_trade_0012_audit()

    metadata_path = outputs.get("reproduction_metadata_json")
    result_path = outputs.get("trade_result_json")

    assert metadata_path is not None
    assert metadata_path.is_file()
    assert result_path is not None
    assert result_path.is_file()

    # Basic structure checks.
    meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert meta["trade_block_id"] == "backtest_long_continuous_trade_0012"
    assert meta["start_index"] == START_INDEX
    assert meta["end_index"] == END_INDEX
    assert meta["start_time"] == EXPECTED_START_TIME
    assert meta["end_time"] == EXPECTED_END_TIME

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["trade_block_id"] == "backtest_long_continuous_trade_0012"
    # We align the reproduced BacktestResult indices with the original continuous
    # run so start_index matches the absolute index.
    assert result["start_index"] == START_INDEX
    assert "addon_short_events" in result

    # Addon PnL coverage artefacts must have been created.
    addon_cov_json = outputs.get("addon_pnl_coverage_json")
    addon_cov_csv = outputs.get("addon_pnl_coverage_csv")
    assert addon_cov_json is not None and addon_cov_json.is_file()
    assert addon_cov_csv is not None and addon_cov_csv.is_file()

    cov = json.loads(addon_cov_json.read_text(encoding="utf-8"))
    assert cov["trade_block_id"] == "backtest_long_continuous_trade_0012"
    assert cov["round_count"] == 348


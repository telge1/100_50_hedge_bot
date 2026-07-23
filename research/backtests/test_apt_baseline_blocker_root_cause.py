"""Tests for baseline blocker root-cause audit (APTUSDT trade 3)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from research.backtests.apt_baseline_blocker_root_cause import (
    APT_TRADE3_COIN,
    APT_TRADE3_ID,
    APT_TRADE3_MAX_CYCLE,
    APT_TRADE3_MTM,
    APT_TRADE3_START_INDEX,
    PROTECTED_OUTPUT_DIRS,
    analyze_root_cause_markers,
    assert_output_dir_safe,
    build_cycle_snapshots,
    build_exposure_growth_by_cycle,
    build_fill_replay_rows,
    check_baseline_parity,
    load_trade_start_index_from_baseline,
    pnl_reconciliation_rows,
    select_trade_from_continuous,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_add_multistart_metrics import analyze_trade
from research.backtests.run_apt_baseline_blocker_root_cause import run_audit


@pytest.fixture(scope="module")
def apt_candles():
    return normalize_candles(APT_TRADE3_COIN, load_candles_for_symbol(APT_TRADE3_COIN, limit=50000))


@pytest.fixture(scope="module")
def apt_trade3(apt_candles):
    result, meta = select_trade_from_continuous(
        coin=APT_TRADE3_COIN, candles=apt_candles, trade_id=APT_TRADE3_ID
    )
    return result, meta


def test_cli_defaults_module_importable() -> None:
    from research.backtests.run_apt_baseline_blocker_root_cause import DEFAULT_OUT

    assert DEFAULT_OUT.name == "apt_baseline_blocker_root_cause_20260721"


def test_protected_dirs_include_baseline() -> None:
    assert any("current_baseline" in str(p) for p in PROTECTED_OUTPUT_DIRS)


def test_assert_output_dir_refuses_protected(tmp_path: Path) -> None:
    baseline = next(p for p in PROTECTED_OUTPUT_DIRS if "current_baseline" in str(p))
    with pytest.raises(RuntimeError, match="protected"):
        assert_output_dir_safe(baseline)


def test_load_trade_start_index_from_baseline() -> None:
    idx = load_trade_start_index_from_baseline(APT_TRADE3_COIN, APT_TRADE3_ID)
    assert idx == APT_TRADE3_START_INDEX


def test_trade_id_selection(apt_trade3) -> None:
    result, meta = apt_trade3
    assert int(result.trade_number or 0) == APT_TRADE3_ID
    assert int(result.start_index or 0) == APT_TRADE3_START_INDEX
    assert meta["total_trades_in_chain"] >= APT_TRADE3_ID


def test_baseline_parity_apt_trade3(apt_candles, apt_trade3) -> None:
    result, _ = apt_trade3
    start_idx = int(result.start_index or 0)
    analysis = analyze_trade(
        result,
        variant="test",
        long_add_pct=0.5,
        target_profit_usdt=0.015,
        window_candles=apt_candles[start_idx:],
        valid=True,
        skip_reason="ok",
    )
    parity = check_baseline_parity(
        coin=APT_TRADE3_COIN, trade_id=APT_TRADE3_ID, result=result, analysis=analysis
    )
    assert parity["ok"] is True
    assert parity["checks"]["max_cycle"][0] == APT_TRADE3_MAX_CYCLE
    assert abs(float(parity["checks"]["mtm_pnl"][0]) - APT_TRADE3_MTM) <= 0.02


def test_cycle_snapshots_non_empty(apt_candles, apt_trade3) -> None:
    result, _ = apt_trade3
    snaps = build_cycle_snapshots(
        result=result, candles=apt_candles, start_index=int(result.start_index or 0)
    )
    assert len(snaps) >= APT_TRADE3_MAX_CYCLE
    completed = [s for s in snaps if s.get("phase") == "after_short_reduce"]
    assert len(completed) == APT_TRADE3_MAX_CYCLE


def test_pnl_reconciliation_closes(apt_trade3) -> None:
    result, _ = apt_trade3
    rows = pnl_reconciliation_rows(result)
    assert rows[-1]["recon_ok"] is True


def test_root_cause_markers_present(apt_candles, apt_trade3) -> None:
    result, _ = apt_trade3
    start_idx = int(result.start_index or 0)
    snaps = build_cycle_snapshots(result=result, candles=apt_candles, start_index=start_idx)
    exposure = build_exposure_growth_by_cycle(snaps)
    from research.backtests.apt_baseline_blocker_root_cause import build_exit_reachability_by_cycle

    reach = build_exit_reachability_by_cycle(
        result=result, candles=apt_candles, start_index=start_idx, snapshots=snaps
    )
    markers = analyze_root_cause_markers(
        snapshots=snaps,
        reachability=reach,
        exposure_rows=exposure,
        result=result,
        candles=apt_candles,
        start_index=start_idx,
    )
    assert markers.get("last_healthy_cycle") is not None
    assert markers.get("max_cycle_reached") == APT_TRADE3_MAX_CYCLE


def test_deterministic_repeat(apt_candles) -> None:
    r1, _ = select_trade_from_continuous(coin=APT_TRADE3_COIN, candles=apt_candles, trade_id=3)
    r2, _ = select_trade_from_continuous(coin=APT_TRADE3_COIN, candles=apt_candles, trade_id=3)
    assert r1.overall_pnl == r2.overall_pnl
    assert r1.realized_pnl == r2.realized_pnl
    assert len(r1.fill_log) == len(r2.fill_log)


def test_full_audit_runner(tmp_path: Path, apt_candles) -> None:
    out = tmp_path / "apt_rc_audit"
    payload = run_audit(
        coin=APT_TRADE3_COIN,
        trade_id=APT_TRADE3_ID,
        output_dir=out,
        candle_limit=50000,
    )
    assert payload["ok"] is True
    required = [
        "REPORT.md",
        "selected_trade.json",
        "event_timeline.csv",
        "cycle_snapshots.csv",
        "exit_reachability_by_cycle.csv",
        "exposure_growth_by_cycle.csv",
        "pnl_reconciliation.csv",
        "healthy_escalation_no_return.json",
        "selected_recovery_start_state.json",
        "code_path_map.md",
    ]
    for name in required:
        assert (out / name).exists(), name
    selected = json.loads((out / "selected_trade.json").read_text())
    assert selected["coin"] == APT_TRADE3_COIN
    assert selected["trade_id"] == APT_TRADE3_ID


def test_cli_module_invocation(tmp_path: Path) -> None:
    out = tmp_path / "cli_out"
    proc = subprocess.run(
        [
            "python",
            "-m",
            "research.backtests.run_apt_baseline_blocker_root_cause",
            "--coin",
            APT_TRADE3_COIN,
            "--trade-id",
            str(APT_TRADE3_ID),
            "--output-dir",
            str(out),
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "REPORT.md").exists()


def test_freeze_guard_not_active(apt_trade3) -> None:
    result, _ = apt_trade3
    excerpt = dict(result.final_strategy_state_excerpt or {})
    assert excerpt.get("inventory_mtm_freeze_variant") in (None, "A0")
    assert not (excerpt.get("inventory_mtm_freeze_state") or {}).get("cycle_freeze_enabled")

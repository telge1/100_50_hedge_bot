"""Regression tests for short-primary profit target basis audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixed_cycle_hedge_bot.hedge_exit_math import calculate_hedge_exit_price

from research.backtests.audit_short_primary_profit_target_basis import (
    DEFAULT_SOURCE_DIR,
    build_notional_basis_code_paths,
    effective_notionals,
    reconstruct_initial_exit_row,
    run_audit,
)
from research.backtests.backtest_config_loader import resolve_backtest_config

OUTPUT_DIR = Path("research/backtests/results/short_primary_profit_target_basis_audit_test")


def test_effective_primary_notional_long_and_short():
    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT").config
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    long_n = effective_notionals(long_cfg, signal="long")
    short_n = effective_notionals(short_cfg, signal="short")
    assert long_n["effective_primary_notional"] == pytest.approx(100.0)
    assert long_n["effective_hedge_notional"] == pytest.approx(50.0)
    assert short_n["effective_primary_notional"] == pytest.approx(100.0)
    assert short_n["effective_hedge_notional"] == pytest.approx(50.0)


def test_calculate_hedge_exit_price_short_uses_short_primary_basis():
    le, lq = 2.0, 25.0
    se, sq = 2.0, 50.0
    comp = calculate_hedge_exit_price(le, lq, se, sq, 0.25, 0.0002, 0.0, 0.0, primary_side="short")
    assert comp.profit_basis_usdt == pytest.approx(100.0)
    assert comp.target_profit_usdt == pytest.approx(0.25)


def test_calculate_hedge_exit_price_long_uses_primary_basis():
    le, lq = 2.0, 50.0
    se, sq = 2.0, 25.0
    comp = calculate_hedge_exit_price(le, lq, se, sq, 0.25, 0.0002, 0.0, 0.0, primary_side="long")
    assert comp.profit_basis_usdt == pytest.approx(100.0)
    assert comp.target_profit_usdt == pytest.approx(0.25)


@pytest.fixture(scope="module")
def audit_summary():
    if not (DEFAULT_SOURCE_DIR / "short_continuous_results.json").is_file():
        pytest.skip("independent continuous results missing")
    return run_audit(source_dir=DEFAULT_SOURCE_DIR, output_dir=OUTPUT_DIR)


def test_initial_exit_reconstruction_from_trade_blocks(audit_summary: dict):
    assert audit_summary["initial_exit_trade_count"] >= 5
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT").config
    runs = json.loads((DEFAULT_SOURCE_DIR / "short_continuous_results.json").read_text())["runs"]
    for run in runs:
        fills = [
            row
            for row in json.loads(
                list(
                    DEFAULT_SOURCE_DIR.glob(
                        f"APTUSDT_short_continuous_trade_{int(run['trade_number']):04d}_*_trade_blocks.json"
                    )
                )[0].read_text()
            )["trade_blocks"]
            if row.get("row_type") == "fill"
        ]
        if any("CYCLE_" in str(f.get("purpose") or "") for f in fills):
            continue
        row = reconstruct_initial_exit_row(
            run,
            direction="short",
            source_dir=DEFAULT_SOURCE_DIR,
            long_cfg=long_cfg,
            short_cfg=short_cfg,
        )
        if row is None:
            continue
        assert row["formula_E_code_calculate_hedge_exit_price_target"] == pytest.approx(0.25, rel=0.01)
        assert row["formula_B_tp_pct_x_primary_notional"] == pytest.approx(0.25, rel=0.01)
        break
    else:
        pytest.fail("no initial_exit_only short trade found")


def test_audit_outputs_exist(audit_summary: dict):
    output = Path(audit_summary["output_dir"])
    for name in (
        "notional_basis_code_paths.csv",
        "initial_exit_profit_target_reconstruction.csv",
        "cycle_profit_target_basis_comparison.csv",
        "short_existing_vs_primary_basis_target.csv",
        "analysis_summary.json",
        "REPORT.md",
    ):
        assert (output / name).is_file(), name


def test_code_paths_include_hedge_exit_math():
    rows = build_notional_basis_code_paths()
    assert any("hedge_exit_math.py" in r["file"] for r in rows)


def _load_independent_continuous_runs() -> tuple[list[dict], list[dict]]:
    long_path = DEFAULT_SOURCE_DIR / "long_continuous_results.json"
    short_path = DEFAULT_SOURCE_DIR / "short_continuous_results.json"
    if not long_path.is_file() or not short_path.is_file():
        pytest.skip("independent continuous results missing")
    long_runs = list(json.loads(long_path.read_text(encoding="utf-8")).get("runs") or [])
    short_runs = list(json.loads(short_path.read_text(encoding="utf-8")).get("runs") or [])
    return long_runs, short_runs


def _require_int(value: object, *, field: str) -> int:
    if value is None or value == "":
        raise AssertionError(f"missing {field}")
    return int(value)


def _assert_independent_direction_invariants(runs: list[dict], *, direction: str) -> None:
    assert runs, f"{direction} runs must not be empty"
    assert _require_int(runs[0].get("start_index"), field="start_index") == 0
    assert _require_int(runs[0].get("trade_number"), field="trade_number") == 1

    previous_start: int | None = None
    for offset, run in enumerate(runs, start=1):
        trade_number = _require_int(run.get("trade_number"), field="trade_number")
        start_index = _require_int(run.get("start_index"), field="start_index")
        trade_block_id = str(run.get("trade_block_id") or "")
        assert trade_number == offset
        assert trade_block_id == f"backtest_{direction}_continuous_trade_{trade_number:04d}"
        if previous_start is not None:
            assert start_index > previous_start
        previous_start = start_index


def test_independent_continuous_reentry_invariants():
    """Structural invariants for independent long/short continuous re-entry.

    Long and Short share only the initial start_index=0; thereafter each
    direction runs its own continuous sequence and may produce different
    trade counts (unlike the old paired-start misinterpretation with 226/117).
    """
    long_runs, short_runs = _load_independent_continuous_runs()
    _assert_independent_direction_invariants(long_runs, direction="long")
    _assert_independent_direction_invariants(short_runs, direction="short")

    # Independence: trade counts need not match.
    assert len(long_runs) != len(short_runs)

    expected_last_index = 52568
    combined_summary_path = DEFAULT_SOURCE_DIR / "combined_summary.json"
    if combined_summary_path.is_file():
        series_end = json.loads(combined_summary_path.read_text(encoding="utf-8")).get(
            "series_end_index"
        ) or {}
        expected_last_index = int(series_end.get("expected_last_index") or expected_last_index)
        assert int(series_end.get("long_last_end_index")) == expected_last_index
        assert int(series_end.get("short_last_end_index")) == expected_last_index

    assert _require_int(long_runs[-1].get("end_index"), field="end_index") == expected_last_index
    assert _require_int(short_runs[-1].get("end_index"), field="end_index") == expected_last_index
    # Last starts remain near the series end (room for a final unfinished/open trade).
    assert _require_int(long_runs[-1].get("start_index"), field="start_index") >= expected_last_index - 500
    assert _require_int(short_runs[-1].get("start_index"), field="start_index") >= expected_last_index - 500


@pytest.mark.integration
def test_independent_continuous_trade_count_snapshot():
    """Data-dependent snapshot of the current APTUSDT 52_569-candle run."""
    long_runs, short_runs = _load_independent_continuous_runs()
    assert len(long_runs) == 172
    assert len(short_runs) == 87
    assert _require_int(long_runs[-1].get("start_index"), field="start_index") == 52316
    assert _require_int(short_runs[-1].get("start_index"), field="start_index") == 52155

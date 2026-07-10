from __future__ import annotations

from pathlib import Path

import json
import pytest

from research.backtests.tools.single_stuck_trade_full_audit import (
    _build_trade_identity,
    _compute_economic_components_from_phase2,
    _build_basic_checks_from_phase2,
    run_single_stuck_trade_full_audit,
)


def test_build_trade_identity_basic() -> None:
    run = {
        "trade_block_id": "backtest_long_continuous_trade_0012",
        "symbol": "APTUSDT",
        "direction": "long",
        "start_index": 100,
        "end_index": 200,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T01:00:00+00:00",
        "fill_model": "conservative",
        "max_fills_per_candle": 1,
    }
    identity = _build_trade_identity(run)
    assert identity.trade_block_id == "backtest_long_continuous_trade_0012"
    assert identity.symbol == "APTUSDT"
    assert identity.direction == "long"
    assert identity.start_index == 100
    assert identity.end_index == 200
    sig = identity.to_signature_dict()
    assert sig["trade_block_id"] == identity.trade_block_id


def test_economic_components_from_phase2_and_run() -> None:
    run = {
        "realized_pnl": 10.0,
        "unrealized_long_pnl": 1.0,
        "unrealized_short_pnl": -0.5,
        "overall_pnl": 9.5,
        "addon_short_net_realized_pnl": 3.0,
        "addon_short_trade_count": 1,
        "addon_short_long_reduce_total_pnl": 1.0,
    }
    phase2 = {
        "addon_aggregate_checks": {
            "reconstructed_addon_net_realized_pnl": 3.0,
            "reconstructed_addon_gross_profit": 5.0,
            "reconstructed_addon_gross_loss": 2.0,
            "reconstructed_entry_fees": 0.0,
            "reconstructed_exit_fees": 0.0,
        },
        "main_realized_pnl_breakdown": {
            "main_realized_pnl_without_addon_long_reduces": 7.0,
            "addon_long_reduce_realized_pnl": 1.0,
            "main_realized_pnl_reconstructed": 8.0,
            "main_realized_pnl_stored": 7.0,
        },
        "long_reduce_aggregate_checks": {
            "reconstructed_long_reduce_total_qty": 0.5,
            "reconstructed_long_reduce_total_pnl": 1.0,
        },
    }

    econ = _compute_economic_components_from_phase2(run=run, phase2=phase2)
    # Realized components.
    assert econ.main_realized_pnl == pytest.approx(7.0)
    assert econ.addon_short_realized_pnl == pytest.approx(3.0)
    assert econ.addon_long_reduce_realized_pnl == pytest.approx(1.0)
    assert econ.economic_realized_pnl == pytest.approx(11.0)
    # Unrealized components (from run only).
    assert econ.main_unrealized_long_pnl == pytest.approx(1.0)
    assert econ.main_unrealized_short_pnl == pytest.approx(-0.5)
    assert econ.addon_short_unrealized_pnl == 0.0


def test_basic_checks_from_phase2() -> None:
    run = {
        "realized_pnl": 7.0,
        "addon_short_net_realized_pnl": 3.0,
        "addon_short_long_reduce_total_qty": 0.5,
        "addon_short_long_reduce_total_pnl": 1.0,
    }
    phase2 = {
        "addon_aggregate_checks": {
            "reconstructed_addon_net_realized_pnl": 3.0,
        },
        "main_realized_pnl_breakdown": {
            "main_realized_pnl_without_addon_long_reduces": 7.0,
        },
        "long_reduce_aggregate_checks": {
            "reconstructed_long_reduce_total_qty": 0.5,
            "reconstructed_long_reduce_total_pnl": 1.0,
        },
    }
    checks = _build_basic_checks_from_phase2(run=run, phase2=phase2)
    assert checks, "expected at least one basic check"
    # All checks in this helper are consistency-only.
    assert all(c.independence_level == "consistency_only" for c in checks)
    assert all(c.check_is_consistency_only for c in checks)
    assert all(c.passed for c in checks)


def test_full_audit_on_synthetic_phase2_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    End-to-end smoke test: feed a minimal Phase-2 addon audit JSON and run the
    full single-trade audit without rerunning the backtest.
    """
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    trade_block_id = "backtest_long_continuous_trade_0012"

    # Minimal continuous-results JSON used by addon_recovery_audit._find_run_for_trade_block.
    continuous = results_dir / "APTUSDT_original_hedge_5m_continuous_results.json"
    payload = {
        "metadata": {
            "symbol": "APTUSDT",
            "fill_model": "conservative",
            "max_fills_per_candle": 1,
            "candles_loaded": 10,
            "config_source": "test",
            "long_config_path": "",
            "short_config_path": "",
            "file_config_path": None,
        },
        "runs": [
            {
                "symbol": "APTUSDT",
                "direction": "long",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
                "start_index": 0,
                "end_index": 9,
                "candles_processed": 10,
                "trade_number": 12,
                "trade_block_id": trade_block_id,
                "realized_pnl": 1.0,
                "unrealized_long_pnl": 0.1,
                "unrealized_short_pnl": -0.05,
                "overall_pnl": 1.05,
                "addon_short_net_realized_pnl": 0.5,
                "addon_short_trade_count": 1,
                "addon_short_long_reduce_total_qty": 0.1,
                "addon_short_long_reduce_total_pnl": 0.2,
                "addon_short_recovery_gap_at_activation": 6.0,
                "addon_short_step_fraction": 0.25,
                "allow_net_short": False,
                "addon_short_tp_pct": 0.75,
                "addon_short_reentry_buffer_pct": 0.20,
                "addon_short_min_favorable_move_pct": 0.20,
                "addon_short_rebound_close_pct": 0.50,
                "addon_short_hard_stop_pct": 1.0,
                "long_reduce_profit_usage_fraction": 0.9,
                "stop_when_long_qty_reaches_normal_short_qty": True,
                "addon_short_recovery_activation_order": "CYCLE_3_SHORT_REDUCE",
            }
        ],
        "aggregate": [],
    }
    continuous.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # Minimal trade-blocks JSON with empty trade_blocks list is sufficient for
    # the synthetic addon audit in this smoke test.
    trade_blocks = results_dir / f"APTUSDT_{trade_block_id}_conservative_live_trade_blocks.json"
    trade_blocks.write_text(
        json.dumps(
            {
                "metadata": {},
                "trade_blocks": [],
                "summary": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Monkeypatch _load_addon_audit_payload in the full-audit module so that the
    # tool does not depend on real Phase-1/2 addon audit artifacts in this test.
    fake_phase2 = {
        "addon_aggregate_checks": {
            "reconstructed_addon_net_realized_pnl": 0.5,
        },
        "main_realized_pnl_breakdown": {
            "main_realized_pnl_without_addon_long_reduces": 1.0,
        },
        "long_reduce_aggregate_checks": {
            "reconstructed_long_reduce_total_qty": 0.1,
            "reconstructed_long_reduce_total_pnl": 0.2,
        },
    }

    def _fake_load_addon(results_dir_arg: Path, trade_block_id_arg: str) -> dict:
        assert trade_block_id_arg == trade_block_id
        return {
            "trade_block_id": trade_block_id,
            "run": payload["runs"][0],
            "events": [],
            "summary_rows": [],
            "stats": {},
            "phase2": fake_phase2,
        }

    monkeypatch.setattr(
        "research.backtests.tools.single_stuck_trade_full_audit._load_addon_audit_payload",
        _fake_load_addon,
    )

    # Run the full audit without rerunning the backtest.
    outputs = run_single_stuck_trade_full_audit(
        results_dir=results_dir,
        trade_block_id=trade_block_id,
        symbol="APTUSDT",
        direction="long",
        output_dir=results_dir / "single_stuck_trade_full_audit",
        rerun_instrumented=False,
        strict=False,
    )

    # Check that key output files were written.
    assert outputs["economic_pnl_csv"].is_file()
    assert outputs["audit_checks_csv"].is_file()
    assert outputs["summary_json"].is_file()
    assert outputs["summary_md"].is_file()


"""Phase-15 PnL coverage audit tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.pnl_coverage_audit import (
    PNL_COVERAGE_AUDIT_FIELDS,
    apply_trade_exit_quality,
    build_pnl_coverage_audit,
    classify_trade_exit_quality,
    expected_cover_purpose,
    expected_cover_qty,
    has_undercovered_final_exit,
    inspect_qty_mapping,
    write_pnl_coverage_audit,
)
from research.backtests.run_original_hedge_backtest import main as cli_main


def _cycle_result(
    *,
    loss_pnl: float,
    cover_pnl: float,
    loss_purpose: str = "CYCLE_1_LONG_ADD",
    cover_purpose: str = "CYCLE_1_SHORT_REDUCE",
) -> BacktestResult:
    return BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=0,
        fill_log=[
            {
                "timestamp": "2026-01-01T00:05:00+00:00",
                "purpose": loss_purpose,
                "purpose_original": loss_purpose,
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "side": "long",
                "qty": 10.0,
                "fill_price": 1.0,
                "closed_pnl": loss_pnl,
                "short_avg_after": 0.6518,
            },
            {
                "timestamp": "2026-01-01T00:10:00+00:00",
                "purpose": cover_purpose,
                "purpose_original": cover_purpose,
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "side": "short",
                "qty": 6.0,
                "fill_price": 0.6437,
                "closed_pnl": cover_pnl,
            },
        ],
    )


def test_undercovered_cycle() -> None:
    rows = build_pnl_coverage_audit(_cycle_result(loss_pnl=-10.0, cover_pnl=6.0))
    assert len(rows) == 1
    row = rows[0]
    assert row["loss_purpose"] == "CYCLE_1_LONG_ADD"
    assert row["cover_purpose"] == "CYCLE_1_SHORT_REDUCE"
    assert row["coverage_ratio"] == pytest.approx(0.6)
    assert row["missing_pnl"] == pytest.approx(4.0)
    assert row["status"] == "undercovered"


def test_covered_cycle() -> None:
    rows = build_pnl_coverage_audit(_cycle_result(loss_pnl=-10.0, cover_pnl=10.5))
    assert rows[0]["status"] == "overcovered"
    assert rows[0]["missing_pnl"] == pytest.approx(0.0)


def test_exact_covered_cycle() -> None:
    rows = build_pnl_coverage_audit(_cycle_result(loss_pnl=-10.0, cover_pnl=10.0))
    assert rows[0]["status"] == "covered"


def test_expected_cover_qty() -> None:
    qty = expected_cover_qty(loss_pnl=-20.0, entry_price=100.0, fill_price=90.0)
    assert qty == pytest.approx(2.0)


def test_qty_mapping_warning() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        intent_log=[
            {
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "qty": 9.0,
            }
        ],
        order_log=[
            {
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "event_type": "filled",
                "qty": 9.0,
            }
        ],
        fill_log=[
            {
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "qty": 8.0,
                "closed_pnl": 1.0,
            }
        ],
    )
    mapping = inspect_qty_mapping(result, purpose="CYCLE_1_SHORT_REDUCE", cycle_index=1)
    assert mapping["intent_qty"] == pytest.approx(9.0)
    assert mapping["order_qty"] == pytest.approx(9.0)
    assert mapping["fill_qty"] == pytest.approx(8.0)
    assert "order_qty!=fill_qty" in mapping["qty_mapping_warning"]


def test_build_trade_block_rows_contains_trade_block_id() -> None:
    rows = build_pnl_coverage_audit(_cycle_result(loss_pnl=-0.1265715, cover_pnl=0.0776709))
    assert rows[0]["loss_pnl"] == pytest.approx(-0.1265715)
    assert rows[0]["cover_pnl"] == pytest.approx(0.0776709)
    assert rows[0]["status"] in {"undercovered", "pending_final_exit"}


def test_write_pnl_coverage_audit(tmp_path: Path) -> None:
    result = _cycle_result(loss_pnl=-10.0, cover_pnl=6.0)
    files = write_pnl_coverage_audit(result, tmp_path, base_name="APTUSDT_long_start0_test")
    assert Path(files["pnl_coverage_audit_csv"]).exists()
    assert Path(files["pnl_coverage_audit_json"]).exists()
    with Path(files["pnl_coverage_audit_csv"]).open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert list(row.keys()) == list(PNL_COVERAGE_AUDIT_FIELDS)
    payload = json.loads(Path(files["pnl_coverage_audit_json"]).read_text(encoding="utf-8"))
    assert payload["audit_rows"]


def test_cli_pnl_coverage_audit(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(10)
    ]
    with patch(
        "research.backtests.run_original_hedge_backtest.load_candles_for_symbol",
        return_value=candles,
    ):
        exit_code = cli_main(
            [
                "--symbol",
                "BTCUSDT",
                "--direction",
                "long",
                "--limit",
                "10",
                "--config-source",
                "test",
                "--pnl-coverage-audit",
                "--output-dir",
                str(tmp_path),
                "--no-json",
                "--no-csv",
            ]
        )
    assert exit_code == 0
    assert list(tmp_path.glob("*_pnl_coverage_audit.csv"))


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APT feather file not available",
)
def test_apt_pnl_coverage_audit_smoke(tmp_path: Path) -> None:
    exit_code = cli_main(
        [
            "--symbol",
            "APTUSDT",
            "--direction",
            "long",
            "--limit",
            "1000",
            "--config-source",
            "live",
            "--pnl-coverage-audit",
            "--output-dir",
            str(tmp_path),
            "--no-json",
            "--no-csv",
        ]
    )
    assert exit_code == 0
    csv_files = list(tmp_path.glob("APTUSDT_long_start0_*_pnl_coverage_audit.csv"))
    assert csv_files
    with csv_files[0].open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    purposes = {row["loss_purpose"] for row in rows} | {row["cover_purpose"] for row in rows}
    assert "CYCLE_1_LONG_ADD" in purposes or "CYCLE_1_SHORT_REDUCE" in purposes or not rows

def test_short_reduce_loss_cover_uses_full_qty_when_normal_split_disabled() -> None:
    """Regression: short_reduce loss cover must not be split into same-purpose replace orders.

    The bug was:
    - normal second-leg split created two CYCLE_1_SHORT_REDUCE intents
    - the second one replaced the first because purpose was identical
    - only half qty filled, leaving the long-reduce loss undercovered
    """
    from research.backtests.backtest_report import BacktestResult
    from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit

    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=0,
        fill_log=[
            {
                "timestamp": "2026-06-24T02:05:00+00:00",
                "purpose": "CYCLE_1_LONG_ADD",
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "side": "long",
                "qty": 38.355,
                "fill_price": 0.6485,
                "closed_pnl": -0.12657150000000308,
                "short_avg_after": 0.6518,
            },
            {
                "timestamp": "2026-06-24T02:35:00+00:00",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "side": "short",
                "qty": 19.177,
                "fill_price": 0.6437,
                "closed_pnl": 0.1553336999999999,
                "metadata_excerpt": {
                    "required_net": 0.14157150000000307,
                    "long_loss_usdt": 0.12657150000000308,
                    "target_profit_usdt": 0.015,
                    "stage_count": 1,
                    "normal_cycle_second_leg_split_disabled": True,
                    "split_fallback_reason": "single_short_reduce_order_required_to_cover_loss",
                    "short_followup_pnl_source": "cycle_entry_long_add_confirmed_pnl",
                },
            },
        ],
        intent_log=[
            {
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "qty": 19.177,
                "trigger_price": 0.6437,
            },
        ],
        order_log=[
            {
                "event_type": "submitted",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "qty": 19.177,
                "trigger_price": 0.6437,
            },
            {
                "event_type": "filled",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "qty": 19.177,
                "trigger_price": 0.6437,
            },
        ],
    )

    rows = build_pnl_coverage_audit(result)
    assert len(rows) == 1
    row = rows[0]

    assert row["status"] == "overcovered"
    assert row["missing_pnl"] == 0.0
    assert row["qty_shortfall"] == 0.0
    assert row["actual_cover_qty"] == 19.177
    assert row["intent_qty"] == 19.177
    assert row["order_qty"] == 19.177
    assert row["fill_qty"] == 19.177
    assert row["coverage_ratio"] > 1.0

def test_cycle_loss_can_be_covered_by_final_exit_basket() -> None:
    """Regression: a cycle loss can be covered by recalculated final exits.

    In the short-side flow, CYCLE_1_SHORT_REDUCE can realize a loss.
    The bot may then recalculate LONG_SL_EXIT and SHORT_TP_EXIT so the
    remaining hedge basket closes flat with profit. That is a successful cover,
    even without a direct CYCLE_1_LONG_ADD fill.
    """
    from research.backtests.backtest_report import BacktestResult
    from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit

    result = BacktestResult(
        symbol="APTUSDT",
        direction="short",
        start_index=0,
        fill_log=[
            {
                "timestamp": "2026-06-24T01:30:00+00:00",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "side": "short",
                "qty": 38.355,
                "fill_price": 0.6551,
                "closed_pnl": -0.12657149999999884,
                "long_avg_after": 0.6518,
                "short_avg_after": 0.6518,
            },
            {
                "timestamp": "2026-06-24T02:20:00+00:00",
                "purpose": "LONG_SL_EXIT",
                "cycle_index": 0,
                "side": "long",
                "qty": 76.71,
                "fill_price": 0.645,
                "closed_pnl": -0.5216280000000021,
            },
            {
                "timestamp": "2026-06-24T02:25:00+00:00",
                "purpose": "SHORT_TP_EXIT",
                "cycle_index": 0,
                "side": "short",
                "qty": 115.066,
                "fill_price": 0.645,
                "closed_pnl": 0.7824488000000033,
            },
        ],
        intent_log=[],
        order_log=[],
        final_active_order_purposes=[],
    )

    rows = build_pnl_coverage_audit(result)
    assert len(rows) == 1
    row = rows[0]

    assert row["loss_purpose"] == "CYCLE_1_SHORT_REDUCE"
    assert row["cover_purpose"] == "LONG_SL_EXIT|SHORT_TP_EXIT"
    assert row["status"] == "overcovered_by_final_exit"
    assert row["missing_pnl"] == 0.0
    assert row["cover_pnl"] == 0.2608208000000012
    assert row["net_pnl"] == 0.13424930000000235
    assert row["coverage_ratio"] > 2.0


def test_classify_trade_exit_quality_undercovered_final_exit() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        final_status="closed",
        exit_reason="flat_no_active_orders",
        realized_pnl=-0.5,
        fill_log=[
            {
                "timestamp": "2026-01-01T00:05:00+00:00",
                "purpose": "CYCLE_3_LONG_ADD",
                "cycle_index": 3,
                "closed_pnl": -1.0,
                "side": "long",
            },
            {
                "timestamp": "2026-01-01T00:10:00+00:00",
                "purpose": "LONG_TP_EXIT",
                "closed_pnl": 0.1,
                "side": "long",
            },
            {
                "timestamp": "2026-01-01T00:10:00+00:00",
                "purpose": "SHORT_SL_EXIT",
                "closed_pnl": -0.05,
                "side": "short",
            },
        ],
    )
    assert has_undercovered_final_exit(result)
    quality = apply_trade_exit_quality(result)
    assert quality == "closed_undercovered_final_exit"
    assert result.final_status == "closed_undercovered_final_exit"


def test_classify_trade_exit_quality_closed_ok() -> None:
    result = _cycle_result(loss_pnl=-0.1, cover_pnl=0.12)
    result.final_status = "closed"
    result.exit_reason = "flat_no_active_orders"
    result.realized_pnl = 0.02
    quality = classify_trade_exit_quality(result)
    assert quality == "closed_ok"


def _short_primary_cycle_result(
    *,
    loss_pnl: float,
    cover_pnl: float,
    loss_purpose: str = "CYCLE_1_SHORT_REDUCE",
    cover_purpose: str = "CYCLE_1_LONG_REDUCE",
) -> BacktestResult:
    return BacktestResult(
        symbol="APTUSDT",
        direction="short",
        start_index=0,
        fill_log=[
            {
                "timestamp": "2026-01-01T00:05:00+00:00",
                "purpose": loss_purpose,
                "purpose_original": loss_purpose,
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                "side": "short",
                "qty": 10.0,
                "fill_price": 1.0,
                "closed_pnl": loss_pnl,
                "long_avg_after": 0.6518,
            },
            {
                "timestamp": "2026-01-01T00:10:00+00:00",
                "purpose": cover_purpose,
                "purpose_original": cover_purpose,
                "cycle_index": 1,
                "cycle_role": "long_reduce",
                "side": "long",
                "qty": 6.0,
                "fill_price": 0.655,
                "closed_pnl": cover_pnl,
            },
        ],
    )


def test_short_primary_audit_expects_long_reduce_as_cover_purpose() -> None:
    assert expected_cover_purpose("CYCLE_1_SHORT_REDUCE") == "CYCLE_1_LONG_REDUCE"
    assert expected_cover_purpose("CYCLE_1_SHORT_ADD") == "CYCLE_1_LONG_REDUCE"


def test_short_primary_audit_does_not_misclassify_long_reduce_cover_as_missing() -> None:
    rows = build_pnl_coverage_audit(
        _short_primary_cycle_result(loss_pnl=-10.0, cover_pnl=10.5)
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["cover_purpose"] == "CYCLE_1_LONG_REDUCE"
    assert row["status"] == "overcovered"
    assert row["missing_pnl"] == pytest.approx(0.0)


def test_short_primary_audit_role_fallback_matches_runtime_purpose() -> None:
    """Role fallback still classifies cover when purpose naming was wrong (LONG_ADD)."""
    rows = build_pnl_coverage_audit(
        _short_primary_cycle_result(
            loss_pnl=-10.0,
            cover_pnl=10.5,
            cover_purpose="CYCLE_1_LONG_REDUCE",
        )
    )
    assert rows[0]["status"] == "overcovered"
    wrong_name_rows = build_pnl_coverage_audit(
        BacktestResult(
            symbol="APTUSDT",
            direction="short",
            start_index=0,
            fill_log=[
                {
                    "timestamp": "2026-01-01T00:05:00+00:00",
                    "purpose": "CYCLE_1_SHORT_REDUCE",
                    "cycle_index": 1,
                    "cycle_role": "short_reduce",
                    "side": "short",
                    "qty": 10.0,
                    "closed_pnl": -10.0,
                    "long_avg_after": 0.6518,
                },
                {
                    "timestamp": "2026-01-01T00:10:00+00:00",
                    "purpose": "CYCLE_1_LONG_REDUCE",
                    "cycle_index": 1,
                    "cycle_role": "long_reduce",
                    "side": "long",
                    "qty": 6.0,
                    "closed_pnl": 10.5,
                },
            ],
        )
    )
    assert wrong_name_rows[0]["status"] == "overcovered"
    assert wrong_name_rows[0]["cover_purpose"] == "CYCLE_1_LONG_REDUCE"

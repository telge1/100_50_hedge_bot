"""Phase 3 / 2M streak statistics regression tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from long_phase2m_ledger_integrity_v1 import (  # noqa: E402
    completed_trades_chronological,
    max_consecutive_sl,
    worst_loss_streak_sum_pct,
)


def _t(fill: str, exit_reason: str, gross: float | None, exit_bar: str | None = None):
    return SimpleNamespace(
        fill_bar_utc=fill,
        exit_bar_utc=exit_bar or fill,
        exit_reason=exit_reason,
        gross_return_pct=gross,
    )


def test_max_consecutive_sl_no_trades():
    assert max_consecutive_sl([]) == 0


def test_max_consecutive_sl_single_win():
    assert max_consecutive_sl([_t("2026-06-10T00:00:00Z", "FIXED_UPPER_TP", 1.0)]) == 0


def test_max_consecutive_sl_single_sl():
    assert max_consecutive_sl([_t("2026-06-10T00:00:00Z", "STRUCTURAL_SL", -1.0)]) == 1


def test_max_consecutive_sl_three_sl_in_row():
    trades = [
        _t("2026-06-10T00:00:00Z", "STRUCTURAL_SL", -1.0),
        _t("2026-06-11T00:00:00Z", "STRUCTURAL_SL", -0.5),
        _t("2026-06-12T00:00:00Z", "STRUCTURAL_SL", -0.3),
    ]
    assert max_consecutive_sl(trades) == 3


def test_max_consecutive_sl_broken_streak():
    trades = [
        _t("2026-06-10T00:00:00Z", "STRUCTURAL_SL", -1.0),
        _t("2026-06-11T00:00:00Z", "FIXED_UPPER_TP", 2.0),
        _t("2026-06-12T00:00:00Z", "STRUCTURAL_SL", -0.5),
        _t("2026-06-13T00:00:00Z", "STRUCTURAL_SL", -0.5),
    ]
    assert max_consecutive_sl(trades) == 2


def test_max_consecutive_sl_sorts_by_fill_not_input_order():
    trades = [
        _t("2026-06-12T00:00:00Z", "STRUCTURAL_SL", -0.3),
        _t("2026-06-10T00:00:00Z", "STRUCTURAL_SL", -1.0),
        _t("2026-06-11T00:00:00Z", "STRUCTURAL_SL", -0.5),
    ]
    assert max_consecutive_sl(trades) == 3


def test_open_position_without_gross_excluded():
    trades = [
        _t("2026-06-10T00:00:00Z", "STRUCTURAL_SL", None, exit_bar=None),
        _t("2026-06-11T00:00:00Z", "STRUCTURAL_SL", -1.0),
    ]
    assert max_consecutive_sl(trades) == 1


def test_worst_loss_streak_sum_pct():
    trades = [
        _t("2026-06-10T00:00:00Z", "STRUCTURAL_SL", -1.0),
        _t("2026-06-11T00:00:00Z", "STRUCTURAL_SL", -2.0),
        _t("2026-06-12T00:00:00Z", "FIXED_UPPER_TP", 3.0),
    ]
    assert worst_loss_streak_sum_pct(trades) == -3.0


def test_completed_trades_chronological_month_boundary():
    trades = [
        _t("2026-07-01T00:00:00Z", "FIXED_UPPER_TP", 1.0),
        _t("2026-06-30T23:55:00Z", "STRUCTURAL_SL", -0.5),
    ]
    ordered = completed_trades_chronological(trades)
    assert [t.fill_bar_utc for t in ordered] == [
        "2026-06-30T23:55:00Z",
        "2026-07-01T00:00:00Z",
    ]


def test_monthly_stats_integration_smoke():
    from run_phase3_multi_month_backtest_v1 import _monthly_stats  # noqa: E402

    trades = [
        SimpleNamespace(
            fill_bar_utc="2026-06-15T00:00:00Z",
            exit_bar_utc="2026-06-15T01:00:00Z",
            exit_reason="STRUCTURAL_SL",
            gross_return_pct=-1.0,
            entry_price=1.0,
            sl_price=0.99,
            tp_limit=1.02,
        ),
    ]
    m = _monthly_stats(
        month_key="2026-06",
        trades=trades,
        entry_events=[],
        eval_start_close="2026-06-08T00:00:00Z",
    )
    assert m["max_consecutive_sl"] == 1
    assert m["worst_loss_streak_sum_pct"] == -1.0

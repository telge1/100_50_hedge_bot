"""Generalization metric and causality boundary tests."""

from __future__ import annotations

from research.regime_scanner.tem_structure_break.eval_common import AAVE_DEV_TRADE_ID, lead_hours
from research.regime_scanner.tem_structure_break.generalization_metrics import (
    confusion,
    split_rows,
)
from research.regime_scanner.tem_structure_break.monitor import (
    FrozenLevels,
    MonitorRuntime,
    ScannerState,
    step_monitor,
)


def test_split_rows_separates_aave_dev() -> None:
    rows = [
        {"trade_id": AAVE_DEV_TRADE_ID, "cohort": "blocker"},
        {"trade_id": "X|two_early_medium|continuous|0001", "cohort": "blocker"},
        {"trade_id": "Y|two_early_medium|continuous|0002", "cohort": "control"},
    ]
    parts = split_rows(rows)
    assert len(parts["aave_dev"]) == 1
    assert len(parts["blockers_holdout26"]) == 1
    assert len(parts["controls"]) == 1


def test_confusion_matrix_basic() -> None:
    blockers = [{"pred_invalidated": True}, {"pred_invalidated": False}]
    controls = [{"pred_invalidated": True}, {"pred_invalidated": False}, {"pred_invalidated": False}]
    cm = confusion(blockers, controls, pred_key="pred_invalidated")
    assert cm["tp"] == 1 and cm["fn"] == 1 and cm["fp"] == 1 and cm["tn"] == 2
    assert abs(cm["precision"] - 0.5) < 1e-9
    assert abs(cm["recall"] - 0.5) < 1e-9


def test_lead_hours_sign() -> None:
    assert lead_hours("2026-01-19T00:00:00+00:00", "2026-01-20T22:35:00+00:00") > 40
    assert lead_hours("2026-01-21T00:00:00+00:00", "2026-01-20T00:00:00+00:00") < 0


def test_reclaim_boundary_requires_next_4h_close() -> None:
    rt = MonitorRuntime()
    rt.state = ScannerState.BREAK_PENDING
    rt.frozen = FrozenLevels(
        entry_bar=0,
        entry_timestamp="t0",
        entry_price=100.0,
        side="long",
        protected_low_5m=90.0,
        protected_high_5m=None,
        protected_low_1h=95.0,
        protected_low_4h=None,
        major_5m_at_entry=1,
        h4_major_at_entry=-1,
    )
    rt.active_break_level = 96.0
    rt.broken_level = 96.0
    rt.reclaim_deadline_4h_open = __import__("pandas").Timestamp("2026-01-01T04:00:00+00:00")
    # same close_decision as deadline → no resolve yet
    step_monitor(
        rt,
        bar_i=1,
        row_5m={
            "timestamp": "2026-01-01T03:55:00+00:00",
            "decision_time": "2026-01-01T04:00:00+00:00",
            "close": 97.0,
            "major_direction": -1,
            "protected_low": None,
        },
        h1=None,
        h4={
            "index": 5,
            "close": 97.0,
            "protected_low": None,
            "close_decision": "2026-01-01T04:00:00+00:00",
            "external_bos_down": False,
            "close_break_protected_down": False,
        },
        prev_h4_idx=4,
    )
    assert rt.state == ScannerState.BREAK_PENDING
    # next 4h close above level → reclaim
    step_monitor(
        rt,
        bar_i=2,
        row_5m={
            "timestamp": "2026-01-01T07:55:00+00:00",
            "decision_time": "2026-01-01T08:00:00+00:00",
            "close": 97.0,
            "major_direction": -1,
            "protected_low": None,
        },
        h1=None,
        h4={
            "index": 6,
            "close": 97.0,
            "protected_low": None,
            "close_decision": "2026-01-01T08:00:00+00:00",
            "external_bos_down": False,
            "close_break_protected_down": False,
        },
        prev_h4_idx=5,
    )
    assert rt.state == ScannerState.RECLAIMED

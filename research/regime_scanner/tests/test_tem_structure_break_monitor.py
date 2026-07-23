"""Unit tests for TEM structure-break monitor (multi-episode after reclaim)."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from research.regime_scanner.tem_structure_break.monitor import (
    EntryDecision,
    FrozenLevels,
    MonitorRuntime,
    ScannerState,
    decide_entry_long,
    run_in_trade_monitor,
    step_monitor,
)


def test_decide_weak_allow_when_h4_bearish_ltf_bullish() -> None:
    row = {
        "major_direction": 1,
        "ema_regime_direction": 1,
        "m30_major_direction": 1,
        "h4_major_direction": -1,
        "protected_low": 170.0,
        "protected_high": 180.0,
        "ema_9": 175.0,
        "ema_20": 174.0,
        "ema_59": 173.0,
        "ema_200": 170.0,
        "close_vs_ema_200_pct": 3.0,
    }
    d = decide_entry_long(
        row,
        {"protected_low": 171.0, "major_direction": 1},
        {"protected_low": 172.0, "major_direction": -1},
    )
    assert d.decision == "WEAK_ALLOW"
    assert d.g1_long == "allow"


def test_decide_block_when_g1_bearish_major() -> None:
    row = {
        "major_direction": -1,
        "ema_regime_direction": -1,
        "m30_major_direction": -1,
        "h4_major_direction": -1,
        "protected_low": 170.0,
        "protected_high": 180.0,
        "ema_9": 1,
        "ema_20": 2,
        "ema_59": 3,
        "ema_200": 4,
    }
    d = decide_entry_long(row, None, {"major_direction": -1})
    assert d.decision == "BLOCK"


def test_frozen_5m_break_emits_warning_without_lookahead() -> None:
    rt = MonitorRuntime()
    rt.state = ScannerState.STRUCTURE_INTACT
    rt.decision = EntryDecision(
        decision="WEAK_ALLOW",
        reasons=[],
        major_5m=1,
        ema_regime=1,
        m30_major=1,
        h4_major=-1,
        g1_long="allow",
        protected_low_5m=100.0,
        protected_high_5m=110.0,
        protected_low_1h=99.0,
        protected_low_4h=98.0,
        ema_stack="bullish_aligned",
        price_vs_ema200_pct=1.0,
    )
    rt.frozen = FrozenLevels(
        entry_bar=10,
        entry_timestamp="2026-01-01T00:00:00+00:00",
        entry_price=105.0,
        side="long",
        protected_low_5m=100.0,
        protected_high_5m=110.0,
        protected_low_1h=99.0,
        protected_low_4h=98.0,
        major_5m_at_entry=1,
        h4_major_at_entry=-1,
    )
    step_monitor(
        rt,
        bar_i=11,
        row_5m={
            "timestamp": "2026-01-01T00:05:00+00:00",
            "decision_time": "2026-01-01T00:10:00+00:00",
            "close": 100.5,
            "major_direction": 1,
            "protected_low": 100.0,
        },
        h1=None,
        h4=None,
        prev_h4_idx=None,
    )
    assert rt.warning_bar is None
    step_monitor(
        rt,
        bar_i=12,
        row_5m={
            "timestamp": "2026-01-01T00:10:00+00:00",
            "decision_time": "2026-01-01T00:15:00+00:00",
            "close": 99.5,
            "major_direction": 1,
            "protected_low": 100.0,
        },
        h1=None,
        h4=None,
        prev_h4_idx=None,
    )
    assert rt.warning_bar == 12
    assert rt.state == ScannerState.STRUCTURE_WARNING
    assert rt.first_5m_frozen_break_bar == 12


def test_run_in_trade_monitor_continues_after_entry_when_end_bar_none() -> None:
    n = 6
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [100.0, 100.0, 100.0, 99.0, 98.5, 98.0],
            "volume": 1.0,
        }
    )
    trace = frame.copy()
    trace["decision_time"] = ts + pd.Timedelta(minutes=5)
    trace["major_direction"] = 1
    trace["ema_regime_direction"] = 1
    trace["m30_major_direction"] = 1
    trace["h4_major_direction"] = -1
    trace["protected_low"] = 99.5
    trace["protected_high"] = 102.0
    trace["ema_9"] = 100.0
    trace["ema_20"] = 99.0
    trace["ema_59"] = 98.0
    trace["ema_200"] = 97.0
    trace["close_vs_ema_200_pct"] = 2.0
    empty_htf = pd.DataFrame(
        columns=[
            "timestamp",
            "htf_close_decision",
            "close",
            "protected_low",
            "protected_high",
            "major_direction",
            "close_break_protected_down",
            "external_bos_down",
            "arm_edge_external_bear",
        ]
    )

    with (
        patch(
            "research.regime_scanner.tem_structure_break.monitor.build_5m_trace",
            return_value=trace,
        ),
        patch(
            "research.regime_scanner.tem_structure_break.monitor.build_htf_structure_frame",
            return_value=empty_htf,
        ),
    ):
        rt = run_in_trade_monitor(
            frame_5m=frame,
            entry_bar=2,
            entry_price=100.0,
            side="long",
            end_bar=None,
        )
    assert len(rt.timeline) == 3
    assert rt.first_5m_frozen_break_bar == 3
    assert rt.warning_kind == "entry_protected_low_5m_close_break"


def _base_rt(*, frozen_1h: float = 160.64) -> MonitorRuntime:
    rt = MonitorRuntime()
    rt.state = ScannerState.STRUCTURE_AT_RISK
    rt.frozen = FrozenLevels(
        entry_bar=0,
        entry_timestamp="2026-01-13T22:10:00+00:00",
        entry_price=178.5,
        side="long",
        protected_low_5m=173.6,
        protected_high_5m=None,
        protected_low_1h=frozen_1h,
        protected_low_4h=None,
        major_5m_at_entry=1,
        h4_major_at_entry=-1,
    )
    rt.last_reclaim_level = 170.86
    rt.ever_broken = True
    rt.break_cycle_id = 2
    return rt


def test_reclaim_transitions_to_structure_at_risk_not_intact() -> None:
    rt = _base_rt()
    rt.state = ScannerState.RECLAIMED
    rt.last_reclaim_level = 170.86
    rt.first_5m_frozen_break_bar = 1  # already warned earlier; do not re-enter WARNING
    step_monitor(
        rt,
        bar_i=100,
        row_5m={
            "timestamp": "2026-01-16T08:00:00+00:00",
            "decision_time": "2026-01-16T08:05:00+00:00",
            "close": 172.0,
            "major_direction": -1,
            "protected_low": None,
        },
        h1=None,
        h4={
            "index": 10,
            "close": 172.0,
            "protected_low": None,
            "close_decision": "2026-01-16T08:00:00+00:00",
        },
        prev_h4_idx=10,
    )
    assert rt.state == ScannerState.STRUCTURE_AT_RISK
    assert any(e["event"] == "STRUCTURE_AT_RISK" for e in rt.events)


def test_new_break_cycle_after_reclaim_via_rebreak_level() -> None:
    """Early reclaim must not permanently neutralize a later independent episode."""
    rt = _base_rt()
    # Episode arm: close back below last reclaim level; live PL NaN / no BOS edge
    step_monitor(
        rt,
        bar_i=200,
        row_5m={
            "timestamp": "2026-01-18T20:00:00+00:00",
            "decision_time": "2026-01-18T20:05:00+00:00",
            "close": 168.67,
            "major_direction": -1,
            "protected_low": None,
        },
        h1=None,
        h4={
            "index": 20,
            "close": 168.67,
            "protected_low": None,
            "close_decision": "2026-01-19T00:00:00+00:00",
            "external_bos_down": False,
            "close_break_protected_down": False,
        },
        prev_h4_idx=19,
    )
    assert rt.state == ScannerState.BREAK_PENDING
    assert rt.break_cycle_id == 3
    assert rt.break_kind == "rebreak_last_reclaim_level"
    assert rt.active_break_level == 170.86

    # Next 4h still below → invalidate
    step_monitor(
        rt,
        bar_i=201,
        row_5m={
            "timestamp": "2026-01-19T00:00:00+00:00",
            "decision_time": "2026-01-19T00:05:00+00:00",
            "close": 163.41,
            "major_direction": -1,
            "protected_low": None,
        },
        h1=None,
        h4={
            "index": 21,
            "close": 163.41,
            "protected_low": None,
            "close_decision": "2026-01-19T04:00:00+00:00",
            "external_bos_down": False,
            "close_break_protected_down": False,
        },
        prev_h4_idx=20,
    )
    assert rt.state == ScannerState.LONG_THESIS_INVALIDATED
    assert rt.invalidated_ts == "2026-01-19T04:00:00+00:00"


def test_frozen_1h_floor_arms_when_live_pl_nan() -> None:
    rt = _base_rt(frozen_1h=160.64)
    rt.last_reclaim_level = None  # only frozen floor
    step_monitor(
        rt,
        bar_i=300,
        row_5m={
            "timestamp": "2026-01-20T08:00:00+00:00",
            "decision_time": "2026-01-20T08:05:00+00:00",
            "close": 159.44,
            "major_direction": -1,
            "protected_low": None,
        },
        h1=None,
        h4={
            "index": 30,
            "close": 159.44,
            "protected_low": None,
            "close_decision": "2026-01-20T12:00:00+00:00",
            "external_bos_down": False,
            "close_break_protected_down": False,
        },
        prev_h4_idx=29,
    )
    assert rt.state == ScannerState.BREAK_PENDING
    assert rt.break_kind == "frozen_entry_protected_low_1h"
    assert rt.active_break_level == 160.64

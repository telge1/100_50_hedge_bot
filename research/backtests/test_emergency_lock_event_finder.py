"""Tests for Emergency-Lock Phase C event finder."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.backtests.emergency_lock.config import EmergencyLockRecoveryConfig
from research.backtests.emergency_lock.event_finder import (
    SELECTION_TYPE,
    dedupe_crash_candidates,
    drop_bucket,
    find_crash_events,
    find_raw_crash_candidates,
)
from research.backtests.emergency_lock.phase_c_runner import phase_b_baseline_config


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).replace(
        hour=(i * 5) // 60, minute=(i * 5) % 60
    )


def _c(i: int, *, h: float, l: float, c: float | None = None) -> dict:
    close = c if c is not None else (h + l) / 2.0
    return {
        "timestamp": _ts(i),
        "open": close,
        "high": h,
        "low": l,
        "close": close,
        "volume": 1.0,
    }


def _cfg(**kwargs) -> EmergencyLockRecoveryConfig:
    base = phase_b_baseline_config()
    data = {
        "event_peak_lookback_bars": 3,
        "event_max_drop_bars": 20,
        "event_post_low_bars": 5,
        "event_cooldown_bars": 5,
        "event_min_separation_bars": 3,
        "event_pre_peak_bars": 0,
    }
    data.update(kwargs)
    for k, v in data.items():
        setattr(base, k, v)
    return base


def test_10_pct_event_detected() -> None:
    # Peak high=100 then low=90 → 10%
    rows = [_c(i, h=100 - i * 0.1, l=99 - i * 0.1) for i in range(5)]
    rows.append(_c(5, h=100.0, l=99.0, c=99.5))  # peak
    for i in range(6, 12):
        rows.append(_c(i, h=95.0, l=90.0, c=91.0))
    result = find_crash_events(rows, _cfg())
    assert len(result.events) >= 1
    ev = max(result.events, key=lambda e: e.max_drop_pct)
    assert ev.max_drop_pct == pytest.approx(0.10, abs=1e-9)
    assert ev.qualified_10_pct is True
    assert ev.selection_type == SELECTION_TYPE


def test_flags_12_5_and_15() -> None:
    rows = [_c(0, h=100.0, l=99.0, c=99.5)]
    rows += [_c(1, h=90.0, l=85.0, c=86.0)]  # 15% from 100
    rows += [_c(i, h=86.0, l=85.0) for i in range(2, 8)]
    evs = find_crash_events(rows, _cfg(event_peak_lookback_bars=0))
    assert any(e.qualified_15_pct for e in evs.events)
    assert any(e.qualified_12_5_pct for e in evs.events)


def test_no_event_below_threshold() -> None:
    rows = [_c(0, h=100.0, l=99.0)]
    rows += [_c(i, h=97.0, l=96.0) for i in range(1, 10)]  # 4% drop
    raw = find_raw_crash_candidates(rows, _cfg(event_peak_lookback_bars=0))
    assert raw == []


def test_peak_to_low_calculation() -> None:
    rows = [_c(0, h=200.0, l=199.0, c=199.5)]
    rows += [_c(i, h=180.0, l=170.0, c=171.0) for i in range(1, 5)]
    raw = find_raw_crash_candidates(rows, _cfg(event_peak_lookback_bars=0))
    assert len(raw) == 1
    assert raw[0].peak_price == pytest.approx(200.0)
    assert raw[0].low_price == pytest.approx(170.0)
    assert raw[0].max_drop_pct == pytest.approx(0.15)
    assert raw[0].bars_peak_to_low == 1


def test_event_window() -> None:
    rows = [_c(i, h=100.0 if i == 0 else 90.0, l=99.0 if i == 0 else 90.0) for i in range(20)]
    # force deeper low
    rows[3] = _c(3, h=90.0, l=85.0, c=86.0)
    cfg = _cfg(event_peak_lookback_bars=0, event_post_low_bars=4)
    raw = find_raw_crash_candidates(rows, cfg)
    assert raw
    ev = raw[0]
    assert ev.simulation_start_index == ev.peak_index
    assert ev.simulation_end_index == min(ev.low_index + 4, len(rows) - 1)


def test_dedupe_prefers_larger_drawdown() -> None:
    # Two overlapping peaks into same low; larger drop wins.
    rows = []
    for i in range(0, 3):
        rows.append(_c(i, h=100.0 + i, l=99.0))
    # higher peak
    rows.append(_c(3, h=110.0, l=109.0, c=109.5))
    rows.append(_c(4, h=105.0, l=104.0, c=104.5))  # secondary peak
    for i in range(5, 12):
        rows.append(_c(i, h=95.0, l=88.0, c=89.0))  # deep low shared
    cfg = _cfg(
        event_peak_lookback_bars=2,
        event_cooldown_bars=20,
        event_min_separation_bars=1,
        event_max_drop_bars=20,
    )
    result = find_crash_events(rows, cfg)
    assert len(result.events) == 1
    assert result.events[0].peak_price == pytest.approx(110.0)
    assert any(not c.kept for c in result.raw_candidates)


def test_cooldown_and_no_double_count() -> None:
    rows = [_c(0, h=100.0, l=99.0)]
    rows += [_c(i, h=92.0, l=90.0) for i in range(1, 6)]
    rows.append(_c(6, h=100.0, l=99.0))
    rows += [_c(i, h=92.0, l=90.0) for i in range(7, 12)]
    cfg = _cfg(
        event_peak_lookback_bars=0,
        event_cooldown_bars=100,
        event_min_separation_bars=100,
    )
    result = find_crash_events(rows, cfg)
    assert len(result.events) == 1


def test_drop_bucket_labels() -> None:
    assert drop_bucket(0.11) == "10–12.5%"
    assert drop_bucket(0.13) == "12.5–15%"
    assert drop_bucket(0.20) == ">=15%"


def test_data_end_truncation_flag() -> None:
    rows = [_c(0, h=100.0, l=99.0)]
    rows += [_c(1, h=90.0, l=85.0)]
    cfg = _cfg(event_peak_lookback_bars=0, event_post_low_bars=1000)
    raw = find_raw_crash_candidates(rows, cfg)
    assert raw[0].window_truncated_at_data_end is True

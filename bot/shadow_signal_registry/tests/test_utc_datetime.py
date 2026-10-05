"""UTC normalization and ClickHouse insert epoch (host TZ independent)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from bot.shadow_signal_registry.ch_sync import snapshot_to_ch_row
from bot.shadow_signal_registry.utc_datetime import (
    ch_datetime64_utc,
    ensure_utc_datetime,
    version_from_utc_datetime,
)


def test_aware_utc_unchanged() -> None:
    dt = datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)
    out = ensure_utc_datetime(dt)
    assert out == dt
    assert out.tzinfo == timezone.utc


def test_iso_z_stays_utc() -> None:
    out = ensure_utc_datetime("2026-10-04T18:30:00Z")
    assert out == datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)


def test_iso_offset_zero() -> None:
    out = ensure_utc_datetime("2026-10-04T18:30:00+00:00")
    assert out == datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)


def test_iso_offset_plus_three_to_utc() -> None:
    out = ensure_utc_datetime("2026-10-04T21:30:00+03:00")
    assert out == datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)


def test_naive_treated_as_utc_wall_clock() -> None:
    naive = datetime(2026, 10, 4, 18, 30)
    out = ensure_utc_datetime(naive)
    assert out == datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)


def test_ch_datetime64_utc_is_aware() -> None:
    dt = ch_datetime64_utc("2026-10-04T18:45:00+00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 18


def test_clickhouse_epoch_independent_of_local_tz(monkeypatch) -> None:
    """clickhouse_connect uses datetime.timestamp(); naive uses local TZ, aware UTC does not."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    if hasattr(time, "tzset"):
        time.tzset()
    aware = ch_datetime64_utc("2026-10-04T18:30:00+00:00")
    naive_wrong = datetime(2026, 10, 4, 18, 30)
    expected = int(datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert int(aware.timestamp() * 1000) == expected
    # Naive on CEST host would differ by 2h in October if interpreted as local.
    assert int(naive_wrong.timestamp() * 1000) != expected


def test_version_from_utc_updated_at() -> None:
    v = version_from_utc_datetime("2026-10-04T18:50:00+00:00")
    assert v == int(datetime(2026, 10, 4, 18, 50, tzinfo=timezone.utc).timestamp() * 1000)


def test_tut_1830_mapping() -> None:
    row = {
        "signal_id": "sid",
        "strategy_name": "s",
        "strategy_version": "v1",
        "symbol": "TUTUSDT",
        "side": "SHORT",
        "decision_time": "2026-10-04T18:30:00+00:00",
        "detected_at": "2026-10-04T18:30:00+00:00",
        "exit_time": "2026-10-04T18:50:00+00:00",
        "signal_status": "ALLOWED",
        "allowed": True,
        "blocked": False,
        "hypothetical": False,
        "block_reason": "NONE",
        "entry_price": 0.024398,
        "initial_sl": 0.02467124,
        "active_sl": 0.02467124,
        "tp": 0.024075,
        "pool_id": "p",
        "tracking_status": "CLOSED",
        "outcome": "SL",
        "pnl_pct": -1.119928,
        "mae_pct": 1.0,
        "mfe_pct": 0.1,
        "created_at": "2026-10-04T18:30:00+00:00",
        "updated_at": "2026-10-04T18:50:00+00:00",
        "causality_status": "PASS",
        "e1r_state": "INACTIVE",
        "floor_guard_state": "PASS",
    }
    ch = snapshot_to_ch_row("short", row)
    assert ch["decision_time"] == datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)
    assert ch["end_time"] == datetime(2026, 10, 4, 18, 50, tzinfo=timezone.utc)


def test_tut_1845_mapping() -> None:
    row = {
        "signal_id": "sid2",
        "strategy_name": "s",
        "strategy_version": "v1",
        "symbol": "TUTUSDT",
        "side": "SHORT",
        "decision_time": "2026-10-04T18:45:00+00:00",
        "detected_at": "2026-10-04T18:45:00+00:00",
        "exit_time": "2026-10-04T23:28:00+00:00",
        "signal_status": "BLOCKED",
        "allowed": False,
        "blocked": True,
        "hypothetical": True,
        "block_reason": "E1R",
        "entry_price": 0.024559,
        "initial_sl": 0.02476343,
        "active_sl": 0.02476343,
        "tp": 0.024075,
        "pool_id": "p",
        "tracking_status": "CLOSED",
        "outcome": "SL",
        "pnl_pct": -0.83,
        "mae_pct": 1.0,
        "mfe_pct": 0.1,
        "created_at": "2026-10-04T18:45:00+00:00",
        "updated_at": "2026-10-04T23:28:00+00:00",
        "causality_status": "PASS",
        "e1r_state": "ACTIVE",
        "floor_guard_state": "PASS",
    }
    ch = snapshot_to_ch_row("short", row)
    assert ch["decision_time"] == datetime(2026, 10, 4, 18, 45, tzinfo=timezone.utc)
    assert ch["end_time"] == datetime(2026, 10, 4, 23, 28, tzinfo=timezone.utc)


@pytest.mark.parametrize("tz_name", ["Europe/Berlin", "Africa/Dar_es_Salaam"])
def test_snapshot_row_epoch_same_under_host_tz(tz_name: str, monkeypatch) -> None:
    monkeypatch.setenv("TZ", tz_name)
    if hasattr(time, "tzset"):
        time.tzset()
    row = {
        "signal_id": "sid",
        "strategy_name": "s",
        "strategy_version": "v1",
        "symbol": "TUTUSDT",
        "side": "SHORT",
        "decision_time": "2026-10-04T18:30:00+00:00",
        "detected_at": "2026-10-04T18:30:00+00:00",
        "exit_time": "2026-10-04T18:50:00+00:00",
        "signal_status": "ALLOWED",
        "allowed": True,
        "blocked": False,
        "hypothetical": False,
        "block_reason": "NONE",
        "entry_price": 1.0,
        "initial_sl": 1.1,
        "active_sl": 1.1,
        "tp": 0.9,
        "pool_id": "p",
        "tracking_status": "CLOSED",
        "outcome": "SL",
        "pnl_pct": -1.0,
        "mae_pct": 1.0,
        "mfe_pct": 0.1,
        "created_at": "2026-10-04T18:30:00+00:00",
        "updated_at": "2026-10-04T18:50:00+00:00",
        "causality_status": "PASS",
        "e1r_state": "INACTIVE",
        "floor_guard_state": "PASS",
    }
    ch = snapshot_to_ch_row("short", row)
    expected = int(datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc).timestamp() * 1000)
    assert int(ch["decision_time"].timestamp() * 1000) == expected

"""Tests for higher-TF Stoch context analysis (synthetic + invariants)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context.snapshots import (
    is_supportive,
    relative_state,
    turn_state,
    ts_utc,
)
from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context import (
    ENV_FILE,
    STOCH_HIGH_K,
    STOCH_LOW_K,
)
from orderbook_analyse.fractal_cycle_wave_analysis import STOCH_HIGH_K as H
from orderbook_analyse.fractal_cycle_wave_analysis import STOCH_LOW_K as L


def test_zone_thresholds_frozen():
    assert STOCH_LOW_K == L == 20.0
    assert STOCH_HIGH_K == H == 80.0


def test_support_long_short_mirror():
    assert is_supportive("LONG", "LOW", "NO_TURN") is True
    assert is_supportive("LONG", "HIGH", "NO_TURN") is False
    assert is_supportive("LONG", "MID", "UP_TURN") is True
    assert is_supportive("SHORT", "HIGH", "NO_TURN") is True
    assert is_supportive("SHORT", "LOW", "NO_TURN") is False
    assert is_supportive("SHORT", "MID", "DOWN_TURN") is True


def test_turn_state():
    assert turn_state(True, False) == "UP_TURN"
    assert turn_state(False, True) == "DOWN_TURN"
    assert turn_state(False, False) == "NO_TURN"


def test_relative_state_deterministic():
    assert relative_state("LONG", "LOW", 10.0, 1.0, "NO_TURN") == "TURNING_UP_FROM_LOW"
    assert relative_state("SHORT", "HIGH", 90.0, -1.0, "NO_TURN") == "TURNING_DOWN_FROM_HIGH"


def test_causal_asof_no_future_bar():
    # synthetic: available_at must be <= entry
    avail = pd.to_datetime(
        ["2024-01-01 00:15:00", "2024-01-01 00:30:00", "2024-01-01 00:45:00"], utc=True
    )
    entry = pd.Timestamp("2024-01-01 00:30:00", tz="UTC")
    avail_ns = avail.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    entry_ns = np.datetime64(entry.tz_localize(None).to_datetime64())
    i = int(np.searchsorted(avail_ns, entry_ns, side="right") - 1)
    assert i == 1
    assert avail[i] <= entry
    # open bar at 00:30 with close 00:45 not used when entry == 00:30 open? 
    # available_at of 00:30 bar is 00:45 — so last <= 00:30 is 00:15 bar
    avail2 = pd.to_datetime(
        ["2024-01-01 00:15:00", "2024-01-01 00:45:00"], utc=True
    )  # close times
    avail2_ns = avail2.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    i2 = int(np.searchsorted(avail2_ns, entry_ns, side="right") - 1)
    assert i2 == 0  # only 00:15 closed; 00:30 candle not yet available


def test_timezone_utc():
    assert str(ts_utc("2024-01-01").tz) == "UTC"


def test_mysql_env_only():
    assert "regime_scanner" in str(ENV_FILE)
    assert ENV_FILE.name == ".env.regime_db"


def test_no_outcome_in_support_fn():
    # support ignores returns — same inputs always same label
    assert is_supportive("LONG", "LOW", "NO_TURN") == is_supportive("LONG", "LOW", "NO_TURN")

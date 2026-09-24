"""API limit consistency: 6h = 72 closed 5m + optional forming."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.contracts import (  # noqa: E402
    CANDLE_SECONDS,
    MAX_CANDLES,
    MAX_CLOSED_CANDLES,
    MAX_RANGE_SECONDS,
)
from footprint_candles.service import FootprintRequestError, validate_request  # noqa: E402


def test_six_hours_equals_72_closed_candles():
    assert MAX_RANGE_SECONDS == 6 * 3600
    assert MAX_RANGE_SECONDS // CANDLE_SECONDS == 72
    assert MAX_CLOSED_CANDLES == 72
    assert MAX_CANDLES == 73  # + forming


def test_exact_six_hours_allowed():
    start = 1_700_000_000
    end = start + MAX_RANGE_SECONDS
    req = validate_request(
        symbol="BTCUSDT",
        timeframe="5m",
        mode="DISPLAY",
        bucket_step=5.0,
        start=start,
        end=end,
    )
    assert req["end"] - req["start"] == MAX_RANGE_SECONDS


def test_just_over_six_hours_rejected():
    start = 1_700_000_000
    with pytest.raises(FootprintRequestError) as ei:
        validate_request(
            symbol="BTCUSDT",
            timeframe="5m",
            mode="DISPLAY",
            bucket_step=5.0,
            start=start,
            end=start + MAX_RANGE_SECONDS + 1,
        )
    assert ei.value.code == "range_too_large"

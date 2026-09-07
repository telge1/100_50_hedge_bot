"""Contract helpers and constants."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.contracts import (  # noqa: E402
    BUCKET_STEP,
    IMBALANCE_RATIO,
    MIN_COMPARE_SIZE,
    STACKED_MIN_LEVELS,
    SUPPORTED_MODE,
    SUPPORTED_SYMBOL,
    SUPPORTED_TIMEFRAME,
    bucket_bounds,
    bucket_index_for_price,
    candle_start_unix,
)


def test_mvp_locks():
    assert SUPPORTED_SYMBOL == "BTCUSDT"
    assert SUPPORTED_TIMEFRAME == "5m"
    assert SUPPORTED_MODE == "DISPLAY"
    assert BUCKET_STEP == Decimal("5")
    assert IMBALANCE_RATIO == 3.0
    assert MIN_COMPARE_SIZE == 0.01
    assert STACKED_MIN_LEVELS == 3


def test_bucket_boundaries_and_on_edge():
    assert bucket_index_for_price("100000") == 20000
    assert bucket_index_for_price(100000) == 20000
    assert bucket_index_for_price(Decimal("100000")) == 20000
    # Exact boundary belongs to the higher bucket's low == this index's high of lower
    assert bucket_index_for_price("100005") == 20001
    assert bucket_index_for_price("100004.999999") == 20000
    lo, hi = bucket_bounds(20000)
    assert lo == 100000.0
    assert hi == 100005.0


def test_decimal_stability_near_float_noise():
    # Values that are awkward in binary float still floor correctly via Decimal.
    assert bucket_index_for_price("110.1") == 22  # 110.1 / 5 = 22.02 → 22
    assert bucket_index_for_price("109.999999999") == 21
    assert bucket_index_for_price("110") == 22


def test_candle_start_half_open_alignment():
    # 2026-09-06 12:00:00Z
    t = 1757156400
    assert candle_start_unix(t) == t
    assert candle_start_unix(t + 299) == t
    assert candle_start_unix(t + 300) == t + 300

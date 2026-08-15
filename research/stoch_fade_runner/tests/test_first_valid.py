from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from research.stoch_fade_runner.first_valid import (
    FROZEN_INDICATOR_FIELDS,
    first_valid_from_indicators,
)


def test_first_valid_is_not_first_htf_bar() -> None:
    rows = []
    start = datetime(2025, 10, 1, tzinfo=timezone.utc)
    for i in range(5):
        rec = {
            "timestamp": start + pd.Timedelta(hours=4 * i),
            "rsi": float("nan") if i < 3 else 50.0,
            "stoch_k": float("nan") if i < 3 else 20.0,
            "stoch_d": float("nan") if i < 3 else 18.0,
            "cci": float("nan") if i < 3 else 10.0,
            "ema9": 1.0,
            "ema20": 1.0,
            "ema100": 1.0,
            "ema400": float("nan") if i < 4 else 1.0,
        }
        rows.append(rec)
    df = pd.DataFrame(rows)
    meta = first_valid_from_indicators(df, signal_start=datetime(2025, 12, 11, tzinfo=timezone.utc))
    assert meta["first_htf_bar_at"] == "2025-10-01T00:00:00Z"
    assert meta["first_stoch_valid_at"] == "2025-10-01T12:00:00Z"
    assert meta["first_ema400_valid_at"] == "2025-10-01T16:00:00Z"
    assert meta["first_indicator_valid_at"] == "2025-10-01T16:00:00Z"
    assert meta["first_indicator_valid_at"] != meta["first_htf_bar_at"]
    assert set(FROZEN_INDICATOR_FIELDS) == {
        "rsi",
        "stoch_k",
        "stoch_d",
        "cci",
        "ema9",
        "ema20",
        "ema100",
        "ema400",
    }

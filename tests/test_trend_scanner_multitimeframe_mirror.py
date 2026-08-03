"""Mirror / field-symmetry tests for multi-TF structure outputs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from orderbook_analyse.trend_scanner_multitimeframe import (
    MIRROR_FIELD_TABLE,
    aggregate_ohlcv_from_5m,
    run_mirror_parity_audit,
    run_structure_for_timeframe,
)


def _synth_5m(n: int) -> pd.DataFrame:
    start = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    price = 10.0
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        c = price + (0.02 if i % 3 else -0.01)
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": max(price, c) + 0.05,
                "low": min(price, c) - 0.05,
                "close": c,
                "volume": 5.0,
            }
        )
        price = c
    return pd.DataFrame(rows)


def test_pl_ph_and_break_flags_present_each_tf() -> None:
    df = _synth_5m(12 * 24)  # 24h of 5m → enough for 1h and some 4h
    structures = {}
    for tf in ("5m", "1h", "4h"):
        agg = aggregate_ohlcv_from_5m(df, tf, require_complete=True)
        assert len(agg) > 0
        structures[tf] = run_structure_for_timeframe(
            agg, timeframe=tf, symbol="MIRROR", warmup_bars=5
        )
        s = structures[tf]
        assert "protected_low" in s.columns
        assert "protected_high" in s.columns
        assert "close_break_protected_down" in s.columns
        assert "close_break_protected_up" in s.columns
        assert "bearish_choch" in s.columns
        assert "bullish_choch" in s.columns

    audit = run_mirror_parity_audit(structures)
    assert audit["pass"] is True
    assert audit["mirror_field_table"] == MIRROR_FIELD_TABLE


def test_mirror_table_documented() -> None:
    assert MIRROR_FIELD_TABLE["protected_low"] == "protected_high"
    assert MIRROR_FIELD_TABLE["close_break_protected_down"] == "close_break_protected_up"
    assert MIRROR_FIELD_TABLE["bearish_choch"] == "bullish_choch"

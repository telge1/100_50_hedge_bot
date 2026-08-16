"""Unit tests for Signal dataclass validation (no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from signal_generator.db.signals import Signal


def test_direction_must_be_long_or_short():
    with pytest.raises(ValueError):
        Signal(
            symbol="BTCUSDT",
            timeframe="15m",
            direction="FLAT",
            signal_type="x",
            signal_price=1,
            candle_open_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            candle_close_time=datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc),
            generator_version="g",
            strategy_version="s",
        )


def test_signal_defaults_preserve_unselected_untraded():
    sig = Signal(
        symbol="DOGEUSDT",
        timeframe="15m",
        direction="SHORT",
        signal_type="wave_fade",
        signal_price=0.1,
        candle_open_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        candle_close_time=datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc),
        generator_version="g1",
        strategy_version="s1",
        selected=False,
        traded=False,
    )
    row = sig.as_row()
    # selected / traded column positions in SIGNAL_COLUMNS
    assert row[15] == 0  # selected
    assert row[22] == 0  # traded
    assert row[6] == "SHORT"

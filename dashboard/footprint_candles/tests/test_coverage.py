"""Coverage status rules."""

from __future__ import annotations

import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.contracts import CoverageStatus  # noqa: E402
from footprint_candles.coverage import (  # noqa: E402
    CandleTradePresence,
    allows_imbalance_highlight,
    classify_candle,
    classify_window,
    gap_sep5_sep6_unix,
    overlaps_known_gap,
)


def test_complete_requires_positive_proof():
    assert (
        classify_candle(has_ohlc=True, trade_count=1000, sources=("live",))
        == CoverageStatus.UNKNOWN
    )
    assert (
        classify_candle(
            has_ohlc=True, trade_count=1000, sources=("live",), positive_complete=True
        )
        == CoverageStatus.COMPLETE
    )


def test_missing_when_ohlc_without_trades():
    assert (
        classify_candle(has_ohlc=True, trade_count=0) == CoverageStatus.MISSING
    )


def test_archive_is_partial():
    assert (
        classify_candle(has_ohlc=True, trade_count=10, sources=("archive",))
        == CoverageStatus.PARTIAL
    )


def test_window_rollup_and_gap_fixture():
    g0, g1 = gap_sep5_sep6_unix()
    assert g0 < g1
    # Candle inside gap hours
    mid = g0 + 3600
    assert overlaps_known_gap(mid, mid + 300)

    candles = [
        CandleTradePresence(time=1, has_ohlc=True, trade_count=0, sources=()),
        CandleTradePresence(time=2, has_ohlc=True, trade_count=5, sources=("live",)),
    ]
    assert classify_window(candles) == CoverageStatus.PARTIAL

    all_missing = [
        CandleTradePresence(time=1, has_ohlc=True, trade_count=0),
        CandleTradePresence(time=2, has_ohlc=True, trade_count=0),
    ]
    assert classify_window(all_missing) == CoverageStatus.MISSING


def test_imbalance_highlight_gate():
    assert allows_imbalance_highlight(CoverageStatus.COMPLETE) is True
    assert allows_imbalance_highlight("PARTIAL") is False
    assert allows_imbalance_highlight("UNKNOWN") is False
    assert allows_imbalance_highlight("MISSING") is False

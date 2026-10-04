"""Live poll / refresh / exactly-once tests (mocked candles)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.e1r_live_scanner.config import ScannerConfig
from bot.e1r_live_scanner.live_refresh import (
    missed_15m_closes,
    new_bars_after,
    scan_new_bar_metrics,
)
from bot.e1r_live_scanner.processing import SymbolProcessor
from bot.e1r_live_scanner.state import Readiness, SymbolState


def _bar(close_time: datetime, idx: int) -> dict:
    return {
        "open_time": close_time,
        "close_time": close_time,
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.0,
        "ema9": 1.0,
        "ema20": 1.0,
        "ema59": 1.0,
        "ema200": 1.0,
        "candle_index": idx,
    }


def _make_st(bars: list[dict]) -> SymbolState:
    st = SymbolState(symbol="XRPUSDT", readiness=Readiness.LIVE_READY)
    st.bars15 = bars
    st.candles15 = []
    st.by_open = {b["open_time"]: b for b in bars}
    st.market = {"4h": {"bars": bars}}
    st.lld_cfg = object()
    st.lld_cache = {}
    st.floor_guard = _NoopGuard()
    st.e1r_engine = _NoopE1R()
    st.last_processed_15m_close = bars[0]["close_time"] if bars else None
    return st


class _NoopGuard:
    def advance(self, bar, moment):
        return False, None


class _NoopE1R:
    e1r_state = "INACTIVE"

    def process_bar(self, *a, **k):
        return None

    def evaluate_signal(self, *a, **k):
        return {"e1r_state": "INACTIVE", "ignore": False, "status": "INACTIVE"}


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_no_new_bar_no_processing(mock_refresh, mock_process):
    t0 = datetime(2026, 10, 4, 12, 15, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
    st = _make_st([_bar(t0, 0), _bar(t1, 1)])
    st.last_processed_15m_close = t1
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 12, 31, tzinfo=timezone.utc))
    assert res.bars_processed == 0
    assert res.catchup_bars == 0
    assert res.duplicate_bars == 0
    assert res.out_of_order == 0
    assert res.missed_bars == 0
    mock_process.assert_not_called()


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_one_new_bar_one_step(mock_refresh, mock_process):
    t0 = datetime(2026, 10, 4, 12, 15, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 4, 12, 45, tzinfo=timezone.utc)
    st = _make_st([_bar(t0, 0), _bar(t1, 1), _bar(t2, 2)])
    st.last_processed_15m_close = t1
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc))
    assert res.bars_processed == 1
    assert res.catchup_bars == 1
    assert res.duplicate_bars == 0
    assert res.out_of_order == 0
    assert res.missed_bars == 0
    assert mock_process.call_count == 1


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_three_new_bars_chronological(mock_refresh, mock_process):
    bars = [
        _bar(datetime(2026, 10, 4, 12, 15, tzinfo=timezone.utc), 0),
        _bar(datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc), 1),
        _bar(datetime(2026, 10, 4, 12, 45, tzinfo=timezone.utc), 2),
        _bar(datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc), 3),
        _bar(datetime(2026, 10, 4, 13, 15, tzinfo=timezone.utc), 4),
    ]
    st = _make_st(bars)
    st.last_processed_15m_close = bars[1]["close_time"]
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 13, 20, tzinfo=timezone.utc))
    assert res.bars_processed == 3
    assert res.catchup_bars == 3
    assert res.duplicate_bars == 0
    assert res.out_of_order == 0
    assert res.missed_bars == 0
    assert mock_process.call_count == 3


@patch.object(SymbolProcessor, "_fetch_market_and_pane")
def test_waiting_on_non_strict(mock_fetch):
    mock_fetch.return_value = ({}, [], [], {"strict_complete_buckets": False}, False)
    st = SymbolState(symbol="XRPUSDT")
    st.readiness = Readiness.LIVE_READY
    proc = SymbolProcessor(ScannerConfig(live=True))
    proc.refresh_candles(st, datetime.now(timezone.utc))
    assert st.readiness == Readiness.WAITING_FOR_DATA


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_repeated_poll_no_duplicate(mock_refresh, mock_process):
    t1 = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
    st = _make_st([_bar(t1, 0)])
    st.last_processed_15m_close = t1
    proc = SymbolProcessor(ScannerConfig(live=True))
    proc.poll_symbol_live(st, datetime(2026, 10, 4, 12, 31, tzinfo=timezone.utc))
    proc.poll_symbol_live(st, datetime(2026, 10, 4, 12, 32, tzinfo=timezone.utc))
    mock_process.assert_not_called()


def test_lld_cache_invalidated_on_append():
    st = SymbolState(symbol="XRPUSDT")
    st.lld_cache["_ui_selected"] = ("pools", "tip")
    st.bars15 = [_bar(datetime(2026, 1, 1, tzinfo=timezone.utc), 0)]
    proc = SymbolProcessor(ScannerConfig(live=True))

    def fake_fetch(symbol, now=None):
        b2 = _bar(datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc), 1)
        return {"4h": {"bars": []}}, [], [st.bars15[0], b2], {"strict_complete_buckets": True}, True

    with patch.object(proc, "_fetch_market_and_pane", side_effect=fake_fetch):
        proc.refresh_candles(st, datetime.now(timezone.utc))
    assert "_ui_selected" not in st.lld_cache
    assert len(st.bars15) == 2


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_metrics_large_history_one_new_bar(mock_refresh, mock_process):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(minutes=15 * i), i) for i in range(20_001)]
    st = _make_st(bars)
    st.last_processed_15m_close = bars[19_999]["close_time"]
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc))
    assert res.catchup_bars == 1
    assert res.duplicate_bars == 0
    assert res.out_of_order == 0
    assert res.missed_bars == 0
    assert mock_process.call_count == 1


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_metrics_missed_gap(mock_refresh, mock_process):
    t0 = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)
    st = _make_st([_bar(t0, 0), _bar(t2, 1)])
    st.last_processed_15m_close = t0
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 14, 35, tzinfo=timezone.utc))
    assert res.missed_bars == 1
    assert res.catchup_bars == 1
    assert res.duplicate_bars == 0
    assert res.out_of_order == 0


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_metrics_duplicate_in_new_bars(mock_refresh, mock_process):
    t0 = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 4, 14, 15, tzinfo=timezone.utc)
    dup = _bar(t1, 2)
    st = _make_st([_bar(t0, 0), _bar(t1, 1), dup])
    st.last_processed_15m_close = t0
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 14, 20, tzinfo=timezone.utc))
    assert res.duplicate_bars == 1
    assert res.catchup_bars == 1
    assert mock_process.call_count == 1


@patch.object(SymbolProcessor, "_process_one_bar", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_metrics_out_of_order_in_new_bars(mock_refresh, mock_process):
    t0 = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 4, 14, 15, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)
    st = _make_st([_bar(t0, 0), _bar(t2, 1), _bar(t1, 2)])
    st.last_processed_15m_close = t0
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 14, 35, tzinfo=timezone.utc))
    assert res.out_of_order == 1
    assert res.catchup_bars == 1
    assert res.missed_bars == 0
    assert mock_process.call_count == 1


def test_new_bars_helpers_missed_and_scan():
    t0 = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 4, 14, 15, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)
    bars = [_bar(t0, 0), _bar(t2, 1)]
    new = new_bars_after(bars, t0)
    assert len(new) == 1
    assert missed_15m_closes(t0, t2, new) == 1
    dup, oo = scan_new_bar_metrics([_bar(t1, 0), _bar(t1, 1)], t0)
    assert dup == 1
    assert oo == 0

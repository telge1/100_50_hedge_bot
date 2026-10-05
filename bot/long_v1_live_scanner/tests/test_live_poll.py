"""Poll metrics tests (mirrors short scanner invariants)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.long_v1_live_scanner.config import ScannerConfig
from bot.long_v1_live_scanner.live_refresh import new_bars_after, scan_new_bar_metrics
from bot.long_v1_live_scanner.processing import SymbolProcessor
from bot.long_v1_live_scanner.state import Readiness, SymbolState


def _bar(close_time: datetime, idx: int) -> dict:
    return {
        "open_time": close_time - timedelta(minutes=15),
        "close_time": close_time,
        "open": 1.0,
        "high": 1.1,
        "low": 0.9,
        "close": 1.0,
        "candle_index": idx,
    }


def _make_st(bars: list[dict]) -> SymbolState:
    st = SymbolState(symbol="XRPUSDT", readiness=Readiness.LIVE_READY)
    st.bars15 = bars
    st.candles15 = []
    st.by_open = {b["open_time"]: b for b in bars}
    st.lld_cfg = object()
    st.lld_cache = {}
    st.last_processed_15m_close = bars[0]["close_time"] if bars else None
    return st


@patch.object(SymbolProcessor, "_process_one_15m", return_value=[])
@patch.object(SymbolProcessor, "_process_management", return_value=[])
@patch.object(SymbolProcessor, "refresh_candles")
def test_no_new_bar_no_15m_step(mock_refresh, mock_mgmt, mock_process):
    t1 = datetime(2026, 10, 4, 12, 30, tzinfo=timezone.utc)
    st = _make_st([_bar(t1, 1)])
    st.last_processed_15m_close = t1
    proc = SymbolProcessor(ScannerConfig(live=True))
    res = proc.poll_symbol_live(st, datetime(2026, 10, 4, 12, 31, tzinfo=timezone.utc))
    assert res.bars_processed == 0
    assert res.duplicate_bars == 0
    mock_process.assert_not_called()

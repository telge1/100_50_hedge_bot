"""MySQL data loading for paper runner."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.fractal_wave_fade_forward_paper import ENV_FILE, STALE_AGE_MINUTES
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import SymbolBooks
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def ensure_env() -> None:
    load_env_file(ENV_FILE)


def load_books(symbol: str) -> SymbolBooks:
    c1 = load_mysql_ohlcv_tf(symbol=symbol, timeframe="1m", env_file=ENV_FILE)
    return SymbolBooks(
        high=c1["high"].astype(float).to_numpy(),
        low=c1["low"].astype(float).to_numpy(),
        close=c1["close"].astype(float).to_numpy(),
        opens=c1["open"].astype(float).to_numpy(),
        open_times=c1["timestamp"].to_numpy(dtype="datetime64[ns]"),
    )


def latest_1m_ts(books: SymbolBooks) -> pd.Timestamp:
    return pd.Timestamp(books.open_times[-1], tz="UTC")


def freshness(books: SymbolBooks, *, now: pd.Timestamp | None = None) -> dict[str, Any]:
    now = now or pd.Timestamp.now(tz="UTC")
    latest = latest_1m_ts(books)
    age = (now - latest).total_seconds() / 60.0
    return {
        "latest_db_ts": latest.isoformat(),
        "current_utc": now.isoformat(),
        "age_minutes": age,
        "stale": age > STALE_AGE_MINUTES,
    }


def load_signals(symbol: str, books: SymbolBooks, edges: dict | None = None) -> pd.DataFrame:
    if edges is None:
        edges = frozen_eff_edges_all_signal_tfs()
    sig = build_symbol_signals(symbol, edges)
    sig = resolve_entries(sig, books.open_times, books.opens)
    return sig[sig["entry_valid"]].copy()


def btc_forward_available(paper_start: pd.Timestamp) -> tuple[bool, str]:
    """BTC only if 1m coverage reaches paper_start."""
    books = load_books("BTCUSDT")
    latest = latest_1m_ts(books)
    if latest < paper_start:
        return False, "BTC_FORWARD_COVERAGE_UNAVAILABLE"
    return True, "BTC_FORWARD_DATA_READY"

"""Load / regenerate independent APT+DOGE trade lists (frozen engine)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db.signals import (
    build_symbol_signals,
    frozen_eff_edges_all_signal_tfs,
    resolve_entries,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db import (
    ENV_FILE,
    PRIMARY_FEE,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.analysis import (
    _normalize_old_trades,
)
from orderbook_analyse.fractal_wave_fade_multicoin_overlap import (
    INDEPENDENT_CACHE,
    REF_GLOBAL_SUMMARY,
    REF_GLOBAL_TRADES,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    run_symbol_backtest,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _books(symbol: str, end: pd.Timestamp) -> SymbolBooks:
    c1 = load_mysql_ohlcv_tf(symbol=symbol, timeframe="1m", env_file=ENV_FILE)
    ts = pd.to_datetime(c1["timestamp"], utc=True)
    c1 = c1.loc[ts <= end].reset_index(drop=True)
    return SymbolBooks(
        high=c1["high"].astype(float).to_numpy(),
        low=c1["low"].astype(float).to_numpy(),
        close=c1["close"].astype(float).to_numpy(),
        opens=c1["open"].astype(float).to_numpy(),
        open_times=c1["timestamp"].to_numpy(dtype="datetime64[ns]"),
    )


def load_common_window() -> tuple[pd.Timestamp, pd.Timestamp]:
    s = json.loads(Path(REF_GLOBAL_SUMMARY).read_text())
    return pd.Timestamp(s["common_start"]), pd.Timestamp(s["common_end"])


def load_global_trades() -> pd.DataFrame:
    t = pd.read_csv(REF_GLOBAL_TRADES)
    for c in ("entry_time", "exit_time", "signal_time"):
        if c in t.columns:
            t[c] = pd.to_datetime(t[c], utc=True)
    return t.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)


def document_sources(independent: pd.DataFrame, global_df: pd.DataFrame) -> dict[str, Any]:
    return {
        "independent_trades_file": str(INDEPENDENT_CACHE),
        "independent_n": int(len(independent)),
        "independent_columns": list(independent.columns),
        "global_trades_file": str(REF_GLOBAL_TRADES),
        "global_n": int(len(global_df)),
        "global_columns": list(global_df.columns),
        "timezone": "UTC",
        "entry_definition": (
            "entry_time = T0 = first 1m open strictly after confirmation_available_at; "
            "interval [entry_time, exit_time)"
        ),
        "comparable_axes": True,
        "note": (
            "Independent trades regenerated with frozen run_symbol_backtest "
            "(OLD_PER_SYMBOL_MAX1). Not a new strategy."
        ),
    }


def build_independent_trades(
    *,
    cache_path: Path = INDEPENDENT_CACHE,
    force: bool = False,
) -> pd.DataFrame:
    if cache_path.exists() and not force:
        df = pd.read_csv(cache_path)
        for c in ("entry_time", "exit_time", "signal_time"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], utc=True)
        print(f"[data] loaded cached independent trades n={len(df)} from {cache_path}", flush=True)
        return df.sort_values(["entry_time", "symbol", "trade_id"]).reset_index(drop=True)

    load_env_file(ENV_FILE)
    common_start, common_end = load_common_window()
    print(f"[data] regenerating independent trades {common_start} → {common_end}", flush=True)
    edges = frozen_eff_edges_all_signal_tfs()
    raw: list[dict] = []
    for sym in SYMBOLS:
        print(f"[data] {sym} signals+1m+backtest …", flush=True)
        sig = build_symbol_signals(sym, edges)
        books = _books(sym, common_end)
        sig = resolve_entries(sig, books.open_times, books.opens)
        sig = sig[sig["entry_valid"]].copy()
        et = pd.to_datetime(sig["entry_time"], utc=True)
        sig = sig[(et >= common_start) & (et <= common_end)].copy().reset_index(drop=True)
        res = run_symbol_backtest(
            sym,
            sig,
            books,
            tier_a_only=True,
            upgrade_policy="P5A",
            conflict_exit=True,
            fee_pct=PRIMARY_FEE,
            extra_4h=False,
        )
        raw.extend(res["trades"])
        print(f"  {sym} trades={len(res['trades'])}", flush=True)

    trades = pd.DataFrame(_normalize_old_trades(raw))
    # normalize column names to match global where useful
    trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True)
    trades["exit_time"] = pd.to_datetime(trades["exit_time"], utc=True)
    if "signal_time" in trades.columns:
        trades["signal_time"] = pd.to_datetime(trades["signal_time"], utc=True)
    trades["gross_return_pct"] = trades["gross_return_pct"].astype(float)
    trades["fee_pct"] = trades["fee_pct"].astype(float)
    trades["net_return_pct"] = trades["net_return_pct"].astype(float)
    trades["holding_minutes"] = trades["holding_minutes"].astype(float)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(cache_path, index=False)
    print(f"[data] wrote {cache_path} n={len(trades)}", flush=True)
    return trades.sort_values(["entry_time", "symbol", "trade_id"]).reset_index(drop=True)


def split_by_symbol(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in SYMBOLS:
        out[sym] = (
            trades[trades["symbol"] == sym]
            .sort_values(["entry_time", "trade_id"])
            .reset_index(drop=True)
        )
    return out

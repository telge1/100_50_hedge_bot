"""Build / load waves without changing segmentation logic."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.indicators import attach_indicators
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import (
    coverage_audit,
    load_mysql_ohlcv_tf,
)
from orderbook_analyse.fractal_cycle_wave_analysis.waves import segment_stoch_waves
from orderbook_analyse.fractal_all_wave_fade_generalization import TRADING_TFS


def symbol_coverage(symbol: str, timeframes: tuple[str, ...] = TRADING_TFS + ("1m",)) -> list[dict]:
    return coverage_audit(symbol=symbol, timeframes=timeframes)


def build_or_load_waves(
    symbol: str,
    *,
    cache_dir: Path,
    timeframes: tuple[str, ...] = TRADING_TFS,
    force: bool = False,
) -> dict[str, pd.DataFrame]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        path = cache_dir / f"waves_{tf}.csv"
        if path.exists() and not force:
            print(f"[waves] load cache {symbol} {tf}", flush=True)
            out[tf] = pd.read_csv(path)
            continue
        print(f"[waves] build {symbol} {tf}", flush=True)
        raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe=tf)
        if raw.empty:
            out[tf] = pd.DataFrame()
            out[tf].to_csv(path, index=False)
            continue
        ind = attach_indicators(raw)
        waves = segment_stoch_waves(ind)
        waves["symbol"] = symbol
        waves["timeframe"] = tf
        waves.to_csv(path, index=False)
        out[tf] = waves
        print(f"[waves] {symbol} {tf}: n={len(waves)}", flush=True)
    return out

"""Shared market-data load windows for frozen EDC strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from ....cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    fetch_candles_1m,
    fetch_liquidations,
    fetch_ob_1m,
    fetch_oi_1m,
    fetch_trades_1m,
)
from ....cluster_sweep_research.ema_features import attach_emas
from ...config import EMA_DUAL_CROSS_DEFAULTS
from ...ema_candidate import attach_atr
from .semantics import OUTCOME_PAD_HOURS, SOURCE_PAD_HOURS, WARMUP_PAD_DAYS


def _utc(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_strategy_market_data(
    client: Any,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Canonical pads: warm 5d, outcome 12h, source 2h (original XRP loaders)."""
    start_u, end_u = _utc(start), _utc(end)
    warm = timedelta(days=WARMUP_PAD_DAYS)
    out_pad = timedelta(hours=OUTCOME_PAD_HOURS)
    src_pad = timedelta(hours=SOURCE_PAD_HOURS)
    c1m = fetch_candles_1m(client, symbol, start_u - warm, end_u + out_pad)
    trades = fetch_trades_1m(client, symbol, start_u - src_pad, end_u + src_pad)
    ob = fetch_ob_1m(client, symbol, start_u - src_pad, end_u + src_pad)
    oi = fetch_oi_1m(client, symbol, start_u - src_pad, end_u + src_pad)
    liq = fetch_liquidations(client, symbol, start_u - src_pad, end_u + src_pad)
    return {
        "candles_1m": c1m,
        "trades": trades,
        "ob": ob,
        "oi": oi,
        "liq": liq,
        "pads": {
            "warmup_pad_days": WARMUP_PAD_DAYS,
            "outcome_pad_hours": OUTCOME_PAD_HOURS,
            "source_pad_hours": SOURCE_PAD_HOURS,
        },
    }


def prepare_tf_frames(candles_1m: pd.DataFrame, timeframes: tuple[str, ...] = ("5m", "15m", "30m")) -> dict[str, pd.DataFrame]:
    cfg = EMA_DUAL_CROSS_DEFAULTS
    out: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        df = aggregate_timeframe(candles_1m, tf)
        df = attach_emas(df, fast=cfg.ema_fast, medium=cfg.ema_medium, slow=cfg.ema_slow)
        df = attach_atr(df, cfg.atr_period)
        out[tf] = df
    return out

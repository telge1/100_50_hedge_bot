"""Approach-regime features from closed candles (causal, pre-touch)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    fetch_candles_1m,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

from orderbook_analyse.liquidity_location_pool_lifecycle.ema_context import attach_context


def _tf_minutes(tf: str) -> int:
    t = str(tf).lower()
    if t.endswith("m"):
        return int(t[:-1])
    if t.endswith("h"):
        return int(t[:-1]) * 60
    return 15


def classify_approach(
    *,
    ret_3: float,
    ret_6: float,
    speed_atr: float,
    side: str,
    ema20_slope: float | None,
    consec: int,
) -> str:
    """Map features to approach regime (mirrored for BID/ASK)."""
    # signed toward pool: BID = down move toward support; ASK = up toward resistance
    toward = -ret_6 if side == "BID" else ret_6
    away = -toward
    impulsive = abs(speed_atr) >= 0.75 or abs(ret_3) >= 0.004
    slow = abs(speed_atr) < 0.35 and abs(ret_6) < 0.003
    flat = abs(ret_6) < 0.0015 and abs(speed_atr) < 0.25

    if flat:
        return "flat_range"
    if toward > 0 and impulsive:
        return "impulsive_toward"
    if toward > 0 and slow:
        return "slow_toward"
    if toward > 0:
        return "toward_pool"
    if away > 0:
        return "away_from_pool"
    return "flat_range"


def enrich_approach(df: pd.DataFrame) -> pd.DataFrame:
    """Attach approach features using CH candles; fails soft if CH unavailable."""
    out = df.copy()
    out["ret_3"] = np.nan
    out["ret_6"] = np.nan
    out["ret_12"] = np.nan
    out["speed_atr"] = np.nan
    out["consec_dir_bars"] = np.nan
    out["approach_volume_ratio"] = np.nan
    out["vol_regime"] = "unknown"
    out["trend_to_pool"] = "unknown"
    out["approach_regime"] = "unknown"

    try:
        client = get_clickhouse_client()
    except Exception as exc:  # noqa: BLE001
        out.attrs["approach_error"] = str(exc)
        return out

    # window from data
    tmin = pd.to_datetime(out["known_at_ts"].min()) - timedelta(days=5)
    tmax = pd.to_datetime(out["known_at_ts"].max()) + timedelta(days=2)
    if tmin.tzinfo is None:
        tmin = tmin.tz_localize("UTC")
        tmax = tmax.tz_localize("UTC")

    cache: dict[tuple[str, str], pd.DataFrame] = {}
    atr_pct_cache: dict[tuple[str, str], float] = {}

    for (sym, tf), g in out.groupby(["symbol", "timeframe"]):
        key = (sym, tf)
        if key not in cache:
            df1 = fetch_candles_1m(client, sym, tmin.to_pydatetime(), tmax.to_pydatetime())
            bars = aggregate_timeframe(df1, tf)
            bars = attach_context(bars)
            cache[key] = bars
            if not bars.empty and bars["atr_14"].notna().any():
                atr_pct_cache[key] = float(bars["atr_14"].quantile(0.66))
            else:
                atr_pct_cache[key] = float("nan")

        bars = cache[key]
        if bars.empty:
            continue
        highs = bars["high"].to_numpy(float)
        lows = bars["low"].to_numpy(float)
        closes = bars["close"].to_numpy(float)
        vols = bars["volume"].to_numpy(float) if "volume" in bars.columns else np.ones(len(bars))
        atrs = bars["atr_14"].to_numpy(float)
        e20s = bars["ema_20"].to_numpy(float) if "ema_20" in bars.columns else np.full(len(bars), np.nan)

        for i, row in g.iterrows():
            # use first_approach_index if present else analysis_start_index / first_touch
            ai = row.get("first_approach_index")
            if pd.isna(ai):
                ai = row.get("first_touch_index")
            if pd.isna(ai):
                ai = row.get("analysis_start_index")
            if pd.isna(ai):
                continue
            ai = int(ai)
            if ai < 12 or ai >= len(bars):
                continue
            c0 = closes[ai]
            if c0 <= 0:
                continue
            ret3 = closes[ai] / closes[ai - 3] - 1.0
            ret6 = closes[ai] / closes[ai - 6] - 1.0
            ret12 = closes[ai] / closes[ai - 12] - 1.0
            atr = atrs[ai] if not np.isnan(atrs[ai]) else np.nan
            speed = (closes[ai] - closes[ai - 3]) / atr if atr and atr == atr and atr > 0 else np.nan
            # consecutive direction bars ending at ai-1
            consec = 0
            direction = 1 if closes[ai - 1] >= closes[ai - 2] else -1
            for k in range(1, 12):
                if ai - k - 1 < 0:
                    break
                d = 1 if closes[ai - k] >= closes[ai - k - 1] else -1
                if d != direction:
                    break
                consec += 1
            vol_ratio = float(np.mean(vols[ai - 3 : ai])) / float(np.mean(vols[ai - 12 : ai - 3]) + 1e-12)

            out.at[i, "ret_3"] = ret3
            out.at[i, "ret_6"] = ret6
            out.at[i, "ret_12"] = ret12
            out.at[i, "speed_atr"] = speed
            out.at[i, "consec_dir_bars"] = consec
            out.at[i, "approach_volume_ratio"] = vol_ratio

            thr = atr_pct_cache.get(key, np.nan)
            if atr == atr and thr == thr:
                out.at[i, "vol_regime"] = "high" if atr >= thr else "low"
            e20_slope = None
            if not np.isnan(e20s[ai]) and not np.isnan(e20s[ai - 1]):
                e20_slope = float(e20s[ai] - e20s[ai - 1])
            regime = classify_approach(
                ret_3=float(ret3),
                ret_6=float(ret6),
                speed_atr=float(speed) if speed == speed else 0.0,
                side=str(row["side"]),
                ema20_slope=e20_slope,
                consec=consec,
            )
            out.at[i, "approach_regime"] = regime
            toward = -ret6 if row["side"] == "BID" else ret6
            out.at[i, "trend_to_pool"] = (
                "toward" if toward > 0.0015 else ("away" if toward < -0.0015 else "flat")
            )

    return out

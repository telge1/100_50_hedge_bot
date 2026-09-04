"""Assemble anchored profiles from ClickHouse and mark naked POCs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    aggregate_timeframe,
    fetch_candles_1m,
)

from .contracts import MarketProfile, ProfileWindow, ShapeThresholds
from .loader import (
    densify_bins,
    fetch_volume_at_price,
    fetch_window_ohlc,
    resolve_price_step,
)
from .profile import compute_value_area, find_nodes
from .shape import classify_shape


def build_profile(
    client: Any,
    symbol: str,
    window: ProfileWindow,
    *,
    value_area_pct: float,
    target_bins: int,
    use_final: bool = True,
    thresholds: ShapeThresholds | None = None,
) -> MarketProfile | None:
    """Build one anchored profile, or ``None`` if the window has no data."""
    th = thresholds or ShapeThresholds()

    ohlc = fetch_window_ohlc(client, symbol, window.start, window.end)
    if ohlc is None:
        return None
    open_price, high, low, close_price = ohlc
    if high <= low:
        return None

    step = resolve_price_step(low, high, target_bins)
    raw_bins = fetch_volume_at_price(
        client, symbol, window.start, window.end, step, use_final=use_final
    )
    if not raw_bins:
        return None
    bins = densify_bins(raw_bins, step)

    value_area = compute_value_area(bins, value_area_pct)
    nodes = find_nodes(
        bins,
        hvn_factor=th.hvn_factor,
        lvn_factor=th.lvn_factor,
        min_separation_bins=th.node_min_separation_bins,
        single_print_frac=th.single_print_frac,
        poc_volume=value_area.poc_volume,
    )

    total_volume = sum(b.volume for b in bins)
    shape = classify_shape(
        value_area=value_area,
        nodes=nodes,
        price_low=low,
        price_high=high,
        open_price=open_price,
        close_price=close_price,
        total_volume=total_volume,
        bin_count=len(bins),
        bins=bins,
        thresholds=th,
    )

    return MarketProfile(
        symbol=symbol,
        window=window,
        price_step=step,
        price_low=low,
        price_high=high,
        open_price=open_price,
        close_price=close_price,
        total_volume=total_volume,
        buy_volume=sum(b.buy_volume for b in bins),
        sell_volume=sum(b.sell_volume for b in bins),
        trades=sum(b.trades for b in bins),
        notional=sum(b.notional for b in bins),
        bins=tuple(bins),
        value_area=value_area,
        nodes=nodes,
        shape=shape,
    )


def build_profiles(
    client: Any,
    symbol: str,
    windows: list[ProfileWindow],
    *,
    value_area_pct: float,
    target_bins: int,
    use_final: bool = True,
    thresholds: ShapeThresholds | None = None,
    progress: bool = False,
) -> list[MarketProfile]:
    out: list[MarketProfile] = []
    for i, w in enumerate(windows, start=1):
        if progress:
            print(f"  [{i}/{len(windows)}] {w.label} ...", flush=True)
        p = build_profile(
            client,
            symbol,
            w,
            value_area_pct=value_area_pct,
            target_bins=target_bins,
            use_final=use_final,
            thresholds=thresholds,
        )
        if p is None:
            if progress:
                print(f"      skipped ({w.window_id}: no data in window)", flush=True)
            continue
        out.append(p)
    return out


def mark_naked_pocs(
    profiles: list[MarketProfile], candles_1m: pd.DataFrame
) -> list[MarketProfile]:
    """Flag POCs that price has not traded back through since the window closed.

    A naked POC is unfinished business: the level was the fair price of its
    window and has not been retested, which makes it a candidate target rather
    than a support. Uses 1m candle high/low, so a wick counts as a revisit.
    """
    if not profiles:
        return []
    if candles_1m is None or candles_1m.empty:
        return list(profiles)

    df = candles_1m.sort_values("open_time")
    times = df["open_time"].to_numpy()
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    checked_until = pd.Timestamp(times[-1]).to_pydatetime()

    out: list[MarketProfile] = []
    for p in profiles:
        end = pd.Timestamp(p.window.end)
        if end.tzinfo is not None:
            end = end.tz_convert("UTC").tz_localize(None)
        after = times >= end.to_datetime64()
        poc = p.value_area.poc
        hit = after & (lows <= poc) & (highs >= poc)
        idx = hit.argmax() if hit.any() else None
        revisit = pd.Timestamp(times[idx]).to_pydatetime() if idx is not None else None
        out.append(
            MarketProfile(
                symbol=p.symbol,
                window=p.window,
                price_step=p.price_step,
                price_low=p.price_low,
                price_high=p.price_high,
                open_price=p.open_price,
                close_price=p.close_price,
                total_volume=p.total_volume,
                buy_volume=p.buy_volume,
                sell_volume=p.sell_volume,
                trades=p.trades,
                notional=p.notional,
                bins=p.bins,
                value_area=p.value_area,
                nodes=p.nodes,
                shape=p.shape,
                naked_poc=revisit is None,
                poc_revisit_ts=revisit,
                naked_checked_until=checked_until,
            )
        )
    return out


def load_chart_candles(
    client: Any, symbol: str, start: datetime, end: datetime, timeframe: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(candles_1m, candles_tf)`` for rendering and naked-POC checks."""
    df1m = fetch_candles_1m(client, symbol, start, end)
    if df1m.empty:
        return df1m, df1m
    tf = str(timeframe).strip().lower()
    if tf in ("1m", "1min"):
        return df1m, df1m.copy()
    return df1m, aggregate_timeframe(df1m, tf)

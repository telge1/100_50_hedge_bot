"""Orchestration: build reference profiles, then score the following window."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import fetch_candles_1m
from orderbook_analyse.market_profile.anchor import build_windows
from orderbook_analyse.market_profile.build import build_profile
from orderbook_analyse.market_profile.contracts import ProfileWindow, ShapeThresholds
from orderbook_analyse.market_profile.loader import (
    fetch_volume_at_price,
    fetch_window_ohlc,
    resolve_price_step,
)

from . import MIN_TRADES_PER_WINDOW
from .contracts import SymbolRun, ValidationConfig
from .events import build_pair_events


class CausalityViolation(RuntimeError):
    """Raised when a test bar would predate its reference window's close."""


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def preflight_final_parity(
    client: Any,
    *,
    symbols: list[str],
    windows: list[ProfileWindow],
    target_bins: int,
    sample_n: int,
    seed: int,
) -> dict[str, Any]:
    """Compare FINAL vs non-FINAL aggregates on a random sample of windows.

    FINAL deduplicates the ReplacingMergeTree but costs roughly 60x the
    runtime. Running the full sweep without it is only defensible if the parts
    are already merged for this range, so that gets checked rather than
    assumed.
    """
    rng = random.Random(seed)
    pairs = [(s, w) for s in symbols for w in windows]
    rng.shuffle(pairs)
    checked: list[dict[str, Any]] = []
    mismatches = 0

    for symbol, window in pairs[: max(1, sample_n)]:
        ohlc = fetch_window_ohlc(client, symbol, window.start, window.end)
        if ohlc is None:
            continue
        _, high, low, _ = ohlc
        if high <= low:
            continue
        step = resolve_price_step(low, high, target_bins)
        a = fetch_volume_at_price(
            client, symbol, window.start, window.end, step, use_final=True
        )
        b = fetch_volume_at_price(
            client, symbol, window.start, window.end, step, use_final=False
        )
        va, vb = sum(x.volume for x in a), sum(x.volume for x in b)
        ta, tb = sum(x.trades for x in a), sum(x.trades for x in b)
        ok = (len(a) == len(b)) and (ta == tb) and abs(va - vb) <= 1e-9 * max(1.0, va)
        if not ok:
            mismatches += 1
        checked.append(
            {
                "symbol": symbol,
                "window_id": window.window_id,
                "bins_final": len(a),
                "bins_plain": len(b),
                "trades_final": ta,
                "trades_plain": tb,
                "volume_final": va,
                "volume_plain": vb,
                "match": ok,
            }
        )
        if len(checked) >= sample_n:
            break

    return {
        "sampled": len(checked),
        "mismatches": mismatches,
        "parity": mismatches == 0 and len(checked) > 0,
        "detail": checked,
    }


def run_symbol(
    client: Any,
    symbol: str,
    cfg: ValidationConfig,
    *,
    thresholds: ShapeThresholds | None = None,
) -> SymbolRun:
    """Build every reference profile for one symbol and score the next window."""
    windows = build_windows(
        anchor_mode=cfg.anchor_mode, start=cfg.start, end=cfg.end
    )
    if len(windows) < 2:
        return SymbolRun(
            symbol=symbol,
            windows=len(windows),
            profiles=0,
            skipped_thin=0,
            error="need at least two windows to form a reference/test pair",
        )

    candles = fetch_candles_1m(client, symbol, cfg.start, cfg.end)
    if candles.empty:
        return SymbolRun(
            symbol=symbol,
            windows=len(windows),
            profiles=0,
            skipped_thin=0,
            error="no 1m candles in range",
        )
    candles = candles.sort_values("open_time").reset_index(drop=True)
    times = [t.to_pydatetime() for t in candles["open_time"]]
    opens = candles["open"].to_numpy(dtype=float)
    highs = candles["high"].to_numpy(dtype=float)
    lows = candles["low"].to_numpy(dtype=float)

    max_horizon_bars = int(cfg.max_horizon_min)

    touch_events = []
    revisit_events = []
    built = 0
    skipped_thin = 0

    for ref_w, test_w in zip(windows, windows[1:]):
        profile = build_profile(
            client,
            symbol,
            ref_w,
            value_area_pct=cfg.value_area_pct,
            target_bins=cfg.target_bins,
            use_final=cfg.use_final,
            thresholds=thresholds,
        )
        if profile is None:
            continue
        built += 1
        if profile.trades < MIN_TRADES_PER_WINDOW:
            skipped_thin += 1
            continue

        ref_end = _naive_utc(ref_w.end)
        t_start, t_end = _naive_utc(test_w.start), _naive_utc(test_w.end)
        idx = [i for i, t in enumerate(times) if t_start <= t < t_end]
        if not idx:
            continue
        if times[idx[0]] < ref_end:
            raise CausalityViolation(
                f"{symbol} {test_w.window_id}: first test bar {times[idx[0]]} "
                f"predates reference close {ref_end}"
            )

        i0, i1 = idx[0], idx[-1]
        evs, revisit = build_pair_events(
            symbol=symbol,
            profile=profile,
            test_window_id=test_w.window_id,
            times=times[i0 : i1 + 1],
            opens=opens[i0 : i1 + 1],
            highs=highs[i0 : i1 + 1],
            lows=lows[i0 : i1 + 1],
            edge_margin_fracs=cfg.edge_margin_grid,
            poc_unit_fracs=cfg.poc_unit_grid,
            max_horizon_bars=max_horizon_bars,
        )
        touch_events.extend(evs)
        if revisit is not None:
            revisit_events.append(revisit)

    return SymbolRun(
        symbol=symbol,
        windows=len(windows),
        profiles=built,
        skipped_thin=skipped_thin,
        touch_events=tuple(touch_events),
        revisit_events=tuple(revisit_events),
    )


def event_date_key(event: Any) -> str:
    """Cluster key for date-level resampling (the reference window's label)."""
    label = str(getattr(event, "ref_label", ""))
    return label.split()[0] if label else "unknown"


def event_symbol_key(event: Any) -> str:
    return str(getattr(event, "symbol", "unknown"))


def events_to_frame(events: list) -> pd.DataFrame:
    return pd.DataFrame([e.to_dict() for e in events])

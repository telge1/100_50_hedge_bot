"""Causal short-only trend-pullback state machine (not A6)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.indicators import ema
from research.regime_scanner.short_trend_pullback.config import STPConfig, variant_id
from research.regime_scanner.short_trend_pullback.impulse import (
    build_impulse_from_bars,
    impulse_start_event,
    protected_high_intact,
)
from research.regime_scanner.short_trend_pullback.models import SetupRuntime, SignalEvent
from research.regime_scanner.short_trend_pullback.pullback import (
    new_pullback_state,
    pullback_begin,
    pullback_invalid,
    update_pullback,
)
from research.regime_scanner.short_trend_pullback.regime import context_ok
from research.regime_scanner.short_trend_pullback.trigger import evaluate_trigger


def ensure_slopes(frame: pd.DataFrame, lookback: int = 3) -> pd.DataFrame:
    out = frame.copy()
    for p in (20, 59, 200):
        col = f"ema_{p}"
        if col not in out.columns:
            close = pd.to_numeric(out["close"], errors="coerce").astype("float64")
            out[col] = ema(close, p)
        sc = f"ema_{p}_slope_{lookback}"
        if sc not in out.columns:
            out[sc] = out[col] - out[col].shift(lookback)
    if "ema_9" not in out.columns:
        close = pd.to_numeric(out["close"], errors="coerce").astype("float64")
        out["ema_9"] = ema(close, 9)
    return out


def _row_dict(frame: pd.DataFrame, i: int) -> dict[str, Any]:
    r = frame.iloc[i]
    return {k: r[k] for k in frame.columns}


def _ema200_above_share(frame: pd.DataFrame, i: int, lookback: int) -> float | None:
    if "ema_200" not in frame.columns:
        return None
    a = max(0, i - lookback + 1)
    closes = frame["close"].iloc[a : i + 1].astype(float)
    emas = frame["ema_200"].iloc[a : i + 1].astype(float)
    m = closes.notna() & emas.notna()
    if m.sum() == 0:
        return None
    return float((closes[m] > emas[m]).mean())


def run_strategy_on_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    context: str,
    trigger: str,
    cfg: STPConfig,
    analyze_start: pd.Timestamp | None = None,
) -> list[SignalEvent]:
    """Bar-by-bar causal SM. Trigger on closed bar i; fill at open of i+1."""
    frame = ensure_slopes(frame, cfg.slope_lookback)
    n = len(frame)
    rows = [_row_dict(frame, i) for i in range(n)]
    rt = SetupRuntime(context=context, trigger=trigger)
    signals: list[SignalEvent] = []
    impulse_anchor: int | None = None

    ts = pd.to_datetime(frame["timestamp"], utc=True)
    start_i = 0
    if analyze_start is not None:
        a0 = pd.Timestamp(analyze_start)
        if a0.tzinfo is None:
            a0 = a0.tz_localize("UTC")
        else:
            a0 = a0.tz_convert("UTC")
        start_i = int(np.searchsorted(ts.values, np.datetime64(a0.to_datetime64()), side="left"))

    for i in range(start_i, n):
        row = rows[i]
        share = _ema200_above_share(frame, i, cfg.ema200_above_lookback)
        ctx = context_ok(context, row, cfg=cfg, recent_above_ema200_share=share)

        # --- IDLE: wait for context + impulse start ---
        if rt.state == "IDLE":
            if not ctx:
                continue
            if impulse_start_event(row):
                impulse_anchor = i
                rt.state = "IMPULSE"
                rt.impulse = None
                rt.pullback = None
                rt.invalidate_reason = None
            continue

        # --- IMPULSE: extend until pullback begins or timeout ---
        if rt.state == "IMPULSE":
            if impulse_anchor is None:
                rt.state = "IDLE"
                continue
            if not ctx:
                rt.state = "IDLE"
                impulse_anchor = None
                continue
            # Pullback can only start after at least min_impulse_bars completed
            if (i - impulse_anchor) >= cfg.min_impulse_bars:
                imp = build_impulse_from_bars(rows, start_i=impulse_anchor, end_i=i - 1, cfg=cfg)
                if imp is not None and protected_high_intact(rows[i - 1], imp.protected_high):
                    # provisional: does this bar begin upward pullback vs impulse low?
                    if pullback_begin(row, imp):
                        rt.impulse = imp
                        rt.pullback = new_pullback_state(i, row, imp)
                        rt.state = "PULLBACK"
                        continue
            if impulse_start_event(row) and (i - impulse_anchor) >= cfg.min_impulse_bars:
                impulse_anchor = i
            elif (i - impulse_anchor) >= cfg.max_impulse_bars:
                rt.state = "IDLE"
                impulse_anchor = None
            continue

        # --- PULLBACK: update / invalidate / trigger ---
        if rt.state == "PULLBACK":
            assert rt.impulse is not None and rt.pullback is not None
            if not ctx:
                rt.state = "IDLE"
                impulse_anchor = None
                continue
            # new bearish impulse without trigger → reset
            if impulse_start_event(row):
                rt.state = "IMPULSE"
                impulse_anchor = i
                rt.impulse = None
                rt.pullback = None
                continue
            rt.pullback = update_pullback(rt.pullback, row, i, rt.impulse, cfg=cfg)
            inv = pullback_invalid(rt.pullback, row, rt.impulse, cfg=cfg)
            if inv:
                rt.state = "IDLE"
                impulse_anchor = None
                rt.invalidate_reason = inv
                continue
            if rt.pullback.bars < cfg.min_pullback_bars:
                continue
            if not evaluate_trigger(trigger, row, rt.pullback, rt.impulse):
                continue
            # Trigger on closed bar i; fill next open
            if i + 1 >= n:
                continue  # no fill candle
            if rt.last_trigger_bar is not None and rt.last_trigger_bar == i:
                continue
            fill_row = rows[i + 1]
            entry = float(fill_row["open"])
            ph = rt.impulse.protected_high
            dist = None if ph is None or ph <= 0 else (ph - entry) / ph * 100.0
            feat = _feature_blob(row, rt, share, context, trigger)
            sig = SignalEvent(
                symbol=symbol,
                context=context,
                trigger=trigger,
                variant=variant_id(context, trigger),
                side="short",
                trigger_bar=i,
                trigger_timestamp=row.get("timestamp"),
                trigger_price=float(row["close"]),
                fill_bar=i + 1,
                fill_timestamp=fill_row.get("timestamp"),
                entry_price=entry,
                pullback_high=float(rt.pullback.high),
                trigger_level=float(row["close"]),
                protected_high=ph,
                invalidation_level=ph,
                distance_to_protected_high=dist,
                pullback_retracement=float(rt.pullback.retracement),
                impulse_strength=float(rt.impulse.atr_move),
                regime_variant=context,
                features=feat,
            )
            signals.append(sig)
            rt.last_trigger_bar = i
            # reset after fire (one shot)
            rt.state = "IDLE"
            impulse_anchor = None
            rt.impulse = None
            rt.pullback = None
            continue

    return signals


def _feature_blob(row, rt: SetupRuntime, share, context, trigger) -> dict[str, Any]:
    imp = rt.impulse
    pb = rt.pullback
    return {
        "context_variant": context,
        "trigger_type": trigger,
        "ema20": row.get("ema_20"),
        "ema59": row.get("ema_59"),
        "ema200": row.get("ema_200"),
        "ema20_slope_3": row.get("ema_20_slope_3"),
        "ema59_slope_3": row.get("ema_59_slope_3"),
        "adx": row.get("adx"),
        "di_plus": row.get("plus_di"),
        "di_minus": row.get("minus_di"),
        "major_direction": row.get("major_direction"),
        "recent_above_ema200_share": share,
        "impulse_start": None if imp is None else imp.start_bar,
        "impulse_end": None if imp is None else imp.end_bar,
        "impulse_bars": None if imp is None else imp.bars,
        "impulse_return": None if imp is None else imp.return_pct,
        "impulse_atr": None if imp is None else imp.atr_move,
        "impulse_efficiency": None if imp is None else imp.efficiency,
        "impulse_volume": None if imp is None else imp.volume_sum,
        "pullback_start": None if pb is None else pb.start_bar,
        "pullback_end": None if pb is None else pb.end_bar,
        "pullback_bars": None if pb is None else pb.bars,
        "pullback_high": None if pb is None else pb.high,
        "pullback_return": None if pb is None else pb.return_pct,
        "pullback_atr": None if pb is None else pb.atr_move,
        "retracement_ratio": None if pb is None else pb.retracement,
        "pullback_efficiency": None if pb is None else pb.efficiency,
        "pullback_volume_ratio": None if pb is None else pb.volume_ratio,
        "internal_bullish_bos": None if pb is None else pb.internal_bull_bos,
        "external_bullish_choch": None if pb is None else pb.external_bull_choch,
        "distance_to_protected_high": None if pb is None else pb.dist_protected_high_pct,
        "trigger_body_atr": None,
        "micro_bos": bool(row.get("arm_edge_internal_bear")),
        "micro_choch": bool(row.get("arm_edge_choch_bear")),
    }

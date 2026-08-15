"""Causal liquidity sweep + reclaim state machine (simplified)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.liquidity_sweep_reclaim.config import (
    LSRConfig,
    default_config,
    variant_id,
)
from research.regime_scanner.liquidity_sweep_reclaim.levels import (
    eligible_levels_at_prior_bar,
    level_still_valid,
)
from research.regime_scanner.liquidity_sweep_reclaim.models import LevelSnapshot, SetupRuntime, SignalEvent
from research.regime_scanner.liquidity_sweep_reclaim.reclaim import (
    deeper_break_before_reclaim,
    diagnostic_reclaim_features,
    r1_same_candle,
    r2_one_bar,
    r3_confirmation_ok,
    reclaim_close,
)
from research.regime_scanner.liquidity_sweep_reclaim.sweep import measure_sweep, qualifies_penetration


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return v


def _row_feats(row: pd.Series, level_value: float, side: str) -> dict[str, Any]:
    atr = _finite(row.get("atr_14") or row.get("atr")) or 1e-12
    close = _finite(row.get("close")) or 0.0
    out: dict[str, Any] = {
        "major_direction": int(row.get("major_direction") or 0)
        if pd.notna(row.get("major_direction"))
        else 0,
        "adx": _finite(row.get("adx")),
        "di_spread": None,
        "atr_pct": (atr / max(close, 1e-12)) * 100.0,
        "c31_state": str(row.get("c31_state") or ""),
        "utc_hour": int(_ts(row.get("timestamp")).hour),
        "weekday": int(_ts(row.get("timestamp")).weekday()),
        "month": int(_ts(row.get("timestamp")).month),
    }
    plus = _finite(row.get("plus_di") or row.get("di_plus"))
    minus = _finite(row.get("minus_di") or row.get("di_minus"))
    if plus is not None and minus is not None:
        out["di_spread"] = plus - minus
    for p in (20, 59, 200):
        ema = _finite(row.get(f"ema_{p}"))
        if ema is not None:
            out[f"distance_ema_{p}_atr"] = (close - ema) / atr
            out[f"above_ema_{p}"] = close > ema
    out["distance_to_level_atr"] = abs(close - level_value) / atr
    out["side"] = side
    return out


def _snap_from_rt(rt: SetupRuntime) -> LevelSnapshot:
    return LevelSnapshot(
        level_family=rt.level_family,
        level_id=rt.level_id,
        level_value=rt.level_value,
        side=rt.side,
        confirmed_timestamp=rt.level_confirmed_timestamp,
        confirmed_bar=rt.level_confirmed_bar,
        age_bars=0,
        meta=rt.level_meta,
    )


def _emit(setup_events: list[dict[str, Any]], rt: SetupRuntime, symbol: str) -> None:
    row = {
        "setup_id": rt.setup_id,
        "symbol": symbol,
        "variant": rt.variant,
        "level_family": rt.level_family,
        "level_id": rt.level_id,
        "level_value": rt.level_value,
        "level_confirmed_timestamp": rt.level_confirmed_timestamp,
        "sweep_timestamp": rt.sweep_timestamp,
        "reclaim_timestamp": rt.reclaim_timestamp,
        "confirmation_timestamp": rt.confirmation_timestamp,
        "trigger_timestamp": rt.trigger_timestamp,
        "fill_timestamp": rt.fill_timestamp,
        "side": rt.side,
        "penetration_class": rt.penetration_class,
        "reclaim_type": rt.reclaim_type,
        "state": rt.state,
        "invalidation_reason": rt.invalidation_reason,
        "penetration_atr": rt.penetration_atr,
        "setup_age": None,
        "bars_sweep_to_reclaim": None,
        "bars_reclaim_to_trigger": None,
    }
    if rt.sweep_bar is not None and rt.reclaim_bar is not None:
        row["bars_sweep_to_reclaim"] = rt.reclaim_bar - rt.sweep_bar
    if rt.reclaim_bar is not None and rt.trigger_bar is not None:
        row["bars_reclaim_to_trigger"] = rt.trigger_bar - rt.reclaim_bar
    if rt.trigger_bar is not None:
        row["setup_age"] = rt.trigger_bar - rt.level_confirmed_bar
    setup_events.append(row)


def _try_fill(
    frame: pd.DataFrame,
    rt: SetupRuntime,
    *,
    symbol: str,
    atr: float,
    signals: list[SignalEvent],
    setup_events: list[dict[str, Any]],
) -> bool:
    """Fill at next open after trigger. Returns True if terminal."""
    assert rt.trigger_bar is not None
    fill_i = rt.trigger_bar + 1
    if fill_i >= len(frame):
        rt.state = "INVALIDATED"
        rt.invalidation_reason = "fill_candle_unavailable"
        _emit(setup_events, rt, symbol)
        return True
    fill_row = frame.iloc[fill_i]
    entry = float(fill_row["open"])
    fill_ts = str(_ts(fill_row["timestamp"]))
    trig_close = float(frame.iloc[rt.trigger_bar]["close"])
    rt.features["next_open_gap_atr"] = (entry - trig_close) / max(atr, 1e-12)
    rt.fill_timestamp = fill_ts
    rt.fill_bar = fill_i
    rt.entry_price = entry
    rt.state = "FILLED"
    _emit(setup_events, rt, symbol)
    bars_s2r = (rt.reclaim_bar if rt.reclaim_bar is not None else rt.trigger_bar) - (rt.sweep_bar or 0)
    bars_r2t = rt.trigger_bar - (rt.reclaim_bar if rt.reclaim_bar is not None else rt.trigger_bar)
    signals.append(
        SignalEvent(
            setup_id=rt.setup_id,
            symbol=symbol,
            variant=rt.variant,
            level_family=rt.level_family,
            penetration_class=rt.penetration_class,
            reclaim_type=rt.reclaim_type,
            side=rt.side,
            level_id=rt.level_id,
            level_value=rt.level_value,
            level_confirmed_timestamp=rt.level_confirmed_timestamp,
            sweep_timestamp=str(rt.sweep_timestamp),
            reclaim_timestamp=str(rt.reclaim_timestamp),
            confirmation_timestamp=rt.confirmation_timestamp,
            trigger_timestamp=str(rt.trigger_timestamp),
            fill_timestamp=fill_ts,
            trigger_bar=int(rt.trigger_bar),
            fill_bar=fill_i,
            entry_price=entry,
            trigger_price=trig_close,
            penetration_atr=float(rt.penetration_atr or 0.0),
            penetration_pct=float(rt.penetration_pct or 0.0),
            bars_sweep_to_reclaim=int(bars_s2r),
            bars_reclaim_to_trigger=int(bars_r2t),
            setup_age=int(rt.trigger_bar - rt.level_confirmed_bar),
            features=dict(rt.features),
        )
    )
    return True


def run_strategy_on_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    level_families: tuple[str, ...],
    penetrations: tuple[str, ...],
    reclaims: tuple[str, ...],
    cfg: LSRConfig | None = None,
    analyze_start: pd.Timestamp | None = None,
) -> tuple[list[SignalEvent], list[dict[str, Any]]]:
    c = cfg or default_config()
    n = len(frame)
    if n < 3:
        return [], []

    ts = pd.to_datetime(frame["timestamp"], utc=True)
    start_i = 1
    if analyze_start is not None:
        a0 = _ts(analyze_start)
        start_i = max(1, int(np.searchsorted(ts.values, np.datetime64(a0.to_datetime64()), side="left")))

    families = tuple(lf for lf in level_families if lf != "L3")
    active: list[SetupRuntime] = []
    signals: list[SignalEvent] = []
    setup_events: list[dict[str, Any]] = []
    seen_sweep: set[tuple[str, str, str, int]] = set()
    setup_seq = 0

    vol = frame["volume"].astype(float) if "volume" in frame.columns else None
    vol_ma = vol.rolling(20, min_periods=5).mean() if vol is not None else None

    for i in range(start_i, n):
        row = frame.iloc[i]
        high = float(row["high"])
        low = float(row["low"])
        open_ = float(row["open"])
        close = float(row["close"])
        atr = _finite(row.get("atr_14") or row.get("atr")) or 1e-12
        bar_ts = str(_ts(row["timestamp"]))
        volume = float(vol.iloc[i]) if vol is not None else None
        vma = float(vol_ma.iloc[i]) if vol_ma is not None and np.isfinite(vol_ma.iloc[i]) else None

        next_active: list[SetupRuntime] = []
        for rt in active:
            ok, reason = level_still_valid(frame, i, _snap_from_rt(rt))
            if not ok:
                rt.state = "INVALIDATED"
                rt.invalidation_reason = reason
                _emit(setup_events, rt, symbol)
                continue

            if rt.state == "SWEPT":
                assert rt.sweep_bar is not None and rt.sweep_extreme is not None
                bars_since = i - rt.sweep_bar
                if deeper_break_before_reclaim(
                    side=rt.side,
                    level=rt.level_value,
                    prior_extreme=rt.sweep_extreme,
                    high=high,
                    low=low,
                ):
                    rt.state = "INVALIDATED"
                    rt.invalidation_reason = "deeper_break_before_reclaim"
                    _emit(setup_events, rt, symbol)
                    continue

                if rt.reclaim_type == "R2":
                    if bars_since > 1:
                        rt.state = "INVALIDATED"
                        rt.invalidation_reason = "reclaim_window_missed"
                        _emit(setup_events, rt, symbol)
                        continue
                    if r2_one_bar(
                        side=rt.side,
                        level=rt.level_value,
                        bars_since_sweep=bars_since,
                        close=close,
                    ):
                        rt.state = "RECLAIMED"
                        rt.reclaim_timestamp = bar_ts
                        rt.reclaim_bar = i
                        rt.features.update(
                            diagnostic_reclaim_features(
                                side=rt.side,
                                level=rt.level_value,
                                open_=open_,
                                close=close,
                                atr=atr,
                                bars_to_reclaim=bars_since,
                            )
                        )
                        rt.features["reclaim_close"] = close
                        rt.state = "TRIGGERED"
                        rt.trigger_timestamp = bar_ts
                        rt.trigger_bar = i
                        _try_fill(frame, rt, symbol=symbol, atr=atr, signals=signals, setup_events=setup_events)
                        continue

                elif rt.reclaim_type == "R3":
                    if bars_since > 1:
                        rt.state = "INVALIDATED"
                        rt.invalidation_reason = "reclaim_window_missed"
                        _emit(setup_events, rt, symbol)
                        continue
                    if reclaim_close(side=rt.side, level=rt.level_value, close=close):
                        rt.state = "RECLAIMED"
                        rt.reclaim_timestamp = bar_ts
                        rt.reclaim_bar = i
                        rt.features.update(
                            diagnostic_reclaim_features(
                                side=rt.side,
                                level=rt.level_value,
                                open_=open_,
                                close=close,
                                atr=atr,
                                bars_to_reclaim=bars_since,
                            )
                        )
                        rt.features["reclaim_close"] = close
                        next_active.append(rt)
                        continue
                next_active.append(rt)
                continue

            if rt.state == "RECLAIMED" and rt.reclaim_type == "R3":
                assert rt.reclaim_bar is not None
                if i < rt.reclaim_bar + 1:
                    next_active.append(rt)
                    continue
                if i > rt.reclaim_bar + 1:
                    rt.state = "INVALIDATED"
                    rt.invalidation_reason = "confirmation_window_missed"
                    _emit(setup_events, rt, symbol)
                    continue
                rec_px = float(rt.features.get("reclaim_close") or close)
                if not r3_confirmation_ok(
                    side=rt.side,
                    level=rt.level_value,
                    reclaim_close_px=rec_px,
                    open_=open_,
                    close=close,
                ):
                    rt.state = "INVALIDATED"
                    rt.invalidation_reason = "confirmation_failed"
                    _emit(setup_events, rt, symbol)
                    continue
                rt.state = "CONFIRMED"
                rt.confirmation_timestamp = bar_ts
                rt.confirmation_bar = i
                rt.features["confirmation_strength"] = abs(close - rec_px) / atr
                rt.state = "TRIGGERED"
                rt.trigger_timestamp = bar_ts
                rt.trigger_bar = i
                _try_fill(frame, rt, symbol=symbol, atr=atr, signals=signals, setup_events=setup_events)
                continue

            next_active.append(rt)
        active = next_active

        # New sweeps on this closed bar (level from prior bar only).
        for snap in eligible_levels_at_prior_bar(frame, i, level_families=families, cfg=c):
            sw = measure_sweep(
                side=snap.side,
                level=snap.level_value,
                high=high,
                low=low,
                open_=open_,
                close=close,
                atr=atr,
                volume=volume,
                volume_ma=vma,
            )
            if sw is None:
                continue
            if sw["oversized_break"]:
                setup_events.append(
                    {
                        "setup_id": None,
                        "symbol": symbol,
                        "level_family": snap.level_family,
                        "level_id": snap.level_id,
                        "level_value": snap.level_value,
                        "side": snap.side,
                        "state": "INVALIDATED",
                        "invalidation_reason": "oversized_break",
                        "sweep_timestamp": bar_ts,
                        "penetration_atr": sw["penetration_atr"],
                    }
                )
                continue

            for p in penetrations:
                if not qualifies_penetration(p, sw["penetration_atr"], c):
                    continue
                for r in reclaims:
                    vid = variant_id(snap.level_family, p, r)
                    dup_key = (vid, snap.side, f"{snap.level_value:.8f}", i)
                    if dup_key in seen_sweep:
                        continue
                    seen_sweep.add(dup_key)
                    setup_seq += 1
                    rt = SetupRuntime(
                        setup_id=f"{symbol}_{vid}_{setup_seq}",
                        variant=vid,
                        level_family=snap.level_family,
                        penetration_class=p,
                        reclaim_type=r,
                        side=snap.side,
                        level_id=snap.level_id,
                        level_value=snap.level_value,
                        level_confirmed_timestamp=snap.confirmed_timestamp,
                        level_confirmed_bar=snap.confirmed_bar,
                        state="SWEPT",
                        sweep_timestamp=bar_ts,
                        sweep_bar=i,
                        sweep_extreme=sw["sweep_extreme"],
                        penetration_atr=sw["penetration_atr"],
                        penetration_pct=sw["penetration_pct"],
                        level_meta=dict(snap.meta),
                        sweep_meta=dict(sw),
                        features={
                            **_row_feats(row, snap.level_value, snap.side),
                            **{f"lvl_{k}": v for k, v in snap.meta.items()},
                            **{f"sw_{k}": v for k, v in sw.items()},
                            "level_age": snap.age_bars,
                            "reclaim_type": r,
                            "penetration_class": p,
                        },
                    )

                    if r == "R1":
                        if r1_same_candle(
                            side=snap.side, level=snap.level_value, swept=True, close=close
                        ):
                            rt.state = "RECLAIMED"
                            rt.reclaim_timestamp = bar_ts
                            rt.reclaim_bar = i
                            rt.features.update(
                                diagnostic_reclaim_features(
                                    side=snap.side,
                                    level=snap.level_value,
                                    open_=open_,
                                    close=close,
                                    atr=atr,
                                    bars_to_reclaim=0,
                                )
                            )
                            rt.features["reclaim_close"] = close
                            rt.state = "TRIGGERED"
                            rt.trigger_timestamp = bar_ts
                            rt.trigger_bar = i
                            _try_fill(
                                frame, rt, symbol=symbol, atr=atr, signals=signals, setup_events=setup_events
                            )
                        else:
                            rt.state = "INVALIDATED"
                            rt.invalidation_reason = "reclaim_window_missed"
                            _emit(setup_events, rt, symbol)
                        continue

                    if r == "R3" and reclaim_close(side=snap.side, level=snap.level_value, close=close):
                        rt.state = "RECLAIMED"
                        rt.reclaim_timestamp = bar_ts
                        rt.reclaim_bar = i
                        rt.features.update(
                            diagnostic_reclaim_features(
                                side=snap.side,
                                level=snap.level_value,
                                open_=open_,
                                close=close,
                                atr=atr,
                                bars_to_reclaim=0,
                            )
                        )
                        rt.features["reclaim_close"] = close
                        active.append(rt)
                        continue

                    # R2 always waits one bar; R3 without same-candle reclaim waits.
                    active.append(rt)

    return signals, setup_events

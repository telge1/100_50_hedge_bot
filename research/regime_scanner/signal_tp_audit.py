"""Legacy research baseline: regime edge → synthetic entry → TP audit.

Reuses ``build_point_audit`` / combined regime summary. No fees, hedge, or recovery.

===========================================================================
LEGACY RESEARCH ENTRY BASELINE (Phase 1+)
===========================================================================
This module intentionally keeps the older **direct entry** research path:

* ``is_entry_transition`` / ``get_signal_side`` / ``is_*_signal_regime``
* ``observe_tp`` / ``observe_long_tp`` (forward TP / MAE / MFE)
* Global lockout inside ``scan_bullish_signal_tp`` / ``scan_signal_tp``
* Entry rows written to CSV/JSON

It must **not** be used by the Phase-1 ``regime_snapshot`` /
``SetupActivation`` core. New trading logic should consume
``RegimeSnapshot`` → ``SetupActivation`` and only later add Price Action /
Momentum / Entry in separate modules.

Lockout policy (legacy baseline only)
-------------------------------------
A **global** lockout is used (same as the original long-only audit): after any
signal fires, no further long *or* short signal may start until TP or timeout.
Long and short are therefore **not** independently parallel in this v1.
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np
import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config
from .data_loader import load_symbol_candles
from .point_audit import build_point_audit, json_safe
from .timeframes import parse_timeframes, required_5m_history_candles

Side = Literal["long", "short"]

BULLISH_SIGNAL_REGIMES = frozenset({"bullish_trend", "strong_bullish_trend"})
BEARISH_SIGNAL_REGIMES = frozenset({"bearish_trend", "strong_bearish_trend"})
ALL_SIGNAL_REGIMES = BULLISH_SIGNAL_REGIMES | BEARISH_SIGNAL_REGIMES
SIGNAL_REGIMES = ALL_SIGNAL_REGIMES

ADVERSE_THRESHOLDS_PCT = (-0.25, -0.50, -1.00, -2.00)
TP_HORIZON_BUCKETS = (1, 3, 6, 12, 24, 48)

_WORKER_CANDLES: pd.DataFrame | None = None
_WORKER_CFG: dict[str, Any] | None = None


def get_signal_side(regime: object) -> Side | None:
    """LEGACY: map combined regime → synthetic entry side for TP-audit baseline."""
    name = str(regime or "")
    if name in BULLISH_SIGNAL_REGIMES:
        return "long"
    if name in BEARISH_SIGNAL_REGIMES:
        return "short"
    return None


def is_bullish_signal_regime(regime: object) -> bool:
    """LEGACY research helper — not part of SetupActivation."""
    return get_signal_side(regime) == "long"


def is_bearish_signal_regime(regime: object) -> bool:
    """LEGACY research helper — not part of SetupActivation."""
    return get_signal_side(regime) == "short"


def parse_sides(value: str | list[str] | tuple[str, ...] | None) -> tuple[Side, ...]:
    """Parse ``long``, ``short``, or ``long,short`` into a deduplicated tuple."""
    if value is None:
        return ("long",)
    if isinstance(value, (list, tuple)):
        parts = [str(p).strip().lower() for p in value if str(p).strip()]
    else:
        parts = [p.strip().lower() for p in str(value).split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(
            "sides must be one of: long | short | long,short"
        )
    allowed = {"long", "short"}
    unknown = [p for p in parts if p not in allowed]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"invalid side(s) {unknown}; allowed: long, short"
        )
    out: list[Side] = []
    seen: set[str] = set()
    for part in parts:
        if part not in seen:
            out.append(part)  # type: ignore[arg-type]
            seen.add(part)
    return tuple(out)


def is_entry_transition(
    *,
    previous_regime: object,
    current_regime: object,
    enabled_sides: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> bool:
    """LEGACY: True on the edge into an enabled **synthetic entry** side.

    This is the old research entry trigger, **not** Phase-1 SetupActivation.
    Transitions *within* the same side do not re-fire
    (e.g. bullish_trend → strong_bullish_trend, bearish_trend → strong_bearish_trend).
    """
    sides = (
        frozenset(enabled_sides)
        if enabled_sides is not None
        else frozenset({"long", "short"})
    )
    current_side = get_signal_side(current_regime)
    if current_side is None or current_side not in sides:
        return False
    previous_side = get_signal_side(previous_regime)
    return previous_side != current_side


def decision_time_for_index(candles: pd.DataFrame, index: int) -> pd.Timestamp:
    open_ts = pd.Timestamp(candles.iloc[index]["timestamp"])
    if open_ts.tzinfo is None:
        open_ts = open_ts.tz_localize("UTC")
    else:
        open_ts = open_ts.tz_convert("UTC")
    return open_ts + pd.Timedelta(minutes=5)


def entry_fields_for_index(candles: pd.DataFrame, index: int) -> dict[str, Any]:
    row = candles.iloc[index]
    open_ts = pd.Timestamp(row["timestamp"])
    if open_ts.tzinfo is None:
        open_ts = open_ts.tz_localize("UTC")
    else:
        open_ts = open_ts.tz_convert("UTC")
    return {
        "entry_candle_open": open_ts.isoformat(),
        "entry_price": float(row["close"]),
        "decision_time": (open_ts + pd.Timedelta(minutes=5)).isoformat(),
    }


def observe_tp(
    candles: pd.DataFrame,
    *,
    entry_index: int,
    entry_price: float,
    side: Side = "long",
    tp_pct: float = 0.25,
    max_hold_candles: int = 48,
) -> dict[str, Any]:
    """LEGACY research TP observer (forward-looking on purpose for audits only).

    Not used by RegimeSnapshot / SetupActivation. Peeks future bars after a
    synthetic entry — keep out of the live / setup core.

    Long:
      tp = entry * (1 + tp_pct/100); hit when high >= tp
      MFE = (max_high - entry) / entry * 100
      MAE = (min_low - entry) / entry * 100

    Short:
      tp = entry * (1 - tp_pct/100); hit when low <= tp
      MFE = (entry - min_low) / entry * 100
      MAE = (entry - max_high) / entry * 100   # negative when price rises against short
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    side_key = str(side).strip().lower()
    if side_key not in {"long", "short"}:
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")

    if side_key == "long":
        tp_price = float(entry_price) * (1.0 + float(tp_pct) / 100.0)
    else:
        tp_price = float(entry_price) * (1.0 - float(tp_pct) / 100.0)

    start = int(entry_index) + 1
    end = min(len(candles) - 1, int(entry_index) + int(max_hold_candles))
    if start >= len(candles) or start > end:
        last_open = pd.Timestamp(candles.iloc[int(entry_index)]["timestamp"])
        if last_open.tzinfo is None:
            last_open = last_open.tz_localize("UTC")
        return {
            "tp_price": tp_price,
            "tp_reached": False,
            "tp_timestamp": None,
            "candles_to_tp": None,
            "observation_end_timestamp": last_open.isoformat(),
            "max_favorable_excursion_pct": 0.0,
            "max_adverse_excursion_pct": 0.0,
            "touched_minus_0_25_pct": False,
            "touched_minus_0_50_pct": False,
            "touched_minus_1_00_pct": False,
            "touched_minus_2_00_pct": False,
            "observation_end_index": int(entry_index),
        }

    window = candles.iloc[start : end + 1]
    highs = pd.to_numeric(window["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(window["low"], errors="coerce").to_numpy(dtype=float)
    timestamps = [pd.Timestamp(ts) for ts in window["timestamp"].tolist()]

    tp_reached = False
    tp_timestamp = None
    candles_to_tp: int | None = None
    stop_pos = len(window) - 1
    for pos in range(len(window)):
        high = highs[pos]
        low = lows[pos]
        hit = False
        if side_key == "long":
            hit = bool(np.isfinite(high) and high >= tp_price)
        else:
            hit = bool(np.isfinite(low) and low <= tp_price)
        if hit:
            tp_reached = True
            candles_to_tp = pos + 1
            ts = timestamps[pos]
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            tp_timestamp = ts.isoformat()
            stop_pos = pos
            break

    used_highs = highs[: stop_pos + 1]
    used_lows = lows[: stop_pos + 1]
    max_high = float(np.nanmax(used_highs)) if len(used_highs) else float(entry_price)
    min_low = float(np.nanmin(used_lows)) if len(used_lows) else float(entry_price)
    entry = float(entry_price)
    if side_key == "long":
        mfe_pct = (max_high - entry) / entry * 100.0
        mae_pct = (min_low - entry) / entry * 100.0
    else:
        mfe_pct = (entry - min_low) / entry * 100.0
        mae_pct = (entry - max_high) / entry * 100.0

    end_ts = timestamps[stop_pos]
    if end_ts.tzinfo is None:
        end_ts = end_ts.tz_localize("UTC")
    else:
        end_ts = end_ts.tz_convert("UTC")

    return {
        "tp_price": tp_price,
        "tp_reached": bool(tp_reached),
        "tp_timestamp": tp_timestamp,
        "candles_to_tp": candles_to_tp,
        "observation_end_timestamp": end_ts.isoformat(),
        "max_favorable_excursion_pct": float(mfe_pct),
        "max_adverse_excursion_pct": float(mae_pct),
        "touched_minus_0_25_pct": bool(mae_pct <= -0.25),
        "touched_minus_0_50_pct": bool(mae_pct <= -0.50),
        "touched_minus_1_00_pct": bool(mae_pct <= -1.00),
        "touched_minus_2_00_pct": bool(mae_pct <= -2.00),
        "observation_end_index": int(start + stop_pos),
    }


def observe_long_tp(
    candles: pd.DataFrame,
    *,
    entry_index: int,
    entry_price: float,
    tp_pct: float = 0.25,
    max_hold_candles: int = 48,
) -> dict[str, Any]:
    """LEGACY long wrapper around :func:`observe_tp` (research baseline only)."""
    return observe_tp(
        candles,
        entry_index=entry_index,
        entry_price=entry_price,
        side="long",
        tp_pct=tp_pct,
        max_hold_candles=max_hold_candles,
    )


def extract_regime_snapshot(audit: dict[str, Any]) -> dict[str, Any]:
    combined = audit.get("combined_regime") or audit.get("regime_summary") or {}
    by_tf = audit.get("by_timeframe") or {}

    def _tf_regime(tf: str) -> str | None:
        payload = by_tf.get(tf) or {}
        summary = payload.get("regime_summary") or {}
        return summary.get("regime")

    return {
        "combined_regime": combined.get("regime"),
        "confidence": combined.get("confidence"),
        "reason_codes": combined.get("reason_codes") or [],
        "regime_5m": _tf_regime("5m"),
        "regime_15m": _tf_regime("15m"),
        "regime_30m": _tf_regime("30m"),
    }


def _parse_utc_bound(value: object | None) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def prepare_candle_window(
    candles: pd.DataFrame,
    *,
    start: object | None = None,
    end: object | None = None,
    history_candles: int = 144,
    timeframes: str | tuple[str, ...] = "5m,15m,30m",
) -> dict[str, Any]:
    """Slice candles to ``[start, end)`` plus causal warmup lookback before start."""
    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    start_ts = _parse_utc_bound(start)
    end_ts = _parse_utc_bound(end)
    requested = parse_timeframes(timeframes)
    need = required_5m_history_candles(history_candles, requested)

    if start_ts is not None:
        warmup_start = start_ts - pd.Timedelta(minutes=5 * int(need))
        frame = frame.loc[frame["timestamp"] >= warmup_start].copy()
    if end_ts is not None:
        frame = frame.loc[frame["timestamp"] < end_ts].copy()
    frame = frame.reset_index(drop=True)

    if start_ts is None:
        signal_start_index = int(need) if len(frame) > need else len(frame)
    else:
        matches = frame.index[frame["timestamp"] >= start_ts]
        signal_start_index = int(matches[0]) if len(matches) else len(frame)

    return {
        "candles": frame,
        "signal_start_index": signal_start_index,
        "start": start_ts.isoformat() if start_ts is not None else None,
        "end": end_ts.isoformat() if end_ts is not None else None,
        "warmup_candles": int(need),
    }


def evaluate_regime_at_index(
    candles: pd.DataFrame,
    index: int,
    *,
    symbol: str = "APTUSDT",
    timeframes: str | tuple[str, ...] = "5m,15m,30m",
    history_candles: int = 144,
    config: RegimeScannerConfig | None = None,
) -> dict[str, Any]:
    """Causal combined-regime snapshot at the close of ``candles[index]``."""
    cfg = config or default_regime_scanner_config()
    requested = parse_timeframes(timeframes)
    need = required_5m_history_candles(history_candles, requested)
    decision = decision_time_for_index(candles, index)
    start = max(0, int(index) + 1 - int(need))
    window = candles.iloc[start : int(index) + 1].reset_index(drop=True)
    audit = build_point_audit(
        symbol=symbol,
        decision_time=decision,
        candles=window,
        history_candles=history_candles,
        timeframes=requested,
        config=cfg,
    )
    snap = extract_regime_snapshot(audit)
    snap["decision_time"] = decision.isoformat()
    snap["index"] = int(index)
    return snap


def _init_worker(candles_payload: dict[str, Any], worker_cfg: dict[str, Any]) -> None:
    global _WORKER_CANDLES, _WORKER_CFG
    frame = pd.DataFrame(candles_payload)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    _WORKER_CANDLES = frame
    _WORKER_CFG = worker_cfg


def _worker_regime_at_index(index: int) -> tuple[int, dict[str, Any]]:
    assert _WORKER_CANDLES is not None and _WORKER_CFG is not None
    snap = evaluate_regime_at_index(
        _WORKER_CANDLES,
        index,
        symbol=_WORKER_CFG["symbol"],
        timeframes=_WORKER_CFG["timeframes"],
        history_candles=_WORKER_CFG["history_candles"],
    )
    return index, snap


def scan_bullish_signal_tp(
    candles: pd.DataFrame,
    *,
    symbol: str = "APTUSDT",
    timeframes: str = "5m,15m,30m",
    history_candles: int = 144,
    tp_pct: float = 0.25,
    max_hold_candles: int = 48,
    config: RegimeScannerConfig | None = None,
    regime_fn: Callable[[int], dict[str, Any]] | None = None,
    workers: int = 1,
    progress_every: int = 500,
    prefetch_batch_size: int = 32,
    start: object | None = None,
    end: object | None = None,
    sides: str | list[str] | tuple[str, ...] | None = "long",
) -> dict[str, Any]:
    """LEGACY: walk history and emit **synthetic entry rows** + TP outcomes.

    This remains the research baseline for comparing against future
    Setup→PA→Momentum→Entry pipelines. It does **not** call SetupActivation.

    Default ``sides='long'`` preserves the original long-only behaviour.
    Lockout is global: after any signal, no new long/short signal until TP/timeout.
    """
    enabled_sides = frozenset(parse_sides(sides))
    cfg = config or default_regime_scanner_config()
    requested = parse_timeframes(timeframes)
    prepared = prepare_candle_window(
        candles,
        start=start,
        end=end,
        history_candles=history_candles,
        timeframes=requested,
    )
    frame = prepared["candles"]
    if regime_fn is not None and start is None and end is None:
        need = max(1, int(history_candles))
        start_index = need
    else:
        start_index = int(prepared["signal_start_index"])
    n = len(frame)
    if n <= start_index + 1:
        return {
            "symbol": str(symbol).upper(),
            "timeframes": list(requested),
            "history_candles": int(history_candles),
            "tp_pct": float(tp_pct),
            "max_hold_candles": int(max_hold_candles),
            "sides": sorted(enabled_sides),
            "start": prepared.get("start"),
            "end": prepared.get("end"),
            "rows": [],
            "summary": build_signal_tp_summary([], enabled_sides=enabled_sides),
        }
    regime_cache: dict[int, dict[str, Any]] = {}
    batch_size = max(1, int(prefetch_batch_size))
    use_pool = regime_fn is None and int(workers) > 1
    executor: ProcessPoolExecutor | None = None
    if use_pool:
        candles_payload = {
            col: frame[col].to_numpy()
            for col in ("timestamp", "open", "high", "low", "close", "volume")
        }
        candles_payload["timestamp"] = frame["timestamp"].astype(str).to_numpy()
        worker_cfg = {
            "symbol": str(symbol).upper(),
            "timeframes": ",".join(requested),
            "history_candles": int(history_candles),
        }
        executor = ProcessPoolExecutor(
            max_workers=int(workers),
            initializer=_init_worker,
            initargs=(candles_payload, worker_cfg),
        )

    def _fetch_missing(indices: list[int]) -> None:
        missing = [i for i in indices if i not in regime_cache]
        if not missing:
            return
        if regime_fn is not None:
            for idx in missing:
                regime_cache[idx] = regime_fn(idx)
            return
        if executor is None:
            for idx in missing:
                regime_cache[idx] = evaluate_regime_at_index(
                    frame,
                    idx,
                    symbol=symbol,
                    timeframes=requested,
                    history_candles=history_candles,
                    config=cfg,
                )
            return
        for idx, snap in executor.map(_worker_regime_at_index, missing, chunksize=4):
            regime_cache[idx] = snap

    def _prune(keep_from: int) -> None:
        for key in [k for k in regime_cache if k < keep_from - 1]:
            del regime_cache[key]

    rows: list[dict[str, Any]] = []
    previous_regime: str | None = None
    index = start_index
    signal_id = 0
    evaluated = 0

    try:
        while index < n:
            batch_end = min(n, index + batch_size)
            before = len(regime_cache)
            _fetch_missing(list(range(index, batch_end)))
            evaluated += max(0, len(regime_cache) - before)
            if progress_every and index % int(progress_every) < batch_size:
                print(
                    f"scan progress index={index}/{n} signals={len(rows)} "
                    f"evaluated~={evaluated} cache={len(regime_cache)}",
                    flush=True,
                )

            signal_fired = False
            for cursor in range(index, batch_end):
                snap = regime_cache[cursor]
                current = snap.get("combined_regime")
                if is_entry_transition(
                    previous_regime=previous_regime,
                    current_regime=current,
                    enabled_sides=enabled_sides,
                ):
                    side = get_signal_side(current)
                    assert side is not None
                    signal_id += 1
                    entry = entry_fields_for_index(frame, cursor)
                    outcome = observe_tp(
                        frame,
                        entry_index=cursor,
                        entry_price=float(entry["entry_price"]),
                        side=side,
                        tp_pct=tp_pct,
                        max_hold_candles=max_hold_candles,
                    )
                    rows.append(
                        {
                            "signal_id": f"sig_{signal_id:05d}",
                            "side": side,
                            "decision_time": entry["decision_time"],
                            "entry_candle_open": entry["entry_candle_open"],
                            "entry_price": entry["entry_price"],
                            "regime_5m": snap.get("regime_5m"),
                            "regime_15m": snap.get("regime_15m"),
                            "regime_30m": snap.get("regime_30m"),
                            "combined_regime": current,
                            "reason_codes": snap.get("reason_codes") or [],
                            **{
                                k: v
                                for k, v in outcome.items()
                                if k != "observation_end_index"
                            },
                        }
                    )
                    # Global lockout until TP or timeout (long and short share one lock).
                    unlock_index = int(outcome["observation_end_index"])
                    _fetch_missing([unlock_index])
                    previous_regime = regime_cache[unlock_index].get("combined_regime")
                    index = unlock_index + 1
                    _prune(index)
                    signal_fired = True
                    break
                previous_regime = current

            if not signal_fired:
                index = batch_end
                _prune(index)
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    summary = build_signal_tp_summary(rows, enabled_sides=enabled_sides)
    return {
        "symbol": str(symbol).upper(),
        "timeframes": list(requested),
        "history_candles": int(history_candles),
        "tp_pct": float(tp_pct),
        "max_hold_candles": int(max_hold_candles),
        "sides": sorted(enabled_sides),
        "start": prepared.get("start"),
        "end": prepared.get("end"),
        "candle_count": int(n),
        "scan_start_index": int(start_index),
        "regimes_evaluated": int(evaluated),
        "rows": rows,
        "summary": summary,
    }


# Friendly alias.
scan_signal_tp = scan_bullish_signal_tp


def _subset_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "signal_count": 0,
            "tp_reached_count": 0,
            "tp_missed_count": 0,
            "tp_hit_rate": None,
        }
    reached = [r for r in rows if r.get("tp_reached")]
    missed = [r for r in rows if not r.get("tp_reached")]
    candles_to_tp = [
        float(r["candles_to_tp"])
        for r in reached
        if r.get("candles_to_tp") is not None
    ]
    mae = [
        float(r["max_adverse_excursion_pct"])
        for r in rows
        if r.get("max_adverse_excursion_pct") is not None
    ]
    mfe = [
        float(r["max_favorable_excursion_pct"])
        for r in rows
        if r.get("max_favorable_excursion_pct") is not None
    ]

    def _rate(pred: Callable[[dict[str, Any]], bool]) -> float:
        return float(sum(1 for r in rows if pred(r)) / n)

    horizon = {
        f"tp_within_{h}_candles": int(
            sum(
                1
                for r in reached
                if r.get("candles_to_tp") is not None and int(r["candles_to_tp"]) <= h
            )
        )
        for h in TP_HORIZON_BUCKETS
    }
    return {
        "signal_count": n,
        "tp_reached_count": len(reached),
        "tp_missed_count": len(missed),
        "tp_hit_rate": float(len(reached) / n),
        **horizon,
        "median_candles_to_tp": float(pd.Series(candles_to_tp).median())
        if candles_to_tp
        else None,
        "mean_candles_to_tp": float(pd.Series(candles_to_tp).mean())
        if candles_to_tp
        else None,
        "median_mae_pct": float(pd.Series(mae).median()) if mae else None,
        "mean_mae_pct": float(pd.Series(mae).mean()) if mae else None,
        "worst_mae_pct": float(min(mae)) if mae else None,
        "median_mfe_pct": float(pd.Series(mfe).median()) if mfe else None,
        "mean_mfe_pct": float(pd.Series(mfe).mean()) if mfe else None,
        "share_mae_le_minus_0_25_pct": _rate(lambda r: bool(r.get("touched_minus_0_25_pct"))),
        "share_mae_le_minus_0_50_pct": _rate(lambda r: bool(r.get("touched_minus_0_50_pct"))),
        "share_mae_le_minus_1_00_pct": _rate(lambda r: bool(r.get("touched_minus_1_00_pct"))),
        "share_mae_le_minus_2_00_pct": _rate(lambda r: bool(r.get("touched_minus_2_00_pct"))),
        "count_mae_le_minus_0_50_pct": int(
            sum(1 for r in rows if r.get("touched_minus_0_50_pct"))
        ),
        "count_mae_le_minus_1_00_pct": int(
            sum(1 for r in rows if r.get("touched_minus_1_00_pct"))
        ),
        "count_mae_le_minus_2_00_pct": int(
            sum(1 for r in rows if r.get("touched_minus_2_00_pct"))
        ),
    }


def build_signal_tp_summary(
    rows: list[dict[str, Any]],
    *,
    enabled_sides: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    sides = (
        sorted(enabled_sides)
        if enabled_sides is not None
        else sorted({"long", "short"})
    )
    overall = _subset_stats(rows)
    by_side = {
        side: _subset_stats([r for r in rows if r.get("side") == side])
        for side in ("long", "short")
    }
    # Legacy long-only rows without side field count as long.
    if any(r.get("side") is None for r in rows):
        by_side["long"] = _subset_stats(
            [r for r in rows if r.get("side") in (None, "long")]
        )
    by_regime = {
        regime: _subset_stats([r for r in rows if r.get("combined_regime") == regime])
        for regime in sorted(ALL_SIGNAL_REGIMES)
    }
    direct = overall.get("tp_within_3_candles")
    overall["mostly_direct_to_tp"] = bool(
        overall.get("tp_hit_rate") is not None
        and overall["tp_hit_rate"] >= 0.7
        and direct is not None
        and overall.get("signal_count")
        and (direct / max(int(overall["signal_count"]), 1)) >= 0.5
    )
    return {
        "overall": overall,
        "by_side": by_side,
        "by_combined_regime": by_regime,
        "enabled_sides": list(sides),
    }


def format_summary_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    overall = summary.get("overall") or {}
    lines = [
        "# Regime Signal TP Audit",
        "",
        f"- Symbol: `{payload.get('symbol')}`",
        f"- Timeframes: `{', '.join(payload.get('timeframes') or [])}`",
        f"- Sides: `{', '.join(payload.get('sides') or summary.get('enabled_sides') or [])}`",
        f"- Window: `{payload.get('start')}` → `{payload.get('end')}`",
        f"- TP: `{payload.get('tp_pct')}%`",
        f"- Max hold candles: `{payload.get('max_hold_candles')}`",
        "",
        "## Overall",
        "",
        f"- Signals: **{overall.get('signal_count')}**",
        f"- TP reached: **{overall.get('tp_reached_count')}**",
        f"- TP missed: **{overall.get('tp_missed_count')}**",
        f"- TP hit rate: **{overall.get('tp_hit_rate')}**",
        f"- Median candles to TP: **{overall.get('median_candles_to_tp')}**",
        f"- Mean candles to TP: **{overall.get('mean_candles_to_tp')}**",
        f"- Median MAE%: **{overall.get('median_mae_pct')}**",
        f"- Mean MAE%: **{overall.get('mean_mae_pct')}**",
        f"- Worst MAE%: **{overall.get('worst_mae_pct')}**",
        "",
        "### TP horizons",
        "",
    ]
    for h in TP_HORIZON_BUCKETS:
        lines.append(f"- within {h}: **{overall.get(f'tp_within_{h}_candles')}**")
    lines.extend(["", "### MAE shares", ""])
    for key in (
        "share_mae_le_minus_0_25_pct",
        "share_mae_le_minus_0_50_pct",
        "share_mae_le_minus_1_00_pct",
        "share_mae_le_minus_2_00_pct",
    ):
        lines.append(f"- {key}: **{overall.get(key)}**")
    lines.extend(["", "## By side", ""])
    for side, stats in (summary.get("by_side") or {}).items():
        lines.append(f"### `{side}`")
        lines.append(f"- signals: **{stats.get('signal_count')}**")
        lines.append(f"- tp_hit_rate: **{stats.get('tp_hit_rate')}**")
        lines.append(f"- median_candles_to_tp: **{stats.get('median_candles_to_tp')}**")
        lines.append(f"- median_mae_pct: **{stats.get('median_mae_pct')}**")
        lines.append(f"- worst_mae_pct: **{stats.get('worst_mae_pct')}**")
        lines.append("")
    lines.extend(["", "## By combined regime", ""])
    for regime, stats in (summary.get("by_combined_regime") or {}).items():
        lines.append(f"### `{regime}`")
        lines.append(f"- signals: **{stats.get('signal_count')}**")
        lines.append(f"- tp_hit_rate: **{stats.get('tp_hit_rate')}**")
        lines.append(f"- median_candles_to_tp: **{stats.get('median_candles_to_tp')}**")
        lines.append(f"- median_mae_pct: **{stats.get('median_mae_pct')}**")
        lines.append(f"- worst_mae_pct: **{stats.get('worst_mae_pct')}**")
        lines.append("")
    return "\n".join(lines)


def write_outputs(payload: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = payload.get("rows") or []
    safe_rows = json_safe(rows)
    paths = {
        "csv": out_dir / "signal_tp_rows.csv",
        "rows_json": out_dir / "signal_tp_rows.json",
        "summary_json": out_dir / "signal_tp_summary.json",
        "summary_md": out_dir / "signal_tp_summary.md",
    }
    csv_rows = []
    for row in rows:
        item = dict(row)
        codes = item.get("reason_codes") or []
        if isinstance(codes, list):
            item["reason_codes"] = json.dumps(json_safe(codes), ensure_ascii=True)
        csv_rows.append(item)
    pd.DataFrame(csv_rows).to_csv(paths["csv"], index=False)
    paths["rows_json"].write_text(
        json.dumps(safe_rows, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    paths["summary_json"].write_text(
        json.dumps(json_safe(payload.get("summary")), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    paths["summary_md"].write_text(format_summary_markdown(payload), encoding="utf-8")
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regime signal TP audit for long and/or short (backtest-only).",
    )
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframes", default="5m,15m,30m")
    parser.add_argument("--history-candles", type=int, default=144)
    parser.add_argument("--tp-pct", type=float, default=0.25)
    parser.add_argument("--max-hold-candles", type=int, default=48)
    parser.add_argument(
        "--sides",
        type=parse_sides,
        default=parse_sides("long"),
        help="Trade sides to audit: long | short | long,short (default: long)",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_signal_tp_simple",
    )
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--start",
        default=None,
        help="UTC start bound for signals (ISO). Warmup candles before start are kept.",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="UTC end bound exclusive for candle window (ISO).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(4, (os.cpu_count() or 2))),
        help="Process workers for regime evaluation (1 = sequential)",
    )
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--prefetch-batch-size", type=int, default=32)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    candles = load_symbol_candles(args.symbol, data_dir=args.data_dir)
    payload = scan_bullish_signal_tp(
        candles,
        symbol=args.symbol,
        timeframes=args.timeframes,
        history_candles=args.history_candles,
        tp_pct=args.tp_pct,
        max_hold_candles=args.max_hold_candles,
        workers=args.workers,
        progress_every=args.progress_every,
        prefetch_batch_size=args.prefetch_batch_size,
        start=args.start,
        end=args.end,
        sides=args.sides,
    )
    paths = write_outputs(payload, args.output_dir)
    overall = (payload.get("summary") or {}).get("overall") or {}
    print(
        f"Signal TP audit complete: sides={payload.get('sides')} "
        f"signals={overall.get('signal_count')} "
        f"tp_hit_rate={overall.get('tp_hit_rate')} "
        f"median_candles_to_tp={overall.get('median_candles_to_tp')}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Pure 1m Stoch entry-timing evaluation (research-only, no DB writes)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from signal_generator.research.one_m_entry_timing.constants import (
    TRIGGER_ENTRY_TRIGGERED,
    TRIGGER_NO_ENTRY_TIMEOUT,
    TRIGGER_WAITING_FOR_1M_EXTREME,
    TRIGGER_WAITING_FOR_1M_TURN,
    VARIANT_BASELINE_IMMEDIATE,
    VARIANT_WAIT_1M_EXTREME,
    VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
    VARIANT_WAIT_1M_EXTREME_TURN_SLOPE,
)
from signal_generator.strategy.wave_fade.indicators import stochastic_rsi
from signal_generator.strategy.wave_fade.parameters import (
    STOCH_HIGH_K,
    STOCH_LOW_K,
    STRATEGY_MAX_HOLD_BY_TF,
    TF_BAR_MIN,
)
from signal_generator.strategy.wave_fade.tpsl import tpsl_for_tf
from signal_generator.strategy.wave_fade.exits import scan_exit_sl_first


def _utc(ts: Any) -> datetime:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TimingResult:
    timing_variant: str
    trigger_state: str
    oversold_overbought_reached_at: str | None
    turn_confirmed_at: str | None
    entry_ts: str | None
    entry_price: float | None
    tp_price: float | None
    sl_price: float | None
    tp_pct: float | None
    sl_pct: float | None
    wait_minutes: float | None
    mae_pct: float | None
    mfe_pct: float | None
    result: str
    pnl_pct: float | None
    exit_time: str | None
    exit_price: float | None
    exit_reason: str | None
    duration_seconds: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prepare_1m(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    # Accept either open_time or timestamp
    if "open_time" in work.columns:
        work["timestamp"] = pd.to_datetime(work["open_time"], utc=True)
    elif "timestamp" in work.columns:
        work["timestamp"] = pd.to_datetime(work["timestamp"], utc=True)
    else:
        raise ValueError("1m candles require open_time or timestamp")
    work = work.sort_values("timestamp").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        work[col] = work[col].astype(float)
    k, d = stochastic_rsi(work["close"])
    work["stoch_k"] = k
    work["stoch_d"] = d
    return work


def _levels(side: str, entry: float, tf: str) -> dict[str, float]:
    tp_pct, sl_pct = tpsl_for_tf(tf, extra_4h=False)
    if side == "LONG":
        tp = entry * (1.0 + tp_pct / 100.0)
        sl = entry * (1.0 - sl_pct / 100.0)
    else:
        tp = entry * (1.0 - tp_pct / 100.0)
        sl = entry * (1.0 + sl_pct / 100.0)
    return {"tp": tp, "sl": sl, "tp_pct": tp_pct, "sl_pct": sl_pct}


def _mae_mfe(
    side: str,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    start_i: int,
    end_i: int,
) -> tuple[float | None, float | None]:
    if start_i < 0 or end_i < start_i or entry <= 0:
        return None, None
    hh = highs[start_i : end_i + 1]
    ll = lows[start_i : end_i + 1]
    if hh.size == 0:
        return None, None
    if side == "LONG":
        mfe = float(np.max((hh / entry - 1.0) * 100.0))
        mae = float(np.min((ll / entry - 1.0) * 100.0))
    else:
        mfe = float(np.max((entry - ll) / entry * 100.0))
        mae = float(np.min(-((hh - entry) / entry * 100.0)))
    return mae, mfe


def _simulate_outcome(
    *,
    side: str,
    entry: float,
    tf: str,
    entry_i: int,
    work: pd.DataFrame,
    as_of: datetime,
) -> dict[str, Any]:
    levels = _levels(side, entry, tf)
    highs = work["high"].to_numpy(dtype=float)
    lows = work["low"].to_numpy(dtype=float)
    max_hold = int(STRATEGY_MAX_HOLD_BY_TF.get(tf, 24 * 60))
    # Cap scan at as_of (closed bars only) — keep tz-aware Series searchsorted
    as_of_ts = pd.Timestamp(_utc(as_of))
    last_i = int(work["timestamp"].searchsorted(as_of_ts, side="right") - 1)
    if last_i < entry_i:
        return {
            "result": "OPEN",
            "pnl_pct": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "duration_seconds": None,
            "mae_pct": None,
            "mfe_pct": None,
            "tp_price": levels["tp"],
            "sl_price": levels["sl"],
            "tp_pct": levels["tp_pct"],
            "sl_pct": levels["sl_pct"],
        }
    end_hold = min(
        last_i,
        entry_i + max_hold,
    )
    reason, gross, exit_i, _amb = scan_exit_sl_first(
        side,
        entry,
        highs,
        lows,
        entry_i,
        end_hold,
        levels["tp_pct"],
        levels["sl_pct"],
    )
    mae, mfe = _mae_mfe(side, entry, highs, lows, entry_i, exit_i if exit_i is not None else last_i)
    if reason is None:
        # Still open — mark-to-last close for display MAE/MFE only
        entry_ts = _utc(work.iloc[entry_i]["timestamp"])
        last_ts = _utc(work.iloc[last_i]["timestamp"])
        return {
            "result": "OPEN",
            "pnl_pct": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "duration_seconds": int((last_ts - entry_ts).total_seconds()) if last_i >= entry_i else None,
            "mae_pct": mae,
            "mfe_pct": mfe,
            "tp_price": levels["tp"],
            "sl_price": levels["sl"],
            "tp_pct": levels["tp_pct"],
            "sl_pct": levels["sl_pct"],
        }
    assert exit_i is not None
    exit_ts = _utc(work.iloc[exit_i]["timestamp"])
    entry_ts = _utc(work.iloc[entry_i]["timestamp"])
    # Exit price: SL/TP level approx via gross
    if side == "LONG":
        exit_px = entry * (1.0 + float(gross) / 100.0)
    else:
        exit_px = entry * (1.0 - float(gross) / 100.0)
    return {
        "result": "WIN" if reason == "TP" else "LOSS",
        "pnl_pct": float(gross),
        "exit_time": _iso(exit_ts),
        "exit_price": float(exit_px),
        "exit_reason": reason,
        "duration_seconds": int((exit_ts - entry_ts).total_seconds()),
        "mae_pct": mae,
        "mfe_pct": mfe,
        "tp_price": levels["tp"],
        "sl_price": levels["sl"],
        "tp_pct": levels["tp_pct"],
        "sl_pct": levels["sl_pct"],
    }


def evaluate_1m_entry_timing(
    *,
    direction: str,
    signal_tf: str,
    signal_ts: datetime,
    baseline_entry_ts: datetime | None,
    baseline_entry_price: float | None,
    candles_1m: pd.DataFrame,
    timing_variant: str,
    as_of: datetime | None = None,
    timeout_minutes: int | None = None,
) -> TimingResult:
    """Evaluate research timing for one Tier-A signal using closed 1m bars only.

    Scan starts at the baseline entry open (T0) when provided, else at signal_ts.
    Features use only bars with timestamp <= as_of.
    """
    side = str(direction).upper()
    variant = str(timing_variant).strip()
    now = _utc(as_of or datetime.now(timezone.utc))
    t0 = _utc(baseline_entry_ts or signal_ts)
    timeout_m = int(
        timeout_minutes
        if timeout_minutes is not None
        else TF_BAR_MIN.get(signal_tf, 15)
    )
    deadline = t0 + timedelta(minutes=timeout_m)

    work = _prepare_1m(candles_1m)
    if work.empty:
        return TimingResult(
            timing_variant=variant,
            trigger_state=TRIGGER_WAITING_FOR_1M_EXTREME,
            oversold_overbought_reached_at=None,
            turn_confirmed_at=None,
            entry_ts=None,
            entry_price=None,
            tp_price=None,
            sl_price=None,
            tp_pct=None,
            sl_pct=None,
            wait_minutes=None,
            mae_pct=None,
            mfe_pct=None,
            result="OPEN",
            pnl_pct=None,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            duration_seconds=None,
        )

    # Closed bars available at as_of: open_time + 1m <= as_of → open_time <= as_of - 1m
    cutoff = pd.Timestamp(_utc(now)) - pd.Timedelta(minutes=1)
    work = work.loc[work["timestamp"] <= cutoff].reset_index(drop=True)
    if work.empty:
        return TimingResult(
            timing_variant=variant,
            trigger_state=TRIGGER_WAITING_FOR_1M_EXTREME,
            oversold_overbought_reached_at=None,
            turn_confirmed_at=None,
            entry_ts=None,
            entry_price=None,
            tp_price=None,
            sl_price=None,
            tp_pct=None,
            sl_pct=None,
            wait_minutes=None,
            mae_pct=None,
            mfe_pct=None,
            result="OPEN",
            pnl_pct=None,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            duration_seconds=None,
        )

    times = work["timestamp"]
    start_i = int(times.searchsorted(pd.Timestamp(_utc(t0)), side="left"))
    if start_i >= len(work):
        state = TRIGGER_NO_ENTRY_TIMEOUT if now >= deadline else TRIGGER_WAITING_FOR_1M_EXTREME
        return TimingResult(
            timing_variant=variant,
            trigger_state=state,
            oversold_overbought_reached_at=None,
            turn_confirmed_at=None,
            entry_ts=None,
            entry_price=None,
            tp_price=None,
            sl_price=None,
            tp_pct=None,
            sl_pct=None,
            wait_minutes=None,
            mae_pct=None,
            mfe_pct=None,
            result="OPEN",
            pnl_pct=None,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            duration_seconds=None,
        )

    # --- BASELINE_IMMEDIATE ---
    if variant == VARIANT_BASELINE_IMMEDIATE:
        entry_i = start_i
        entry_px = float(baseline_entry_price) if baseline_entry_price else float(work.iloc[entry_i]["open"])
        entry_ts = _utc(work.iloc[entry_i]["timestamp"])
        out = _simulate_outcome(side=side, entry=entry_px, tf=signal_tf, entry_i=entry_i, work=work, as_of=now)
        return TimingResult(
            timing_variant=variant,
            trigger_state=TRIGGER_ENTRY_TRIGGERED,
            oversold_overbought_reached_at=None,
            turn_confirmed_at=None,
            entry_ts=_iso(entry_ts),
            entry_price=entry_px,
            tp_price=out["tp_price"],
            sl_price=out["sl_price"],
            tp_pct=out["tp_pct"],
            sl_pct=out["sl_pct"],
            wait_minutes=0.0,
            mae_pct=out["mae_pct"],
            mfe_pct=out["mfe_pct"],
            result=out["result"],
            pnl_pct=out["pnl_pct"],
            exit_time=out["exit_time"],
            exit_price=out["exit_price"],
            exit_reason=out["exit_reason"],
            duration_seconds=out["duration_seconds"],
        )

    k = work["stoch_k"].to_numpy(dtype=float)
    d = work["stoch_d"].to_numpy(dtype=float)

    def is_extreme(i: int) -> bool:
        ki = k[i]
        if np.isnan(ki):
            return False
        if side == "LONG":
            return bool(ki <= STOCH_LOW_K)
        return bool(ki >= STOCH_HIGH_K)

    def slope_turn(i: int) -> bool:
        if i <= 0 or np.isnan(k[i]) or np.isnan(k[i - 1]):
            return False
        if side == "LONG":
            return bool(k[i] > k[i - 1])
        return bool(k[i] < k[i - 1])

    def cross_turn(i: int) -> bool:
        if i <= 0:
            return False
        if any(np.isnan(x) for x in (k[i], d[i], k[i - 1], d[i - 1])):
            return False
        if side == "LONG":
            return bool(k[i - 1] <= d[i - 1] and k[i] > d[i])
        return bool(k[i - 1] >= d[i - 1] and k[i] < d[i])

    extreme_i: int | None = None
    turn_i: int | None = None
    entry_i: int | None = None

    # Only scan bars whose open is before deadline (and available)
    last_scan = len(work) - 1
    for i in range(start_i, len(work)):
        bar_t = _utc(work.iloc[i]["timestamp"])
        if bar_t > deadline:
            last_scan = i - 1
            break
        last_scan = i
        if extreme_i is None and is_extreme(i):
            extreme_i = i
            if variant == VARIANT_WAIT_1M_EXTREME:
                # Entry at next bar open after extreme bar close
                if i + 1 < len(work):
                    entry_i = i + 1
                else:
                    entry_i = None  # wait for next closed bar
                break
        if extreme_i is not None and turn_i is None:
            if variant == VARIANT_WAIT_1M_EXTREME_TURN_SLOPE and slope_turn(i):
                turn_i = i
                entry_i = i + 1 if i + 1 < len(work) else None
                break
            if variant == VARIANT_WAIT_1M_EXTREME_TURN_CROSS and cross_turn(i):
                turn_i = i
                entry_i = i + 1 if i + 1 < len(work) else None
                break

    extreme_at = _iso(work.iloc[extreme_i]["timestamp"]) if extreme_i is not None else None
    turn_at = _iso(work.iloc[turn_i]["timestamp"]) if turn_i is not None else None

    # Determine state when entry not yet available
    if entry_i is not None and entry_i < len(work):
        entry_px = float(work.iloc[entry_i]["open"])
        entry_ts = _utc(work.iloc[entry_i]["timestamp"])
        wait_m = (entry_ts - t0).total_seconds() / 60.0
        out = _simulate_outcome(side=side, entry=entry_px, tf=signal_tf, entry_i=entry_i, work=work, as_of=now)
        return TimingResult(
            timing_variant=variant,
            trigger_state=TRIGGER_ENTRY_TRIGGERED,
            oversold_overbought_reached_at=extreme_at,
            turn_confirmed_at=turn_at,
            entry_ts=_iso(entry_ts),
            entry_price=entry_px,
            tp_price=out["tp_price"],
            sl_price=out["sl_price"],
            tp_pct=out["tp_pct"],
            sl_pct=out["sl_pct"],
            wait_minutes=wait_m,
            mae_pct=out["mae_pct"],
            mfe_pct=out["mfe_pct"],
            result=out["result"],
            pnl_pct=out["pnl_pct"],
            exit_time=out["exit_time"],
            exit_price=out["exit_price"],
            exit_reason=out["exit_reason"],
            duration_seconds=out["duration_seconds"],
        )

    # Pending / timeout
    timed_out = now >= deadline or (
        last_scan >= start_i and _utc(work.iloc[last_scan]["timestamp"]) >= deadline
    )
    if timed_out and entry_i is None:
        state = TRIGGER_NO_ENTRY_TIMEOUT
    elif extreme_i is None:
        state = TRIGGER_WAITING_FOR_1M_EXTREME
    else:
        # Extreme reached; waiting for turn confirmation or next bar after extreme
        state = TRIGGER_WAITING_FOR_1M_TURN

    wait_so_far = (min(now, deadline) - t0).total_seconds() / 60.0
    return TimingResult(
        timing_variant=variant,
        trigger_state=state,
        oversold_overbought_reached_at=extreme_at,
        turn_confirmed_at=turn_at,
        entry_ts=None,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        tp_pct=None,
        sl_pct=None,
        wait_minutes=wait_so_far,
        mae_pct=None,
        mfe_pct=None,
        result="OPEN",
        pnl_pct=None,
        exit_time=None,
        exit_price=None,
        exit_reason=None,
        duration_seconds=None,
    )

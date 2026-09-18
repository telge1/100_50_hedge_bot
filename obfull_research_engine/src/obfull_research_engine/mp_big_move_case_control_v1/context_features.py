"""Causal pre-entry price/trend context from completed candles only."""

from __future__ import annotations

from typing import Any, Sequence

from obfull_research_engine.mp_entry_confirmation_v1.candles5m import (
    aggregate_5m_from_1m,
    floor_5m_ns,
)
from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m

from .params import NS

ONE_MIN = 60 * NS


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    """SMA-seed EMA; k=2/(period+1) — matches dashboard/cluster_sweep semantics."""
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period or period < 1:
        return out
    seed = sum(float(v) for v in values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        prev = (float(values[i]) - prev) * k + prev
        out[i] = prev
    return out


def _completed_1m_before(candles: Sequence[Candle1m], entry_ts_ns: int) -> list[Candle1m]:
    """Candles whose open+1m <= entry_ts (fully closed before entry)."""
    out = []
    for c in candles:
        close_known = c.open_time_ns + ONE_MIN
        if close_known <= int(entry_ts_ns):
            out.append(c)
        else:
            break
    return out


def _completed_5m_before(candles_1m: Sequence[Candle1m], entry_ts_ns: int) -> list[Any]:
    # only use 1m bars closed before entry, then aggregate complete 5m buckets
    completed = _completed_1m_before(candles_1m, entry_ts_ns)
    bars5 = aggregate_5m_from_1m(completed)
    # drop incomplete trailing relative to entry: already incomplete dropped by aggregate
    # additionally require 5m close_time <= entry
    return [b for b in bars5 if b.close_time_ns <= int(entry_ts_ns)]


def _ret(closes: Sequence[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    a, b = float(closes[-(n + 1)]), float(closes[-1])
    if a == 0:
        return None
    return 100.0 * (b - a) / a


def _range_pct(candles: Sequence[Candle1m], n: int) -> float | None:
    if len(candles) < n:
        return None
    win = candles[-n:]
    hi = max(c.high for c in win)
    lo = min(c.low for c in win)
    mid = float(win[-1].close)
    if mid <= 0:
        return None
    return 100.0 * (hi - lo) / mid


def atr14_5m_pct(bars5: Sequence[Any]) -> float | None:
    if len(bars5) < 15:
        return None
    trs = []
    prev_c = float(bars5[0].close)
    for b in bars5[1:]:
        tr = max(b.high - b.low, abs(b.high - prev_c), abs(b.low - prev_c))
        trs.append(tr)
        prev_c = float(b.close)
    if len(trs) < 14:
        return None
    atr = sum(trs[-14:]) / 14.0
    px = float(bars5[-1].close)
    if px <= 0:
        return None
    return 100.0 * atr / px


def swing_counts_30m(candles: Sequence[Candle1m]) -> dict[str, int]:
    win = candles[-30:] if len(candles) >= 30 else candles
    hh = lh = hl = ll = 0
    for i in range(1, len(win)):
        if win[i].high > win[i - 1].high:
            hh += 1
        elif win[i].high < win[i - 1].high:
            lh += 1
        if win[i].low > win[i - 1].low:
            hl += 1
        elif win[i].low < win[i - 1].low:
            ll += 1
    return {
        "higher_highs_30m": hh,
        "lower_highs_30m": lh,
        "higher_lows_30m": hl,
        "lower_lows_30m": ll,
    }


def compute_price_context(
    candles_1m: Sequence[Candle1m],
    *,
    reference_entry_ts_ns: int,
    trade_side: str,
) -> dict[str, Any]:
    completed = _completed_1m_before(candles_1m, reference_entry_ts_ns)
    max_feature_ts = completed[-1].open_time_ns + ONE_MIN if completed else None
    leakage_ok = max_feature_ts is None or max_feature_ts <= int(reference_entry_ts_ns)
    out: dict[str, Any] = {
        "feature_cutoff_ts": int(reference_entry_ts_ns),
        "max_feature_ts": max_feature_ts,
        "leakage_check_passed": bool(leakage_ok),
        "n_completed_1m_before_entry": len(completed),
    }
    if not leakage_ok or len(completed) < 2:
        out["NOT_AVAILABLE"] = True
        return out

    closes = [c.close for c in completed]
    px = float(closes[-1])
    out["return_previous_15m_pct"] = _ret(closes, 15)
    out["return_previous_30m_pct"] = _ret(closes, 30)
    out["return_previous_60m_pct"] = _ret(closes, 60)
    out["return_previous_240m_pct"] = _ret(closes, 240)
    out["range_previous_30m_pct"] = _range_pct(completed, 30)
    out["range_previous_60m_pct"] = _range_pct(completed, 60)

    bars5 = _completed_5m_before(candles_1m, reference_entry_ts_ns)
    out["atr14_5m_pct"] = atr14_5m_pct(bars5)
    if len(bars5) >= 2:
        b = bars5[-1]
        out["vol_current_5m_pct"] = 100.0 * (b.high - b.low) / b.close if b.close else None
    else:
        out["vol_current_5m_pct"] = None

    # position in prior 1h range
    if len(completed) >= 60:
        win = completed[-60:]
        hi, lo = max(c.high for c in win), min(c.low for c in win)
        out["pos_in_prior_1h_range"] = (px - lo) / (hi - lo) if hi > lo else None
    else:
        out["pos_in_prior_1h_range"] = None

    for period, name in ((9, "ema9"), (20, "ema20"), (59, "ema59"), (200, "ema200")):
        series = ema_series(closes, period)
        val = series[-1]
        out[f"{name}"] = val
        if val is None or px <= 0:
            out[f"dist_{name}_pct"] = None
            out[f"slope_{name}"] = None
        else:
            out[f"dist_{name}_pct"] = 100.0 * (px - val) / px
            # slope vs previous completed bar's ema
            if len(series) >= 2 and series[-2] is not None:
                out[f"slope_{name}"] = float(val) - float(series[-2])
            else:
                out[f"slope_{name}"] = None

    e9, e20, e59, e200 = out.get("ema9"), out.get("ema20"), out.get("ema59"), out.get("ema200")
    if all(v is not None for v in (e9, e20, e59)):
        if e9 > e20 > e59:
            out["ema_order"] = "BULL_9_20_59"
        elif e9 < e20 < e59:
            out["ema_order"] = "BEAR_9_20_59"
        else:
            out["ema_order"] = "MIXED"
    else:
        out["ema_order"] = None
    out["price_above_ema59"] = (px > e59) if e59 is not None else None
    out["price_above_ema200"] = (px > e200) if e200 is not None else None

    side = trade_side.upper()
    # short-term: EMA9 slope; medium: EMA59; against ema200
    s9 = out.get("slope_ema9")
    s59 = out.get("slope_ema59")
    if s9 is None:
        out["trade_with_short_term_trend"] = None
    else:
        out["trade_with_short_term_trend"] = (s9 > 0) if side == "LONG" else (s9 < 0)
    if s59 is None:
        out["trade_with_medium_term_trend"] = None
    else:
        out["trade_with_medium_term_trend"] = (s59 > 0) if side == "LONG" else (s59 < 0)
    if e200 is None:
        out["trade_against_ema200"] = None
    else:
        # long below ema200 or short above = against
        out["trade_against_ema200"] = (px < e200) if side == "LONG" else (px > e200)

    out.update(swing_counts_30m(completed))
    # session UTC bucket of entry
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(int(reference_entry_ts_ns) / NS, tz=timezone.utc)
    hour = dt.hour
    if 0 <= hour < 8:
        sess = "UTC_0_8"
    elif 8 <= hour < 16:
        sess = "UTC_8_16"
    else:
        sess = "UTC_16_24"
    out["session_utc"] = sess
    return out

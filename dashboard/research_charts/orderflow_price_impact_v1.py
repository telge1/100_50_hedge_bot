"""Orderflow Price Impact (OPI) — rolling wall-clock windows on chart grid.

Reuses public_trades_canonical aggregation via load_orderflow (1m) and chart OHLC.
Missing flow or candle data is never emitted as numeric zero.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .orderflow_indicators import TF_SECONDS, floor_open, load_orderflow
from .public_trades_profile import NO_PUBLIC_TRADE_SYMBOLS
from .service import load_candles
WINDOW_MINUTES_ALLOWED = (5, 15, 30, 60)
MIN_TOTAL_USDT = Decimal("10000")
MIN_ABS_DELTA_USDT = Decimal("1000")
MAX_VISIBLE_SECONDS = 6 * 3600
WARMUP_PADDING_S = 3600
_CACHE_TTL_S = 45.0
_CACHE_MAX = 64
_Q4 = Decimal("0.0001")
_Q2 = Decimal("0.01")

_cache_lock = threading.Lock()
_opi_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def _utc_ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _fmt_money(d: Decimal | None) -> str | None:
    if d is None:
        return None
    return format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _fmt_pct(d: Decimal | None) -> str | None:
    if d is None:
        return None
    return format(d.quantize(_Q4, rounding=ROUND_HALF_UP), "f")


def _fmt_opi(d: Decimal | None) -> str | None:
    if d is None:
        return None
    return format(d.quantize(_Q4, rounding=ROUND_HALF_UP), "f")


def _cache_get(key: tuple) -> dict | None:
    now = time.monotonic()
    with _cache_lock:
        item = _opi_cache.get(key)
        if not item:
            return None
        expires, payload = item
        if expires < now:
            _opi_cache.pop(key, None)
            return None
        _opi_cache.move_to_end(key)
        return dict(payload)


def _cache_put(key: tuple, payload: dict) -> None:
    with _cache_lock:
        _opi_cache[key] = (time.monotonic() + _CACHE_TTL_S, dict(payload))
        _opi_cache.move_to_end(key)
        while len(_opi_cache) > _CACHE_MAX:
            _opi_cache.popitem(last=False)


def clear_opi_cache_for_tests() -> None:
    with _cache_lock:
        _opi_cache.clear()


def compute_opi_metrics(
    *,
    price_change_pct: Decimal,
    buy_usdt: Decimal,
    sell_usdt: Decimal,
    delta_usdt: Decimal,
    total_usdt: Decimal,
) -> dict[str, Any]:
    """Pure formula block for tests."""
    opi_total = None
    opi_delta = None
    quality_total = "ok" if total_usdt >= MIN_TOTAL_USDT else "low_volume"
    quality_delta = "ok" if abs(delta_usdt) >= MIN_ABS_DELTA_USDT else "low_delta"
    if quality_total == "ok" and total_usdt > 0:
        opi_total = price_change_pct / (total_usdt / Decimal("1000000"))
    if quality_delta == "ok" and abs(delta_usdt) > 0:
        opi_delta = price_change_pct / (abs(delta_usdt) / Decimal("1000000"))
    return {
        "buy_usdt": _fmt_money(buy_usdt),
        "sell_usdt": _fmt_money(sell_usdt),
        "total_usdt": _fmt_money(total_usdt),
        "delta_usdt": _fmt_money(delta_usdt),
        "price_change_pct": _fmt_pct(price_change_pct),
        "opi_total": _fmt_opi(opi_total),
        "opi_delta": _fmt_opi(opi_delta),
        "quality_total": quality_total,
        "quality_delta": quality_delta,
    }


def _aggregate_window(
    flow_by_time: dict[int, dict[str, Any]],
    window_start: int,
    window_end: int,
    as_of_s: int,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None, str]:
    """Return buy, sell, total, delta and quality (ok | gap | incomplete)."""
    buy = sell = Decimal("0")
    have_any = False
    t = window_start
    while t < window_end:
        bar = flow_by_time.get(t)
        step = 60
        forming = t <= as_of_s < t + step
        if bar is None:
            if forming:
                t += step
                continue
            return None, None, None, None, "incomplete"
        status = str(bar.get("status") or "")
        if status in {"GAP", "UNKNOWN"}:
            if forming:
                t += step
                continue
            return None, None, None, None, "gap"
        if status not in {"MEASURED", "FORMING"}:
            return None, None, None, None, "incomplete"
        b = _dec(bar.get("buy_usdt"))
        s = _dec(bar.get("sell_usdt"))
        if b is None or s is None:
            if forming:
                t += step
                continue
            return None, None, None, None, "incomplete"
        buy += b
        sell += s
        have_any = True
        t += step
    if not have_any:
        return None, None, None, None, "no_flow"
    total = buy + sell
    delta = buy - sell
    return buy, sell, total, delta, "ok"


def _close_at(close_by_time: dict[int, float], open_s: int) -> float | None:
    c = close_by_time.get(open_s)
    if c is not None and c > 0:
        return float(c)
    return None


def load_orderflow_price_impact(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime | None = None,
    window_minutes: int = 15,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip().lower()
    if sym in NO_PUBLIC_TRADE_SYMBOLS:
        raise ValueError("no_public_trades")
    if tf not in TF_SECONDS:
        raise ValueError("invalid_timeframe")
    wm = int(window_minutes)
    if wm not in WINDOW_MINUTES_ALLOWED:
        raise ValueError("invalid_window_minutes")
    start_s = _utc_ts(start)
    end_s = _utc_ts(end)
    if end_s <= start_s:
        raise ValueError("invalid_time_range")
    if end_s - start_s > MAX_VISIBLE_SECONDS:
        raise ValueError("time_range_too_large")
    as_of = as_of or datetime.now(timezone.utc)
    as_of_s = _utc_ts(as_of)
    if as_of_s < end_s:
        end_s = min(end_s, as_of_s)
    chart_step = TF_SECONDS[tf]
    window_s = wm * 60

    cache_key = (sym, tf, start_s, end_s, as_of_s, wm)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    read_start = datetime.fromtimestamp(start_s - window_s - WARMUP_PADDING_S, tz=timezone.utc)
    read_end = datetime.fromtimestamp(end_s + chart_step, tz=timezone.utc)
    query_end = min(read_end, as_of)

    flow = load_orderflow(sym, "1m", read_start, query_end, as_of=as_of, cvd_reset="range_start")
    flow_bars = flow.get("bars") or []
    flow_by_time: dict[int, dict[str, Any]] = {}
    for b in flow_bars:
        flow_by_time[int(b["time"])] = b

    candle_packed = load_candles(
        sym,
        "1m",
        start=int(read_start.timestamp()),
        end=int(query_end.timestamp()),
        limit=50000,
    )
    close_by_time: dict[int, float] = {}
    for c in candle_packed.get("candles") or []:
        close_by_time[int(c["time"])] = float(c["close"])

    grid_from = floor_open(start_s, chart_step)
    grid_to = floor_open(end_s - 1, chart_step)
    points: list[dict[str, Any]] = []
    open_s = grid_from
    while open_s <= grid_to:
        window_end = min(open_s + chart_step, as_of_s)
        if window_end <= open_s:
            open_s += chart_step
            continue
        window_start = window_end - window_s
        agg = _aggregate_window(flow_by_time, window_start, window_end, as_of_s)
        buy, sell, total, delta, flow_quality = agg
        close_start_open = window_start
        close_end_open = window_end - 60
        if close_end_open < close_start_open:
            close_end_open = close_start_open
        c0 = _close_at(close_by_time, close_start_open)
        c1 = _close_at(close_by_time, close_end_open)
        forming = open_s <= as_of_s < open_s + chart_step
        if flow_quality != "ok" or c0 is None or c1 is None:
            points.append(
                {
                    "time": open_s,
                    "status": "FORMING" if forming else "INSUFFICIENT",
                    "quality": flow_quality if flow_quality != "ok" else "no_price",
                    "buy_usdt": None,
                    "sell_usdt": None,
                    "total_usdt": None,
                    "delta_usdt": None,
                    "price_change_pct": None,
                    "opi_total": None,
                    "opi_delta": None,
                    "window_minutes": wm,
                }
            )
        else:
            price_change_pct = (Decimal(str(c1)) / Decimal(str(c0)) - Decimal("1")) * Decimal("100")
            metrics = compute_opi_metrics(
                price_change_pct=price_change_pct,
                buy_usdt=buy,
                sell_usdt=sell,
                delta_usdt=delta,
                total_usdt=total,
            )
            points.append(
                {
                    "time": open_s,
                    "status": "FORMING" if forming else "MEASURED",
                    "quality": "ok",
                    "window_minutes": wm,
                    "close_start": c0,
                    "close_end": c1,
                    **metrics,
                }
            )
        open_s += chart_step

    visible_from = start_s
    visible = [p for p in points if int(p["time"]) >= visible_from]
    payload = {
        "success": True,
        "symbol": sym,
        "timeframe": tf,
        "window_minutes": wm,
        "as_of": as_of.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "formulas": {
            "price_change_pct": "(close_end / close_start - 1) * 100",
            "opi_total": "price_change_pct / (total_usdt / 1_000_000)",
            "opi_delta": "price_change_pct / (abs(delta_usdt) / 1_000_000)",
        },
        "thresholds": {
            "min_total_usdt": str(MIN_TOTAL_USDT),
            "min_abs_delta_usdt": str(MIN_ABS_DELTA_USDT),
        },
        "bars": visible,
    }
    _cache_put(cache_key, payload)
    return payload
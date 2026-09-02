"""Public trade, OI, and liquidation fact extraction."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from .config import iso_z, utc


def window_trade_facts(
    trades: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    label: str | None = None,
) -> dict[str, Any]:
    start = utc(start)
    end = utc(end)
    xs = [t for t in trades if start <= t["ts"] < end]
    base = {
        "label": label,
        "start_utc": iso_z(start),
        "end_utc": iso_z(end),
        "buy_trade_count": 0,
        "sell_trade_count": 0,
        "buy_notional": 0.0,
        "sell_notional": 0.0,
        "delta_notional": 0.0,
        "first_price": None,
        "last_price": None,
        "high_price": None,
        "low_price": None,
        "price_change_bps": None,
        "range_bps": None,
        "aggressive_notional_total": None,
        "bps_per_million_delta": None,
        "bps_per_million_aggressive_notional": None,
        "large_trade_bucket_counts": {"ge_50k": 0, "ge_100k": 0, "ge_250k": 0},
        "trade_count": 0,
    }
    if not xs:
        return base
    buy = [t for t in xs if t["side"] == "Buy"]
    sell = [t for t in xs if t["side"] == "Sell"]
    buy_n = sum(t["notional"] for t in buy)
    sell_n = sum(t["notional"] for t in sell)
    delta = buy_n - sell_n
    first_p = xs[0]["price"]
    last_p = xs[-1]["price"]
    high_p = max(t["price"] for t in xs)
    low_p = min(t["price"] for t in xs)
    chg_bps = (last_p - first_p) / first_p * 10000.0
    range_bps = (high_p - low_p) / first_p * 10000.0
    aggressive = buy_n if delta >= 0 else sell_n
    bps_per_m_delta = _safe_div(chg_bps, delta / 1e6)
    bps_per_m_agg = _safe_div(abs(chg_bps), aggressive / 1e6)
    buckets = {"ge_50k": 0, "ge_100k": 0, "ge_250k": 0}
    for t in xs:
        n = t["notional"]
        if n >= 50_000:
            buckets["ge_50k"] += 1
        if n >= 100_000:
            buckets["ge_100k"] += 1
        if n >= 250_000:
            buckets["ge_250k"] += 1
    base.update(
        {
            "buy_trade_count": len(buy),
            "sell_trade_count": len(sell),
            "buy_notional": buy_n,
            "sell_notional": sell_n,
            "delta_notional": delta,
            "first_price": first_p,
            "last_price": last_p,
            "high_price": high_p,
            "low_price": low_p,
            "price_change_bps": chg_bps,
            "range_bps": range_bps,
            "aggressive_notional_total": aggressive,
            "bps_per_million_delta": bps_per_m_delta,
            "bps_per_million_aggressive_notional": bps_per_m_agg,
            "large_trade_bucket_counts": buckets,
            "trade_count": len(xs),
        }
    )
    return base


def build_trade_facts(
    trades: list[dict[str, Any]],
    anchor: datetime,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    anchor = utc(anchor)
    return {
        "before_window": window_trade_facts(trades, window_start, anchor, label="before_anchor"),
        "after_window": window_trade_facts(trades, anchor, window_end, label="after_anchor"),
        "full_window": window_trade_facts(trades, window_start, window_end, label="full"),
        "relative_windows": _relative_windows(trades, anchor),
        "time_series_buckets": _time_series_buckets(trades, window_start, window_end, bucket_seconds=60),
    }


def _relative_windows(trades: list[dict[str, Any]], anchor: datetime) -> list[dict[str, Any]]:
    specs = [
        ("anchor_0_10m", anchor, anchor + timedelta(minutes=10)),
        ("anchor_0_30m", anchor, anchor + timedelta(minutes=30)),
        ("anchor_pre_30m", anchor - timedelta(minutes=30), anchor),
    ]
    return [window_trade_facts(trades, s, e, label=label) for label, s, e in specs]


def _time_series_buckets(
    trades: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    bucket_seconds: int,
) -> list[dict[str, Any]]:
    start = utc(start)
    end = utc(end)
    buckets: list[dict[str, Any]] = []
    t = start
    while t < end:
        nxt = min(t + timedelta(seconds=bucket_seconds), end)
        buckets.append(window_trade_facts(trades, t, nxt, label=f"{bucket_seconds}s"))
        t = nxt
    return buckets


def _classify_liquidation_side(side: str) -> str | None:
    s = str(side).upper()
    if "LONG" in s:
        return "long"
    if "SHORT" in s:
        return "short"
    if s.startswith("BUY"):
        return "long"
    if s.startswith("SELL"):
        return "short"
    return None


def oi_liquidation_facts(
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    window_start = utc(window_start)
    window_end = utc(window_end)
    oi = [r for r in oi_rows if window_start <= r["ts"] < window_end]
    liq = [r for r in liq_rows if window_start <= r["ts"] < window_end]
    oi_first = oi[0]["oi"] if oi else None
    oi_last = oi[-1]["oi"] if oi else None
    oi_delta = (oi_last - oi_first) if oi_first is not None and oi_last is not None else None
    oi_delta_pct = _safe_div(oi_delta, oi_first) * 100 if oi_first else None
    long_liq = [r for r in liq if _classify_liquidation_side(r["side"]) == "long"]
    short_liq = [r for r in liq if _classify_liquidation_side(r["side"]) == "short"]
    long_notional = sum(r["notional"] for r in long_liq)
    short_notional = sum(r["notional"] for r in short_liq)
    largest_row = max(liq, key=lambda r: r["notional"], default=None)
    return {
        "oi_first": oi_first,
        "oi_last": oi_last,
        "oi_delta": oi_delta,
        "oi_delta_pct": oi_delta_pct,
        "oi_sample_count": len(oi),
        "oi_unit": {
            "source_field": "open_interest",
            "table": "orderbook_analysis.open_interest_5s",
            "physical_unit_confirmed": False,
            "display_label": "Source-Einheiten (open_interest)",
        },
        "liquidation_count": len(liq),
        "long_liquidation_notional": long_notional or None,
        "short_liquidation_notional": short_notional or None,
        "largest_liquidation": largest_row["notional"] if largest_row else None,
        "liquidation_summary": {
            "long_count": len(long_liq),
            "short_count": len(short_liq),
            "long_notional": long_notional or None,
            "short_notional": short_notional or None,
            "largest_notional": largest_row["notional"] if largest_row else None,
            "largest_ts": iso_z(largest_row["ts"]) if largest_row else None,
            "largest_price": largest_row.get("price") if largest_row else None,
            "side_semantics": "liquidated_position_side: LIQUIDATED_LONG / LIQUIDATED_SHORT",
        },
        "freshness": {
            "oi_last_ts": iso_z(oi[-1]["ts"]) if oi else None,
            "liq_last_ts": iso_z(liq[-1]["ts"]) if liq else None,
        },
        "coverage": {
            "oi_present": bool(oi),
            "liquidations_present": bool(liq),
            "window_start_utc": iso_z(window_start),
            "window_end_utc": iso_z(window_end),
        },
    }


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0 or math.isnan(den) or math.isinf(den):
        return None
    val = num / den
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value

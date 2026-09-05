"""Causal time×price bucket aggregation and size classification."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from orderbook_analyse.public_trade_bubbles import (
    DEFAULT_PRICE_TICKS_PER_BUCKET,
    DEFAULT_TIME_BUCKET_S,
    Q_EXTREME,
    Q_LARGE,
    Q_MEDIUM,
    SIZE_LOOKBACK_CLOSED_BUCKETS,
    SIZE_WARMUP_MIN,
)
from orderbook_analyse.public_trade_bubbles.contract import BubbleRecord, PublicTradeRecord


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def tick_size_for_symbol(symbol: str) -> float:
    sym = str(symbol or "").upper()
    if sym in {"DOGEUSDT", "1000PEPEUSDT", "1000BONKUSDT"}:
        return 1e-5
    if sym.endswith("USDT") and sym.startswith("1000"):
        return 1e-6
    # conservative BTC/ETH-like
    if sym in {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}:
        return 0.1 if sym == "BTCUSDT" else 0.01
    return 1e-4


def bucket_key(
    ts: datetime,
    price: float,
    *,
    time_bucket_s: int,
    price_step: float,
) -> tuple[int, int]:
    ts = _utc(ts)
    epoch = int(ts.timestamp())
    t_bucket = (epoch // time_bucket_s) * time_bucket_s
    if price_step <= 0:
        p_bucket = 0
    else:
        p_bucket = int(math.floor(price / price_step + 1e-12))
    return t_bucket, p_bucket


def _quantile(sorted_vals: Sequence[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    q = min(1.0, max(0.0, float(q)))
    idx = q * (len(sorted_vals) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(sorted_vals[lo])
    w = idx - lo
    return float(sorted_vals[lo] * (1 - w) + sorted_vals[hi] * w)


def classify_size(
    total_notional: float,
    prior_totals: Sequence[float],
) -> tuple[str, dict]:
    """Causal size class from prior closed-bucket totals only."""
    meta = {
        "sample_count": len(prior_totals),
        "threshold_medium": None,
        "threshold_large": None,
        "threshold_extreme": None,
    }
    if len(prior_totals) < SIZE_WARMUP_MIN:
        return "UNCALIBRATED", meta
    ordered = sorted(float(x) for x in prior_totals if x is not None and x >= 0)
    if len(ordered) < SIZE_WARMUP_MIN:
        return "UNCALIBRATED", meta
    t_med = _quantile(ordered, Q_MEDIUM)
    t_lrg = _quantile(ordered, Q_LARGE)
    t_ext = _quantile(ordered, Q_EXTREME)
    meta["threshold_medium"] = t_med
    meta["threshold_large"] = t_lrg
    meta["threshold_extreme"] = t_ext
    n = float(total_notional)
    if t_ext is not None and n >= t_ext:
        return "EXTREME", meta
    if t_lrg is not None and n >= t_lrg:
        return "LARGE", meta
    if t_med is not None and n >= t_med:
        return "MEDIUM", meta
    return "SMALL", meta


def filter_trades_as_of(
    trades: Iterable[PublicTradeRecord],
    as_of: datetime,
    *,
    require_received: bool = False,
) -> list[PublicTradeRecord]:
    """Keep trades with trade_timestamp <= as_of (and received_at if required)."""
    as_of = _utc(as_of)
    out: list[PublicTradeRecord] = []
    seen: set[str] = set()
    for t in trades:
        ts = _utc(t.trade_timestamp)
        if ts > as_of:
            continue
        if require_received and t.received_at is not None and _utc(t.received_at) > as_of:
            continue
        tid = str(t.trade_id)
        if tid in seen:
            continue
        seen.add(tid)
        out.append(t)
    out.sort(key=lambda x: (_utc(x.trade_timestamp), x.trade_id))
    return out


def aggregate_bubbles(
    trades: Sequence[PublicTradeRecord],
    *,
    symbol: str,
    as_of: datetime,
    time_bucket_s: int = DEFAULT_TIME_BUCKET_S,
    price_ticks_per_bucket: int = DEFAULT_PRICE_TICKS_PER_BUCKET,
    tick_size: float | None = None,
    require_received: bool = False,
    include_forming: bool = True,
) -> list[BubbleRecord]:
    """
    Build causal bubbles visible at as_of.

    Closed buckets: known_at = bucket_end, immutable.
    Forming (current open bucket): marked forming=True, known_at=as_of.
    Size class uses only closed buckets ending before the classified bucket start.
    """
    as_of = _utc(as_of)
    tick = float(tick_size if tick_size is not None else tick_size_for_symbol(symbol))
    price_step = tick * max(1, int(price_ticks_per_bucket))
    filtered = filter_trades_as_of(trades, as_of, require_received=require_received)
    if not filtered:
        return []

    # Accumulators: key -> stats
    buckets: dict[tuple[int, int], dict] = {}
    for t in filtered:
        key = bucket_key(
            t.trade_timestamp,
            float(t.price),
            time_bucket_s=time_bucket_s,
            price_step=price_step,
        )
        b = buckets.get(key)
        if b is None:
            b = {
                "buy": 0.0,
                "sell": 0.0,
                "count": 0,
                "max_single": 0.0,
                "price_sum": 0.0,
                "price_w": 0.0,
            }
            buckets[key] = b
        n = float(t.notional_quote)
        if t.is_aggressive_buy:
            b["buy"] += n
        elif t.is_aggressive_sell:
            b["sell"] += n
        else:
            # unknown side — count in total via neither; still track max
            pass
        b["count"] += 1
        b["max_single"] = max(b["max_single"], n)
        b["price_sum"] += float(t.price) * n
        b["price_w"] += n

    # Sort keys by time then price
    keys_sorted = sorted(buckets.keys(), key=lambda k: (k[0], k[1]))
    as_of_epoch = int(as_of.timestamp())
    current_bucket_start = (as_of_epoch // time_bucket_s) * time_bucket_s

    # Collect closed bucket totals chronologically for causal size lookback
    closed_totals_timeline: list[tuple[int, float]] = []  # (bucket_end_epoch, total)
    for t0, p0 in keys_sorted:
        end_epoch = t0 + time_bucket_s
        if end_epoch > as_of_epoch:
            continue  # forming — not in size history yet
        st = buckets[(t0, p0)]
        total = st["buy"] + st["sell"]
        closed_totals_timeline.append((end_epoch, total))
    closed_totals_timeline.sort(key=lambda x: x[0])

    bubbles: list[BubbleRecord] = []
    # For lookback we need totals of closed buckets with end < this bucket start
    # Maintain a sliding window of prior totals
    prior_vals: list[float] = []
    prior_i = 0  # pointer into closed_totals_timeline for streaming rebuild per time

    # Group by time bucket to advance prior lookback once per time
    by_time: dict[int, list[int]] = defaultdict(list)
    for t0, p0 in keys_sorted:
        by_time[t0].append(p0)

    for t0 in sorted(by_time.keys()):
        bucket_end_epoch = t0 + time_bucket_s
        bucket_start_dt = datetime.fromtimestamp(t0, tz=timezone.utc)
        bucket_end_dt = datetime.fromtimestamp(bucket_end_epoch, tz=timezone.utc)
        forming = bucket_end_epoch > as_of_epoch
        if forming and not include_forming:
            continue
        if t0 > current_bucket_start:
            # future relative to as_of open bucket — should not happen after filter
            continue

        # Advance prior closed totals: all with end_epoch <= t0 (strictly before this start)
        # Actually: bucket_end < current_bucket_start  <=> end_epoch <= t0
        while prior_i < len(closed_totals_timeline) and closed_totals_timeline[prior_i][0] <= t0:
            prior_vals.append(closed_totals_timeline[prior_i][1])
            prior_i += 1
            if len(prior_vals) > SIZE_LOOKBACK_CLOSED_BUCKETS:
                prior_vals = prior_vals[-SIZE_LOOKBACK_CLOSED_BUCKETS:]

        lookback = list(prior_vals)
        win_start = None
        win_end = None
        if lookback:
            # approximate window from lookback span
            win_end = bucket_start_dt
            win_start = bucket_start_dt - timedelta(seconds=time_bucket_s * len(lookback))

        for p0 in sorted(by_time[t0]):
            st = buckets[(t0, p0)]
            buy = float(st["buy"])
            sell = float(st["sell"])
            total = buy + sell
            if total <= 0 and st["count"] <= 0:
                continue
            price = (st["price_sum"] / st["price_w"]) if st["price_w"] > 0 else (p0 + 0.5) * price_step
            if buy > sell:
                dom = "BUY"
            elif sell > buy:
                dom = "SELL"
            else:
                dom = "FLAT"
            size_class, meta = classify_size(total, lookback)
            known_at = as_of if forming else bucket_end_dt
            max_feat = known_at
            if max_feat > known_at:
                max_feat = known_at
            bid = f"{symbol}|{t0}|{p0}|{time_bucket_s}|{price_ticks_per_bucket}"
            bubbles.append(
                BubbleRecord(
                    bubble_id=bid,
                    symbol=symbol,
                    bucket_start=bucket_start_dt,
                    bucket_end=bucket_end_dt,
                    price=float(price),
                    buy_notional=buy,
                    sell_notional=sell,
                    total_notional=total,
                    delta_notional=buy - sell,
                    trade_count=int(st["count"]),
                    max_single_trade_notional=float(st["max_single"]),
                    dominant_side=dom,
                    size_class=size_class,
                    known_at=known_at,
                    forming=forming,
                    source_quality="forming" if forming else "ok",
                    normalization_window_start=win_start,
                    normalization_window_end=win_end,
                    sample_count=int(meta["sample_count"]),
                    threshold_medium=meta["threshold_medium"],
                    threshold_large=meta["threshold_large"],
                    threshold_extreme=meta["threshold_extreme"],
                    max_feature_timestamp=max_feat,
                )
            )
    return bubbles


def bubbles_prefix_parity(
    trades: Sequence[PublicTradeRecord],
    *,
    symbol: str,
    as_of: datetime,
    **kwargs,
) -> tuple[list[BubbleRecord], list[BubbleRecord]]:
    """Return (bubbles(full, as_of), bubbles(prefix, as_of)) for equality tests."""
    as_of = _utc(as_of)
    full = aggregate_bubbles(trades, symbol=symbol, as_of=as_of, **kwargs)
    prefix = [t for t in trades if _utc(t.trade_timestamp) <= as_of]
    if kwargs.get("require_received"):
        prefix = [
            t
            for t in prefix
            if t.received_at is None or _utc(t.received_at) <= as_of
        ]
    pref = aggregate_bubbles(prefix, symbol=symbol, as_of=as_of, **kwargs)
    return full, pref


def finalize_forming_at_close(
    forming: BubbleRecord,
    *,
    closed_as_of: datetime,
) -> BubbleRecord:
    """Mark a forming bubble as closed/immutable at bucket_end (no repaint of id/OHLC)."""
    closed_as_of = _utc(closed_as_of)
    if closed_as_of < forming.bucket_end:
        raise ValueError("cannot finalize before bucket_end")
    return replace(
        forming,
        forming=False,
        known_at=forming.bucket_end,
        source_quality="ok",
        max_feature_timestamp=forming.bucket_end,
    )


def filter_display_mode(bubbles: Sequence[BubbleRecord], mode: str) -> list[BubbleRecord]:
    m = str(mode or "off").strip().lower()
    if m in ("off", "none", ""):
        return []
    if m == "large":
        return [b for b in bubbles if b.size_class in ("LARGE", "EXTREME")]
    if m in ("large_medium", "large+medium"):
        return [b for b in bubbles if b.size_class in ("MEDIUM", "LARGE", "EXTREME")]
    if m == "all":
        return list(bubbles)
    if m == "delta_debug":
        return list(bubbles)
    return list(bubbles)

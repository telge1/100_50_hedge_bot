"""Half-open intervals and exact 300-second rolling windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence


MINUTE_NS = 60_000_000_000
BUCKET_100MS_NS = 100_000_000


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    return as_utc(datetime.fromisoformat(text))


def dt_to_ns(dt: datetime) -> int:
    dt = as_utc(dt)
    return int(dt.timestamp() * 1_000_000_000)


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def assert_half_open(start: datetime, end: datetime) -> None:
    if as_utc(end) <= as_utc(start):
        raise ValueError("STOP_XRAY_WINDOW_EMPTY: end must be > start (half-open)")


def strategy_analysis_bounds(
    decision_time: datetime, *, pre_minutes: int, post_minutes: int
) -> tuple[datetime, datetime]:
    decision_time = as_utc(decision_time)
    start = decision_time - timedelta(minutes=int(pre_minutes))
    end = decision_time + timedelta(minutes=int(post_minutes))
    assert_half_open(start, end)
    return start, end


def rolling_exact_300s_windows(
    minute_rows: Sequence[dict[str, Any]],
    *,
    minute_key: str = "minute_ns",
) -> list[list[dict[str, Any]]]:
    """Return only full 5-bucket windows covering exactly 300 seconds.

    Uses ``minute_rows[i:i+5]``. Truncated tails are omitted from rankings.
    """
    out: list[list[dict[str, Any]]] = []
    n = len(minute_rows)
    for i in range(0, n - 4):
        window = list(minute_rows[i : i + 5])
        if len(window) != 5:
            continue
        start_ns = int(window[0][minute_key])
        end_ns = int(window[-1][minute_key]) + MINUTE_NS
        if end_ns - start_ns != 5 * MINUTE_NS:
            continue
        # consecutive minute buckets
        ok = True
        for j in range(4):
            if int(window[j + 1][minute_key]) - int(window[j][minute_key]) != MINUTE_NS:
                ok = False
                break
        if ok:
            out.append(window)
    return out


def rank_rolling_by_abs_net(
    minute_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for w in rolling_exact_300s_windows(minute_rows):
        net = float(w[-1]["c"]) - float(w[0]["o"])
        hi = max(float(x["h"]) for x in w)
        lo = min(float(x["l"]) for x in w)
        buy = sum(float(x.get("buy_notional") or 0.0) for x in w)
        sell = sum(float(x.get("sell_notional") or 0.0) for x in w)
        ranked.append(
            {
                "start_ns": int(w[0]["minute_ns"]),
                "end_ns": int(w[-1]["minute_ns"]) + MINUTE_NS,
                "start_utc": w[0].get("minute_utc"),
                "end_utc": w[-1].get("minute_utc"),
                "bucket_count": 5,
                "span_s": 300,
                "net_move": net,
                "range": hi - lo,
                "high": hi,
                "low": lo,
                "buy_notional": buy,
                "sell_notional": sell,
                "delta": buy - sell,
                "abs_net": abs(net),
            }
        )
    ranked.sort(key=lambda r: r["abs_net"], reverse=True)
    return ranked

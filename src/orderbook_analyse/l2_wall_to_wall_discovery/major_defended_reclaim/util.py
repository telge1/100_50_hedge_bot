"""Helpers for major defended reclaim discovery."""

from __future__ import annotations

import bisect
import csv
import json
import math
from pathlib import Path
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim import MISSING


def write_csv(path: Path, rows: list[dict[str, Any]], *, empty_reason: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        fields = ["note"] if empty_reason else ["_empty"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            if empty_reason:
                w.writerow({fields[0]: empty_reason})
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def safe_float(x: Any) -> float | None:
    if x is None or x == "" or x is MISSING:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def percentile_rank_sorted(sorted_hist: list[float], value: float) -> float | None:
    """Empirical mid-rank CDF using a sorted prior history (O(log n))."""
    if not sorted_hist:
        return None
    n = len(sorted_hist)
    lo = bisect.bisect_left(sorted_hist, value)
    hi = bisect.bisect_right(sorted_hist, value)
    return (lo + hi) * 0.5 / n


def percentile_rank(history: list[float], value: float) -> float | None:
    """Empirical CDF rank in [0,1] using strictly prior history."""
    if not history:
        return None
    return percentile_rank_sorted(sorted(history), value)

def band_around(price: float, tick: float, ticks: int = 2) -> tuple[float, float]:
    return (price - ticks * tick, price + ticks * tick)


def in_band(price: float, low: float, high: float) -> bool:
    return low <= price <= high


def notional(price: float, qty: float) -> float:
    return price * qty


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    m = len(ys) // 2
    if len(ys) % 2:
        return ys[m]
    return 0.5 * (ys[m - 1] + ys[m])


def pctile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = q * (len(ys) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    w = pos - lo
    return ys[lo] * (1 - w) + ys[hi] * w


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = 0.5 * (i + j) + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx <= 0 or deny <= 0:
        return None
    return num / (denx * deny)


def side_mfe_pct(entry: float, path: list[float], direction: str) -> tuple[float | None, int | None]:
    if not path or entry <= 0:
        return None, None
    if direction == "LONG":
        best = max(path)
        mfe = (best / entry - 1.0) * 100.0
        idx = path.index(best)
    else:
        best = min(path)
        mfe = (entry - best) / entry * 100.0
        idx = path.index(best)
    return mfe, idx


def side_endpoint_pct(entry: float, last: float, direction: str) -> float | None:
    if entry <= 0 or last is None:
        return None
    if direction == "LONG":
        return (last / entry - 1.0) * 100.0
    return (entry - last) / entry * 100.0


def bisect_trades(ts_list: list[int], start_ms: int, end_ms: int) -> tuple[int, int]:
    return bisect.bisect_left(ts_list, start_ms), bisect.bisect_left(ts_list, end_ms)


__all__ = [
    "write_csv",
    "write_json",
    "safe_float",
    "percentile_rank",
    "band_around",
    "in_band",
    "notional",
    "median",
    "pctile",
    "spearman",
    "side_mfe_pct",
    "side_endpoint_pct",
    "bisect_trades",
    "tick_size",
    "MISSING",
]

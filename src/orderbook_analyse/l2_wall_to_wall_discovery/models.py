"""Shared helpers for wall-to-wall discovery."""

from __future__ import annotations

import bisect
import csv
from pathlib import Path
from typing import Any, Iterable

from orderbook_analyse.l2_wall_attack_discovery.models import bps_between, safe_div, safe_float, tick_size, ticks_between
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    headers: list[str] | None = None,
    *,
    empty_reason: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        fields = headers or (["note"] if empty_reason else ["_empty"])
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            if empty_reason:
                w.writerow({fields[0]: empty_reason})
        return
    fields = headers or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def sample_index(samples: list[SampleRow]) -> list[int]:
    return [s.ts_ms for s in samples]


def sample_at(samples: list[SampleRow], ts_index: list[int], ts_ms: int) -> SampleRow | None:
    if not samples:
        return None
    i = bisect.bisect_right(ts_index, ts_ms) - 1
    if i < 0:
        return None
    return samples[i]


def samples_between(
    samples: list[SampleRow], ts_index: list[int], start_ms: int, end_ms: int
) -> list[SampleRow]:
    i0 = bisect.bisect_right(ts_index, start_ms)
    i1 = bisect.bisect_right(ts_index, end_ms)
    return samples[i0:i1]


def wall_qty(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    return safe_float(sample.bid_wall_qty if side == "BID" else sample.ask_wall_qty)


def wall_price(sample: SampleRow | None, side: str) -> float | None:
    if sample is None:
        return None
    return safe_float(sample.bid_wall_price if side == "BID" else sample.ask_wall_price)


def trade_side_for_module(module: str, wall_side: str) -> str:
    """Return position side LONG/SHORT."""
    if module == "WALL_HOLD_RECLAIM":
        return "LONG" if wall_side == "BID" else "SHORT"
    # WALL_REMOVED_BREAK
    return "SHORT" if wall_side == "BID" else "LONG"


def side_adjusted_return_bps(mid0: float, mid1: float, position_side: str) -> float | None:
    if mid0 is None or mid1 is None or mid0 <= 0:
        return None
    raw = (mid1 - mid0) / mid0 * 10000.0
    return raw if position_side == "LONG" else -raw


__all__ = [
    "write_csv",
    "read_csv",
    "sample_index",
    "sample_at",
    "samples_between",
    "wall_qty",
    "wall_price",
    "trade_side_for_module",
    "side_adjusted_return_bps",
    "bps_between",
    "ticks_between",
    "tick_size",
    "safe_div",
    "safe_float",
]

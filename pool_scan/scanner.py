"""Walk closed 4h bars once. Recognition at each bar ignores later bars.

``state_at_bar_open`` contains only pools already available and not yet
known-broken. Those are the only pools classified on that bar. A pool that
becomes available at the bar close is admitted afterwards and can be
classified from the next bar on. Outcome numbers are attached later and do
not rewrite recognition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from pool_scan.clock import available_at, bar_close, break_known_at


@dataclass(frozen=True)
class ScanParams:
    near_distances_pct: tuple[float, ...] = (0.1, 0.3, 0.5)
    touch_tolerance_pct: float = 0.0
    outcome_windows: tuple[int, ...] = (1, 2, 4)
    timeframe: str = "4h"

    def as_dict(self) -> dict:
        return {
            "near_distances_pct": list(self.near_distances_pct),
            "touch_tolerance_pct": self.touch_tolerance_pct,
            "outcome_windows": list(self.outcome_windows),
            "timeframe": self.timeframe,
        }


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.isoformat()


def _pool_view(pool, as_of: datetime) -> dict:
    """Pool fields known at ``as_of``. A later break stays null until its close."""
    known = break_known_at(pool)
    break_visible = known is not None and known <= as_of
    return {
        "pool_id": pool.pool_id,
        "side": pool.side,
        "bottom_price": pool.bottom_price,
        "top_price": pool.top_price,
        "source_open": _iso(pool.source_timestamp),
        "confirmation_open": _iso(pool.created_timestamp),
        "available_at": _iso(available_at(pool)),
        "invalidated_open": _iso(getattr(pool, "invalidated_timestamp", None)) if break_visible else None,
        "break_known_at": _iso(known) if break_visible else None,
    }


def _history_view(pool) -> dict:
    """Stored invalidation, including breaks that later bars have not reached yet."""
    return {
        "pool_id": pool.pool_id,
        "side": pool.side,
        "bottom_price": pool.bottom_price,
        "top_price": pool.top_price,
        "source_open": _iso(pool.source_timestamp),
        "confirmation_open": _iso(pool.created_timestamp),
        "available_at": _iso(available_at(pool)),
        "invalidated_open": _iso(getattr(pool, "invalidated_timestamp", None)),
        "break_known_at": _iso(break_known_at(pool)),
    }


def _distance_pct(pool, bar) -> float:
    if pool.side == "lower":
        return (bar.low - pool.top_price) / pool.top_price * 100.0
    if pool.side == "upper":
        return (pool.bottom_price - bar.high) / pool.bottom_price * 100.0
    raise ValueError(f"side must be upper|lower, got {pool.side!r}")


def _breaks(pool, bar) -> bool:
    if pool.side == "lower":
        return bar.low < pool.bottom_price
    return bar.high > pool.top_price


def classify(pool, bar, params: ScanParams) -> dict:
    """Relation of this bar to one pool that was active at the bar open."""
    distance = _distance_pct(pool, bar)
    if _breaks(pool, bar):
        relation = "break"
        bands: list[float] = []
    elif distance <= params.touch_tolerance_pct:
        relation = "touch"
        bands = []
    else:
        bands = [band for band in params.near_distances_pct if distance <= band]
        relation = "near" if bands else "away"
    return {
        "pool_id": pool.pool_id,
        "relation": relation,
        "distance_pct": round(distance, 6),
        "near_bands_pct": bands,
    }


def _engine_break_on_close(pool, close_time: datetime) -> bool:
    known = break_known_at(pool)
    return known is not None and known == close_time


def recognize(bars, pools, params: ScanParams) -> list[dict]:
    """One recognition record per bar. No outcome field is filled here."""
    ordered = tuple(sorted(bars, key=lambda bar: bar.open_time))
    broken: set[str] = set()
    records: list[dict] = []
    previous_close: float | None = None
    for bar in ordered:
        close_time = bar_close(bar.open_time, params.timeframe)
        if close_time != bar.close_time:
            raise ValueError("bar close_time does not match its open plus the timeframe")
        open_pools = [
            pool
            for pool in pools
            if available_at(pool) <= bar.open_time
            and pool.pool_id not in broken
            and not _known_broken_before(pool, bar.open_time)
        ]
        open_pools.sort(key=lambda pool: pool.pool_id)
        events = [classify(pool, bar, params) for pool in open_pools]
        by_id = {pool.pool_id: pool for pool in open_pools}
        for event in events:
            if event["relation"] != "break":
                continue
            broken.add(event["pool_id"])
            pool = by_id[event["pool_id"]]
            known = break_known_at(pool)
            if known == close_time:
                event["invalidated_open"] = _iso(getattr(pool, "invalidated_timestamp", None))
                event["break_known_at"] = _iso(known)
        for pool in pools:
            if _engine_break_on_close(pool, close_time):
                broken.add(pool.pool_id)
        admitted = [
            pool
            for pool in pools
            if available_at(pool) == close_time and pool.pool_id not in broken
        ]
        admitted.sort(key=lambda pool: pool.pool_id)
        close_active = [
            pool
            for pool in pools
            if available_at(pool) <= close_time and pool.pool_id not in broken
        ]
        close_active.sort(key=lambda pool: pool.pool_id)
        move = None if previous_close is None else (bar.close / previous_close - 1.0) * 100.0
        records.append(
            {
                "bar_open": _iso(bar.open_time),
                "bar_close": _iso(close_time),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "move_from_previous_close_pct": None if move is None else round(move, 6),
                "state_at_bar_open": [_pool_view(pool, bar.open_time) for pool in open_pools],
                "events": events,
                "state_at_bar_close": {
                    "active": [_pool_view(pool, close_time) for pool in close_active],
                    "broken_now": sorted(
                        event["pool_id"] for event in events if event["relation"] == "break"
                    ),
                    "admitted_now": [_pool_view(pool, close_time) for pool in admitted],
                },
            }
        )
        previous_close = bar.close
    return records


def _known_broken_before(pool, open_time: datetime) -> bool:
    known = break_known_at(pool)
    return known is not None and known <= open_time


def _window_outcome(bars, index: int, window: int) -> dict:
    bar = bars[index]
    later = bars[index + 1 : index + 1 + window]
    if len(later) < window:
        return {"bars": window, "complete": False}
    closes = [item.close for item in later]
    highs = [item.high for item in later]
    lows = [item.low for item in later]
    return {
        "bars": window,
        "complete": True,
        "max_up_from_close_pct": round((max(closes) / bar.close - 1.0) * 100.0, 6),
        "max_down_from_close_pct": round((1.0 - min(closes) / bar.close) * 100.0, 6),
        "max_up_from_low_pct": round((max(highs) / bar.low - 1.0) * 100.0, 6),
        "max_down_from_high_pct": round((1.0 - min(lows) / bar.high) * 100.0, 6),
    }


def attach_outcomes(records: list[dict], bars, params: ScanParams) -> list[dict]:
    """Add outcome beside recognition. The recognition mapping is not edited."""
    ordered = tuple(sorted(bars, key=lambda bar: bar.open_time))
    if len(ordered) != len(records):
        raise ValueError("outcome rows must match recognition rows")
    attached: list[dict] = []
    for index, record in enumerate(records):
        frozen = json.loads(json.dumps(record, sort_keys=True))
        row = {
            "recognition": frozen,
            "outcome": {
                "windows": [_window_outcome(ordered, index, window) for window in params.outcome_windows],
            },
        }
        if row["recognition"] != frozen:
            raise RuntimeError("outcome pass changed the recognition snapshot")
        attached.append(row)
    return attached


def scan(bars, pools, params: ScanParams, *, symbol: str, head: str, dirty: bool, start: str, end: str) -> dict:
    recognition = recognize(bars, pools, params)
    rows = attach_outcomes(recognition, bars, params)
    for row in rows:
        row["params"] = params.as_dict()
    return {
        "header": {
            "kind": "pool_scan",
            "symbol": symbol,
            "start": start,
            "end": end,
            "head": head,
            "dirty": dirty,
            "params": params.as_dict(),
            "later_pool_history": {
                "kind": "later_history",
                "note": "Stored invalidation times. Not known at earlier bar opens.",
                "pools": [_history_view(pool) for pool in sorted(pools, key=lambda pool: pool.pool_id)],
            },
            "time_semantics": {
                "bar_timestamp": "open",
                "pool_available_at": "confirmation_bar_close",
                "invalidated_timestamp": "breaking_bar_open",
                "break_known_at": "breaking_bar_close",
            },
        },
        "rows": rows,
    }


def to_jsonl(result: dict) -> str:
    lines = [json.dumps(result["header"], sort_keys=True, separators=(",", ":"))]
    for row in result["rows"]:
        lines.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    return "\n".join(lines) + "\n"

"""Real OB200 book levels for Research Charts depth side-panel.

Read-only reconstruction via ``ob200_walls.replay_book_as_of``.
No wall filtering, no synthetic levels, no features table fallback.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .ob200_walls import (
    DEFAULT_LIVE_ROOT,
    DEFAULT_SHADOW_ROOT,
    Ob200WallsError,
    replay_book_as_of,
)
from .trade_bubbles import tick_size

SOURCE_NAME = "ob200_raw_shadow_v3"
DEPTH = 200

# Freshness thresholds (ms). Three states, fully covering [0, +inf).
FRESH_MS = 15_000
STALE_MS = 180_000

_LEVELS_CACHE_MAX = 16
_LIVE_CACHE_TTL = 8.0
_HIST_CACHE_TTL = 45.0

_levels_cache_lock = threading.Lock()
_levels_cache: OrderedDict[tuple, tuple[float, dict[str, Any]]] = OrderedDict()
_inflight_lock = threading.Lock()
_inflight: dict[tuple, dict[str, Any]] = {}
_last_good_lock = threading.Lock()
_last_good: dict[str, dict[str, Any]] = {}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _level(price: Decimal | float, size: Decimal | float, side: str) -> dict[str, Any] | None:
    p = _finite(price)
    s = _finite(size)
    if p is None or s is None or s < 0:
        return None
    return {"price": p, "size": s, "side": side}


def sanitize_book_levels(
    bids_raw: list[tuple[Any, Any]] | list[dict[str, Any]],
    asks_raw: list[tuple[Any, Any]] | list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize, drop invalid/NaN, sort bids desc / asks asc, dedupe prices."""
    bids: list[dict[str, Any]] = []
    asks: list[dict[str, Any]] = []
    seen_b: set[float] = set()
    seen_a: set[float] = set()

    for item in bids_raw or []:
        if isinstance(item, dict):
            lvl = _level(item.get("price"), item.get("size"), "bid")
        else:
            lvl = _level(item[0], item[1], "bid")
        if lvl is None or lvl["price"] in seen_b:
            continue
        seen_b.add(lvl["price"])
        bids.append(lvl)

    for item in asks_raw or []:
        if isinstance(item, dict):
            lvl = _level(item.get("price"), item.get("size"), "ask")
        else:
            lvl = _level(item[0], item[1], "ask")
        if lvl is None or lvl["price"] in seen_a:
            continue
        seen_a.add(lvl["price"])
        asks.append(lvl)

    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    return bids, asks


def bar_length(
    notional: float,
    *,
    max_notional: float,
    scale: str,
    panel_width: float,
) -> float:
    """Map notional to bar pixel length. Shared Bid/Ask normalization."""
    if not math.isfinite(notional) or notional <= 0:
        return 0.0
    if not math.isfinite(max_notional) or max_notional <= 0:
        return 0.0
    if not math.isfinite(panel_width) or panel_width <= 0:
        return 0.0
    mode = (scale or "sqrt").strip().lower()
    ratio = notional / max_notional
    if mode == "linear":
        frac = ratio
    elif mode == "log":
        frac = math.log1p(ratio * 9.0) / math.log1p(9.0)
    else:
        frac = math.sqrt(ratio)
    frac = max(0.0, min(1.0, frac))
    return frac * panel_width * 0.95


def aggregate_levels(
    levels: list[dict[str, Any]],
    *,
    bucket_size: float,
    side: str,
) -> list[dict[str, Any]]:
    """Deterministic price-bucket aggregation. Never mixes bid and ask."""
    if bucket_size <= 0 or not math.isfinite(bucket_size):
        return [dict(x) for x in levels if x.get("side") == side.lower()]
    side_l = side.lower()
    buckets: dict[int, dict[str, Any]] = {}
    for lvl in levels:
        if lvl.get("side") != side_l:
            continue
        p = float(lvl["price"])
        s = float(lvl["size"])
        idx = int(math.floor(p / bucket_size + 1e-12))
        low = idx * bucket_size
        high = low + bucket_size
        mid_p = low + bucket_size * 0.5
        entry = buckets.get(idx)
        if entry is None:
            buckets[idx] = {
                "price": mid_p,
                "size": s,
                "side": side_l,
                "bucket_low": low,
                "bucket_high": high,
                "raw_level_count": 1,
            }
        else:
            entry["size"] += s
            entry["raw_level_count"] += 1
    out = list(buckets.values())
    if side_l == "bid":
        out.sort(key=lambda x: x["price"], reverse=True)
    else:
        out.sort(key=lambda x: x["price"])
    return out


def auto_bucket_size(
    tick: float,
    visible_low: float | None,
    visible_high: float | None,
    *,
    target_bars: int = 80,
) -> float:
    """Derive aggregation bucket from tick and visible price span."""
    t = tick if tick > 0 and math.isfinite(tick) else 1e-4
    if (
        visible_low is None
        or visible_high is None
        or not math.isfinite(visible_low)
        or not math.isfinite(visible_high)
        or visible_high <= visible_low
    ):
        return t * 10
    span = visible_high - visible_low
    raw = span / max(target_bars, 1)
    n = max(1, int(math.ceil(raw / t)))
    return n * t


def freshness_state(freshness_ms: int | None) -> str:
    """Classify freshness into fresh / delayed / stale / unknown."""
    if freshness_ms is None:
        return "unknown"
    if freshness_ms < 0:
        return "unknown"
    if freshness_ms <= FRESH_MS:
        return "fresh"
    if freshness_ms <= STALE_MS:
        return "delayed"
    return "stale"


def _parse_timestamp_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _apply_freshness(payload: dict[str, Any]) -> dict[str, Any]:
    book_ts = _parse_timestamp_utc(payload.get("timestamp_utc"))
    if book_ts is None:
        payload["freshness_ms"] = None
        payload["freshness_state"] = "unknown"
        return payload
    now = datetime.now(timezone.utc)
    freshness_ms = int((now - book_ts).total_seconds() * 1000)
    payload["freshness_ms"] = freshness_ms
    payload["freshness_state"] = freshness_state(freshness_ms)
    return payload


def _cache_key(sym: str, at_u: datetime, *, explicit_at: bool) -> tuple:
    """Cache key must preserve exact historical as-of semantics.

    Live tip (no explicit ``at``): one bounded entry per symbol; refresh via TTL.
    Source freshness is always derived from the replayed book timestamp in the payload.
    Historical ``at=``: exact UTC second — never time-bucketed.
    """
    if explicit_at:
        return (sym, "hist", int(at_u.timestamp()))
    return (sym, "live")


def _payload_from_snap(
    sym: str,
    snap: dict[str, Any],
    at_u: datetime,
    *,
    cached: bool,
    data_status: str = "current",
    data_status_reason: str | None = None,
) -> dict[str, Any]:
    bids, asks = sanitize_book_levels(snap["bids"], snap["asks"])
    book_ts: datetime = snap["as_of"]
    if book_ts.tzinfo is None:
        book_ts = book_ts.replace(tzinfo=timezone.utc)
    mid = _finite(snap.get("mid"))
    tick = tick_size(sym)
    payload: dict[str, Any] = {
        "symbol": sym,
        "timestamp_utc": book_ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "sequence": None,
        "depth": DEPTH,
        "bids": bids,
        "asks": asks,
        "source": SOURCE_NAME,
        "mid": mid,
        "best_bid": _finite(snap.get("best_bid")),
        "best_ask": _finite(snap.get("best_ask")),
        "tick_size": tick,
        "live_open": bool(snap.get("live_open")),
        "clamped": bool(snap.get("clamped")),
        "lag_seconds": snap.get("lag_seconds"),
        "as_of_requested_utc": at_u.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "cached": cached,
        "data_status": data_status,
    }
    if data_status_reason:
        payload["data_status_reason"] = data_status_reason
    return _apply_freshness(payload)


def _cache_put(key: tuple, payload: dict[str, Any], ttl: float) -> None:
    exp = time.monotonic() + ttl
    with _levels_cache_lock:
        _levels_cache[key] = (exp, dict(payload))
        _levels_cache.move_to_end(key)
        while len(_levels_cache) > _LEVELS_CACHE_MAX:
            _levels_cache.popitem(last=False)


def _cache_get(key: tuple) -> dict[str, Any] | None:
    now = time.monotonic()
    with _levels_cache_lock:
        hit = _levels_cache.get(key)
        if not hit or now >= hit[0]:
            return None
        _levels_cache.move_to_end(key)
        return dict(hit[1])


def _last_good_get(sym: str) -> dict[str, Any] | None:
    with _last_good_lock:
        hit = _last_good.get(sym)
        return dict(hit) if hit else None


def _last_good_put(sym: str, payload: dict[str, Any]) -> None:
    with _last_good_lock:
        _last_good[sym] = dict(payload)


def _last_good_fallback(sym: str, reason: str) -> dict[str, Any] | None:
    prev = _last_good_get(sym)
    if not prev or prev.get("symbol") != sym:
        return None
    out = dict(prev)
    out["cached"] = True
    out["data_status"] = "last_good"
    out["data_status_reason"] = reason
    return _apply_freshness(out)


def clear_ob200_levels_cache_for_tests() -> None:
    with _levels_cache_lock:
        _levels_cache.clear()
    with _inflight_lock:
        _inflight.clear()
    with _last_good_lock:
        _last_good.clear()


def load_ob200_levels(
    symbol: str,
    *,
    at: datetime | None = None,
    known_symbol: bool = True,
) -> dict[str, Any]:
    """Return full real OB200 levels for the side panel."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("empty_symbol")
    if not known_symbol and sym != "XAUUSDT":
        raise KeyError(sym)

    at_u = at or datetime.now(timezone.utc)
    if at_u.tzinfo is None:
        at_u = at_u.replace(tzinfo=timezone.utc)

    cache_key = _cache_key(sym, at_u, explicit_at=at is not None)
    cached = _cache_get(cache_key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return _apply_freshness(out)

    leader = False
    with _inflight_lock:
        slot = _inflight.get(cache_key)
        if slot is None:
            slot = {"event": threading.Event(), "result": None, "error": None}
            _inflight[cache_key] = slot
            leader = True

    if not leader:
        if not slot["event"].wait(timeout=120):
            fb = _last_good_fallback(sym, "coalesce_timeout")
            if fb:
                return fb
            raise TimeoutError("ob200_levels_coalesce_timeout")
        if slot["error"] is not None:
            fb = _last_good_fallback(sym, str(slot["error"]))
            if fb:
                return fb
            raise slot["error"]
        out = dict(slot["result"] or {})
        out["cached"] = True
        return _apply_freshness(out)

    try:
        snap = replay_book_as_of(
            sym,
            at_u,
            roots=[DEFAULT_SHADOW_ROOT, DEFAULT_LIVE_ROOT],
        )
        payload = _payload_from_snap(sym, snap, at_u, cached=False)
        _last_good_put(sym, payload)
        ttl = _LIVE_CACHE_TTL if cache_key[1] == "live" else _HIST_CACHE_TTL
        _cache_put(cache_key, payload, ttl)
        slot["result"] = payload
        return payload
    except Ob200WallsError as exc:
        fb = _last_good_fallback(sym, exc.code)
        if fb:
            slot["result"] = fb
            return fb
        slot["error"] = ValueError(exc.code)
        raise ValueError(exc.code) from exc
    except Exception as exc:
        fb = _last_good_fallback(sym, type(exc).__name__)
        if fb:
            slot["result"] = fb
            return fb
        slot["error"] = exc
        raise
    finally:
        slot["event"].set()
        with _inflight_lock:
            if _inflight.get(cache_key) is slot:
                del _inflight[cache_key]

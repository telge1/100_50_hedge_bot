"""ClickHouse read-only loader for AVR 1s buckets + candle summaries.

Includes a short TTL cache for second-bucket preroll so overlapping forming
polls (≈1.5s) do not each re-query and recompute the full 30m baseline from
scratch. Cache keys include symbol, range, and config_hash. No CH writes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .contracts import QUERY_TIMEOUT_S, TRADES_FQN
from .coverage import overlaps_known_gap
from .response_contracts import (
    BASELINE_LOOKBACK_S,
    DEFAULT_THRESHOLDS,
    RESPONSE_ENGINE_ID,
    RESPONSE_ENGINE_VERSION,
    AvrThresholds,
    config_hash,
)
from .response_engine import SecondBucket, SecondSeries, summarize_candle_response

logger = logging.getLogger(__name__)

# Forming-poll friendly: keep recent 1s series briefly.
_CACHE_TTL_S = 2.0
_CACHE_MAX = 8
_cache_lock = threading.Lock()
_bucket_cache: dict[str, tuple[float, list[SecondBucket]]] = {}


def _dt(unix: int) -> datetime:
    return datetime.fromtimestamp(int(unix), tz=timezone.utc)


def _cache_key(symbol: str, start: int, end: int, cfg_hash: str) -> str:
    return f"{symbol}|{int(start)}|{int(end)}|{cfg_hash}"


def _cache_get(key: str) -> list[SecondBucket] | None:
    now = time.monotonic()
    with _cache_lock:
        row = _bucket_cache.get(key)
        if not row:
            return None
        ts, buckets = row
        if now - ts > _CACHE_TTL_S:
            _bucket_cache.pop(key, None)
            return None
        return buckets


def _cache_put(key: str, buckets: list[SecondBucket]) -> None:
    with _cache_lock:
        if len(_bucket_cache) >= _CACHE_MAX:
            # Drop oldest
            oldest = min(_bucket_cache.items(), key=lambda kv: kv[1][0])[0]
            _bucket_cache.pop(oldest, None)
        _bucket_cache[key] = (time.monotonic(), buckets)


def clear_avr_bucket_cache() -> None:
    with _cache_lock:
        _bucket_cache.clear()


def fetch_second_buckets(
    client: Any,
    symbol: str,
    start: int,
    end: int,
) -> list[SecondBucket]:
    """Deduped trades → UTC 1s buckets. First/last via (trade_ts, trade_id)."""
    rows = client.query(
        f"""
        WITH dedup AS (
          SELECT
            argMax(trade_ts, ingest_timestamp) AS ts,
            argMax(side, ingest_timestamp) AS sd,
            argMax(toFloat64(price), ingest_timestamp) AS px,
            argMax(toFloat64(size), ingest_timestamp) AS sz,
            argMax(toFloat64(notional), ingest_timestamp) AS nt,
            trade_id
          FROM {TRADES_FQN}
          PREWHERE symbol = {{s:String}}
          WHERE trade_ts >= {{a:DateTime64(3,'UTC')}}
            AND trade_ts < {{b:DateTime64(3,'UTC')}}
          GROUP BY symbol, trade_id
        )
        SELECT
          toUnixTimestamp(toStartOfSecond(ts)) AS second_ts,
          sumIf(nt, sd = 'Buy') AS buy_notional,
          sumIf(nt, sd = 'Sell') AS sell_notional,
          sumIf(sz, sd = 'Buy') AS buy_size,
          sumIf(sz, sd = 'Sell') AS sell_size,
          countIf(sd = 'Buy') AS buy_trade_count,
          countIf(sd = 'Sell') AS sell_trade_count,
          argMin(px, (ts, trade_id)) AS first_price,
          argMax(px, (ts, trade_id)) AS last_price,
          max(px) AS high_price,
          min(px) AS low_price
        FROM dedup
        GROUP BY second_ts
        ORDER BY second_ts
        """,
        parameters={"s": symbol, "a": _dt(start), "b": _dt(end)},
        settings={"max_execution_time": int(QUERY_TIMEOUT_S)},
    ).result_rows
    out: list[SecondBucket] = []
    for row in rows:
        out.append(
            SecondBucket(
                second_ts=int(row[0]),
                buy_notional=float(row[1] or 0.0),
                sell_notional=float(row[2] or 0.0),
                buy_size=float(row[3] or 0.0),
                sell_size=float(row[4] or 0.0),
                buy_trade_count=int(row[5] or 0),
                sell_trade_count=int(row[6] or 0),
                first_price=float(row[7]) if row[7] is not None else None,
                last_price=float(row[8]) if row[8] is not None else None,
                high_price=float(row[9]) if row[9] is not None else None,
                low_price=float(row[10]) if row[10] is not None else None,
            )
        )
    return out


def fetch_second_buckets_cached(
    client: Any,
    symbol: str,
    start: int,
    end: int,
    *,
    cfg_hash: str,
) -> tuple[list[SecondBucket], bool]:
    """Return (buckets, cache_hit)."""
    # Quantize end to whole seconds; start already aligned.
    start_i, end_i = int(start), int(end)
    key = _cache_key(symbol, start_i, end_i, cfg_hash)
    hit = _cache_get(key)
    if hit is not None:
        return hit, True
    buckets = fetch_second_buckets(client, symbol, start_i, end_i)
    _cache_put(key, buckets)
    return buckets, False


def precompute_primary_features(
    series: SecondSeries,
    start: int,
    end: int,
    thresholds: AvrThresholds = DEFAULT_THRESHOLDS,
) -> dict[int, dict[str, Any]]:
    """Precompute features keyed by available_at (= second_ts + 1)."""
    from .response_engine import attach_acceleration

    out: dict[int, dict[str, Any]] = {}
    w = int(thresholds.primary_window_s)
    for sec in series.ts:
        available_at = int(sec) + 1
        if available_at <= int(start):
            continue
        if available_at > int(end):
            break
        feats = series.features_at(
            available_at, w, min_valid_frac=thresholds.minimum_valid_seconds
        )
        if feats is None:
            continue
        attach_acceleration(
            feats, series, available_at, w, min_valid_frac=thresholds.minimum_valid_seconds
        )
        out[available_at] = feats
    return out


def attach_avr_to_footprint(
    payload: dict[str, Any],
    *,
    client: Any,
    thresholds: AvrThresholds = DEFAULT_THRESHOLDS,
    now_unix: int | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if not enabled:
        payload["avr_engine"] = {"enabled": False}
        return payload

    t0 = time.perf_counter()
    symbol = payload["symbol"]
    rng = payload.get("range") or {}
    start = int(rng["from"])
    end = int(rng["to"])
    preroll_start = start - BASELINE_LOOKBACK_S
    now = int(now_unix) if now_unix is not None else int(time.time())
    cfg = config_hash(thresholds)

    query_s = 0.0
    compute_s = 0.0
    cache_hit = False
    try:
        tq = time.perf_counter()
        buckets, cache_hit = fetch_second_buckets_cached(
            client, symbol, preroll_start, end, cfg_hash=cfg
        )
        query_s = time.perf_counter() - tq
    except Exception:  # noqa: BLE001
        logger.exception("avr second-bucket query failed")
        payload["avr_engine"] = {
            "enabled": True,
            "success": False,
            "error": "avr_query_failed",
            "engine_id": RESPONSE_ENGINE_ID,
            "version": RESPONSE_ENGINE_VERSION,
            "config_hash": cfg,
        }
        return payload

    tc = time.perf_counter()
    series = SecondSeries(buckets)
    baseline_invalidated = overlaps_known_gap(preroll_start, start)
    invalidate_reason = "known_coverage_gap_in_baseline" if baseline_invalidated else None

    feats = precompute_primary_features(
        series, preroll_start, end, thresholds=thresholds
    )

    candles = payload.get("candles") or []
    for candle in candles:
        ct = int(candle["time"])
        avr = summarize_candle_response(
            candle_time=ct,
            ohlc={
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
            },
            coverage=str(candle.get("coverage") or "UNKNOWN"),
            series=series,
            now_unix=now,
            thresholds=thresholds,
            baseline_invalidated=baseline_invalidated,
            baseline_invalidate_reason=invalidate_reason,
            vpoc_price=candle.get("vpoc_price"),
            sample_every_s=5,
            precomputed_feats=feats,
        )
        candle["avr"] = avr

    compute_s = time.perf_counter() - tc
    total_s = time.perf_counter() - t0
    avr_bytes = len(
        json.dumps([c.get("avr") for c in candles], separators=(",", ":")).encode("utf-8")
    )

    payload["avr_engine"] = {
        "enabled": True,
        "success": True,
        "engine_id": RESPONSE_ENGINE_ID,
        "version": RESPONSE_ENGINE_VERSION,
        "config_hash": cfg,
        "thresholds": thresholds.to_dict(),
        "baseline_lookback_s": BASELINE_LOOKBACK_S,
        "baseline_method": "empirical_percentiles_causal_prior_1s_features",
        "causality": "half_open_[T-W,T)_trade_ts_lt_T",
        "preroll_from": preroll_start,
        "seconds_loaded": len(buckets),
        "bucket_cache_hit": cache_hit,
        "bucket_cache_ttl_s": _CACHE_TTL_S,
        "timing": {
            "query_s": round(query_s, 3),
            "compute_s": round(compute_s, 3),
            "total_s": round(total_s, 3),
        },
        "avr_payload_bytes": avr_bytes,
        "note": (
            "Decision support only — not a price forecast or trading signal. "
            "VACUUM_* states are PROXY_UNCONFIRMED without full OB proof."
        ),
    }
    return payload


__all__ = [
    "fetch_second_buckets",
    "fetch_second_buckets_cached",
    "clear_avr_bucket_cache",
    "precompute_primary_features",
    "attach_avr_to_footprint",
]

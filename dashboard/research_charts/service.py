"""Research history + indicator service. ClickHouse SoT; collector orchestrated."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from .boundary import SUPPORTED_TIMEFRAMES
from .clickhouse_source import SOURCE_NAME as CH_SOURCE_NAME
from .clickhouse_source import ClickHouseResearchCandleSource
from .collector_control import fetch_collector_status, fetch_forming_candle
from .data_source import MySQLResearchCandleSource, SOURCE_TF
from .live_universe import classify_live_capability, is_live_configured, load_live_universe_symbols
from .trp_import import load_trp

DEFAULT_LIMIT = 1500
MAX_LIMIT = 3000
# HTF panes need more completed bars so LLD can keep distant highs/lows.
# Calendar coverage (approx): 5m ~5d, 15m ~17d, 30m ~38d, 1h ~92d, 4h ~10m.
DEFAULT_LIMIT_BY_TF = {
    "1m": 1500,
    "5m": 1500,
    "15m": 1600,
    "30m": 1800,
    "1h": 2200,
    "4h": 1800,
}
_TF_SEC = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}
# Pine `amount` is a newest-N box cap. Same cap on 1h/4h drops old extreme pools.
LLD_AMOUNT_MULTIPLIER_BY_TF = {
    "1m": 1.0,
    "5m": 1.0,
    "15m": 1.5,
    "30m": 2.0,
    "1h": 3.0,
    "4h": 4.0,
}
MAX_LLD_AMOUNT = 4000
# Layout 4 default panes — 1m visible for smoke + HTF stack.
DEFAULT_PANE_TIMEFRAMES = ("1m", "5m", "15m", "1h")
DEFAULT_SOURCE_KIND = "clickhouse"

_CACHE_MAX = 48
_cache_lock = threading.Lock()
_candle_cache: OrderedDict[tuple, tuple[float, list]] = OrderedDict()
_inflight_lock = threading.Lock()
_inflight: dict[tuple, dict] = {}
_symbol_cache: tuple[float, list[dict]] | None = None
_SYMBOL_TTL = 15.0
_HISTORY_TTL = 45.0
_DEFAULT_WINDOW_TTL = 2.0


def _now() -> float:
    return time.monotonic()


def _cache_get(key: tuple, *, allow_stale: bool = False) -> list | None:
    with _cache_lock:
        item = _candle_cache.get(key)
        if not item:
            return None
        expires, value = item
        if _now() >= expires:
            if not allow_stale:
                _candle_cache.pop(key, None)
                return None
        _candle_cache.move_to_end(key)
        return value


def _cache_put(key: tuple, value: list, ttl: float | None) -> None:
    if ttl is None or ttl <= 0:
        return
    with _cache_lock:
        _candle_cache[key] = (_now() + float(ttl), value)
        _candle_cache.move_to_end(key)
        while len(_candle_cache) > _CACHE_MAX:
            _candle_cache.popitem(last=False)


def _cache_ttl(start: int | None, end: int | None) -> float | None:
    """Live tip / incremental polls are not cached for 45s."""
    if start is not None and end is None:
        return None
    if end is None:
        return _DEFAULT_WINDOW_TTL
    return _HISTORY_TTL


def _cache_key(symbol: str, timeframe: str, start, end, lim) -> tuple:
    return (symbol, timeframe, start, end, lim)


def clear_candle_cache_for_tests() -> None:
    with _cache_lock:
        _candle_cache.clear()
    with _inflight_lock:
        _inflight.clear()


def source_kind() -> str:
    raw = str(os.environ.get("RESEARCH_CANDLE_SOURCE") or DEFAULT_SOURCE_KIND).strip().lower()
    if raw in {"mysql", "market_candles", "mysql_market_candles_1m"}:
        return "mysql"
    return "clickhouse"


def get_source():
    if source_kind() == "mysql":
        return MySQLResearchCandleSource()
    return ClickHouseResearchCandleSource()


def candle_source_name() -> str:
    src = get_source()
    return getattr(src, "source_name", None) or (
        CH_SOURCE_NAME if source_kind() == "clickhouse" else "mysql_market_candles_1m"
    )


def candles_to_payload(candles) -> list[dict[str, float | int]]:
    return [
        {
            "time": int(c.unix_seconds),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume),
        }
        for c in candles
    ]


def apply_live_forming_tip(packed: dict[str, Any]) -> dict[str, Any]:
    """Stamp collector forming price onto the candle tip (never cached).

    ClickHouse 1m often lags the live forming bar by 1–2 minutes. Without this,
    Research charts freeze on the last closed CH candle even while forming-bar
    polls succeed.

    Historical windows (explicit ``to`` / ``end`` far in the past) are left alone.
    """
    out = dict(packed)
    candles = [dict(c) for c in (out.get("candles") or [])]
    sym = str(out.get("symbol") or "").strip().upper()
    tf = str(out.get("timeframe") or SOURCE_TF)
    step = _TF_SEC.get(tf)
    out["live_tip"] = False
    if not sym or not step or not candles:
        return out
    # Do not mutate historical pinned ranges.
    end_meta = out.get("to") or out.get("end")
    try:
        end_i = int(end_meta) if end_meta is not None else None
    except (TypeError, ValueError):
        end_i = None
    if end_i is not None and end_i < int(time.time()) - 180:
        out["forming"] = None
        return out
    try:
        forming = fetch_forming_candle(sym, timeout=1.5)
    except Exception:
        forming = None
    out["forming"] = forming
    if not isinstance(forming, dict):
        return out
    try:
        ft = int(forming["time"])
        px = float(forming["close"])
        hi = float(forming["high"] if forming.get("high") is not None else px)
        lo = float(forming["low"] if forming.get("low") is not None else px)
        vol = float(forming["volume"] if forming.get("volume") is not None else 0)
    except (TypeError, ValueError, KeyError):
        return out
    if ft <= 0 or px <= 0:
        return out
    bucket = (ft // step) * step
    last = candles[-1]
    try:
        last_t = int(last["time"])
        last_o = float(last["open"])
        last_h = float(last["high"])
        last_l = float(last["low"])
        last_c = float(last["close"])
    except (TypeError, ValueError, KeyError):
        return out

    if last_t == bucket:
        candles[-1] = {
            **last,
            "high": max(last_h, hi, px),
            "low": min(last_l, lo, px),
            "close": px,
        }
    elif last_t < bucket:
        candles.append(
            {
                "time": bucket,
                "open": last_c,
                "high": max(last_c, hi, px),
                "low": min(last_c, lo, px),
                "close": px,
                "volume": vol,
            }
        )
        # Keep payload size stable for limit-based loads (drop oldest closed bar).
        lim = out.get("limit")
        try:
            lim_i = int(lim) if lim is not None else None
        except (TypeError, ValueError):
            lim_i = None
        if lim_i is not None and lim_i > 0 and len(candles) > lim_i:
            candles = candles[-lim_i:]
    else:
        candles[-1] = {
            **last,
            "high": max(last_h, hi, px),
            "low": min(last_l, lo, px),
            "close": px,
        }
    out["candles"] = candles
    out["live_tip"] = True
    return out


def _unix_to_dt(value: int | None) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _enrich_symbols(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    universe = load_live_universe_symbols()
    status = fetch_collector_status()
    by_sym = {
        str(item.get("symbol") or "").upper(): item
        for item in (status.get("symbols") or [])
        if isinstance(item, dict)
    }
    out = []
    for row in rows:
        item = dict(row)
        sym = str(item.get("symbol") or "").upper()
        live_cfg = is_live_configured(sym, universe)
        runtime = by_sym.get(sym) or {}
        item["collector_configured"] = live_cfg
        item["collector_runtime_state"] = runtime.get("state")
        item["live_capability"] = classify_live_capability(
            history_available=int(item.get("candle_count") or 0) > 0,
            live_configured=live_cfg,
        )
        out.append(item)
    return out


def list_symbols(*, use_cache: bool = True) -> list[dict[str, Any]]:
    global _symbol_cache
    if use_cache and _symbol_cache and _now() - _symbol_cache[0] < _SYMBOL_TTL:
        return list(_symbol_cache[1])
    rows = _enrich_symbols(get_source().list_symbol_meta())
    _symbol_cache = (_now(), rows)
    return list(rows)


def known_symbols() -> set[str]:
    return {row["symbol"] for row in list_symbols()}


def symbol_meta(symbol: str) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    for row in list_symbols():
        if row["symbol"] == sym:
            return row
    return None


def default_limit(timeframe: str) -> int:
    return int(DEFAULT_LIMIT_BY_TF.get(timeframe, DEFAULT_LIMIT))


def scaled_lld_amount(amount: int, timeframe: str) -> int:
    """Keep more displayed pools on higher timeframes without changing the 5m base."""
    base = max(1, int(amount))
    mult = float(LLD_AMOUNT_MULTIPLIER_BY_TF.get(str(timeframe), 1.0))
    return max(1, min(int(round(base * mult)), MAX_LLD_AMOUNT))


def lld_config_for_timeframe(config, timeframe: str):
    """Copy LLD config with a TF-scaled display amount. Detection lengths stay as set."""
    trp = load_trp()
    tf = str(timeframe or "")
    return trp["dc_replace"](config, amount=scaled_lld_amount(int(config.amount), tf))


def load_candles(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    trp = load_trp()
    t_trp = time.perf_counter()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("invalid_timeframe")
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("invalid_symbol")
    if sym not in known_symbols():
        raise KeyError("unknown_symbol")

    lim = int(limit) if limit else default_limit(timeframe)
    min_lim = 1 if start is not None else 20
    lim = max(min_lim, min(lim, MAX_LIMIT))
    src_name = candle_source_name()
    cache_key = (sym, timeframe, start, end, lim)
    cached = _cache_get(cache_key)
    if cached is not None:
        return apply_live_forming_tip(
            _packed_from_cached(cached, sym, timeframe, src_name, start, end, lim, t0)
        )

    leader = False
    with _inflight_lock:
        slot = _inflight.get(cache_key)
        if slot is None:
            slot = {"event": threading.Event(), "result": None, "error": None}
            _inflight[cache_key] = slot
            leader = True
    if not leader:
        if not slot["event"].wait(timeout=120):
            raise TimeoutError("candle_coalesce_timeout")
        if slot["error"] is not None:
            raise slot["error"]
        # Re-stamp forming on coalesced waiters so tip stays live.
        return apply_live_forming_tip(dict(slot["result"] or {}))
    try:
        packed = apply_live_forming_tip(
            _load_candles_compute(
                trp, sym, timeframe, start, end, lim, src_name, cache_key, t0, t_trp
            )
        )
        # Cache store happens inside _load_candles_compute without forming tip.
        slot["result"] = packed
        return packed
    except Exception as exc:
        slot["error"] = exc
        raise
    finally:
        slot["event"].set()
        with _inflight_lock:
            if _inflight.get(cache_key) is slot:
                del _inflight[cache_key]


def _packed_from_cached(cached, sym, timeframe, src_name, start, end, lim, t0):
    return {
        "symbol": sym,
        "timeframe": timeframe,
        "source": src_name,
        "aggregation": "none" if timeframe == SOURCE_TF else "trp_aggregate_strict",
        "strict_complete_buckets": timeframe != SOURCE_TF,
        "feed_ready": True,
        "candles": candles_to_payload(cached),
        "from": start,
        "to": end,
        "limit": lim,
        "cache": "hit",
        "timings_ms": {"total": round((time.perf_counter() - t0) * 1000, 2)},
    }


def _load_candles_compute(trp, sym, timeframe, start, end, lim, src_name, cache_key, t0, t_trp):
    src = get_source()
    t_db0 = time.perf_counter()
    if timeframe == SOURCE_TF:
        candles = src.get_1m_candles(
            sym,
            start=_unix_to_dt(start),
            end=_unix_to_dt(end),
            limit=None if (start is not None and end is not None) else lim,
            newest_first_limit=start is None,
        )
        if start is not None and end is None:
            candles = [c for c in candles if c.unix_seconds >= int(start)]
        if end is not None:
            candles = [c for c in candles if c.unix_seconds <= int(end)]
        if start is None and end is None and len(candles) > lim:
            candles = candles[-lim:]
        t_db1 = time.perf_counter()
        t_agg1 = t_db1
    else:
        need = int(trp["expected_source_bars"](SOURCE_TF, timeframe))
        pad = need  # extra 1m bars so the newest HTF bucket can complete
        if start is None and end is None:
            source_1m = src.get_1m_candles(
                sym, limit=lim * need + pad, newest_first_limit=True
            )
        else:
            src_start = start
            if start is not None:
                src_start = int(start) - need * 60
            source_1m = src.get_1m_candles(
                sym,
                start=_unix_to_dt(src_start),
                end=_unix_to_dt(end),
                newest_first_limit=False,
            )
        t_db1 = time.perf_counter()
        candles = trp["aggregate"](source_1m, timeframe, strict_complete_buckets=True)
        if start is not None:
            candles = [c for c in candles if c.unix_seconds >= int(start)]
        if end is not None:
            candles = [c for c in candles if c.unix_seconds <= int(end)]
        if start is None and end is None and len(candles) > lim:
            candles = candles[-lim:]
        t_agg1 = time.perf_counter()

    _cache_put(cache_key, candles, _cache_ttl(start, end))
    payload = candles_to_payload(candles)
    t1 = time.perf_counter()
    return {
        "symbol": sym,
        "timeframe": timeframe,
        "source": src_name,
        "aggregation": "none" if timeframe == SOURCE_TF else "trp_aggregate_strict",
        "strict_complete_buckets": timeframe != SOURCE_TF,
        "feed_ready": True,
        "candles": payload,
        "from": start if payload else start,
        "to": end,
        "limit": lim,
        "cache": "miss",
        "timings_ms": {
            "trp_import": round((t_trp - t0) * 1000, 2),
            "db": round((t_db1 - t_db0) * 1000, 2),
            "aggregate": round((t_agg1 - t_db1) * 1000, 2),
            "serialize": round((t1 - t_agg1) * 1000, 2),
            "total": round((t1 - t0) * 1000, 2),
        },
        "response_bytes_est": len(payload) * 64,
    }


def _candles_from_packed(packed: dict[str, Any], *, allow_stale: bool = False) -> list:
    cache_key = (
        packed["symbol"],
        packed["timeframe"],
        packed.get("from"),
        packed.get("to"),
        packed.get("limit"),
    )
    candles = _cache_get(cache_key, allow_stale=allow_stale)
    if candles is not None:
        return candles
    trp = load_trp()
    Candle = trp["Candle"]
    return [
        Candle(
            timestamp=datetime.fromtimestamp(int(c["time"]), tz=timezone.utc),
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            volume=float(c["volume"]),
            symbol=packed["symbol"],
            timeframe=packed["timeframe"],
        )
        for c in packed["candles"]
    ]


def candle_objects(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
    allow_stale: bool = False,
) -> list:
    packed = resolve_candle_pack(
        symbol, timeframe, start=start, end=end, limit=limit, allow_stale=allow_stale
    )
    return _candles_from_packed(packed, allow_stale=True)


def resolve_candle_pack(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
    allow_stale: bool = False,
) -> dict[str, Any]:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("invalid_timeframe")
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("invalid_symbol")
    if sym not in known_symbols():
        raise KeyError("unknown_symbol")
    lim = int(limit) if limit else default_limit(timeframe)
    min_lim = 1 if start is not None else 20
    lim = max(min_lim, min(lim, MAX_LIMIT))
    cache_key = _cache_key(sym, timeframe, start, end, lim)
    cached = _cache_get(cache_key, allow_stale=allow_stale)
    if cached is not None:
        return apply_live_forming_tip(
            _packed_from_cached(
                cached, sym, timeframe, candle_source_name(), start, end, lim, time.perf_counter()
            )
        )
    return load_candles(symbol, timeframe, start=start, end=end, limit=limit)


def _indicators_from_candles(
    packed: dict[str, Any],
    candles: list,
    *,
    ema: dict | None = None,
    stochastic: dict | None = None,
    liquidity: dict | None = None,
) -> dict[str, Any]:
    trp = load_trp()
    ema_raw = dict(ema or {})
    stoch_raw = dict(stochastic or {})
    lld_raw = dict(liquidity or {})

    ema_payload = {"series": []}
    if ema_raw.get("lines") is not None:
        ema_cfg = trp["EmaOverlaysConfig"].from_dict(ema_raw)
        ema_payload = trp["ema_overlays_payload"](candles, ema_cfg)
    elif bool(ema_raw.get("enabled")):
        ema_cfg = trp["EmaOverlaysConfig"].defaults()
        ema_payload = trp["ema_overlays_payload"](candles, ema_cfg)

    stoch_cfg = trp["StochasticConfig"].from_dict(stoch_raw) if stoch_raw else trp["StochasticConfig"].defaults()
    stoch_payload = trp["stochastic_payload"](None, stoch_cfg)
    if bool(stoch_cfg.enabled):
        result = trp["compute_stochastic"](candles, stoch_cfg)
        stoch_payload = trp["stochastic_payload"](result, stoch_cfg)

    overlays: list = []
    lld_ema = {"fast": [], "slow": [], "fast_visible": False, "slow_visible": False}
    lld_clusters = {"3": 0, "4-5": 0, "6+": 0}
    lld_cfg = (
        trp["LiquidityLocationConfig"].from_dict(lld_raw)
        if lld_raw
        else trp["LiquidityLocationConfig"].defaults()
    )
    if bool(lld_cfg.enabled):
        lld_cfg = lld_config_for_timeframe(lld_cfg, packed.get("timeframe") or "")
        lld_result = trp["run_liquidity_location"](candles, lld_cfg)
        clusters = None
        if bool(lld_cfg.clusters_enabled):
            clusters = trp["cluster_pools"](
                lld_result.pools,
                gap_pct=float(lld_cfg.cluster_gap_pct),
                active_only=True,
            )
            shown = trp["filter_clusters"](
                clusters, minimum_pools=int(lld_cfg.minimum_cluster_pools)
            )
            lld_clusters = trp["cluster_bucket_counts"](shown)
        overlays = trp["serialize_overlays"](
            trp["compose_lld_overlays"](lld_result, lld_cfg, clusters=clusters)
        )
        lld_ema = trp["lld_ema_payload"](lld_result, lld_cfg)

    return {
        "success": True,
        "feed_ready": True,
        "compute_in": "python",
        "symbol": packed["symbol"],
        "timeframe": packed["timeframe"],
        "ema": ema_payload,
        "stochastic": stoch_payload,
        "liquidity": {
            "overlays": overlays,
            "ema": lld_ema,
            "clusters": lld_clusters,
        },
    }


def compute_indicators(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
    ema: dict | None = None,
    stochastic: dict | None = None,
    liquidity: dict | None = None,
) -> dict[str, Any]:
    packed = resolve_candle_pack(
        symbol, timeframe, start=start, end=end, limit=limit, allow_stale=True
    )
    candles = _candles_from_packed(packed, allow_stale=True)
    return _indicators_from_candles(
        packed, candles, ema=ema, stochastic=stochastic, liquidity=liquidity
    )


def pane_bundle(
    symbol: str,
    timeframe: str,
    *,
    start: int | None = None,
    end: int | None = None,
    limit: int | None = None,
    ema: dict | None = None,
    stochastic: dict | None = None,
    liquidity: dict | None = None,
    allow_stale: bool = False,
    liquidity_location_as_of: str | None = None,
) -> dict[str, Any]:
    """One candle read, then EMA/Stoch/LLD/overlays from that same payload."""
    from .workspace_session import get_workspace

    packed = resolve_candle_pack(
        symbol, timeframe, start=start, end=end, limit=limit, allow_stale=allow_stale
    )
    candles = _candles_from_packed(packed, allow_stale=True)
    ws = get_workspace()
    ema_cfg = ema if ema is not None else ws.ema_config.to_dict()
    stoch_cfg = stochastic if stochastic is not None else ws.stoch_config.to_dict()
    lld_cfg = liquidity if liquidity is not None else ws.lld_config.to_dict()
    indicators = _indicators_from_candles(
        packed, candles, ema=ema_cfg, stochastic=stoch_cfg, liquidity={"enabled": False}
    )
    trp = load_trp()
    lld_config_obj = (
        trp["LiquidityLocationConfig"].from_dict(lld_cfg) if lld_cfg else ws.lld_config
    )
    liquidity_meta: dict[str, Any] = {"mode": "live", "liquidity_location_as_of": None}
    lld_asof_raw = str(liquidity_location_as_of or "").strip() or None
    use_causal = bool(lld_asof_raw) and bool(lld_cfg.get("enabled", lld_config_obj.enabled))
    if use_causal:
        # OA path must be bootstrapped before any orderbook_analyse import.
        from .canonical_lld import build_causal_lld_payload, parse_liquidity_location_as_of
        from .oa_import import ensure_oa_on_path

        ensure_oa_on_path()
        as_of_dt = parse_liquidity_location_as_of(lld_asof_raw)
        render_end_dt = None
        if end is not None:
            try:
                render_end_dt = datetime.fromtimestamp(int(end), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                render_end_dt = None
        causal = build_causal_lld_payload(
            symbol=packed["symbol"],
            timeframe=packed["timeframe"],
            as_of=as_of_dt,
            liquidity=lld_cfg,
            render_end=render_end_dt,
        )
        lld_serialized = causal["overlays"]
        lld_ema = causal["ema"]
        clusters = causal["clusters"]
        liquidity_meta = causal["meta"]
        from .workspace_session import overlay_namespace
        from .nested_ask_pool_backtester import json_safe

        lld_payloads = []
        for payload in lld_serialized:
            row = dict(payload)
            row["namespace"] = overlay_namespace(row)
            cleaned = json_safe(row)
            if isinstance(cleaned, dict):
                lld_payloads.append(cleaned)
        overlays = ws.composed_overlays(packed["symbol"], packed["timeframe"], lld_overlays=None)
        overlays = overlays + lld_payloads
    else:
        lld_objs, lld_ema, clusters = ws.lld_objects(candles, config=lld_config_obj)
        lld_serialized = trp["serialize_overlays"](lld_objs) if lld_objs else []
        overlays = ws.composed_overlays(packed["symbol"], packed["timeframe"], lld_objs)
    return {
        **packed,
        "success": True,
        "feed_ready": True,
        "ema": indicators["ema"],
        "stochastic": indicators["stochastic"],
        "liquidity": {
            "overlays": lld_serialized,
            "ema": lld_ema,
            "clusters": clusters,
            "liquidity_location": liquidity_meta,
        },
        "overlays": overlays,
        "lld_ema": lld_ema,
        "clusters": clusters,
        "liquidity_location_mode": liquidity_meta.get("mode"),
        "liquidity_location_as_of": liquidity_meta.get("liquidity_location_as_of"),
        "canonical_snapshot_sha256": liquidity_meta.get("canonical_snapshot_sha256"),
    }

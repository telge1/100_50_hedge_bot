"""Direct reuse of the Research Chart Liquidity Location engine.

Invariant
---------
``engine_function`` is the identical callable bound by
``research_charts.trp_import.load_trp()["run_liquidity_location"]``,
i.e. ``indicators.liquidity_location.engine.run_liquidity_location``.

No pool formulas are copied. No nested strategy. No trading logic.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.liquidity_pool_signal.contracts import (
    MarketPoolLocation,
    PoolSide,
    PoolSnapshot,
)

DASHBOARD_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")

DEFAULT_SUPPORT_COLOR = "#228bab"  # BID / turquoise
DEFAULT_RESISTANCE_COLOR = "#ec4079"  # ASK / pink
NOT_PRESENT = "NOT_PRESENT_IN_CHART_CONTRACT"

DEFAULT_LIQUIDITY = {
    "enabled": True,
    "amount": 300,
    "highest_len": 2,
    "lowest_len": 2,
    "clusters_enabled": True,
    "show_single_pools": True,
    "support_color": DEFAULT_SUPPORT_COLOR,
    "resistance_color": DEFAULT_RESISTANCE_COLOR,
}

_trp = None
_engine_function = None


def _ensure_dashboard_path() -> None:
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))


def _load_chart_bindings():
    """Lazy bind chart TRP handles; engine_function identity is asserted once."""
    global _trp, _engine_function
    if _engine_function is not None:
        return _trp, _engine_function
    _ensure_dashboard_path()
    from research_charts.trp_import import load_trp

    trp = load_trp()
    eng = trp["run_liquidity_location"]
    assert eng.__module__ == "indicators.liquidity_location.engine"
    assert eng.__name__ == "run_liquidity_location"
    _trp = trp
    _engine_function = eng
    return _trp, _engine_function


def get_engine_function():
    """Chart pool engine (identical object to Research Charts)."""
    _, eng = _load_chart_bindings()
    return eng


def engine_function():
    """Callable alias: returns the chart ``run_liquidity_location`` object."""
    return get_engine_function()


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unix(dt: datetime) -> int:
    return int(_utc(dt).timestamp())


def side_ask_bid(engine_side: str) -> PoolSide:
    if engine_side == "upper":
        return PoolSide.ASK
    if engine_side == "lower":
        return PoolSide.BID
    raise ValueError(engine_side)


def chart_color(engine_side: str, cfg: Any) -> str:
    if engine_side == "upper":
        return str(getattr(cfg, "resistance_color", DEFAULT_RESISTANCE_COLOR))
    return str(getattr(cfg, "support_color", DEFAULT_SUPPORT_COLOR))


def build_liquidity_config(raw: dict[str, Any] | None, timeframe: str):
    _ensure_dashboard_path()
    from research_charts.service import lld_config_for_timeframe

    trp, _ = _load_chart_bindings()
    base = dict(DEFAULT_LIQUIDITY)
    if raw:
        base.update(raw)
    base["enabled"] = True
    cfg = trp["LiquidityLocationConfig"].from_dict(base)
    return lld_config_for_timeframe(cfg, timeframe)


def load_chart_candles(
    symbol: str,
    timeframe: str,
    *,
    start: datetime,
    end: datetime,
):
    """Same candle resolver as chart ``compute_indicators`` / ``pane_bundle``."""
    _ensure_dashboard_path()
    from research_charts.service import _candles_from_packed, resolve_candle_pack

    packed = resolve_candle_pack(
        symbol,
        timeframe,
        start=_unix(start),
        end=_unix(end),
        limit=None,
        allow_stale=True,
    )
    candles = _candles_from_packed(packed, allow_stale=True)
    return packed, candles


def chart_lookback_start(as_of: datetime, timeframe: str = "5m") -> datetime:
    """Match research chart default pack: newest DEFAULT_LIMIT_BY_TF bars ending at as_of."""
    _ensure_dashboard_path()
    from research_charts.service import DEFAULT_LIMIT_BY_TF, _TF_SEC

    lim = int(DEFAULT_LIMIT_BY_TF.get(timeframe, 1500))
    sec = int(_TF_SEC.get(timeframe, 300))
    return datetime.fromtimestamp(_utc(as_of).timestamp() - lim * sec, tz=timezone.utc)


def run_chart_backend_lld(
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    liquidity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the exact chart LLD branch."""
    _ensure_dashboard_path()
    from research_charts.service import _indicators_from_candles

    trp, eng = _load_chart_bindings()
    packed, candles = load_chart_candles(symbol, timeframe, start=start, end=end)
    cfg = build_liquidity_config(liquidity, timeframe)
    indicators = _indicators_from_candles(
        packed,
        candles,
        ema={"enabled": False},
        stochastic={"enabled": False},
        liquidity=cfg.to_dict(),
    )
    result = eng(candles, cfg)
    overlays = trp["compose_lld_overlays"](
        result,
        cfg,
        clusters=(
            trp["cluster_pools"](result.pools, gap_pct=float(cfg.cluster_gap_pct), active_only=True)
            if cfg.clusters_enabled
            else None
        ),
    )
    return {
        "packed": packed,
        "candles": candles,
        "config": cfg,
        "indicators": indicators,
        "engine_result": result,
        "overlays": overlays,
        "serialized_overlays": indicators.get("liquidity", {}).get("overlays") or [],
    }


def pool_row_from_engine(
    pool: Any, *, cfg: Any, as_of: datetime, market_price: float | None
) -> dict[str, Any]:
    from indicators.liquidity_location.availability import pool_availability_timestamps

    avail = pool_availability_timestamps(pool)
    available_at = avail["available_at"]
    invalidated = pool.invalidated_timestamp
    as_of_u = _utc(as_of)
    active_asof = available_at <= as_of_u and (invalidated is None or _utc(invalidated) > as_of_u)
    lower = float(pool.bottom_price)
    upper = float(pool.top_price)
    ask_bid = side_ask_bid(pool.side)
    dist_bps = None
    if market_price and market_price > 0:
        if ask_bid is PoolSide.ASK:
            dist_bps = (lower - market_price) / market_price * 10000.0
        else:
            dist_bps = (market_price - upper) / market_price * 10000.0

    return {
        "pool_id": pool.pool_id,
        "symbol": pool.symbol,
        "side": ask_bid.value,
        "engine_side": pool.side,
        "source_timeframe": pool.timeframe,
        "created_ts": _iso_z(pool.created_timestamp),
        "valid_from_ts": _iso_z(available_at),
        "available_at": _iso_z(available_at),
        "known_at": _iso_z(avail["known_at"]),
        "source_timestamp": _iso_z(pool.source_timestamp),
        "origin_ts": _iso_z(pool.source_timestamp),
        "confirmation_bar_start": _iso_z(avail["confirmation_bar_start"]),
        "confirmation_bar_end": _iso_z(avail["confirmation_bar_end"]),
        "last_seen_ts": NOT_PRESENT,
        "invalidated_ts": _iso_z(invalidated),
        "invalidated_at": _iso_z(invalidated),
        "active_as_of": bool(active_asof),
        "engine_active_flag": bool(pool.active),
        "lower_edge": lower,
        "upper_edge": upper,
        "center": (lower + upper) / 2.0,
        "width": upper - lower,
        "strength": None if pool.strength is None else float(pool.strength),
        "strong_pool": bool(pool.strong_pool),
        "source_level_node": NOT_PRESENT,
        "parent_source_ids": NOT_PRESENT,
        "chart_color": chart_color(pool.side, cfg),
        "chart_layer": "Liquidity Location",
        "distance_to_market_bps_diagnostic": dist_bps,
        "as_of": _iso_z(as_of),
        "raw_metadata": dict(pool.metadata or {}),
    }


def to_pool_snapshot(row: dict[str, Any]) -> PoolSnapshot:
    return PoolSnapshot(
        pool_id=str(row["pool_id"]),
        symbol=str(row["symbol"]),
        source_timeframe=str(row["source_timeframe"]),
        side=PoolSide(row["side"]),
        lower_edge=float(row["lower_edge"]),
        upper_edge=float(row["upper_edge"]),
        strength=None if row.get("strength") is None else float(row["strength"]),
        origin_ts=row.get("origin_ts") or row.get("source_timestamp"),
        available_at=str(row["available_at"]),
        invalidated_at=row.get("invalidated_at") or row.get("invalidated_ts"),
        active_as_of=bool(row["active_as_of"]),
    )


def market_price_at(candles: list, as_of: datetime) -> float | None:
    as_of_u = _utc(as_of)
    last = None
    for c in candles:
        ts = _utc(c.timestamp)
        if ts <= as_of_u:
            last = c
        else:
            break
    return None if last is None else float(last.close)


def normalize_pool_payload(pools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "pool_id",
        "side",
        "source_timeframe",
        "lower_edge",
        "upper_edge",
        "available_at",
        "created_ts",
        "invalidated_ts",
        "chart_color",
        "chart_layer",
        "active_as_of",
    )
    rows = [{k: p.get(k) for k in keys} for p in pools]
    rows.sort(key=lambda r: (r["pool_id"], r["side"]))
    return rows


def fingerprint(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def active_pools_at(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("active_as_of")]


def classify_market_pool_location(
    rows: list[dict[str, Any]], market: float | None
) -> MarketPoolLocation:
    if not rows:
        return MarketPoolLocation.NO_ACTIVE_POOLS
    if market is None:
        return MarketPoolLocation.BETWEEN_POOLS
    inside_ask = any(
        r["side"] == PoolSide.ASK.value and r["lower_edge"] <= market <= r["upper_edge"] for r in rows
    )
    inside_bid = any(
        r["side"] == PoolSide.BID.value and r["lower_edge"] <= market <= r["upper_edge"] for r in rows
    )
    if inside_ask and inside_bid:
        return MarketPoolLocation.INSIDE_OVERLAPPING_POOLS
    if inside_ask:
        return MarketPoolLocation.INSIDE_ASK_POOL
    if inside_bid:
        return MarketPoolLocation.INSIDE_BID_POOL
    return MarketPoolLocation.BETWEEN_POOLS


def nearest_front(rows: list[dict[str, Any]], market: float | None) -> dict[str, Any]:
    location = classify_market_pool_location(rows, market)
    out: dict[str, Any] = {
        "market_price": market,
        "market_pool_location": location.value,
        "market_inside_pool": location
        in (
            MarketPoolLocation.INSIDE_ASK_POOL,
            MarketPoolLocation.INSIDE_BID_POOL,
            MarketPoolLocation.INSIDE_OVERLAPPING_POOLS,
        ),
        "nearest_ask_pool_above_market": None,
        "nearest_bid_pool_below_market": None,
        "inside_pool_ids": [],
    }
    if market is None:
        return out

    inside = [r for r in rows if r["lower_edge"] <= market <= r["upper_edge"]]
    out["inside_pool_ids"] = [r["pool_id"] for r in inside]
    if inside:
        # Preserve prior CLI string for CSV compatibility when inside any pool.
        out["nearest_ask_pool_above_market"] = "MARKET_INSIDE_POOL"
        out["nearest_bid_pool_below_market"] = "MARKET_INSIDE_POOL"
        return out

    asks = [r for r in rows if r["side"] == PoolSide.ASK.value and r["upper_edge"] > market]
    bids = [r for r in rows if r["side"] == PoolSide.BID.value and r["lower_edge"] < market]
    if asks:
        nearest_ask = min(asks, key=lambda r: r["lower_edge"])
        out["nearest_ask_pool_above_market"] = {
            "pool_id": nearest_ask["pool_id"],
            "lower_edge": nearest_ask["lower_edge"],
            "upper_edge": nearest_ask["upper_edge"],
            "source_timeframe": nearest_ask["source_timeframe"],
        }
    if bids:
        nearest_bid = max(bids, key=lambda r: r["upper_edge"])
        out["nearest_bid_pool_below_market"] = {
            "pool_id": nearest_bid["pool_id"],
            "lower_edge": nearest_bid["lower_edge"],
            "upper_edge": nearest_bid["upper_edge"],
            "source_timeframe": nearest_bid["source_timeframe"],
        }
    return out


def export_snapshot(
    *,
    symbol: str,
    timeframe: str,
    window_start: datetime,
    as_of: datetime,
    liquidity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Causal as-of snapshot via chart engine (lookback pack ending at as_of)."""
    del window_start
    eng = get_engine_function()
    as_of_u = _utc(as_of)
    pack_start = chart_lookback_start(as_of_u, timeframe)
    bundle = run_chart_backend_lld(
        symbol=symbol,
        timeframe=timeframe,
        start=pack_start,
        end=as_of_u,
        liquidity=liquidity,
    )
    cfg = bundle["config"]
    market = market_price_at(bundle["candles"], as_of_u)
    rows = [
        pool_row_from_engine(p, cfg=cfg, as_of=as_of_u, market_price=market)
        for p in bundle["engine_result"].pools
    ]
    active = active_pools_at(rows)
    front = nearest_front(active, market)
    trp, _ = _load_chart_bindings()
    as_of_unix = _unix(as_of_u)
    serialized = trp["serialize_overlays"](bundle["overlays"])
    clipped = [
        o
        for o in serialized
        if (o.get("start_timestamp") is None or int(o["start_timestamp"]) <= as_of_unix)
    ]

    from orderbook_analyse.liquidity_pool_signal.canonical import (
        CANONICAL_PROVIDER_VERSION,
        canonical_pool_record,
        overlay_fields_for_pool,
    )

    canonical_rows = [
        canonical_pool_record(
            row,
            as_of=as_of_u,
            overlay_fields=overlay_fields_for_pool(clipped, row["pool_id"], as_of_unix=as_of_unix),
        )
        for row in rows
    ]
    pool_norm = normalize_pool_payload(rows)
    sha = fingerprint(
        {"pools": pool_norm, "as_of": _iso_z(as_of_u), "provider": CANONICAL_PROVIDER_VERSION}
    )
    return {
        "as_of": _iso_z(as_of_u),
        "symbol": symbol,
        "timeframe": timeframe,
        "market_price": market,
        "n_displayed_pools": len(rows),
        "n_active": len(active),
        "n_ask_active": sum(1 for r in active if r["side"] == PoolSide.ASK.value),
        "n_bid_active": sum(1 for r in active if r["side"] == PoolSide.BID.value),
        "pools": rows,
        "active_pools": active,
        "pool_snapshots": [to_pool_snapshot(r) for r in active],
        "nearest": front,
        "market_pool_location": front["market_pool_location"],
        "indicators_liquidity_overlay_count": len(bundle["serialized_overlays"]),
        "engine_module": eng.__module__,
        "engine_name": eng.__name__,
        "canonical_provider_version": CANONICAL_PROVIDER_VERSION,
        "canonical_snapshot_sha256": sha,
        "canonical_pools": canonical_rows,
        "active_canonical_pools": [r for r in canonical_rows if r["active_as_of"]],
    }


def parity_pair(
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    liquidity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A = chart ``_indicators_from_candles``; B = same engine + compose."""
    _ensure_dashboard_path()
    from research_charts.service import _indicators_from_candles

    trp, eng = _load_chart_bindings()
    packed, candles = load_chart_candles(symbol, timeframe, start=start, end=end)
    cfg = build_liquidity_config(liquidity, timeframe)

    chart_ind = _indicators_from_candles(
        packed,
        candles,
        ema={"enabled": False},
        stochastic={"enabled": False},
        liquidity=cfg.to_dict(),
    )
    chart_overlays = chart_ind.get("liquidity", {}).get("overlays") or []

    result = eng(candles, cfg)
    clusters = None
    if cfg.clusters_enabled:
        clusters = trp["cluster_pools"](
            result.pools, gap_pct=float(cfg.cluster_gap_pct), active_only=True
        )
    cli_overlays = trp["serialize_overlays"](
        trp["compose_lld_overlays"](result, cfg, clusters=clusters)
    )

    def norm_overlays(ovs: list) -> list[dict[str, Any]]:
        out = []
        for o in ovs:
            if not isinstance(o, dict):
                continue
            meta = o.get("metadata") or {}
            oid = str(o.get("id") or "")
            if not (
                oid.startswith("lld:")
                or oid.startswith("lldc:")
                or meta.get("source") in ("lld", "lld-cluster")
            ):
                continue
            out.append(
                {
                    "id": oid,
                    "type": o.get("type"),
                    "top_price": o.get("top_price"),
                    "bottom_price": o.get("bottom_price"),
                    "start_timestamp": o.get("start_timestamp"),
                    "end_timestamp": o.get("end_timestamp"),
                    "extend_right": o.get("extend_right"),
                    "metadata_pool_id": meta.get("pool_id") or meta.get("cluster_id"),
                    "metadata_side": meta.get("side"),
                    "metadata_source": meta.get("source"),
                    "metadata_available_at": meta.get("available_at"),
                    "color": (o.get("style") or {}).get("color") or o.get("border_color"),
                }
            )
        out.sort(key=lambda x: (str(x["id"]), str(x.get("type"))))
        return out

    as_of = end
    market = market_price_at(candles, as_of)
    chart_result = eng(candles, cfg)
    assert [p.pool_id for p in chart_result.pools] == [p.pool_id for p in result.pools]

    chart_pools = [
        pool_row_from_engine(p, cfg=cfg, as_of=as_of, market_price=market) for p in chart_result.pools
    ]
    cli_pools = [
        pool_row_from_engine(p, cfg=cfg, as_of=as_of, market_price=market) for p in result.pools
    ]
    chart_norm = {"overlays": norm_overlays(chart_overlays), "pools": normalize_pool_payload(chart_pools)}
    cli_norm = {"overlays": norm_overlays(cli_overlays), "pools": normalize_pool_payload(cli_pools)}
    sha_chart = fingerprint(chart_norm)
    sha_cli = fingerprint(cli_norm)
    return {
        "chart_payload_normalized": chart_norm,
        "cli_payload_normalized": cli_norm,
        "chart_payload_sha256": sha_chart,
        "cli_payload_sha256": sha_cli,
        "parity_pass": sha_chart == sha_cli,
        "engine_identity": {
            "cli_is_chart": eng is get_engine_function(),
            "module": eng.__module__,
            "name": eng.__name__,
        },
    }


def chart_pool_engine():
    """Explicit name for the foundation invariant ``chart_pool_engine is engine``."""
    return get_engine_function()

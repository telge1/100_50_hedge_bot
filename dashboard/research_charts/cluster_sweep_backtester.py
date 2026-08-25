"""Cluster Sweep research backtester → TRP OverlayMarkers (no live orders)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .oa_import import load_cluster_sweep
from .service import candle_objects, resolve_candle_pack
from .trp_import import load_trp

BACKTESTER_SOURCE = "cluster_sweep_backtester"
STRATEGY_ID = "cluster_sweep_ema_9_20_59"

# Default visible markers (detail markers optional)
DEFAULT_MARKER_KINDS = ("CONFIRMATION", "ENTRY_NEXT_OPEN", "INVALIDATED")
DETAIL_MARKER_KINDS = (
    "APPROACH",
    "FIRST_TOUCH",
    "CLUSTER_ENTRY",
    "PRICE_CROSSED_EMA59",
    "MAX_SWEEP",
    "RECLAIM_PENDING",
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def candles_to_frame(candles: list[Any]) -> Any:
    import pandas as pd

    rows = []
    for c in candles:
        ts = c.timestamp
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        rows.append(
            {
                "open_time": ts,
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(getattr(c, "volume", 0.0) or 0.0),
            }
        )
    return pd.DataFrame(rows)


def run_cluster_sweep_backtest(
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    minimum_cluster_pools: int = 3,
    ema_fast: int = 9,
    ema_medium: int = 20,
    ema_slow: int = 59,
    show_detail_markers: bool = False,
    debug_low_pool: bool = False,
    expire_bars: int = 24,
) -> dict[str, Any]:
    """Load CH candles via research pack + OA detector; return meta/events/marker specs."""
    oa = load_cluster_sweep()
    sym = str(symbol).strip().upper()
    if "," in sym or " " in sym:
        raise ValueError("exactly one symbol per run")
    tf = str(timeframe).strip() or "5m"
    start_u, end_u = _utc(start), _utc(end)
    if end_u <= start_u:
        raise ValueError("end must be after start")

    min_pools = int(minimum_cluster_pools)
    if debug_low_pool and min_pools >= 3:
        min_pools = 1
    if not debug_low_pool and min_pools < 3:
        min_pools = 3

    warm_bars = int(oa["required_warmup_bars"](ema_slow, 40))
    try:
        bar_m = int(tf.replace("m", ""))
    except ValueError:
        bar_m = 5
    load_start = start_u - timedelta(minutes=warm_bars * bar_m)
    span_min = max(1, int((end_u - load_start).total_seconds() // 60))
    need_bars = max(200, span_min // bar_m + 10)

    packed: dict[str, Any] = {}
    candles: list[Any] = []
    candle_source = "research_charts"
    try:
        packed = resolve_candle_pack(
            sym,
            tf,
            start=int(load_start.timestamp()),
            end=int(end_u.timestamp()),
            limit=need_bars,
            allow_stale=True,
        )
        candles = candle_objects(
            sym,
            tf,
            start=int(load_start.timestamp()),
            end=int(end_u.timestamp()),
            limit=need_bars,
            allow_stale=True,
        )
        df = candles_to_frame(candles)
    except Exception as exc:  # noqa: BLE001
        # Fallback: OA ClickHouse loaders (signal_generator.candles_1m) if research
        # symbol catalog / table config is unavailable in this process.
        candle_source = f"oa_fallback:{exc}"
        from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
            aggregate_timeframe,
            default_client,
            fetch_candles_1m,
        )

        client0 = default_client()
        try:
            c1 = fetch_candles_1m(client0, sym, load_start, end_u)
            df = aggregate_timeframe(c1, tf)
        finally:
            if hasattr(client0, "close"):
                try:
                    client0.close()
                except Exception:
                    pass
        packed = {"source": "oa_clickhouse_candles_1m"}

    if df is None or getattr(df, "empty", True):
        return {
            "meta": {
                "strategy_id": STRATEGY_ID,
                "error": "NO_CANDLES",
                "symbol": sym,
                "timeframe": tf,
                "candle_source": candle_source,
            },
            "events": [],
            "markers": [],
            "coverage": {},
        }

    client = None
    coverage: dict[str, Any] = {}
    trades = ob = oi = liq = None
    try:
        client = oa["default_client"]()
        coverage = oa["coverage_report"](client, sym, start_u, end_u)
        pad = timedelta(hours=1)
        trades = oa["fetch_trades_1m"](client, sym, start_u - pad, end_u + pad)
        ob = oa["fetch_ob_1m"](client, sym, start_u - pad, end_u + pad)
        oi = oa["fetch_oi_1m"](client, sym, start_u - pad, end_u + pad)
        liq = oa["fetch_liquidations"](client, sym, start_u - pad, end_u + pad)
    except Exception as exc:  # noqa: BLE001
        coverage = {
            "status": "PARTIAL",
            "note": f"orderflow enrichment skipped: {exc}",
            "candles_pack": {"status": "VALID", "row_count": len(df)},
        }
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass

    result = oa["run_cluster_sweep_on_candles"](
        df,
        symbol=sym,
        timeframe=tf,
        window_start=start_u,
        window_end=end_u,
        minimum_cluster_pools=min_pools,
        expire_bars=expire_bars,
        trades_1m=trades,
        ob_1m=ob,
        oi_1m=oi,
        liq=liq,
        coverage=coverage,
        evaluate=True,
    )
    meta = dict(result["meta"])
    meta["symbol"] = sym
    meta["timeframe"] = tf
    meta["start"] = start_u.isoformat()
    meta["end"] = end_u.isoformat()
    meta["ema_fast"] = int(ema_fast)
    meta["ema_medium"] = int(ema_medium)
    meta["ema_slow"] = int(ema_slow)
    meta["show_detail_markers"] = bool(show_detail_markers)
    meta["debug_low_pool_zones"] = bool(debug_low_pool) or min_pools < 3
    meta["candle_source"] = candle_source or packed.get("source") or packed.get("feed")
    meta["n_candles_loaded"] = len(df)

    kinds = set(DEFAULT_MARKER_KINDS)
    if show_detail_markers:
        kinds.update(DETAIL_MARKER_KINDS)
    markers = events_to_marker_specs(result["events"], kinds=kinds)
    payload = {
        "meta": meta,
        "events": result["events"],
        "markers": markers,
        "coverage": result.get("coverage") or coverage,
        "strategy_id": STRATEGY_ID,
    }
    try:
        from .cluster_sweep_outcomes import enrich_backtest_with_outcomes

        payload = enrich_backtest_with_outcomes(payload)
    except Exception as exc:  # noqa: BLE001
        payload["outcome_analysis"] = {"status": "FAILED", "error": str(exc)}
    return payload


def events_to_marker_specs(
    events: list[dict[str, Any]],
    *,
    kinds: set[str] | None = None,
) -> list[dict[str, Any]]:
    kind_set = kinds or set(DEFAULT_MARKER_KINDS)
    out: list[dict[str, Any]] = []
    for ev in events:
        direction = str(ev.get("direction") or "")
        bull = direction == "BULLISH"
        status = str(ev.get("final_status") or "")
        base_color = "#1b9e77" if bull else "#d95f02"
        if status == "INVALIDATED":
            base_color = "#6c757d"
        if status in ("NO_CONFIRMATION", "INCONCLUSIVE_DATA"):
            base_color = "#adb5bd"

        def add(kind: str, ts: Any, price: Any, shape: str, color: str | None = None) -> None:
            if kind not in kind_set:
                return
            t = _parse_iso(ts)
            if t is None:
                return
            try:
                px = float(price) if price is not None else None
            except (TypeError, ValueError):
                px = None
            out.append(
                {
                    "overlay_id": f"csw-{ev.get('event_id')}-{kind}",
                    "kind": kind,
                    "event_id": ev.get("event_id"),
                    "direction": direction,
                    "status": status,
                    "timestamp": t,
                    "price": px,
                    "shape": shape,
                    "color": color or base_color,
                    "text": "" if kind in DETAIL_MARKER_KINDS else kind[:3],
                    "position": "below" if bull else "above",
                    "event": ev,
                }
            )

        mid = ev.get("cluster_mid") or ev.get("entry_price") or ev.get("ema_59")
        add("APPROACH", ev.get("approach_at"), mid, "circle")
        add("FIRST_TOUCH", ev.get("first_touch_at"), mid, "diamond")
        add("CLUSTER_ENTRY", ev.get("cluster_entry_at"), mid, "diamond")
        add("PRICE_CROSSED_EMA59", ev.get("price_cross_ema59_at"), ev.get("ema_59"), "square")
        add("MAX_SWEEP", ev.get("max_sweep_at"), mid, "arrow_down" if bull else "arrow_up")
        add(
            "CONFIRMATION",
            ev.get("confirmation_at"),
            mid,
            "arrow_up" if bull else "arrow_down",
            "#2ca02c" if bull else "#d62728",
        )
        add(
            "ENTRY_NEXT_OPEN",
            ev.get("entry_at"),
            ev.get("entry_price") or mid,
            "arrow_up" if bull else "arrow_down",
            "#000000",
        )
        add("INVALIDATED", ev.get("invalidated_at"), mid, "square", "#6c757d")
    return out


def build_overlay_markers(marker_specs: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    out = []
    for spec in marker_specs:
        meta = {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "kind": spec["kind"],
            "event_id": spec["event_id"],
            "direction": spec["direction"],
            "status": spec["status"],
            "event": spec.get("event") or {},
        }
        out.append(
            OverlayMarker(
                overlay_id=str(spec["overlay_id"]),
                symbol=str(symbol).upper(),
                timestamp=ensure_utc(spec["timestamp"]),
                price=spec.get("price"),
                position=spec.get("position") or "at_price",
                shape=spec.get("shape") or "circle",
                text=str(spec.get("text") or ""),
                size=9.0 if spec["kind"] in DEFAULT_MARKER_KINDS else 7.0,
                style=OverlayStyle(color=spec.get("color") or "#888888", width=1.0),
                timeframe_scope="all",
                visible=True,
                z_order=40,
                metadata=meta,
            )
        )
    return out

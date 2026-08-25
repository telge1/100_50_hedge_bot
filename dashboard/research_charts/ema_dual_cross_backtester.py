"""EMA dual-cross multi-source research backtester → TRP OverlayMarkers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .oa_import import load_ema_dual_cross
from .service import candle_objects, resolve_candle_pack

BACKTESTER_SOURCE = "ema_dual_cross_backtester"
STRATEGY_ID = "ema_dual_cross_multisource_v1"
OA_EXPORT_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/ema_dual_cross_multisource"
)

DEFAULT_MARKER_KINDS = ("CAND", "ALLOW", "ENT")
RESEARCH_MARKER_KINDS = ("BLOCK", "INC", "REJ")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_ema_dual_cross_cfg(
    *,
    enable_sync_cross: bool | None = None,
    enable_compressed_rebound: bool | None = None,
) -> Any:
    oa = load_ema_dual_cross()
    base = oa["EMA_DUAL_CROSS_DEFAULTS"]
    if enable_sync_cross is None and enable_compressed_rebound is None:
        return base
    kwargs: dict[str, bool] = {}
    if enable_sync_cross is not None:
        kwargs["enable_sync_cross"] = bool(enable_sync_cross)
    if enable_compressed_rebound is not None:
        kwargs["enable_compressed_rebound"] = bool(enable_compressed_rebound)
    return replace(base, **kwargs)


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


def run_ema_dual_cross_backtest(
    *,
    symbol: str,
    timeframe: str = "15m",
    start: datetime,
    end: datetime,
    show_candidates: bool = True,
    show_allow: bool = True,
    show_block: bool = False,
    show_inconclusive: bool = False,
    show_rejected: bool = False,
    enable_sync_cross: bool | None = None,
    enable_compressed_rebound: bool | None = None,
    export: bool = True,
) -> dict[str, Any]:
    oa = load_ema_dual_cross()
    sym = str(symbol).strip().upper()
    tf = str(timeframe).strip() or "15m"
    start_u, end_u = _utc(start), _utc(end)
    if end_u <= start_u:
        raise ValueError("end must be after start")

    warm_bars = int(oa["required_warmup_bars"](59, 20))
    try:
        bar_m = int(tf.replace("m", ""))
    except ValueError:
        bar_m = 15
    load_start = start_u - timedelta(minutes=warm_bars * bar_m)
    span_min = max(1, int((end_u - load_start).total_seconds() // 60))
    need_bars = max(200, span_min // bar_m + 10)

    candle_source = "research_charts"
    try:
        resolve_candle_pack(sym, tf, start=int(load_start.timestamp()), end=int(end_u.timestamp()), limit=need_bars, allow_stale=True)
        candles = candle_objects(sym, tf, start=int(load_start.timestamp()), end=int(end_u.timestamp()), limit=need_bars, allow_stale=True)
        df = candles_to_frame(candles)
    except Exception as exc:  # noqa: BLE001
        candle_source = f"oa_fallback:{exc}"
        from orderbook_analyse.cluster_sweep_research.clickhouse_source import aggregate_timeframe, default_client, fetch_candles_1m

        client0 = default_client()
        try:
            c1 = fetch_candles_1m(client0, sym, load_start, end_u)
            df = aggregate_timeframe(c1, tf)
        finally:
            if hasattr(client0, "close"):
                client0.close()

    if df is None or getattr(df, "empty", True):
        return {"meta": {"strategy_id": STRATEGY_ID, "error": "NO_CANDLES", "symbol": sym}, "candidates": [], "markers": []}

    client = None
    coverage: dict[str, Any] = {}
    trades = ob = oi = liq = None
    try:
        client = oa["default_client"]()
        coverage = oa["coverage_report"](client, sym, start_u, end_u)
        pad = timedelta(hours=2)
        trades = oa["fetch_trades_1m"](client, sym, start_u - pad, end_u + pad)
        ob = oa["fetch_ob_1m"](client, sym, start_u - pad, end_u + pad)
        oi = oa["fetch_oi_1m"](client, sym, start_u - pad, end_u + pad)
        liq = oa["fetch_liquidations"](client, sym, start_u - pad, end_u + pad)
    except Exception as exc:  # noqa: BLE001
        coverage = {"status": "PARTIAL", "note": str(exc)}
    finally:
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass

    export_dir = None
    if export:
        export_dir = str(OA_EXPORT_ROOT / f"{sym.lower()}_{tf}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")

    cfg = build_ema_dual_cross_cfg(
        enable_sync_cross=enable_sync_cross,
        enable_compressed_rebound=enable_compressed_rebound,
    )
    result = oa["run_ema_dual_cross_on_candles"](
        df,
        symbol=sym,
        timeframe=tf,
        window_start=start_u,
        window_end=end_u,
        trades_1m=trades,
        ob_1m=ob,
        oi_1m=oi,
        liq=liq,
        coverage=coverage,
        cfg=cfg,
        export_dir=export_dir,
    )
    meta = dict(result.get("meta") or {})
    meta["candle_source"] = candle_source
    meta["n_candles_loaded"] = len(df)
    meta["marker_visibility"] = {
        "candidates": show_candidates,
        "allow": show_allow,
        "block": show_block,
        "inconclusive": show_inconclusive,
        "rejected": show_rejected,
    }

    kinds = set()
    if show_candidates:
        kinds.add("CAND")
    if show_allow:
        kinds.update(("ALLOW", "ENT"))
    if show_block:
        kinds.add("BLOCK")
    if show_inconclusive:
        kinds.add("INC")
    if show_rejected:
        kinds.add("REJ")

    markers = candidates_to_marker_specs(
        result.get("candidates") or [],
        rejected=result.get("rejected_ema_crosses") or [],
        kinds=kinds,
    )
    return {
        "meta": meta,
        "candidates": result.get("candidates") or [],
        "rejected_ema_crosses": result.get("rejected_ema_crosses") or [],
        "markers": markers,
        "coverage": result.get("coverage") or coverage,
        "summary": result.get("summary") or {},
        "policy": result.get("policy") or {},
        "export_paths": result.get("export_paths") or {},
        "strategy_id": STRATEGY_ID,
    }


def candidates_to_marker_specs(
    candidates: list[dict[str, Any]],
    *,
    rejected: list[dict[str, Any]] | None = None,
    kinds: set[str] | None = None,
) -> list[dict[str, Any]]:
    kind_set = kinds or set(DEFAULT_MARKER_KINDS)
    out: list[dict[str, Any]] = []

    def add(kind: str, c: dict[str, Any], ts: Any, price: Any, text: str, color: str) -> None:
        if kind not in kind_set:
            return
        if ts is None:
            return
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        bull = str(c.get("direction")) == "BULLISH"
        try:
            px = float(price) if price is not None else None
        except (TypeError, ValueError):
            px = None
        out.append(
            {
                "overlay_id": f"edc-{c.get('candidate_id')}-{kind}",
                "kind": kind,
                "candidate_id": c.get("candidate_id"),
                "direction": c.get("direction"),
                "verdict": c.get("final_verdict"),
                "timestamp": _utc(ts),
                "price": px,
                "shape": "arrow_up" if bull else "arrow_down",
                "color": color,
                "text": text,
                "position": "below" if bull else "above",
                "candidate": c,
            }
        )

    for c in candidates:
        bull = str(c.get("direction")) == "BULLISH"
        prefix = "B" if bull else "S"
        px = c.get("entry_price") or (c.get("ema_after") or {}).get("close")
        add("CAND", c, c.get("candidate_at"), px, f"{prefix}-CAND", "#17becf")
        v = str(c.get("final_verdict") or "")
        if v == "ALLOW":
            add("ALLOW", c, c.get("candidate_at"), px, f"{prefix}-ALLOW", "#2ca02c" if bull else "#d62728")
            add("ENT", c, c.get("entry_at"), c.get("entry_price"), f"{prefix}-ENT", "#000000")
        elif v == "BLOCK":
            add("BLOCK", c, c.get("candidate_at"), px, f"{prefix}-BLOCK", "#ff7f0e")
        elif v == "INCONCLUSIVE_DATA":
            add("INC", c, c.get("candidate_at"), px, f"{prefix}-INC", "#9467bd")

    for c in rejected or []:
        bull = str(c.get("direction")) == "BULLISH"
        prefix = "B" if bull else "S"
        px = (c.get("ema_after") or {}).get("close")
        add("REJ", c, c.get("candidate_at"), px, f"{prefix}-REJ", "#adb5bd")

    return out


def build_overlay_markers(marker_specs: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    from .trp_import import load_trp

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
            "candidate_id": spec.get("candidate_id"),
            "direction": spec.get("direction"),
            "verdict": spec.get("verdict"),
            "candidate": spec.get("candidate") or {},
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
                size=9.0 if spec["kind"] in ("ENT", "ALLOW") else 7.0,
                style=OverlayStyle(color=spec.get("color") or "#888888", width=1.0),
                timeframe_scope="all",
                visible=True,
                z_order=42,
                metadata=meta,
            )
        )
    return out

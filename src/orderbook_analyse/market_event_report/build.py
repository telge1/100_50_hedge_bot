"""Assemble market-event report artifacts from loaded frames."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .classify import classify_event
from .lld_context import build_lld_context
from .metrics import pre_post_price_metrics


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        if not np.isfinite(v):
            return None
        return v
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    return obj


def _row_at(df: pd.DataFrame, time_col: str, t: pd.Timestamp) -> pd.Series | None:
    if df.empty or time_col not in df.columns:
        return None
    hit = df.loc[df[time_col] == t]
    if hit.empty:
        return None
    return hit.iloc[0]


def _window_trade_stats(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    """Aggregate trades with minute in [start, end) — end exclusive."""
    if trades.empty:
        return {
            "trade_count": 0,
            "tps": 0.0,
            "total_volume": 0.0,
            "aggressive_buy_volume": 0.0,
            "aggressive_sell_volume": 0.0,
            "trade_delta": 0.0,
            "delta_ratio": None,
        }
    sl = trades.loc[(trades["minute"] >= start) & (trades["minute"] < end)]
    if sl.empty:
        return {
            "trade_count": 0,
            "tps": 0.0,
            "total_volume": 0.0,
            "aggressive_buy_volume": 0.0,
            "aggressive_sell_volume": 0.0,
            "trade_delta": 0.0,
            "delta_ratio": None,
        }
    buy = float(sl["aggressive_buy_volume"].sum())
    sell = float(sl["aggressive_sell_volume"].sum())
    delta = buy - sell
    denom = buy + sell
    minutes = max(1, int((end - start).total_seconds() // 60))
    tc = float(sl["trade_count"].sum())
    return {
        "trade_count": tc,
        "tps": tc / (minutes * 60.0),
        "total_volume": float(sl["total_volume"].sum()),
        "aggressive_buy_volume": buy,
        "aggressive_sell_volume": sell,
        "trade_delta": delta,
        "delta_ratio": (delta / denom) if denom > 0 else None,
    }


def build_cvd_series(
    trades: pd.DataFrame,
    *,
    event_t: pd.Timestamp,
    pre_m: int = 15,
    post_m: int = 60,
) -> list[dict[str, Any]]:
    start = event_t - pd.Timedelta(minutes=pre_m)
    end = event_t + pd.Timedelta(minutes=post_m + 1)  # include event + post_m
    if trades.empty:
        return []
    sl = trades.loc[(trades["minute"] >= start) & (trades["minute"] < end)].sort_values("minute")
    cvd = 0.0
    out: list[dict[str, Any]] = []
    for r in sl.itertuples(index=False):
        cvd += float(r.trade_delta)
        phase = "pre" if r.minute < event_t else ("event" if r.minute == event_t else "post")
        out.append(
            {
                "minute": str(r.minute),
                "trade_delta": float(r.trade_delta),
                "cvd": cvd,
                "phase": phase,
            }
        )
    return out


def build_trade_context(trades: pd.DataFrame, event_t: pd.Timestamp) -> dict[str, Any]:
    known = {
        "15m_before": _window_trade_stats(trades, event_t - pd.Timedelta(minutes=15), event_t),
        "5m_before": _window_trade_stats(trades, event_t - pd.Timedelta(minutes=5), event_t),
        "1m_before": _window_trade_stats(trades, event_t - pd.Timedelta(minutes=1), event_t),
    }
    event_row = _row_at(trades, "minute", event_t)
    if event_row is None:
        event_block = {
            "available": False,
            "trade_count": 0,
            "tps": 0.0,
            "total_volume": 0.0,
            "aggressive_buy_volume": 0.0,
            "aggressive_sell_volume": 0.0,
            "trade_delta": 0.0,
            "delta_ratio": None,
        }
    else:
        event_block = {
            "available": True,
            "trade_count": float(event_row["trade_count"]),
            "tps": float(event_row["tps"]),
            "total_volume": float(event_row["total_volume"]),
            "aggressive_buy_volume": float(event_row["aggressive_buy_volume"]),
            "aggressive_sell_volume": float(event_row["aggressive_sell_volume"]),
            "trade_delta": float(event_row["trade_delta"]),
            "delta_ratio": float(event_row["delta_ratio"])
            if pd.notna(event_row["delta_ratio"])
            else None,
        }
    after = {
        "5m": _window_trade_stats(trades, event_t + pd.Timedelta(minutes=1), event_t + pd.Timedelta(minutes=6)),
        "15m": _window_trade_stats(trades, event_t + pd.Timedelta(minutes=1), event_t + pd.Timedelta(minutes=16)),
        "30m": _window_trade_stats(trades, event_t + pd.Timedelta(minutes=1), event_t + pd.Timedelta(minutes=31)),
        "60m": _window_trade_stats(trades, event_t + pd.Timedelta(minutes=1), event_t + pd.Timedelta(minutes=61)),
        "240m": _window_trade_stats(trades, event_t + pd.Timedelta(minutes=1), event_t + pd.Timedelta(minutes=241)),
    }
    return {
        "known_before_event": known,
        "event_minute": event_block,
        "after_event": after,
        "cvd_15m_pre_to_60m_post": build_cvd_series(trades, event_t=event_t),
        "side_semantics": {
            "Buy": "aggressive buy",
            "Sell": "aggressive sell",
        },
    }


def build_ob_context(ob: pd.DataFrame, event_t: pd.Timestamp) -> dict[str, Any]:
    def snap(t: pd.Timestamp) -> dict[str, Any] | None:
        row = _row_at(ob, "minute", t)
        if row is None:
            return None
        return {
            "minute": str(t),
            "spread_bps": float(row["spread_bps"]) if pd.notna(row["spread_bps"]) else None,
            "imbalance_l10": float(row["imbalance_l10"]) if pd.notna(row["imbalance_l10"]) else None,
            "imbalance_l50": float(row["imbalance_l50"]) if pd.notna(row["imbalance_l50"]) else None,
            "bid_depth_l50": float(row["bid_depth_l50"]) if pd.notna(row["bid_depth_l50"]) else None,
            "ask_depth_l50": float(row["ask_depth_l50"]) if pd.notna(row["ask_depth_l50"]) else None,
            "ofi": float(row["ofi"]) if pd.notna(row["ofi"]) else None,
            "ofi_1m": float(row["ofi_1m"]) if "ofi_1m" in row and pd.notna(row["ofi_1m"]) else None,
            "ofi_5m": float(row["ofi_5m"]) if "ofi_5m" in row and pd.notna(row["ofi_5m"]) else None,
            "ofi_15m": float(row["ofi_15m"]) if "ofi_15m" in row and pd.notna(row["ofi_15m"]) else None,
            "seconds": int(row["seconds"]) if pd.notna(row["seconds"]) else None,
            "valid_seconds": int(row["valid_seconds"]) if pd.notna(row["valid_seconds"]) else None,
            "invalid_seconds": int(row["invalid_seconds"]) if pd.notna(row["invalid_seconds"]) else None,
            "carried_forward_seconds": int(row["carried_forward_seconds"])
            if pd.notna(row["carried_forward_seconds"])
            else None,
        }

    known = {
        "15m_before": snap(event_t - pd.Timedelta(minutes=15)),
        "5m_before": snap(event_t - pd.Timedelta(minutes=5)),
        "1m_before": snap(event_t - pd.Timedelta(minutes=1)),
        "note": "Pre snapshots are last closed OB minutes strictly before event (no event/future).",
    }
    event_snap = snap(event_t)
    after = {
        "5m": snap(event_t + pd.Timedelta(minutes=5)),
        "15m": snap(event_t + pd.Timedelta(minutes=15)),
        "30m": snap(event_t + pd.Timedelta(minutes=30)),
        "60m": snap(event_t + pd.Timedelta(minutes=60)),
        "240m": snap(event_t + pd.Timedelta(minutes=240)),
    }
    return {
        "available": not ob.empty,
        "parser_version": "ob200_v3",
        "depth": 200,
        "known_before_event": known,
        "event_minute": event_snap,
        "after_event": after,
    }


def build_report(
    *,
    symbol: str,
    event_time_utc: datetime,
    candles: pd.DataFrame,
    trades: pd.DataFrame,
    orderbook: pd.DataFrame,
    oi_liq: dict[str, Any] | None = None,
    trp_root: Path | None = None,
) -> dict[str, Any]:
    event_t = pd.Timestamp(event_time_utc.replace(tzinfo=None) if event_time_utc.tzinfo else event_time_utc)
    # Normalize naive
    if getattr(event_time_utc, "tzinfo", None) is not None:
        event_t = pd.Timestamp(event_time_utc).tz_convert("UTC").tz_localize(None)

    price = pre_post_price_metrics(candles, event_open_time=event_t)
    trades_ctx = build_trade_context(trades, event_t)
    ob_ctx = build_ob_context(orderbook, event_t)
    lld = build_lld_context(candles, symbol=symbol, event_open_time=event_t, trp_root=trp_root)

    path60 = (price.get("path_metrics") or {}).get("60m") or {}
    classification = classify_event(
        pre_range_15m=(price.get("known_before_event") or {}).get("range_15m"),
        path_60m_long=path60.get("LONG"),
        path_60m_short=path60.get("SHORT"),
        future_return_60m=(price.get("after_event") or {}).get("future_return_60m"),
        event_delta_ratio=(trades_ctx.get("event_minute") or {}).get("delta_ratio"),
        event_ofi=(ob_ctx.get("event_minute") or {}).get("ofi") if ob_ctx.get("event_minute") else None,
        lld=lld,
    )

    oi_liq = oi_liq or {"available": False, "reason": "not_requested"}

    summary = {
        "tool": "market_event_report",
        "version": "1",
        "disclaimer": (
            "Research diagnostic only. Not a trading signal. No strategy/exit optimization. "
            "Pre-event sections exclude future data."
        ),
        "symbol": symbol,
        "event_time_utc": event_t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price,
        "trades": trades_ctx,
        "orderbook": ob_ctx,
        "lld": {
            "available": lld.get("available"),
            "reason": lld.get("reason"),
            "nearest_upper_pool": lld.get("nearest_upper_pool"),
            "nearest_lower_pool": lld.get("nearest_lower_pool"),
            "distance_upper_bps": lld.get("distance_upper_bps"),
            "distance_lower_bps": lld.get("distance_lower_bps"),
            "event_interaction": lld.get("event_interaction"),
        },
        "oi_liq": {
            "available": oi_liq.get("available"),
            "reason": oi_liq.get("reason"),
            "n_oi_buckets": oi_liq.get("n_oi_buckets"),
            "n_liquidations": oi_liq.get("n_liquidations"),
        },
        "classification": classification,
        "data_coverage": {
            "candles_rows": int(len(candles)),
            "trades_1m_rows": int(len(trades)),
            "orderbook_1m_rows": int(len(orderbook)),
        },
    }
    return {
        "summary": _jsonable(summary),
        "lld_full": _jsonable(lld),
        "oi_liq_full": _jsonable(oi_liq),
    }


def render_markdown(summary: dict[str, Any]) -> str:
    s = summary
    price = s.get("price") or {}
    known = price.get("known_before_event") or {}
    event = price.get("event_minute") or {}
    after = price.get("after_event") or {}
    path = price.get("path_metrics") or {}
    trades = s.get("trades") or {}
    ob = s.get("orderbook") or {}
    lld = s.get("lld") or {}
    oi = s.get("oi_liq") or {}
    cls = s.get("classification") or {}

    def fmt(x: Any, pct: bool = False) -> str:
        if x is None:
            return "n/a"
        try:
            v = float(x)
        except (TypeError, ValueError):
            return str(x)
        return f"{v:.4%}" if pct else f"{v:.6g}"

    lines = [
        f"# Market Event Report — {s.get('symbol')} @ {s.get('event_time_utc')}",
        "",
        "> Research diagnostic only. **Not a trading signal.**",
        "",
        "## Causality split",
        "",
        "- **Known before event:** metrics with `open_time` / `minute` **strictly before** event minute.",
        "- **Event minute:** labeled separately.",
        "- **After event:** path starts at **next 1m open** after event; future returns / MFE / MAE live here.",
        "",
        f"**Primary classification:** `{cls.get('primary')}`",
        f"**Labels:** {', '.join(f'`{x}`' for x in (cls.get('labels') or []))}",
        f"**Disclaimer:** {cls.get('disclaimer')}",
        "",
        "## 1) Price / Candles",
        "",
        "### Known before event",
        "",
        f"- return_15m: {fmt(known.get('return_15m'), True)} | range_15m: {fmt(known.get('range_15m'), True)}",
        f"- return_5m: {fmt(known.get('return_5m'), True)} | range_5m: {fmt(known.get('range_5m'), True)}",
        f"- return_1m: {fmt(known.get('return_1m'), True)} | range_1m: {fmt(known.get('range_1m'), True)}",
        "",
        "### Event minute",
        "",
        f"- event_minute_return: {fmt(event.get('event_minute_return'), True)}",
        f"- OHLC: {fmt(event.get('open'))} / {fmt(event.get('high'))} / {fmt(event.get('low'))} / {fmt(event.get('close'))}",
        "",
        "### After event",
        "",
        f"- entry (next open): {fmt(after.get('entry_next_open'))}",
    ]
    for h in (5, 15, 30, 60, 240):
        lines.append(f"- future_return_{h}m: {fmt(after.get(f'future_return_{h}m'), True)}")

    for h in ("60m", "240m"):
        block = path.get(h) or {}
        lines.append("")
        lines.append(f"### Path {h}")
        if not block.get("available"):
            lines.append("- unavailable")
            continue
        for side in ("LONG", "SHORT"):
            m = block.get(side) or {}
            lines.append(
                f"- {side}: MFE={fmt(m.get('mfe'), True)} MAE={fmt(m.get('mae'), True)} "
                f"ret={fmt(m.get('ret'), True)} "
                f"time_to_MFE={m.get('time_to_mfe_m')}m time_to_MAE={m.get('time_to_mae_m')}m"
            )

    te = (trades.get("event_minute") or {})
    tb = (trades.get("known_before_event") or {}).get("1m_before") or {}
    lines.extend(
        [
            "",
            "## 2) Trades",
            "",
            "### Known before (1m)",
            f"- trade_count={fmt(tb.get('trade_count'))} TPS={fmt(tb.get('tps'))} "
            f"delta={fmt(tb.get('trade_delta'))} delta_ratio={fmt(tb.get('delta_ratio'))}",
            "",
            "### Event minute",
            f"- trade_count={fmt(te.get('trade_count'))} TPS={fmt(te.get('tps'))} "
            f"total_volume={fmt(te.get('total_volume'))}",
            f"- aggressive_buy={fmt(te.get('aggressive_buy_volume'))} "
            f"aggressive_sell={fmt(te.get('aggressive_sell_volume'))}",
            f"- trade_delta={fmt(te.get('trade_delta'))} delta_ratio={fmt(te.get('delta_ratio'))}",
            f"- CVD points (15m pre → 60m post): {len(trades.get('cvd_15m_pre_to_60m_post') or [])}",
        ]
    )

    oe = ob.get("event_minute") or {}
    o1 = (ob.get("known_before_event") or {}).get("1m_before") or {}
    lines.extend(
        [
            "",
            "## 3) Orderbook (`ob200_v3` depth=200)",
            "",
            "### Known before (1m)",
            f"- spread_bps={fmt((o1 or {}).get('spread_bps'))} "
            f"imb_l10={fmt((o1 or {}).get('imbalance_l10'))} imb_l50={fmt((o1 or {}).get('imbalance_l50'))}",
            f"- bid_l50={fmt((o1 or {}).get('bid_depth_l50'))} ask_l50={fmt((o1 or {}).get('ask_depth_l50'))}",
            f"- ofi={fmt((o1 or {}).get('ofi'))} ofi_5m={fmt((o1 or {}).get('ofi_5m'))} "
            f"ofi_15m={fmt((o1 or {}).get('ofi_15m'))}",
            f"- valid/invalid/carried_forward="
            f"{(o1 or {}).get('valid_seconds')}/{(o1 or {}).get('invalid_seconds')}/{(o1 or {}).get('carried_forward_seconds')}",
            "",
            "### Event minute",
            f"- spread_bps={fmt((oe or {}).get('spread_bps'))} "
            f"imb_l10={fmt((oe or {}).get('imbalance_l10'))} imb_l50={fmt((oe or {}).get('imbalance_l50'))}",
            f"- bid_l50={fmt((oe or {}).get('bid_depth_l50'))} ask_l50={fmt((oe or {}).get('ask_depth_l50'))}",
            f"- ofi={fmt((oe or {}).get('ofi'))} ofi_1m={fmt((oe or {}).get('ofi_1m'))} "
            f"ofi_5m={fmt((oe or {}).get('ofi_5m'))} ofi_15m={fmt((oe or {}).get('ofi_15m'))}",
            f"- valid/invalid/carried_forward="
            f"{(oe or {}).get('valid_seconds')}/{(oe or {}).get('invalid_seconds')}/{(oe or {}).get('carried_forward_seconds')}",
        ]
    )

    lines.extend(
        [
            "",
            "## 4) LLD / Liquidity pools",
            "",
            f"- available: {lld.get('available')} reason: {lld.get('reason')}",
            f"- nearest upper: {lld.get('nearest_upper_pool')}",
            f"- nearest lower: {lld.get('nearest_lower_pool')}",
            f"- distance_upper_bps: {fmt(lld.get('distance_upper_bps'))}",
            f"- distance_lower_bps: {fmt(lld.get('distance_lower_bps'))}",
            f"- interaction: {lld.get('event_interaction')}",
            "",
            "## 5) OI / Liquidations (optional)",
            "",
            f"- available: {oi.get('available')} reason: {oi.get('reason')}",
            f"- n_oi_buckets: {oi.get('n_oi_buckets')} n_liquidations: {oi.get('n_liquidations')}",
            "",
            "## Classification reasons",
            "",
        ]
    )
    for k, v in (cls.get("reasons") or {}).items():
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(
    output_dir: Path,
    *,
    report: dict[str, Any],
    candles: pd.DataFrame,
    trades: pd.DataFrame,
    orderbook: pd.DataFrame,
) -> dict[str, str]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = report["summary"]
    paths: dict[str, str] = {}

    p_sum = output_dir / "summary.json"
    p_sum.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["summary.json"] = str(p_sum)

    p_md = output_dir / "report.md"
    p_md.write_text(render_markdown(summary), encoding="utf-8")
    paths["report.md"] = str(p_md)

    # Context CSVs: clip around event ±15m pre / +240m post when possible
    event_t = pd.Timestamp(str(summary["event_time_utc"]).replace("Z", ""))
    c0 = event_t - pd.Timedelta(minutes=15)
    c1 = event_t + pd.Timedelta(minutes=240)

    def clip(df: pd.DataFrame, col: str) -> pd.DataFrame:
        if df.empty:
            return df
        d = df.copy()
        d[col] = pd.to_datetime(d[col])
        return d.loc[(d[col] >= c0) & (d[col] <= c1)].copy()

    candles_ctx = clip(candles, "open_time")
    trades_ctx = clip(trades, "minute")
    ob_ctx = clip(orderbook, "minute")

    p_c = output_dir / "candles_context.csv"
    candles_ctx.to_csv(p_c, index=False)
    paths["candles_context.csv"] = str(p_c)

    p_t = output_dir / "trades_1m_context.csv"
    trades_ctx.to_csv(p_t, index=False)
    paths["trades_1m_context.csv"] = str(p_t)

    p_o = output_dir / "orderbook_1m_context.csv"
    ob_ctx.to_csv(p_o, index=False)
    paths["orderbook_1m_context.csv"] = str(p_o)

    p_lld = output_dir / "lld_context.json"
    p_lld.write_text(json.dumps(report["lld_full"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["lld_context.json"] = str(p_lld)

    p_oi = output_dir / "oi_liq_context.json"
    p_oi.write_text(json.dumps(report["oi_liq_full"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["oi_liq_context.json"] = str(p_oi)

    return paths

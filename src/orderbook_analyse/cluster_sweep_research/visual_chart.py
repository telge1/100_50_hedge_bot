"""Causal Plotly HTML chart for cluster-sweep visual audit (research-only)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .audit_export import earliest_confirmation, final_status, structure_flags
from .cluster_adapter import active_clusters_as_of
from .models import EventState, SetupDirection, SweepEvent


def _utc_ts(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def _naive(ts: Any) -> pd.Timestamp:
    t = _utc_ts(ts)
    return t.tz_localize(None) if t.tzinfo else t


MARKER_SPEC: dict[str, dict[str, Any]] = {
    "APPROACH": {"symbol": "triangle-up-open", "size": 9, "color": "#888"},
    "FIRST_TOUCH": {"symbol": "circle", "size": 10, "color": "#1f77b4"},
    "CLUSTER_ENTRY": {"symbol": "diamond", "size": 11, "color": "#17becf"},
    "PRICE_CROSSED_EMA59": {"symbol": "x", "size": 10, "color": "#9467bd"},
    "EMA_STRUCTURE_INTACT": {"symbol": "square-open", "size": 8, "color": "#2ca02c"},
    "MAX_SWEEP": {"symbol": "triangle-down", "size": 11, "color": "#ff7f0e"},
    "RECLAIM_PENDING": {"symbol": "circle-open", "size": 9, "color": "#bcbd22"},
    "RECLAIM_CONFIRMED": {"symbol": "star", "size": 13, "color": "#2ca02c"},
    "REJECTION_CONFIRMED": {"symbol": "star", "size": 13, "color": "#d62728"},
    "ENTRY_NEXT_OPEN": {"symbol": "arrow-up", "size": 14, "color": "#000000"},
    "CLUSTER_BREAK": {"symbol": "x-thin", "size": 12, "color": "#e377c2"},
    "INVALIDATED": {"symbol": "x", "size": 14, "color": "#7f7f7f"},
    "EXPIRED": {"symbol": "line-ew", "size": 10, "color": "#aaaaaa"},
    "NO_CONFIRMATION": {"symbol": "circle-cross", "size": 10, "color": "#c7c7c7"},
    "INCONCLUSIVE_DATA": {"symbol": "hexagon-open", "size": 11, "color": "#ff9896"},
}


def build_causal_cluster_segments(
    pools: Sequence[Any],
    times: Sequence[pd.Timestamp],
    *,
    minimum_pools: int = 3,
    sample_every: int = 1,
) -> list[dict[str, Any]]:
    """Build contiguous as_of cluster geometry segments (no future pools)."""
    if not times:
        return []
    snapshots: list[tuple[pd.Timestamp, list]] = []
    for i, t in enumerate(times):
        if i % sample_every != 0 and i != len(times) - 1:
            continue
        as_of = _utc_ts(t).to_pydatetime()
        snaps = active_clusters_as_of(pools, as_of, minimum_pools=minimum_pools)
        snapshots.append((_naive(t), snaps))

    # merge contiguous identical (id, low, high, pool_count)
    open_seg: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []

    def flush(cid: str) -> None:
        seg = open_seg.pop(cid, None)
        if seg is not None:
            closed.append(seg)

    for idx, (t, snaps) in enumerate(snapshots):
        next_t = snapshots[idx + 1][0] if idx + 1 < len(snapshots) else t
        seen = set()
        for c in snaps:
            key = c.cluster_id
            seen.add(key)
            geom = (round(c.low, 8), round(c.high, 8), c.pool_count)
            cur = open_seg.get(key)
            if cur is None:
                open_seg[key] = {
                    "cluster_id": key,
                    "side": c.side,
                    "low": c.low,
                    "high": c.high,
                    "pool_count": c.pool_count,
                    "strength_mean": c.strength_mean,
                    "oldest_created": c.oldest_created,
                    "x0": t,
                    "x1": next_t,
                    "as_of_start": t,
                }
            elif (round(cur["low"], 8), round(cur["high"], 8), cur["pool_count"]) != geom:
                flush(key)
                open_seg[key] = {
                    "cluster_id": key,
                    "side": c.side,
                    "low": c.low,
                    "high": c.high,
                    "pool_count": c.pool_count,
                    "strength_mean": c.strength_mean,
                    "oldest_created": c.oldest_created,
                    "x0": t,
                    "x1": next_t,
                    "as_of_start": t,
                }
            else:
                cur["x1"] = next_t
        for cid in list(open_seg.keys()):
            if cid not in seen:
                flush(cid)
    for cid in list(open_seg.keys()):
        flush(cid)
    return closed


def _event_markers(event: SweepEvent) -> list[tuple[str, datetime | None]]:
    status = final_status(event)
    pairs: list[tuple[str, datetime | None]] = [
        ("APPROACH", event.t_approach),
        ("FIRST_TOUCH", event.t_first_touch),
        ("CLUSTER_ENTRY", event.t_entry),
        ("PRICE_CROSSED_EMA59", event.t_price_cross_ema59),
        ("MAX_SWEEP", event.t_max_sweep),
        ("ENTRY_NEXT_OPEN", event.t_earliest_entry),
        ("INVALIDATED", event.t_invalidated),
    ]
    if EventState.EMA_STRUCTURE_INTACT in event.states:
        pairs.append(("EMA_STRUCTURE_INTACT", event.t_entry or event.t_first_touch))
    if EventState.RECLAIM_CONFIRMED in event.states:
        pairs.append(("RECLAIM_CONFIRMED", event.t_reclaim_or_reject))
    if EventState.REJECTION_CONFIRMED in event.states:
        pairs.append(("REJECTION_CONFIRMED", event.t_reclaim_or_reject))
    if EventState.CLUSTER_BREAK in event.states:
        pairs.append(("CLUSTER_BREAK", event.t_invalidated or event.t_reclaim_or_reject))
    if EventState.EXPIRED in event.states or status == "NO_CONFIRMATION":
        pairs.append(("EXPIRED" if EventState.EXPIRED in event.states else "NO_CONFIRMATION", event.t_entry))
    if status == "INCONCLUSIVE" or (
        event.coverage and event.coverage.get("orderbook") == "MISSING" and event.coverage.get("trades") == "MISSING"
    ):
        # only mark inconclusive when explicitly needed — avoid spam
        pass
    return [(k, v) for k, v in pairs if v is not None]


def _hover_event(event: SweepEvent) -> str:
    sf = structure_flags(event)
    conf_type, conf_at = earliest_confirmation(event)
    c = event.cluster
    of = (event.features or {}).get("orderflow") or {}
    lines = [
        f"<b>{event.setup_direction.value}</b> {event.event_id}",
        f"status={final_status(event)}",
        f"cluster={c.cluster_id}",
        f"bounds=[{c.low:.6g}, {c.high:.6g}] pools={c.pool_count} strength_mean={c.strength_mean}",
        f"created={c.oldest_created} as_of_touch={event.t_first_touch}",
        f"age_min={ (event.features or {}).get('cluster_age_bars_proxy_minutes') }",
        f"EMA9={sf.get('ema_9')} EMA20={sf.get('ema_20')} EMA59={sf.get('ema_59')}",
        f"9>59={sf.get('ema9_gt_ema59')} 20>59={sf.get('ema20_gt_ema59')} px<59={sf.get('price_below_ema59')}",
        f"9<59={sf.get('ema9_lt_ema59')} 20<59={sf.get('ema20_lt_ema59')} px>59={sf.get('price_above_ema59')}",
        f"slopes 9/20/59={sf.get('ema_9_slope_1')}/{sf.get('ema_20_slope_1')}/{sf.get('ema_59_slope_1')}",
        f"gaps 9-20/9-59/20-59={sf.get('gap_9_20')}/{sf.get('gap_9_59')}/{sf.get('gap_20_59')}",
        f"confirm={conf_type} @ {conf_at}",
        f"entry={event.t_earliest_entry}",
        f"coverage={event.coverage}",
    ]
    for key in ("before_contact", "during_sweep", "after_sweep_to_confirmation"):
        w = of.get(key)
        if isinstance(w, dict):
            lines.append(
                f"{key}: trades={w.get('trades_status')} delta={w.get('delta')} "
                f"ob={w.get('ob_status')} imb={w.get('imbalance_l50_mean')} "
                f"oi={w.get('oi_status')} liq={w.get('liq_status')}"
            )
    # outcomes
    for vk, vo in (event.outcomes or {}).items():
        if not isinstance(vo, dict):
            continue
        h = vo.get("h8") or vo.get("h4") or {}
        if h:
            lines.append(
                f"outcome {vk}: entry={vo.get('entry_price')} mfe={h.get('mfe')} mae={h.get('mae')}"
            )
    return "<br>".join(lines)


def build_visual_audit_figure(
    candles: pd.DataFrame,
    events: Sequence[SweepEvent],
    pools: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
    visible_start: datetime,
    visible_end: datetime,
    title: str | None = None,
    minimum_pools: int = 1,
) -> Any:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    vs, ve = _naive(visible_start), _naive(visible_end)
    vis = df[(df["open_time"] >= vs) & (df["open_time"] < ve)].copy()
    if vis.empty:
        raise ValueError("No candles in visible window")

    # Warmup EMAs already on full frame; plot visible + short warmup lookback for continuity
    warm_n = 20
    start_i = max(0, int(df.index[df["open_time"] >= vs][0]) - warm_n) if any(df["open_time"] >= vs) else 0
    plot_df = df.iloc[start_i:].copy()
    plot_df = plot_df[plot_df["open_time"] < ve]

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.78, 0.22],
        vertical_spacing=0.04,
        subplot_titles=(
            title or f"{symbol} {timeframe} cluster-sweep visual audit (UTC)",
            "Event timeline (markers only; details in hover)",
        ),
    )

    fig.add_trace(
        go.Candlestick(
            x=plot_df["open_time"],
            open=plot_df["open"],
            high=plot_df["high"],
            low=plot_df["low"],
            close=plot_df["close"],
            name="OHLC",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1,
        col=1,
    )
    for col, color, name in (
        ("ema_9", "#f4a261", "EMA 9"),
        ("ema_20", "#2a9d8f", "EMA 20"),
        ("ema_59", "#264653", "EMA 59"),
    ):
        if col in plot_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=plot_df["open_time"],
                    y=plot_df[col],
                    mode="lines",
                    name=name,
                    line=dict(width=1.5, color=color),
                    hovertemplate="%{x|%Y-%m-%d %H:%M} UTC<br>" + name + "=%{y:.6g}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    # Causal cluster segments on visible bars only
    vis_times = list(vis["open_time"])
    segments = build_causal_cluster_segments(
        pools, vis_times, minimum_pools=minimum_pools, sample_every=2
    )
    # Cap background shapes to keep HTML interactive; event clusters are always outlined.
    if len(segments) > 80:
        segments = segments[-80:]
    for seg in segments:
        color = "rgba(46, 139, 87, 0.18)" if seg["side"] == "lower" else "rgba(178, 34, 34, 0.18)"
        line_c = "#2e8b57" if seg["side"] == "lower" else "#b22222"
        fig.add_shape(
            type="rect",
            xref="x",
            yref="y",
            x0=seg["x0"],
            x1=seg["x1"],
            y0=seg["low"],
            y1=seg["high"],
            fillcolor=color,
            line=dict(color=line_c, width=1),
            layer="below",
            row=1,
            col=1,
        )
        # invisible hover proxy at mid
        fig.add_trace(
            go.Scatter(
                x=[seg["x0"], seg["x1"]],
                y=[(seg["low"] + seg["high"]) / 2] * 2,
                mode="lines",
                line=dict(width=0, color=line_c),
                name=f"cluster {seg['side']}",
                showlegend=False,
                hovertemplate=(
                    f"cluster_id={seg['cluster_id']}<br>"
                    f"side={seg['side']}<br>"
                    f"low={seg['low']:.6g} high={seg['high']:.6g}<br>"
                    f"pool_count={seg['pool_count']} strength_mean={seg['strength_mean']}<br>"
                    f"as_of_start={seg['as_of_start']} created={seg['oldest_created']}"
                    "<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )

    # Highlight event clusters at contact geometry
    for ev in events:
        c = ev.cluster
        x0 = _naive(ev.t_first_touch or ev.t_entry or visible_start)
        x1 = _naive(ev.t_earliest_entry or ev.t_reclaim_or_reject or ev.t_invalidated or visible_end)
        if x1 <= x0:
            x1 = x0 + pd.Timedelta(minutes=5)
        edge = "#0b6e4f" if ev.setup_direction == SetupDirection.BULLISH else "#9b2226"
        fig.add_shape(
            type="rect",
            xref="x",
            yref="y",
            x0=x0,
            x1=x1,
            y0=c.low,
            y1=c.high,
            fillcolor="rgba(0,0,0,0)",
            line=dict(color=edge, width=2, dash="dot"),
            row=1,
            col=1,
        )

    # Event markers (row 1 + compact row 2)
    used_legend: set[str] = set()
    for ev in events:
        y_base = float((ev.features or {}).get("close") or (ev.cluster.mid))
        hover = _hover_event(ev)
        for label, ts in _event_markers(ev):
            spec = MARKER_SPEC.get(label, {"symbol": "circle", "size": 9, "color": "#333"})
            bull = ev.setup_direction == SetupDirection.BULLISH
            color = spec["color"]
            if label in ("RECLAIM_CONFIRMED", "ENTRY_NEXT_OPEN") and bull:
                color = "#1b9e77"
            if label in ("REJECTION_CONFIRMED", "ENTRY_NEXT_OPEN") and not bull:
                color = "#d95f02"
            if final_status(ev) == "INVALIDATED" and label == "INVALIDATED":
                color = "#636363"
            show = label not in used_legend
            used_legend.add(label)
            fig.add_trace(
                go.Scatter(
                    x=[_naive(ts)],
                    y=[y_base],
                    mode="markers",
                    name=label,
                    legendgroup=label,
                    showlegend=show,
                    marker=dict(symbol=spec["symbol"], size=spec["size"], color=color, line=dict(width=1, color="#222")),
                    hovertemplate=f"<b>{label}</b><br>" + hover + "<extra></extra>",
                ),
                row=1,
                col=1,
            )
            # timeline strip
            y_map = {
                "BULLISH": 1,
                "BEARISH": 0,
            }
            fig.add_trace(
                go.Scatter(
                    x=[_naive(ts)],
                    y=[y_map[ev.setup_direction.value]],
                    mode="markers",
                    showlegend=False,
                    marker=dict(symbol=spec["symbol"], size=8, color=color),
                    hovertemplate=f"{label}<br>{ev.event_id}<extra></extra>",
                ),
                row=2,
                col=1,
            )

    fig.update_layout(
        template="plotly_white",
        height=900,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis_rangeslider_visible=False,
        margin=dict(l=60, r=30, t=80, b=40),
        hovermode="closest",
    )
    fig.update_xaxes(title_text="Time (UTC)", row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(
        title_text="Dir",
        tickmode="array",
        tickvals=[0, 1],
        ticktext=["BEAR", "BULL"],
        row=2,
        col=1,
        range=[-0.5, 1.5],
    )
    # Restrict default view to visible window (warmup still in traces for EMA continuity)
    fig.update_xaxes(range=[vs, ve], row=1, col=1)
    fig.update_xaxes(range=[vs, ve], row=2, col=1)
    return fig


def write_chart_html(fig: Any, path: Path, *, include_plotlyjs: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(
        str(path),
        include_plotlyjs=True if include_plotlyjs else "cdn",
        full_html=True,
        config={"scrollZoom": True, "displaylogo": False},
    )
    return path


def try_write_png(fig: Any, path: Path) -> Path | None:
    try:
        fig.write_image(str(path), width=1600, height=900, scale=1)
        return path
    except Exception:
        return None

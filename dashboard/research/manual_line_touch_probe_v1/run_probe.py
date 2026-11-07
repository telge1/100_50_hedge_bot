#!/usr/bin/env python3
"""
Phase T1: Read stored manual drawings + DOGEUSDT 5m candles; detect touches.
Read-only — does not modify drawings.json or dashboard code.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

from line_geometry import (  # noqa: E402
    BAR_SEC_5M,
    LineDef,
    classify_candle_interval,
    fmt_utc,
    utc_ts,
)

OUT_DIR = PROBE_DIR
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"

WINDOW_START = utc_ts(datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc))
WINDOW_END = utc_ts(datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc))
SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"


def load_drawings_raw() -> list[dict[str, Any]]:
    if not DRAWINGS_PATH.is_file():
        raise FileNotFoundError(f"drawings store not found: {DRAWINGS_PATH}")
    payload = json.loads(DRAWINGS_PATH.read_text(encoding="utf-8"))
    return list(payload.get("drawings") or [])


def line_from_drawing(d: dict[str, Any]) -> LineDef | None:
    if str(d.get("symbol") or "").upper() != SYMBOL:
        return None
    kind = str(d.get("drawing_type") or "")
    if kind not in ("trend", "hline"):
        return None
    if d.get("visible") is False:
        return None
    meta = d.get("metadata") or {}
    did = str(d.get("drawing_id") or "")
    ext = bool(meta.get("extend_right"))
    if kind == "hline":
        if d.get("price") is None:
            return None
        return LineDef(
            drawing_id=did,
            drawing_type="hline",
            symbol=SYMBOL,
            extend_right=False,
            price=float(d["price"]),
        )
    for key in ("start_timestamp", "start_price", "end_timestamp", "end_price"):
        if d.get(key) is None:
            return None
    t1, t2 = float(d["start_timestamp"]), float(d["end_timestamp"])
    if t1 <= 0 or t2 <= 0:
        return None
    return LineDef(
        drawing_id=did,
        drawing_type="trend",
        symbol=SYMBOL,
        extend_right=ext,
        t1=t1,
        p1=float(d["start_price"]),
        t2=t2,
        p2=float(d["end_price"]),
    )


def load_candles() -> list[dict[str, Any]]:
    from research_charts.service import candle_objects

    rows = candle_objects(
        SYMBOL,
        TIMEFRAME,
        start=WINDOW_START,
        end=WINDOW_END,
        limit=4000,
        allow_stale=True,
    )
    out = []
    for c in rows:
        ts = getattr(c, "timestamp", None) or getattr(c, "time", None)
        if ts is None:
            continue
        if hasattr(ts, "timestamp"):
            t = int(ts.timestamp())
        else:
            t = int(ts)
        if t < WINDOW_START or t >= WINDOW_END:
            continue
        if t <= 0:
            continue
        out.append(
            {
                "time": t,
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
            }
        )
    out.sort(key=lambda x: x["time"])
    return out


@dataclass
class TouchRow:
    timestamp_utc: str
    timeframe: str
    drawing_id: str
    drawing_type: str
    candle_open: float
    candle_high: float
    candle_low: float
    candle_close: float
    line_price: float
    distance_pct: float
    classification: str
    anchor_availability_status: str
    distance_method: str


def write_inventory(lines: list[LineDef]) -> None:
    path = OUT_DIR / "drawings_inventory.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "drawing_id",
                "drawing_type",
                "symbol",
                "anchor1_utc",
                "anchor1_price",
                "anchor2_utc",
                "anchor2_price",
                "hline_price",
                "extend_right",
                "valid_from_utc",
                "valid_to_utc",
                "finite_segment",
                "sample_line_price_at_window_start",
                "sample_line_price_at_window_end",
            ]
        )
        for ln in lines:
            s_lp = ln.price_at(WINDOW_START)
            e_lp = ln.price_at(WINDOW_END - BAR_SEC_5M)
            if ln.drawing_type == "hline":
                w.writerow(
                    [
                        ln.drawing_id,
                        ln.drawing_type,
                        ln.symbol,
                        "",
                        "",
                        "",
                        "",
                        f"{ln.price:.10f}" if ln.price is not None else "",
                        "false",
                        "always",
                        "always",
                        "false",
                        f"{s_lp:.10f}" if s_lp is not None else "",
                        f"{e_lp:.10f}" if e_lp is not None else "",
                    ]
                )
            else:
                finite = not ln.extend_right
                vto = fmt_utc(int(ln.t_hi)) if finite and ln.t_hi else ("extend_ray" if ln.extend_right else fmt_utc(int(ln.t_hi or 0)))
                w.writerow(
                    [
                        ln.drawing_id,
                        ln.drawing_type,
                        ln.symbol,
                        fmt_utc(int(ln.t1)),
                        f"{ln.p1:.10f}",
                        fmt_utc(int(ln.t2)),
                        f"{ln.p2:.10f}",
                        "",
                        str(ln.extend_right).lower(),
                        fmt_utc(int(ln.t_lo or 0)),
                        vto,
                        str(finite).lower(),
                        f"{s_lp:.10f}" if s_lp is not None else "",
                        f"{e_lp:.10f}" if e_lp is not None else "",
                    ]
                )


def episodes(rows: list[TouchRow]) -> list[dict[str, Any]]:
    """Group consecutive candles per (drawing_id, classification) where not NO_TOUCH / inactive."""
    out: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], list[TouchRow]] = defaultdict(list)
    for r in rows:
        if r.classification == "NO_TOUCH" or r.distance_method == "inactive":
            continue
        by_key[(r.drawing_id, r.classification)].append(r)
    epi = 0
    for (did, cls), group in sorted(by_key.items()):
        group.sort(key=lambda x: x.timestamp_utc)
        if not group:
            continue
        start = group[0]
        prev_t = int(datetime.strptime(start.timestamp_utc, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
        run = [start]
        for row in group[1:]:
            t = int(datetime.strptime(row.timestamp_utc, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
            if t == prev_t + BAR_SEC_5M:
                run.append(row)
            else:
                epi += 1
                out.append(
                    {
                        "episode_id": f"ep-{did}-{cls}-{epi}",
                        "drawing_id": did,
                        "classification": cls,
                        "start_utc": run[0].timestamp_utc,
                        "end_utc": run[-1].timestamp_utc,
                        "candle_count": len(run),
                    }
                )
                run = [row]
            prev_t = t
        if run:
            epi += 1
            out.append(
                {
                    "episode_id": f"ep-{did}-{cls}-{epi}",
                    "drawing_id": did,
                    "classification": cls,
                    "start_utc": run[0].timestamp_utc,
                    "end_utc": run[-1].timestamp_utc,
                    "candle_count": len(run),
                }
            )
    return out


def build_report(
    lines: list[LineDef],
    touches: list[TouchRow],
    episodes_list: list[dict[str, Any]],
    candles: list[dict],
) -> str:
    n_trend = sum(1 for l in lines if l.drawing_type == "trend")
    n_h = sum(1 for l in lines if l.drawing_type == "hline")
    cls_counts = defaultdict(int)
    for r in touches:
        if r.distance_method != "inactive":
            cls_counts[r.classification] += 1
    inv_notes = []
    for ln in lines:
        if ln.drawing_type == "trend" and ln.t1 and ln.t2:
            if ln.t1 > ln.t2:
                inv_notes.append(f"{ln.drawing_id}: end anchor time before start anchor time in storage")
    body = [
        "# Manual line touch probe v1",
        "",
        f"- **Data source**: `{DRAWINGS_PATH}` (read-only)",
        f"- **Symbol / TF**: {SYMBOL} {TIMEFRAME}",
        f"- **Window**: {fmt_utc(WINDOW_START)} .. {fmt_utc(WINDOW_END)} (end exclusive)",
        f"- **Candles loaded**: {len(candles)}",
        f"- **Drawings evaluated**: {len(lines)} ({n_trend} trend, {n_h} horizontal)",
        "",
        "## Classifications (candle rows evaluated, excluding inactive segment)",
        "",
        f"- TOUCH: {cls_counts['TOUCH']}",
        f"- NEAR_0_10: {cls_counts['NEAR_0_10']}",
        f"- NEAR_0_20: {cls_counts['NEAR_0_20']}",
        f"- NO_TOUCH: {cls_counts['NO_TOUCH']}",
        "",
        f"- **Touch episodes**: {len(episodes_list)}",
        "",
        "## Episode summary by line",
        "",
    ]
    by_line = defaultdict(list)
    for ep in episodes_list:
        by_line[ep["drawing_id"]].append(ep)
    for did, eps in sorted(by_line.items()):
        parts = ", ".join(f"{e['classification']} {e['start_utc']}→{e['end_utc']} ({e['candle_count']} bars)" for e in eps)
        body.append(f"- `{did}`: {parts}")
    if inv_notes:
        body.extend(["", "## Coordinate notes", ""] + [f"- {n}" for n in inv_notes])
    body.extend(
        [
            "",
            "## Method",
            "",
            "- Touch detection uses **5m interval** line price band vs candle low/high (see `distance_method`).",
            "- Finite trend segments: no rows with `distance_method=inactive` outside anchor span (no artificial touches).",
            "- `anchor_availability_status` flags lookahead (candles after 2nd anchor without extend_right still inactive for touch).",
            "",
        ]
    )
    return "\n".join(body)


def render_diagnostic_chart(lines: list[LineDef], touches: list[TouchRow], candles: list[dict]) -> None:
    try:
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        (OUT_DIR / "diagnostic_chart_skipped.txt").write_text("matplotlib not available", encoding="utf-8")
        return

    if not candles:
        return

    times = [datetime.fromtimestamp(c["time"], tz=timezone.utc) for c in candles]
    fig, ax = plt.subplots(figsize=(14, 7))
    for i, c in enumerate(candles):
        t = mdates.date2num(times[i])
        color = "#3dcc91" if c["close"] >= c["open"] else "#f0616d"
        ax.plot([t, t], [c["low"], c["high"]], color=color, linewidth=1)
        ax.plot([t, t], [c["open"], c["close"]], color=color, linewidth=3)

    t_nums = mdates.date2num(times)
    xmin, xmax = t_nums[0], t_nums[-1]

    for ln in lines:
        if ln.drawing_type == "hline" and ln.price is not None:
            ax.axhline(ln.price, color="#8b9bb4", linewidth=1, linestyle="--", alpha=0.85)
            ax.text(xmin, ln.price, ln.drawing_id, fontsize=7, color="#c9d1e0", va="bottom")
            continue
        if ln.t1 is None or ln.t2 is None:
            continue
        t_lo, t_hi = ln.t_lo, ln.t_hi
        plot_hi = xmax if ln.extend_right else mdates.date2num(datetime.fromtimestamp(int(t_hi), tz=timezone.utc))
        plot_lo = mdates.date2num(datetime.fromtimestamp(int(t_lo), tz=timezone.utc))
        p_lo = ln.price_at(float(t_lo))
        p_hi = ln.price_at(float(t_hi if not ln.extend_right else times[-1].timestamp()))
        if p_lo is None or p_hi is None:
            continue
        if ln.extend_right:
            p_end = ln.price_at(times[-1].timestamp() + BAR_SEC_5M)
            ax.plot([plot_lo, plot_hi], [p_lo, p_end or p_hi], color="#29f536", linewidth=1.2)
        else:
            ax.plot([plot_lo, plot_hi], [p_lo, p_hi], color="#29f536", linewidth=1.2)
        ax.text(plot_lo, p_lo, ln.drawing_id, fontsize=7, color="#8fef9a", va="bottom")

    color_map = {"TOUCH": "#fbbf24", "NEAR_0_10": "#60a5fa", "NEAR_0_20": "#a78bfa"}
    for r in touches:
        if r.classification not in color_map or r.distance_method == "inactive":
            continue
        t = mdates.date2num(datetime.strptime(r.timestamp_utc, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc))
        ax.scatter(t, r.line_price, s=28, c=color_map[r.classification], zorder=5, edgecolors="k", linewidths=0.3)

    ax.set_title(f"{SYMBOL} 5m manual lines + touches ({fmt_utc(WINDOW_START)} .. {fmt_utc(WINDOW_END)})")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=timezone.utc))
    ax.set_ylabel("Price")
    ax.grid(True, alpha=0.2)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT_DIR / "diagnostic_chart.png", dpi=140)
    plt.close(fig)


def main() -> int:
    raw = load_drawings_raw()
    lines: list[LineDef] = []
    for d in raw:
        ln = line_from_drawing(d)
        if ln:
            lines.append(ln)
    candles = load_candles()
    write_inventory(lines)

    touch_rows: list[TouchRow] = []
    for c in candles:
        for ln in lines:
            cls, lp, dist, method, _ = classify_candle_interval(ln, c["time"], c["high"], c["low"])
            status = ln.anchor_status(c["time"])
            if not ln.active_for_candle(c["time"]):
                method = "inactive"
                status = ln.anchor_status(c["time"])
                if ln.drawing_type == "trend" and c["time"] > (ln.t_hi or 0) and not ln.extend_right:
                    status = "AFTER_SECOND_ANCHOR"
                elif ln.drawing_type == "trend" and c["time"] < (ln.t_lo or 0):
                    status = "BEFORE_FIRST_ANCHOR"
            touch_rows.append(
                TouchRow(
                    timestamp_utc=fmt_utc(c["time"]),
                    timeframe=TIMEFRAME,
                    drawing_id=ln.drawing_id,
                    drawing_type=ln.drawing_type,
                    candle_open=c["open"],
                    candle_high=c["high"],
                    candle_low=c["low"],
                    candle_close=c["close"],
                    line_price=lp,
                    distance_pct=dist,
                    classification=cls if method != "inactive" else "NO_TOUCH",
                    anchor_availability_status=status,
                    distance_method=method,
                )
            )

    with (OUT_DIR / "candle_touches.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "timestamp_utc",
                "timeframe",
                "drawing_id",
                "drawing_type",
                "candle_open",
                "candle_high",
                "candle_low",
                "candle_close",
                "line_price",
                "distance_pct",
                "classification",
                "anchor_availability_status",
                "distance_method",
            ]
        )
        for r in touch_rows:
            w.writerow(
                [
                    r.timestamp_utc,
                    r.timeframe,
                    r.drawing_id,
                    r.drawing_type,
                    f"{r.candle_open:.10f}",
                    f"{r.candle_high:.10f}",
                    f"{r.candle_low:.10f}",
                    f"{r.candle_close:.10f}",
                    f"{r.line_price:.10f}",
                    f"{r.distance_pct:.6f}",
                    r.classification,
                    r.anchor_availability_status,
                    r.distance_method,
                ]
            )

    eps = episodes([r for r in touch_rows if r.distance_method != "inactive"])
    with (OUT_DIR / "touch_episodes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["episode_id", "drawing_id", "classification", "start_utc", "end_utc", "candle_count"])
        for e in eps:
            w.writerow([e["episode_id"], e["drawing_id"], e["classification"], e["start_utc"], e["end_utc"], e["candle_count"]])

    (OUT_DIR / "touch_report.md").write_text(build_report(lines, touch_rows, eps, candles), encoding="utf-8")
    render_diagnostic_chart(lines, touch_rows, candles)

    globals()["_LAST_TOUCH_ROWS"] = touch_rows

    active_touches = [r for r in touch_rows if r.classification == "TOUCH" and r.distance_method != "inactive"]
    print(f"lines={len(lines)} candles={len(candles)} touch_rows={len(active_touches)} episodes={len(eps)}")
    for r in active_touches[:25]:
        print(f"  TOUCH {r.timestamp_utc} {r.drawing_id} line={r.line_price:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

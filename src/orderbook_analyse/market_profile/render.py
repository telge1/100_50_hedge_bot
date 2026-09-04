"""Matplotlib rendering: candles, anchored histograms, extended levels.

Layout follows a fixed-range profile on a trading terminal: each profile's
histogram is drawn at its own anchor, and the derived levels continue to the
right so their interaction with later price is visible rather than implied.

Level lifetime is deliberate. A profile's POC and value-area edges are drawn
solid inside their own window and thinner across the following window, which
is where they are actually used. A naked POC keeps running to the right edge,
because an untested POC is the one level that stays relevant indefinitely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .contracts import MarketProfile  # noqa: E402

THEMES: dict[str, dict[str, Any]] = {
    "dark": {
        "fig": "#131722",
        "ax": "#131722",
        "grid": "#242832",
        "text": "#d1d4dc",
        "muted": "#787b86",
        "up": "#26a69a",
        "down": "#ef5350",
        "poc": "#f5c542",
        "va": "#5b9cf6",
        "naked": "#e569d1",
        "lvn": "#ff9f43",
        "sep": "#2a2e39",
    },
    "light": {
        "fig": "#ffffff",
        "ax": "#ffffff",
        "grid": "#e3e6ef",
        "text": "#1f2430",
        "muted": "#6a7180",
        "up": "#26a69a",
        "down": "#ef5350",
        "poc": "#b8860b",
        "va": "#2d6fd1",
        "naked": "#b02fa0",
        "lvn": "#d2691e",
        "sep": "#c8ccd8",
    },
}


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _bar_span(times, start: datetime, end: datetime) -> tuple[int, int] | None:
    """Bar index range ``[i0, i1]`` covered by ``[start, end)``."""
    s, e = _naive_utc(start), _naive_utc(end)
    idx = [i for i, t in enumerate(times) if s <= t < e]
    if not idx:
        return None
    return idx[0], idx[-1]


def _fmt_price(v: float) -> str:
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}"
    if a >= 0.01:
        return f"{v:.4f}"
    return f"{v:.6f}"


def render_profile_chart(
    *,
    symbol: str,
    candles_tf,
    profiles: list[MarketProfile],
    out_path: Path,
    timeframe: str,
    anchor_mode: str,
    theme: str = "dark",
    profile_width_frac: float = 0.45,
    profile_max_width_frac: float = 0.22,
    show_single_prints: bool = True,
    show_lvn: bool = True,
    figsize: tuple[float, float] = (22.0, 11.0),
    dpi: int = 130,
) -> Path:
    """Render candles plus anchored volume profiles to a PNG."""
    if candles_tf is None or candles_tf.empty:
        raise ValueError("no candles to render")

    c = THEMES.get(str(theme).lower(), THEMES["dark"])
    times = [t.to_pydatetime() for t in candles_tf["open_time"]]
    n = len(times)
    opens = candles_tf["open"].to_numpy(dtype=float)
    highs = candles_tf["high"].to_numpy(dtype=float)
    lows = candles_tf["low"].to_numpy(dtype=float)
    closes = candles_tf["close"].to_numpy(dtype=float)

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    fig.patch.set_facecolor(c["fig"])
    ax.set_facecolor(c["ax"])
    for spine in ax.spines.values():
        spine.set_color(c["sep"])
    ax.tick_params(colors=c["muted"], labelsize=9)
    ax.grid(True, color=c["grid"], linewidth=0.5, alpha=0.6, zorder=0)
    ax.set_axisbelow(True)

    # Profiles first so candles stay readable on top of the histogram.
    spans: list[tuple[MarketProfile, tuple[int, int]]] = []
    for p in profiles:
        span = _bar_span(times, p.window.start, p.window.end)
        if span is not None:
            spans.append((p, span))

    for p, (i0, i1) in spans:
        span_bars = (i1 - i0) + 1
        usable = min(span_bars * profile_width_frac, n * profile_max_width_frac)
        max_vol = max((b.volume for b in p.bins), default=0.0)
        if max_vol <= 0:
            continue

        if show_single_prints:
            for lo, hi in p.nodes.single_print_ranges:
                ax.add_patch(
                    Rectangle(
                        (i0, lo),
                        usable,
                        max(hi - lo, p.price_step * 0.05),
                        facecolor=c["lvn"],
                        alpha=0.10,
                        edgecolor="none",
                        zorder=1,
                    )
                )

        for b in p.bins:
            if b.volume <= 0:
                continue
            w = usable * (b.volume / max_vol)
            buy_w = w * (b.buy_volume / b.volume) if b.volume > 0 else 0.0
            ax.barh(
                b.price_low,
                buy_w,
                height=p.price_step,
                left=i0,
                align="edge",
                color=c["up"],
                alpha=0.55,
                linewidth=0,
                zorder=2,
            )
            ax.barh(
                b.price_low,
                w - buy_w,
                height=p.price_step,
                left=i0 + buy_w,
                align="edge",
                color=c["down"],
                alpha=0.55,
                linewidth=0,
                zorder=2,
            )

        ax.axvline(i0, color=c["sep"], linewidth=0.8, alpha=0.9, zorder=1)

        if show_lvn:
            for lvn in p.nodes.lvn:
                ax.plot(
                    [i0, i0 + usable * 0.12],
                    [lvn, lvn],
                    color=c["lvn"],
                    linewidth=1.2,
                    alpha=0.85,
                    zorder=4,
                )

        # Anchored inside the plot below the window high so it cannot collide
        # with the title or the legend.
        label = f"{p.window.label}  {p.shape.letter} {p.shape.kind}"
        ax.annotate(
            label,
            xy=(i0 + 0.4, p.price_high),
            xytext=(0, -4),
            textcoords="offset points",
            color=c["text"],
            fontsize=8,
            va="top",
            ha="left",
            alpha=0.92,
            zorder=6,
        )

    # Levels: solid in-window, thinner across the next window, naked POC to the edge.
    starts = [i0 for _, (i0, _) in spans]
    for k, (p, (i0, i1)) in enumerate(spans):
        nxt_end = starts[k + 2] - 1 if k + 2 < len(starts) else n - 1
        va = p.value_area

        ax.plot([i0, i1], [va.poc, va.poc], color=c["poc"], linewidth=2.0, zorder=5)
        for lvl in (va.vah, va.val):
            ax.plot(
                [i0, i1],
                [lvl, lvl],
                color=c["va"],
                linewidth=1.4,
                linestyle="--",
                zorder=5,
            )

        if nxt_end > i1:
            ax.plot(
                [i1, nxt_end],
                [va.poc, va.poc],
                color=c["poc"],
                linewidth=1.0,
                alpha=0.55,
                zorder=4,
            )
            for lvl in (va.vah, va.val):
                ax.plot(
                    [i1, nxt_end],
                    [lvl, lvl],
                    color=c["va"],
                    linewidth=0.9,
                    linestyle=":",
                    alpha=0.5,
                    zorder=4,
                )

        if p.naked_poc:
            ax.plot(
                [i1, n - 1],
                [va.poc, va.poc],
                color=c["naked"],
                linewidth=1.2,
                linestyle="-.",
                alpha=0.85,
                zorder=5,
            )
            ax.text(
                n - 1,
                va.poc,
                f" nPOC {_fmt_price(va.poc)}",
                color=c["naked"],
                fontsize=7.5,
                va="center",
                ha="left",
                zorder=6,
            )

    # Candles on top.
    body_w = 0.6
    for i in range(n):
        up = closes[i] >= opens[i]
        col = c["up"] if up else c["down"]
        ax.plot([i, i], [lows[i], highs[i]], color=col, linewidth=0.9, zorder=7)
        lo_b, hi_b = min(opens[i], closes[i]), max(opens[i], closes[i])
        ax.add_patch(
            Rectangle(
                (i - body_w / 2, lo_b),
                body_w,
                max(hi_b - lo_b, (highs[i] - lows[i]) * 0.001 or 1e-9),
                facecolor=col,
                edgecolor=col,
                linewidth=0.4,
                zorder=8,
            )
        )

    ax.set_xlim(-1, n + max(6, int(n * 0.02)))
    lo_p, hi_p = float(lows.min()), float(highs.max())
    pad = (hi_p - lo_p) * 0.04 or 1.0
    ax.set_ylim(lo_p - pad, hi_p + pad)

    step = max(1, n // 14)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [times[i].strftime("%m-%d %H:%M") for i in ticks], rotation=0, fontsize=8
    )

    first, last = times[0], times[-1]
    ax.set_title(
        f"{symbol} — market profile ({anchor_mode}, {timeframe} candles)   "
        f"{first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC   "
        f"[{len(spans)} profiles]",
        color=c["text"],
        fontsize=12,
        pad=12,
    )
    ax.set_ylabel("price (quote)", color=c["muted"], fontsize=9)

    handles = [
        plt.Line2D([], [], color=c["poc"], linewidth=2.0, label="POC"),
        plt.Line2D([], [], color=c["va"], linewidth=1.4, linestyle="--", label="VAH / VAL"),
        plt.Line2D(
            [], [], color=c["naked"], linewidth=1.2, linestyle="-.", label="naked POC"
        ),
        plt.Line2D([], [], color=c["lvn"], linewidth=1.2, label="LVN"),
        plt.Line2D([], [], color=c["up"], linewidth=6, alpha=0.35, label="taker buy vol"),
        plt.Line2D([], [], color=c["down"], linewidth=6, alpha=0.35, label="taker sell vol"),
    ]
    leg = ax.legend(
        handles=handles,
        loc="lower right",
        fontsize=8,
        facecolor=c["ax"],
        edgecolor=c["sep"],
        framealpha=0.9,
        ncol=3,
    )
    for t in leg.get_texts():
        t.set_color(c["text"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path

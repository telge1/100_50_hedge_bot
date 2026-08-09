"""Export idle-time analysis artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_idle_time_analysis import DEFINITIONS_DOC


def _fmt_ts(x) -> str:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return _fmt_ts(x)
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def render_summary_md(payload: dict[str, Any]) -> str:
    s = payload["stats"]
    cum = payload["cumulative"]
    b = payload["budget"]
    cap = payload["capacity"]
    lines = [
        "# Idle Time Analysis",
        "",
        f"- Audit: `{payload['audit_version']}`",
        f"- Trades: `{payload['trades_path']}`",
        f"- n trades: **{payload['n_trades']}** | n gaps: **{s['n_gaps']}**",
        "",
        "## Basic idle statistics",
        "",
        f"- mean: **{s['hours']['mean']:.2f} h** ({s['minutes']['mean']:.1f} min)",
        f"- median: **{s['hours']['median']:.2f} h** ({s['minutes']['median']:.1f} min)",
        f"- p25 / p50 / p75: {s['hours']['p25']:.2f} / {s['hours']['p50']:.2f} / {s['hours']['p75']:.2f} h",
        f"- p90 / p95 / p99: {s['hours']['p90']:.2f} / {s['hours']['p95']:.2f} / {s['hours']['p99']:.2f} h",
        f"- min / max: {s['hours']['min']:.3f} / {s['hours']['max']:.2f} h",
        "",
        "## Next trade within …",
        "",
    ]
    for k, label in [
        ("within_15min", "15 minutes"),
        ("within_30min", "30 minutes"),
        ("within_1h", "1 hour"),
        ("within_3h", "3 hours"),
        ("within_6h", "6 hours"),
        ("within_12h", "12 hours"),
        ("within_24h", "24 hours"),
    ]:
        c = cum[k]
        lines.append(f"- {label}: **{c['share_pct']:.1f}%** (n={c['n']})")
    lines += [
        "",
        "## Time in market",
        "",
        f"- Total span: **{b['total_span_days']:.1f} days**",
        f"- Time in market: **{b['time_in_market_pct']:.1f}%**",
        f"- Flat idle: **{b['flat_idle_pct']:.1f}%**",
        f"- Mean holding: **{b['mean_holding_hours']:.2f} h** (median {b['median_holding_hours']:.2f} h)",
        f"- Mean idle: **{b['mean_idle_hours']:.2f} h** (median {b['median_idle_hours']:.2f} h)",
        "",
        "## 2-coin / multi-coin capacity (descriptive only)",
        "",
        f"- Level: **{cap['unused_time_level']}**",
        f"- {cap['descriptive_note']}",
        f"- Disclaimer: {cap['disclaimer']}",
        "",
        "## Buckets",
        "",
        "| Bucket | n | share % |",
        "|---|---:|---:|",
    ]
    for _, r in payload["buckets"].iterrows():
        lines.append(f"| {r['bucket']} | {int(r['n'])} | {r['share_pct']:.1f} |")
    lines += ["", "## Half-year idle", "", "| Period | Trades | Med idle h | P90 h | Max h | <%1h | <%3h | <%6h |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for _, r in payload["halfyear"].iterrows():
        lines.append(
            f"| {r['period']} | {int(r['trades'])} | {r['median_idle_hours']:.2f} | "
            f"{r['p90_idle_hours']:.2f} | {r['max_idle_hours']:.2f} | "
            f"{100*r['share_idle_lt_1h']:.1f}% | {100*r['share_idle_lt_3h']:.1f}% | "
            f"{100*r['share_idle_lt_6h']:.1f}% |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    p = out_dir / "idle_gaps.csv"
    payload["gaps"].to_csv(p, index=False)
    paths["gaps"] = p

    p = out_dir / "idle_bucket_statistics.csv"
    payload["buckets"].to_csv(p, index=False)
    paths["buckets"] = p

    p = out_dir / "idle_by_halfyear.csv"
    payload["halfyear"].to_csv(p, index=False)
    paths["halfyear"] = p

    p = out_dir / "idle_by_month.csv"
    payload["monthly"].to_csv(p, index=False)
    paths["monthly"] = p

    p = out_dir / "longest_idle_periods.csv"
    payload["longest"].to_csv(p, index=False)
    paths["longest"] = p

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    p = out_dir / "summary.md"
    p.write_text(render_summary_md(payload), encoding="utf-8")
    paths["summary_md"] = p

    summary = {
        "audit_version": payload["audit_version"],
        "trades_path": payload["trades_path"],
        "n_trades": payload["n_trades"],
        "stats": payload["stats"],
        "cumulative": payload["cumulative"],
        "budget": _jsonable(payload["budget"]),
        "capacity": payload["capacity"],
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = p
    return paths

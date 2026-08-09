"""Export trend-filter generalization artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {"events_with_trend"}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k not in skip}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    ev = payload["events_with_trend"]
    if isinstance(ev, pd.DataFrame):
        ev.to_csv(out_dir / "events_with_trend.csv", index=False)
    paths["events_with_trend"] = out_dir / "events_with_trend.csv"

    for name, key in (
        ("trend_filter_comparison.csv", "trend_filter_comparison"),
        ("trend_filter_long_short.csv", "trend_filter_long_short"),
        ("trend_aligned_efficiency_quartiles.csv", "trend_aligned_efficiency_quartiles"),
        ("efficiency_monotonicity.csv", "efficiency_monotonicity"),
        ("countertrend_edge.csv", "countertrend_edge"),
        ("signal_retention.csv", "signal_retention"),
        ("opportunity_adjusted.csv", "opportunity_adjusted"),
        ("monthly_stability.csv", "monthly_stability"),
        ("cross_symbol_consistency.csv", "cross_symbol_consistency"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    (out_dir / "TREND_DEFINITION.md").write_text(
        str(payload.get("trend_definition") or ""), encoding="utf-8"
    )

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# Wave Fade × Trend Filter — {payload.get('audit_version')}",
        "",
        f"Source: `{payload.get('source')}`",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## Q4: **{dec.get('q4')}**",
        f"## Countertrend: **{dec.get('countertrend')}**",
        "",
        "## Trend definition (H4 / DEFINITIONS)",
        "",
        "```",
        str(payload.get("trend_definition")),
        "```",
        "",
        "## Comparison snapshot (COMBINED groups)",
        "",
        "| Symbol | TF | Group | n | hit | med net |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for r in payload.get("trend_filter_comparison") or []:
        if r.get("side") != "COMBINED":
            continue
        if r.get("trend_group") not in ("ALL", "TREND_ALIGNED", "COUNTERTREND", "MIXED"):
            continue
        lines.append(
            f"| {r.get('symbol')} | {r.get('timeframe')} | {r.get('trend_group')} | "
            f"{r.get('n')} | {r.get('hit_rate')} | {r.get('median_net')} |"
        )

    lines += [
        "",
        "> Frozen definitions only. No new thresholds. APT OOS not claimed.",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('q4')}\n{dec.get('countertrend')}\n",
        encoding="utf-8",
    )
    return paths

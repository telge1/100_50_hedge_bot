"""Export directional control artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k != "joined_trigger_context"}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and (obj != obj):  # NaN
        return None
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def _csv(name: str, rows: list[dict]) -> Path:
        p = out_dir / name
        pd.DataFrame(rows).to_csv(p, index=False)
        paths[name] = p
        return p

    _csv("directional_control_summary.csv", payload["directional_control_summary"])
    _csv("realignment_results.csv", payload["realignment_results"])
    _csv("cci_turn_results.csv", payload["cci_turn_results"])
    _csv("cci_wave_failure_results.csv", payload["cci_wave_failure_results"])

    joined = payload["joined_trigger_context"]
    jp = out_dir / "joined_trigger_context.csv"
    joined.to_csv(jp, index=False)
    paths["joined_trigger_context.csv"] = jp

    summary = _jsonable(payload)
    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# Fractal Directional Control — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Primary: **{dec.get('directional_control')}**",
        "",
        f"## CCI: **{dec.get('cci_turn')}**",
        "",
        "## Directional control (next opposite wave after failed wave)",
        "",
        "| label | tf | n | median_next_signed | baseline_median | edge | small |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in payload["directional_control_summary"]:
        lines.append(
            f"| {r.get('label')} | {r.get('timeframe')} | {r.get('n')} | "
            f"{r.get('median_next_signed_price_move_pct')} | {r.get('baseline_median_next_signed')} | "
            f"{r.get('edge_vs_baseline_median')} | {r.get('small_sample')} |"
        )

    lines += [
        "",
        "## Re-alignment",
        "",
        "| label | tf | n | median_next_signed | edge | small |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for r in payload["realignment_results"]:
        lines.append(
            f"| {r.get('label')} | {r.get('timeframe')} | {r.get('n')} | "
            f"{r.get('median_next_signed_price_move_pct')} | {r.get('edge_vs_baseline_median')} | "
            f"{r.get('small_sample')} |"
        )

    lines += [
        "",
        "## CCI turn buckets (selected)",
        "",
        "| tf | end_dir | bucket | n | median_next_signed | small |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for r in payload["cci_turn_results"]:
        if r.get("cci_bucket") not in ("lt100", "150_200", "200_300", "gt300"):
            continue
        lines.append(
            f"| {r.get('timeframe')} | {r.get('end_direction')} | {r.get('cci_bucket')} | "
            f"{r.get('n')} | {r.get('median_next_signed_price_move_pct')} | {r.get('small_sample')} |"
        )

    lines += [
        "",
        "## CCI + wave failure",
        "",
        "| label | tf | n_with | median_with | n_without | median_without | lift |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["cci_wave_failure_results"]:
        lines.append(
            f"| {r.get('label')} | {r.get('timeframe')} | {r.get('n')} | {r.get('median_with')} | "
            f"{r.get('n_without')} | {r.get('median_without')} | {r.get('lift_median')} |"
        )

    lines += [
        "",
        "## Method",
        "",
        f"- `{payload.get('method_notes')}`",
        "- Causal as-of join on `end_available_at` (no future leaks).",
        "- Fixed logical groups only; no threshold optimization.",
        "- 1W/1M context only; not used for decision weighting.",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('directional_control')}\n{dec.get('cci_turn')}\n",
        encoding="utf-8",
    )
    return paths

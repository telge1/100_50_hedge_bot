"""Export generalization artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {"symbol_status"}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k not in skip}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    (out_dir / "DEFINITIONS.md").write_text(str(payload.get("definitions") or ""), encoding="utf-8")
    paths["DEFINITIONS.md"] = out_dir / "DEFINITIONS.md"

    for name, key in (
        ("coverage.csv", "coverage"),
        ("all_wave_results.csv", "all_wave_results"),
        ("failure_comparison.csv", "failure_comparison"),
        ("direction_results.csv", "direction_results"),
        ("endzone_results.csv", "endzone_results"),
        ("wave_quality_results.csv", "wave_quality_results"),
        ("trend_rsi_results.csv", "trend_rsi_results"),
        ("previous_wave_results.csv", "previous_wave_results"),
        ("edge_decay.csv", "edge_decay"),
        ("pivot_utility.csv", "pivot_utility"),
        ("cross_symbol_summary.csv", "cross_symbol_summary"),
        ("hypothesis_summary.csv", "hypothesis_summary"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    lines = [
        f"# All-Wave Fade Generalization — {payload.get('audit_version')}",
        "",
        f"Source IS audit: `{payload.get('source_audit')}`",
        f"APT IS end (frozen): `{payload.get('apt_is_end')}`",
        "",
        f"## Primary: **{payload.get('primary_decision')}**",
        f"## Failure filter: **{payload.get('failure_filter_decision')}**",
        f"## Pivot utility: **{payload.get('pivot_utility_decision')}**",
        "",
        "## Coverage",
        "",
    ]
    for k, v in (payload.get("symbol_status") or {}).items():
        lines.append(f"- `{k}`: {v.get('coverage_status')} {v.get('coverage_note') or ''}")

    lines += [
        "",
        "## Cross-symbol ALL-WAVE (main H)",
        "",
        "| TF | APT OOS | DOGE | BTC | #pos |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for r in payload.get("cross_symbol_summary") or []:
        lines.append(
            f"| {r.get('timeframe')} | {r.get('APTUSDT_OOS')} | {r.get('DOGEUSDT')} | "
            f"{r.get('BTCUSDT')} | {r.get('symbols_positive')} |"
        )

    lines += [
        "",
        "## Hypotheses",
        "",
        "| H | APT OOS | DOGE | BTC | overall |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in payload.get("hypothesis_summary") or []:
        lines.append(
            f"| {r.get('hypothesis')} | {r.get('APT_OOS')} | {r.get('DOGE')} | "
            f"{r.get('BTC')} | {r.get('overall')} |"
        )

    lines += [
        "",
        "> No strategy implementation. No parameter optimization. Frozen IS definitions only.",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{payload.get('primary_decision')}\n"
        f"{payload.get('failure_filter_decision')}\n"
        f"{payload.get('pivot_utility_decision')}\n",
        encoding="utf-8",
    )
    return paths

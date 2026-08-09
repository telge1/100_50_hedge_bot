"""Export cycle-phase failure artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k != "failure_events"}
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

    fe = out_dir / "failure_events.csv"
    payload["failure_events"].to_csv(fe, index=False)
    paths["failure_events"] = fe

    for name, key in (
        ("failure_phase_summary.csv", "failure_phase_summary"),
        ("failure_phase_1d.csv", "failure_phase_1d"),
        ("failure_phase_1d_4h.csv", "failure_phase_1d_4h"),
        ("failure_phase_1d_4h_1h.csv", "failure_phase_1d_4h_1h"),
        ("early_late_cycle_results.csv", "early_late_cycle_results"),
        ("relative_wave_weakness.csv", "relative_wave_weakness"),
        ("rsi_context_results.csv", "rsi_context_results"),
        ("ema_context_results.csv", "ema_context_results"),
        ("micro_tf_diagnostic.csv", "micro_tf_diagnostic"),
        ("base_failure_results.csv", "base_failure_results"),
    ):
        p = out_dir / name
        pd.DataFrame(payload[key]).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# Cycle Phase × 15m Failure — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Cycle-phase: **{dec.get('cycle_phase')}**",
        "",
        f"## Signal: **{dec.get('signal')}**",
        "",
        f"Events: n={payload.get('n_events')} "
        f"(UP-fail={payload.get('n_failed_up')}, DOWN-fail={payload.get('n_failed_down')})",
        "",
        "## Base failure → expected reversal",
        "",
        "| type | n | hit60 | med60 | hit120 | med120 | sample |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in payload["base_failure_results"]:
        lines.append(
            f"| {r.get('failure_type')} | {r.get('n')} | {r.get('hit_rate_60m')} | "
            f"{r.get('median_dir_ret_60m')} | {r.get('hit_rate_120m')} | "
            f"{r.get('median_dir_ret_120m')} | {r.get('sample_flag')} |"
        )

    lines += [
        "",
        "## Early vs Late (1D)",
        "",
        "| type | bucket | n | hit60 | med60 | sample |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for r in payload["early_late_cycle_results"]:
        if r.get("tf") != "1d":
            continue
        lines.append(
            f"| {r.get('failure_type')} | {r.get('bucket')} | {r.get('n')} | "
            f"{r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} | {r.get('sample_flag')} |"
        )

    lines += [
        "",
        "## Top 1D phases by |med60| among n>=30",
        "",
        "| type | D1_phase | n | hit60 | med60 | sample |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    ranked = sorted(
        [r for r in payload["failure_phase_1d"] if r.get("n", 0) >= 30],
        key=lambda r: abs(r.get("median_dir_ret_60m") or 0),
        reverse=True,
    )[:12]
    for r in ranked:
        lines.append(
            f"| {r.get('failure_type')} | {r.get('D1_phase')} | {r.get('n')} | "
            f"{r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} | {r.get('sample_flag')} |"
        )

    lines += [
        "",
        "## Relative wave weakness",
        "",
        "| type | slice | n | hit60 | med60 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for r in payload["relative_wave_weakness"]:
        lines.append(
            f"| {r.get('failure_type')} | {r.get('slice')} | {r.get('n')} | "
            f"{r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} |"
        )

    lines += [
        "",
        "## Micro TF diagnostic",
        "",
        "| type | diag | n | hit60 | med60 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for r in payload["micro_tf_diagnostic"]:
        lines.append(
            f"| {r.get('failure_type')} | {r.get('micro_diag')} | {r.get('n')} | "
            f"{r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} |"
        )

    lines += [
        "",
        "## Method",
        "",
        "```",
        str((payload.get("method") or {}).get("phase")),
        "",
        str((payload.get("method") or {}).get("failure")),
        "```",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('cycle_phase')}\n{dec.get('signal')}\n", encoding="utf-8"
    )
    return paths

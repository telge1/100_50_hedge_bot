"""Export early-detection artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {"intra_wave_snapshots", "persistence_raw"}
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

    p = out_dir / "intra_wave_snapshots.csv"
    payload["intra_wave_snapshots"].to_csv(p, index=False)
    paths["intra_wave_snapshots"] = p

    for name, key in (
        ("early_failure_prediction.csv", "early_failure_prediction"),
        ("early_failure_forward_returns.csv", "early_failure_forward_returns"),
        ("lead_time_results.csv", "lead_time_results"),
        ("failure_persistence.csv", "failure_persistence"),
        ("efficiency_decay_results.csv", "efficiency_decay_results"),
        ("micro_tf_overlay.csv", "micro_tf_overlay"),
    ):
        path = out_dir / name
        pd.DataFrame(payload[key]).to_csv(path, index=False)
        paths[name] = path

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# 15m Failure Early Detection — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Primary: **{dec.get('primary')}**",
        "",
        f"## Partial efficiency: **{dec.get('partial_efficiency')}**",
        "",
        f"Waves={payload.get('n_waves')} | Failures={payload.get('n_failures')} | "
        f"Snapshots={payload.get('n_snapshots')}",
        "",
        "## Test 1 — Predict later failure",
        "",
        "| dir | offset | n | n_cand | precision | recall | fail_rate_cand | fail_rate_other | lift |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["early_failure_prediction"]:
        lines.append(
            f"| {r.get('direction')} | {r.get('offset_min')} | {r.get('n')} | {r.get('n_candidates')} | "
            f"{r.get('precision')} | {r.get('recall')} | {r.get('failure_rate_candidate')} | "
            f"{r.get('failure_rate_non_candidate')} | {r.get('lift')} |"
        )

    lines += [
        "",
        "## Test 2 — Forward from early candidate (60m)",
        "",
        "| dir | offset | slice | n | hit60 | med60 | med60_net_fee |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["early_failure_forward_returns"]:
        if r.get("offset_min") not in (5, 8, 10, 15) and r.get("slice") != "completion_failure":
            if r.get("offset_min") not in (3, 5, 8, 10, 12, 15):
                continue
        if r.get("slice") not in (
            "early_candidate",
            "all_same_direction",
            "completion_failure",
            "later_fail_no_early_cand",
        ):
            continue
        lines.append(
            f"| {r.get('direction')} | {r.get('offset_min')} | {r.get('slice')} | {r.get('n')} | "
            f"{r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} | "
            f"{r.get('median_dir_ret_60m_net_fee')} |"
        )

    lines += [
        "",
        "## Lead time (first candidate)",
        "",
        "| dir | bucket | n | median_lead | hit60 | med60 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["lead_time_results"]:
        lines.append(
            f"| {r.get('direction')} | {r.get('bucket')} | {r.get('n')} | "
            f"{r.get('median_lead_min')} | {r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} |"
        )

    lines += [
        "",
        "## Method",
        "",
        "```",
        str(payload.get("method") or ""),
        "```",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('partial_efficiency')}\n", encoding="utf-8"
    )
    return paths

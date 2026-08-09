"""Compact export for dynamic cluster-upgrade research."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_dynamic_cluster_upgrade_db import DEFINITIONS_DOC


def _jsonable(obj: Any) -> Any:
    import numpy as np

    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    defs = out_dir / "DEFINITIONS.md"
    defs.write_text(
        f"# DEFINITIONS — {payload.get('audit_version')}\n\n```\n{DEFINITIONS_DOC.strip()}\n```\n",
        encoding="utf-8",
    )
    paths["DEFINITIONS.md"] = defs

    for name, key in (
        ("policy_comparison.csv", "policy_comparison"),
        ("upgrade_sequence_results.csv", "upgrade_sequence_results"),
        ("giveback_results.csv", "giveback_results"),
        ("highest_tf_results.csv", "highest_tf_results"),
        ("conflict_after_entry.csv", "conflict_after_entry"),
        ("four_hour_target_comparison.csv", "four_hour_target_comparison"),
        ("cross_symbol_consistency.csv", "cross_symbol_consistency"),
        ("period_stability.csv", "period_stability"),
        ("cluster_level.csv", "cluster_level"),
        ("near_tp_results.csv", "near_tp_results"),
        ("time_bucket_results.csv", "time_bucket_results"),
        ("path_summary.csv", "path_summary"),
        ("opposite_after_upgrade.csv", "opposite_after_upgrade"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    # keep summary lean: drop bulky period rows from json duplicate (still in csv)
    slim = {k: v for k, v in payload.items() if k != "period_stability"}
    slim["period_stability_note"] = "see period_stability.csv"
    sp.write_text(json.dumps(_jsonable(slim), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    ans = payload.get("answers") or {}
    lines = [
        f"# Dynamic Cluster Upgrade (P5) — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## SL policy: **{dec.get('sl_policy')}**",
        f"## Giveback: **{dec.get('giveback')}**",
        f"## Conflict: **{dec.get('conflict')}**",
        "",
        "## Answers A–H",
        f"- **A**: {ans.get('A', {}).get('answer')}",
        f"- **B**: {ans.get('B', {}).get('answer')}",
        f"- **C**: never-loosen better? `{ans.get('C', {}).get('answer')}` ({ans.get('C', {}).get('detail')})",
        f"- **D**: mean giveback when open profit = `{ans.get('D', {}).get('mean_giveback_when_open_profit')}` → {ans.get('D', {}).get('decision')}",
        f"- **E**: 15m→1h exp=`{ans.get('E', {}).get('exp_15m_to_1h')}`; 1h→4h exp=`{ans.get('E', {}).get('exp_1h_to_4h')}`",
        f"- **F**: choice=`{ans.get('F', {}).get('choice')}` prefer6=`{ans.get('F', {}).get('prefer_tp6_sl3')}` (Δexp=`{ans.get('F', {}).get('mean_delta_6_minus_4_expectancy')}`, ΔPF=`{ans.get('F', {}).get('mean_delta_6_minus_4_pf')}`)",
        f"- **G**: {ans.get('G', {}).get('decision')}",
        f"- **H**: `{ans.get('H', {}).get('candidate')}`",
        "",
        "Note: P5B ≡ P5C under frozen TPSL (higher-TF SL never tighter than lower-TF).",
        "",
        "> Research only. MySQL SoT. No CSV inputs. See DEFINITIONS.md.",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = out_dir / "summary.md"
    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('sl_policy')}\n{dec.get('giveback')}\n{dec.get('conflict')}\n",
        encoding="utf-8",
    )
    return paths

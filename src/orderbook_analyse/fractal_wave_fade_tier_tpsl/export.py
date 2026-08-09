"""Export tier TP/SL generalization artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    heavy = {
        "tpsl_grid",
        "tp_reachability",
        "large_move_matrix",
        "monthly_stability",
        "short_horizon_sensitivity",
        "long_short_results",
    }
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k not in heavy}
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

    for name, key in (
        ("signal_tiers.csv", "signal_tiers"),
        ("tpsl_grid.csv", "tpsl_grid"),
        ("tpsl_reference_combos.csv", "tpsl_reference_combos"),
        ("tier_comparison.csv", "tier_comparison"),
        ("tp_reachability.csv", "tp_reachability"),
        ("mfe_mae_by_tier.csv", "mfe_mae_by_tier"),
        ("large_move_matrix.csv", "large_move_matrix"),
        ("tpsl_frontier.csv", "tpsl_frontier"),
        ("long_short_results.csv", "long_short_results"),
        ("monthly_stability.csv", "monthly_stability"),
        ("cross_symbol_consistency.csv", "cross_symbol_consistency"),
        ("coverage.csv", "coverage"),
        ("short_horizon_sensitivity.csv", "short_horizon_sensitivity"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# Tier × TP/SL Wave Fade — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## Tier A: **{dec.get('tier')}**",
        f"## Large TP: **{dec.get('large_tp')}**",
        "",
        "## TF research candidate ranges (not strategy confirmation)",
        "",
    ]
    for tf, rec in (payload.get("tf_recommendations") or {}).items():
        lines.append(
            f"- `{tf}`: TP {rec.get('recommended_tp_range')} / SL {rec.get('recommended_sl_range')} "
            f"| TierA MFE med={rec.get('tier_a_mfe_median')} | reachable@>=25%={rec.get('evidence_reachable_tps_ge25pct')}"
        )

    lines += [
        "",
        "## Method",
        "",
        "```",
        str(payload.get("method")),
        "```",
        "",
        "> Research candidates only. No strategy confirmation.",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('tier')}\n{dec.get('large_tp')}\n",
        encoding="utf-8",
    )
    return paths

"""Export 1h/4h exit-path artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    heavy = {
        "path_metrics",
        "target_before_adverse",
        "single_tpsl_results",
        "scaleout_results",
        "mfe_capture",
        "giveback_analysis",
        "long_short_results",
        "time_stability",
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
        ("path_metrics.csv", "path_metrics"),
        ("target_before_adverse.csv", "target_before_adverse"),
        ("single_tpsl_results.csv", "single_tpsl_results"),
        ("scaleout_results.csv", "scaleout_results"),
        ("runner_results.csv", "runner_results"),
        ("mfe_capture.csv", "mfe_capture"),
        ("giveback_analysis.csv", "giveback_analysis"),
        ("long_short_results.csv", "long_short_results"),
        ("cross_symbol_comparison.csv", "cross_symbol_comparison"),
        ("time_stability.csv", "time_stability"),
        ("comparison_table.csv", "comparison_table"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    ans = payload.get("answers") or {}
    lines = [
        f"# 1h/4h Tier-A Exit/Path Research — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## Breakeven: **{dec.get('breakeven')}**",
        f"## Monetizable: **{dec.get('monetizable')}**",
        "",
        "## Fee semantics",
        "",
        "```",
        str(payload.get("fee_semantics")),
        "```",
        "",
        "## Comparison (see comparison_table.csv)",
        "",
    ]
    for r in payload.get("comparison_table") or []:
        lines.append(
            f"- `{r.get('timeframe')}` `{r.get('variant_label')}` ({r.get('variant')}): "
            f"DOGE Exp={r.get('DOGE_expectancy')} PF={r.get('DOGE_pf')} | "
            f"BTC Exp={r.get('BTC_expectancy')} PF={r.get('BTC_pf')} | "
            f"{r.get('cross_symbol_status')}"
        )

    lines += ["", "## Answers A–G", ""]
    for k, v in ans.items():
        lines.append(f"- **{k}**: `{v}`")

    lines += [
        "",
        "## Method",
        "",
        "```",
        str(payload.get("method")),
        "```",
        "",
        "> Research only. No strategy confirmation. Frozen Tier A / T0.",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('breakeven')}\n{dec.get('monetizable')}\n",
        encoding="utf-8",
    )
    return paths

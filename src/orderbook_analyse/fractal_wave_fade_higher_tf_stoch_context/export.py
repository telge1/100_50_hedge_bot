"""Write higher-TF Stoch context artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        v = float(x)
        return None if v != v else v
    if hasattr(x, "item"):
        try:
            return _jsonable(x.item())
        except Exception:
            pass
    if isinstance(x, float) and x != x:
        return None
    return x


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    def wdf(name: str, df: pd.DataFrame | None) -> None:
        p = out_dir / name
        if df is None or df.empty:
            pd.DataFrame().to_csv(p, index=False)
        else:
            df.to_csv(p, index=False)
        paths[name] = p

    wdf("trade_mtf_stoch_snapshots.csv", payload["snapshots"])
    wdf("support_count_statistics.csv", payload["support_count_statistics"])
    wdf("raw_k_bucket_statistics.csv", payload["raw_k_bucket_statistics"])
    wdf("pattern_statistics.csv", payload["pattern_statistics"])
    wdf("by_first_signal_tf.csv", payload["by_first_signal_tf"])
    wdf("by_symbol.csv", payload["by_symbol"])
    wdf("by_side.csv", payload["by_side"])
    wdf("case_study_apt_20260806_1016.csv", payload["case_study"])
    wdf("deep_dive_15m.csv", payload["deep_dive_15m"])
    wdf("support_label_statistics.csv", payload.get("support_label_statistics"))

    (out_dir / "DEFINITIONS.md").write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["DEFINITIONS.md"] = out_dir / "DEFINITIONS.md"

    # strip heavy frames from json
    summary = {
        "audit_version": payload["audit_version"],
        "n_trades": payload["n_trades"],
        "causality_violations": payload["causality_violations"],
        "baseline": payload["baseline"],
        "baseline_15m": payload["baseline_15m"],
        "baseline_15m_long": payload["baseline_15m_long"],
        "baseline_15m_short": payload["baseline_15m_short"],
        "decisions": payload["decisions"],
        "answers": payload["answers"],
        "tf_contribution": {
            k: {
                kk: vv
                for kk, vv in v.items()
                if kk not in ("supportive", "not_supportive")
            }
            | {
                "supportive": v.get("supportive"),
                "not_supportive": v.get("not_supportive"),
            }
            for k, v in payload["tf_contribution"].items()
        },
        "support_count_15m": payload["support_count_statistics"].to_dict(orient="records")
        if payload["support_count_statistics"] is not None
        and not payload["support_count_statistics"].empty
        else [],
        "case_study": payload["case_study"].to_dict(orient="records")
        if payload["case_study"] is not None and not payload["case_study"].empty
        else [],
        "deep_dive_15m": payload["deep_dive_15m"].to_dict(orient="records")
        if payload["deep_dive_15m"] is not None and not payload["deep_dive_15m"].empty
        else [],
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["summary.json"] = p

    p = out_dir / "summary.md"
    p.write_text(_md(payload), encoding="utf-8")
    paths["summary.md"] = p
    return paths


def _md(p: dict[str, Any]) -> str:
    d = p["decisions"]
    b = p["baseline"]
    lines = [
        f"# Higher-TF Stoch Context — {p['audit_version']}",
        "",
        f"## Primary decision",
        f"**{d['primary']}**",
        "",
        f"Trades analyzed: **{p['n_trades']}** · causality violations: **{p['causality_violations']}**",
        "",
        f"## Baseline (all trades)",
        f"- TP rate: {b.get('tp_rate')}",
        f"- Expectancy: {b.get('expectancy')}",
        f"- PF: {b.get('profit_factor')}",
        "",
        "## Secondary decisions",
        f"- {d['30m']}",
        f"- {d['1h']}",
        f"- {d['4h']}",
        f"- {d['alignment']}",
        f"- {d['without_support']}",
        f"- monotonic_lift_visible: {d['monotonic_lift_visible']}",
        "",
        "## 15m entries by higher_tf_support_count",
        "",
        "| count | n | TP rate | exp | PF | Δexp | ΔTP |",
        "|------|---|--------|-----|----|------|-----|",
    ]
    sc = p["support_count_statistics"]
    if sc is not None and not sc.empty:
        for _, r in sc.sort_values("higher_tf_support_count").iterrows():
            lines.append(
                f"| {r['higher_tf_support_count']} | {r['n']} | {r.get('tp_rate')} | "
                f"{r.get('expectancy')} | {r.get('profit_factor')} | "
                f"{r.get('delta_expectancy_vs_baseline')} | {r.get('delta_tp_rate_vs_baseline')} |"
            )
    lines += [
        "",
        "## Role assessment",
        f"{p['answers'].get('q10_role')}",
        "",
        "## APT case study 2026-08-06 10:16",
        "",
    ]
    cs = p["case_study"]
    if cs is not None and not cs.empty:
        lines.append("| TF | K | D | zone | turn | wave | supportive | available_at |")
        lines.append("|----|---|---|------|------|------|------------|--------------|")
        for _, r in cs.iterrows():
            lines.append(
                f"| {r['tf']} | {r.get('k')} | {r.get('d')} | {r.get('zone')} | "
                f"{r.get('turn')} | {r.get('wave_direction')} | {r.get('supportive')} | "
                f"{r.get('available_at')} |"
            )
    lines += [
        "",
        "> Analysis only. No strategy change. See DEFINITIONS.md.",
        "",
    ]
    return "\n".join(lines)

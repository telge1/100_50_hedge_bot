"""Export parent × lower-TF context artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    heavy = {
        "parent_signals_with_lower_tf",
        "single_lower_tf_phase_results",
        "propagation_timing",
        "fixed_tpsl_by_lower_context",
        "long_short_results",
    }
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k not in heavy}
    if isinstance(obj, list):
        # trim huge nested answer blobs
        return [_jsonable(x) for x in obj[:500]] if len(obj) > 500 else [_jsonable(x) for x in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, key in (
        ("parent_signals_with_lower_tf.csv", "parent_signals_with_lower_tf"),
        ("single_lower_tf_phase_results.csv", "single_lower_tf_phase_results"),
        ("exhausted_count_results.csv", "exhausted_count_results"),
        ("ready_count_results.csv", "ready_count_results"),
        ("phase_sequence_results.csv", "phase_sequence_results"),
        ("propagation_timing.csv", "propagation_timing"),
        ("fixed_tpsl_by_lower_context.csv", "fixed_tpsl_by_lower_context"),
        ("long_short_results.csv", "long_short_results"),
        ("cross_symbol_consistency.csv", "cross_symbol_consistency"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    # slim answers for json
    slim = dict(payload)
    # answers may contain full row dicts — keep as-is but jsonable
    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(slim), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    ans = payload.get("answers") or {}
    lines = [
        f"# Parent Signal × Lower-TF Context — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## Extended: **{dec.get('extended')}**",
        f"## Ready: **{dec.get('ready')}**",
        "",
        f"## G recommendation: **{ans.get('G_recommendation')}**",
        "",
        "## Answers A–F (see summary.json for detail)",
        "",
    ]
    for k in ("A_1h_SHORT_all_LOW", "B_1h_LONG_all_HIGH", "C_counts_1h", "D_ready_all_lower", "E_best_30m_phase_1h", "F_4h"):
        if k in ans:
            lines.append(f"- **{k}**: `{ans[k] if not isinstance(ans[k], dict) else 'see summary.json'}`")

    lines += [
        "",
        "## Method",
        "",
        "```",
        str(payload.get("method")),
        "```",
        "",
        "> Research only. No hard filter. Frozen Tier A / T0.",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp
    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('extended')}\n{dec.get('ready')}\n",
        encoding="utf-8",
    )
    return paths

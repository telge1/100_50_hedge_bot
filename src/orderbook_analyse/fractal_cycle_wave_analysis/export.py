"""Export fractal wave analysis artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k != "waves_by_tf"}
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, float):
        if pd.isna(obj):
            return None
        return obj
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return obj


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    summary = _jsonable(payload)
    p_json = out_dir / "summary.json"
    p_json.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    paths["summary"] = p_json

    # Per-TF wave CSVs
    waves_by_tf = payload.get("waves_by_tf") or {}
    for tf, waves in waves_by_tf.items():
        if waves is None or getattr(waves, "empty", True):
            continue
        safe = tf.replace("/", "_")
        path = out_dir / f"waves_{safe}.csv"
        waves.to_csv(path, index=False)
        paths[f"waves_{safe}"] = path

    # TF summary table
    rows = []
    for s in payload.get("tf_summaries") or []:
        asym = s.get("asymmetry") or {}
        up = s.get("up") or {}
        down = s.get("down") or {}
        rows.append(
            {
                "timeframe": s.get("timeframe"),
                "n_waves": s.get("n_waves"),
                "n_up": s.get("n_up"),
                "n_down": s.get("n_down"),
                "up_mean_price_move_pct": up.get("mean_price_move_pct"),
                "down_mean_price_move_pct": down.get("mean_price_move_pct"),
                "up_mean_eff": up.get("mean_directional_efficiency"),
                "down_mean_eff": down.get("mean_directional_efficiency"),
                "up_inefficient_share": up.get("inefficient_share"),
                "down_inefficient_share": down.get("inefficient_share"),
                "directionally_coherent": asym.get("directionally_coherent"),
                "abs_mean_asymmetry": asym.get("abs_mean_up_minus_abs_mean_down"),
            }
        )
    p_tf = out_dir / "tf_efficiency_summary.csv"
    pd.DataFrame(rows).to_csv(p_tf, index=False)
    paths["tf_efficiency_summary"] = p_tf

    # Markdown report
    vis = payload.get("visibility") or {}
    cov = payload.get("coverage") or []
    lines = [
        f"# Fractal Cycle Wave Analysis — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Decision: **{vis.get('decision')}**",
        "",
        f"- Coherent TFs (UP mean>0 & DOWN mean<0): `{vis.get('coherent_tfs')}`",
        f"- n_coherent: `{vis.get('n_coherent_tfs')}`",
        f"- median |mean move| %: `{vis.get('median_abs_mean_move_pct')}`",
        f"- thresholds: `{vis.get('thresholds')}`",
        "",
        "## Coverage",
        "",
        "| TF | n | min_open | max_open |",
        "| --- | ---: | --- | --- |",
    ]
    for c in cov:
        lines.append(
            f"| {c['timeframe']} | {c['n']} | {c.get('min_open')} | {c.get('max_open')} |"
        )
    lines += [
        "",
        f"Full stack window: `{payload.get('full_stack_window')}`",
        "",
        "## UP vs DOWN price efficiency (mean price_move_pct)",
        "",
        "| TF | n_up | up_mean% | n_down | down_mean% | coherent |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for s in payload.get("tf_summaries") or []:
        up = s.get("up") or {}
        down = s.get("down") or {}
        asym = s.get("asymmetry") or {}
        lines.append(
            f"| {s.get('timeframe')} | {s.get('n_up')} | {up.get('mean_price_move_pct')} | "
            f"{s.get('n_down')} | {down.get('mean_price_move_pct')} | "
            f"{asym.get('directionally_coherent')} |"
        )

    lines += ["", "## Parent 1d context (COUNTER vs ALIGNED abs price)", ""]
    for tf, sm in (payload.get("parent_1d_context") or {}).items():
        lines.append(
            f"- **{tf} vs 1d**: ALIGNED abs={((sm.get('ALIGNED') or {}).get('mean_abs_price_move_pct'))}, "
            f"COUNTER abs={((sm.get('COUNTER') or {}).get('mean_abs_price_move_pct'))}, "
            f"counter_weaker={sm.get('counter_weaker_than_aligned')}"
        )

    lines += ["", "## Re-alignment sequences", ""]
    for r in payload.get("re_alignment") or []:
        seq = r.get("sequences") or {}
        lines.append(
            f"- {r.get('child_tf')}→{r.get('parent_tf')}: candidates={seq.get('n_candidates')}, "
            f"realign={seq.get('n_realign')}, rate={seq.get('realign_rate')}, "
            f"followthrough_signed={seq.get('mean_followthrough_signed_pct')}"
        )

    lines += [
        "",
        "## Notes",
        "",
        "- Waves = completed Stoch RSI K/D-cross runs (min 3 bars).",
        "- Directional efficiency = signed_price_move_pct / |ΔK|.",
        "- Inefficient flag: |ΔK|≥10 and |price_move_pct|≤0.02.",
        "- No trading rules, protected levels, or orderbook joins.",
        "",
    ]
    p_md = out_dir / "REPORT.md"
    p_md.write_text("\n".join(lines), encoding="utf-8")
    paths["report"] = p_md

    p_dec = out_dir / "DECISION.txt"
    p_dec.write_text(str(vis.get("decision") or "UNKNOWN") + "\n", encoding="utf-8")
    paths["decision"] = p_dec
    return paths

"""Export multi-TF failure artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {"failure_events_all_tf"}
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

    ev = payload["failure_events_all_tf"]
    if isinstance(ev, pd.DataFrame):
        ev.to_csv(out_dir / "failure_events_all_tf.csv", index=False)
    else:
        pd.DataFrame(ev).to_csv(out_dir / "failure_events_all_tf.csv", index=False)
    paths["failure_events_all_tf"] = out_dir / "failure_events_all_tf.csv"

    for name, key in (
        ("failure_forward_returns.csv", "failure_forward_returns"),
        ("failure_baseline_comparison.csv", "failure_baseline_comparison"),
        ("first_touch_by_tf.csv", "first_touch_by_tf"),
        ("edge_decay_by_tf.csv", "edge_decay_by_tf"),
        ("failure_strength_by_tf.csv", "failure_strength_by_tf"),
        ("previous_wave_asymmetry.csv", "previous_wave_asymmetry"),
        ("monthly_stability.csv", "monthly_stability"),
        ("timeframe_ranking.csv", "timeframe_ranking"),
    ):
        p = out_dir / name
        pd.DataFrame(payload[key]).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    tf_dec = payload.get("tf_decisions") or {}
    overall = payload.get("overall_decision")
    lines = [
        f"# Multi-TF Wave Failure — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Overall: **{overall}**",
        "",
        "## Per-TF decisions",
        "",
    ]
    for tf, dec in tf_dec.items():
        lines.append(f"- `{tf}`: **{dec}**")

    lines += [
        "",
        "### Decision notes",
        "",
        "- `FAILURE_SIGNAL_HAS_EDGE`: absolute edge on main horizon **and** hit-lift vs NON_FAILED >= +3pp.",
        "- `FAILURE_SIGNAL_CONTEXT_DEPENDENT`: absolute fade-after-failure edge present, but failure filter does not beat same-direction non-failed / all-wave fade baseline.",
        "- `FAILURE_SIGNAL_NO_EDGE`: no usable absolute edge after fees/horizon rules.",
        "",
        "## Ranking (fixed preregistered horizons)",
        "",
        "| TF | Failure | n | H | hit | med | net-fee | lift | monthly+ | decision |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in payload.get("timeframe_ranking") or []:
        if r.get("failure_type") != "ALL":
            continue
        lines.append(
            f"| {r.get('timeframe')} | {r.get('failure_type')} | {r.get('n')} | "
            f"{r.get('best_preregistered_horizon_min')} | {r.get('hit_rate')} | "
            f"{r.get('median_dir_ret')} | {r.get('median_net_after_fee')} | "
            f"{r.get('hit_rate_lift_vs_baseline')} | {r.get('monthly_share_median_positive')} | "
            f"{r.get('decision')} |"
        )

    lines += [
        "",
        "> APT in-sample only. No strategy confirmation.",
        "",
        "## Method",
        "",
        "```",
        str((payload.get("method") or {}).get("failure")),
        "",
        str((payload.get("method") or {}).get("general")),
        "```",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    dec_lines = [str(overall)] + [f"{tf}={dec}" for tf, dec in tf_dec.items()]
    (out_dir / "DECISION.txt").write_text("\n".join(dec_lines) + "\n", encoding="utf-8")
    return paths

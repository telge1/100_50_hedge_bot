"""Export all-wave fade artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {"all_wave_events"}
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

    ev = payload["all_wave_events"]
    if isinstance(ev, pd.DataFrame):
        ev.to_csv(out_dir / "all_wave_events.csv", index=False)
    else:
        pd.DataFrame(ev).to_csv(out_dir / "all_wave_events.csv", index=False)
    paths["all_wave_events"] = out_dir / "all_wave_events.csv"

    for name, key in (
        ("all_wave_forward_returns.csv", "all_wave_forward_returns"),
        ("failure_vs_all_comparison.csv", "failure_vs_all_comparison"),
        ("stoch_end_zone_results.csv", "stoch_end_zone_results"),
        ("stoch_path_results.csv", "stoch_path_results"),
        ("wave_duration_results.csv", "wave_duration_results"),
        ("efficiency_quantiles.csv", "efficiency_quantiles"),
        ("wave_size_quantiles.csv", "wave_size_quantiles"),
        ("rsi_context_results.csv", "rsi_context_results"),
        ("ema_context_results.csv", "ema_context_results"),
        ("previous_wave_results.csv", "previous_wave_results"),
        ("edge_decay.csv", "edge_decay"),
        ("first_touch.csv", "first_touch"),
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
    fail_dec = payload.get("failure_filter_decision")
    lines = [
        f"# All-Wave Stoch Fade — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Overall: **{overall}**",
        "",
        f"## Failure filter: **{fail_dec}**",
        "",
        "## Per-TF decisions",
        "",
    ]
    for tf, dec in tf_dec.items():
        lines.append(f"- `{tf}`: **{dec}**")

    lines += [
        "",
        "## Ranking ALL / FAILED / NON_FAILED (COMBINED, fixed main H)",
        "",
        "| TF | group | n | H | hit | med | net-fee | monthly+ |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload.get("timeframe_ranking") or []:
        if r.get("side") != "COMBINED":
            continue
        lines.append(
            f"| {r.get('timeframe')} | {r.get('wave_group')} | {r.get('n')} | "
            f"{r.get('main_horizon')} | {r.get('hit')} | {r.get('median_return')} | "
            f"{r.get('net_after_fee')} | {r.get('monthly_positive_share')} |"
        )

    lines += [
        "",
        "> APT in-sample only. No strategy confirmation.",
        "",
        "## Method",
        "",
        "```",
        str(payload.get("method")),
        "```",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    dec_lines = [str(overall), str(fail_dec)] + [f"{tf}={dec}" for tf, dec in tf_dec.items()]
    (out_dir / "DECISION.txt").write_text("\n".join(dec_lines) + "\n", encoding="utf-8")
    return paths

"""Export fractal direction + entry artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {
        "direction_state_snapshots",
        "entry_signals",
    }
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

    snap = out_dir / "direction_state_snapshots.csv"
    payload["direction_state_snapshots"].to_csv(snap, index=False)
    paths["direction_state_snapshots"] = snap

    sig = out_dir / "entry_signals.csv"
    payload["entry_signals"].to_csv(sig, index=False)
    paths["entry_signals"] = sig

    for name, key in (
        ("direction_state_summary.csv", "direction_state_summary"),
        ("direction_forward_returns.csv", "direction_forward_returns"),
        ("direction_monthly_robustness.csv", "direction_monthly_robustness"),
        ("direction_half_blocks.csv", "direction_half_blocks"),
        ("entry_signal_summary.csv", "entry_signal_summary"),
        ("entry_baseline_comparison.csv", "entry_baseline_comparison"),
    ):
        p = out_dir / name
        pd.DataFrame(payload[key]).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# Fractal Direction + Entry — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Direction: **{dec.get('direction')}**",
        "",
        f"## Entry: **{dec.get('entry')}**",
        "",
        f"Snapshots: n={payload.get('n_snapshots')} | "
        f"LONG entries={payload.get('n_long_entries')} | "
        f"SHORT entries={payload.get('n_short_entries')}",
        "",
        "## Direction state summary (60m / 120m)",
        "",
        "| state | n | hit60 | med60 | hit120 | med120 | small |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in payload["direction_state_summary"]:
        lines.append(
            f"| {r.get('state')} | {r.get('n')} | {r.get('hit_rate_60m')} | "
            f"{r.get('median_dir_ret_60m')} | {r.get('hit_rate_120m')} | "
            f"{r.get('median_dir_ret_120m')} | {r.get('small_sample')} |"
        )

    lines += [
        "",
        "## Half-period robustness",
        "",
        "| block | state | n | hit60 | med60 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for r in payload.get("direction_half_blocks") or []:
        lines.append(
            f"| {r.get('block')} | {r.get('state')} | {r.get('n')} | "
            f"{r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} |"
        )

    lines += [
        "",
        "## Entry vs baselines (60m)",
        "",
        "| side | slice | n | hit60 | med60 | med60_net_fee | small |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in payload["entry_baseline_comparison"]:
        lines.append(
            f"| {r.get('side')} | {r.get('slice')} | {r.get('n')} | {r.get('hit_rate_60m')} | "
            f"{r.get('median_dir_ret_60m')} | {r.get('median_dir_ret_60m_net_fee')} | "
            f"{r.get('small_sample')} |"
        )

    lines += [
        "",
        "## Method notes",
        "",
        "- CCI carried only; not used for regime/entry.",
        "- No protected-level filter.",
        "- No threshold optimization; rules fixed a priori.",
        "- Fee 0.11% roundtrip shown as reference only.",
        "",
        "### Regime rules",
        "",
        "```",
        str((payload.get("method") or {}).get("regime_rules") or ""),
        "```",
        "",
        "### Episode dedupe",
        "",
        "```",
        str((payload.get("method") or {}).get("episode_dedupe") or ""),
        "```",
        "",
    ]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('direction')}\n{dec.get('entry')}\n", encoding="utf-8"
    )
    return paths

"""Export confirmation-entry artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _jsonable(obj: Any) -> Any:
    skip = {
        "entry_delay_detail",
        "micro_wait_detail",
        "pullback_detail",
        "first_touch_detail",
        "confirmation_events",
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

    p = out_dir / "confirmation_events.csv"
    payload["confirmation_events"].to_csv(p, index=False)
    paths["confirmation_events"] = p

    # optional detail for reproducibility of entries
    payload["entry_delay_detail"].to_csv(out_dir / "entry_delay_detail.csv", index=False)

    for name, key in (
        ("entry_delay_results.csv", "entry_delay_results"),
        ("edge_decay.csv", "edge_decay"),
        ("micro_alignment_results.csv", "micro_alignment_results"),
        ("micro_wait_strategy.csv", "micro_wait_strategy"),
        ("pullback_entry_results.csv", "pullback_entry_results"),
        ("first_touch_results.csv", "first_touch_results"),
        ("failure_strength_results.csv", "failure_strength_results"),
    ):
        path = out_dir / name
        pd.DataFrame(payload[key]).to_csv(path, index=False)
        paths[name] = path

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# 15m Failure Confirmation Entry — {payload.get('symbol')}",
        "",
        f"Audit: `{payload.get('audit_version')}`",
        "",
        f"## Primary: **{dec.get('primary')}**",
        "",
        f"## Pullback: **{dec.get('pullback')}**",
        "",
        f"Events: n={payload.get('n_events')}",
        "",
        "## Entry price semantics",
        "",
        "```",
        str((payload.get("method") or {}).get("entry_price") or ""),
        "```",
        "",
        "## Edge decay (COMBINED, 60m)",
        "",
        "| delay | n | hit60 | med60 | med60_net_fee | med_fav | med_adv |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["edge_decay"]:
        if r.get("side") != "COMBINED":
            continue
        lines.append(
            f"| {r.get('delay_min')} | {r.get('n')} | {r.get('hit_rate_60m')} | "
            f"{r.get('median_dir_ret_60m')} | {r.get('median_dir_ret_60m_net_fee')} | "
            f"{r.get('median_fav_60m')} | {r.get('median_adv_60m')} |"
        )

    lines += [
        "",
        "## Micro wait strategies (COMBINED)",
        "",
        "| strategy | n_filled | fill_rate | med_wait | hit60 | med60 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["micro_wait_strategy"]:
        if r.get("side") != "COMBINED":
            continue
        lines.append(
            f"| {r.get('strategy')} | {r.get('n_filled')} | {r.get('fill_rate')} | "
            f"{r.get('median_wait_min')} | {r.get('hit_rate_60m')} | {r.get('median_dir_ret_60m')} |"
        )

    lines += [
        "",
        "## Pullback (COMBINED)",
        "",
        "| bucket | fill | missed | med_improv | hit60 | med60 | opp_adj_med60 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["pullback_entry_results"]:
        if r.get("side") != "COMBINED":
            continue
        lines.append(
            f"| {r.get('bucket')} | {r.get('fill_rate')} | {r.get('missed_rate')} | "
            f"{r.get('median_entry_improvement_pct')} | {r.get('hit_rate_60m')} | "
            f"{r.get('median_dir_ret_60m')} | {r.get('opportunity_adjusted_med60')} |"
        )

    lines += [
        "",
        "## First touch after T0 (COMBINED)",
        "",
        "| level | fav_first | adv_first | both | none |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload["first_touch_results"]:
        if r.get("side") != "COMBINED":
            continue
        lines.append(
            f"| {r.get('level_pct')} | {r.get('share_favorable_first')} | "
            f"{r.get('share_adverse_first')} | {r.get('share_both_same_bar')} | "
            f"{r.get('share_none')} |"
        )

    lines += ["", "## Method", "", f"- `{payload.get('method')}`", ""]
    mp = out_dir / "summary.md"
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp

    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('pullback')}\n", encoding="utf-8"
    )
    return paths

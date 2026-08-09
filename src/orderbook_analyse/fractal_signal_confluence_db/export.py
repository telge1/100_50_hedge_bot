"""Compact export for confluence DB research."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_signal_confluence_db import DEFINITIONS_DOC


def _jsonable(obj: Any) -> Any:
    heavy = {"tier_within_confluence", "entry_policy_comparison"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in heavy:
                continue
            if k == "answers":
                # trim nested lists
                ans = dict(v or {})
                for ak in ("B_strength_mono", "C_dedupe", "D_overlap", "E_conflict"):
                    if ak in ans and isinstance(ans[ak], list):
                        ans[ak] = f"see csv ({len(ans[ak])} rows)"
                out[k] = _jsonable(ans)
            else:
                out[k] = _jsonable(v)
        return out
    if isinstance(obj, list):
        return [_jsonable(x) for x in obj[:200]]
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
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
        ("confluence_summary.csv", "confluence_summary"),
        ("confluence_combinations.csv", "confluence_combinations"),
        ("entry_policy_comparison.csv", "entry_policy_comparison"),
        ("conflict_results.csv", "conflict_results"),
        ("dedupe_summary.csv", "dedupe_summary"),
        ("overlap_summary.csv", "overlap_summary"),
        ("cross_symbol_consistency.csv", "cross_symbol_consistency"),
        ("strength_monotonicity.csv", "strength_monotonicity"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    sp.write_text(json.dumps(_jsonable(payload), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    lines = [
        f"# Multi-TF Signal Confluence (DB) — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## Dedupe: **{dec.get('dedupe')}**",
        f"## Conflict: **{dec.get('conflict')}**",
        f"## Policy: **{dec.get('policy')}**",
        "",
        "> Research only. MySQL SoT. No CSV inputs. See DEFINITIONS.md.",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = out_dir / "summary.md"
    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('dedupe')}\n{dec.get('conflict')}\n{dec.get('policy')}\n",
        encoding="utf-8",
    )
    return paths

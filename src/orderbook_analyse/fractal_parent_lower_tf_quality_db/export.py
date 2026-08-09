"""Compact export for DB quality-rank research."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_parent_lower_tf_quality_db import QUALITY_RULE_DOC


def _jsonable(obj: Any) -> Any:
    heavy = {"db_inventory"}
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

    defs = out_dir / "DEFINITIONS.md"
    defs.write_text(
        "\n".join(
            [
                f"# DEFINITIONS — {payload.get('audit_version')}",
                "",
                "## Database",
                "",
                str(payload.get("db_inventory_note")),
                "",
                f"APT IS end (frozen): `{payload.get('apt_is_end')}`",
                "",
                "Efficiency Q1–Q4 edges recomputed from APTUSDT `market_candles` via existing",
                "`segment_stoch_waves` + IS cutoff (no CSV event inputs).",
                "",
                "## Quality rule (a priori)",
                "",
                "```",
                QUALITY_RULE_DOC.strip(),
                "```",
                "",
                "## Parent Tier A (frozen)",
                "",
                "- TREND_ALIGNED: SHORT=UP+EMA_BULL; LONG=DOWN+EMA_BEAR",
                "- Q4: directional_efficiency above frozen APT-IS Q3 edge",
                "- Fade: UP→SHORT, DOWN→LONG",
                "- T0: first 1m open strictly after confirmation_available_at (= end_available_at)",
                "",
                "## Fees / exits",
                "",
                "- Roundtrip fee 0.11%",
                "- SL_FIRST on same-bar TP/SL",
                "- Fixed TPSL only: 1h 2/1.5 and 3/2; 4h 4/2 and 6/3",
                "",
            ]
        ),
        encoding="utf-8",
    )
    paths["DEFINITIONS.md"] = defs

    for name, key in (
        ("quality_summary.csv", "quality_summary"),
        ("fixed_tpsl_by_quality.csv", "fixed_tpsl_by_quality"),
        ("cross_symbol_consistency.csv", "cross_symbol_consistency"),
        ("monotonicity.csv", "monotonicity"),
        ("sizing_research.csv", "sizing_research"),
        ("tp_reach_by_quality.csv", "tp_reach_by_quality"),
        ("frequency.csv", "frequency"),
    ):
        p = out_dir / name
        pd.DataFrame(payload.get(key) or []).to_csv(p, index=False)
        paths[name] = p

    sp = out_dir / "summary.json"
    # answers.B_cross_symbol duplicates csv — keep compact
    slim = dict(payload)
    ans = dict(slim.get("answers") or {})
    if "B_cross_symbol" in ans:
        ans["B_cross_symbol"] = "see cross_symbol_consistency.csv"
    if "frequency_note" in ans:
        ans["frequency_note"] = "see frequency.csv"
    if "C_sizing" in ans:
        ans["C_sizing"] = "see sizing_research.csv"
    slim["answers"] = ans
    sp.write_text(json.dumps(_jsonable(slim), indent=2, default=str), encoding="utf-8")
    paths["summary.json"] = sp

    dec = payload.get("decisions") or {}
    ans = payload.get("answers") or {}
    lines = [
        f"# Lower-TF Quality Rank (DB) — {payload.get('audit_version')}",
        "",
        f"## Primary: **{dec.get('primary')}**",
        f"## Sizing: **{dec.get('sizing')}**",
        f"## TP selection: **{dec.get('tp_selection')}**",
        "",
        f"Later use (E): **{ans.get('E_later_use')}**",
        f"T0 (F): **{ans.get('F_T0_for_all')}**",
        "",
        "## DB",
        "",
        str(payload.get("db_inventory_note")),
        "",
        "> Research only. A priori quality classes. No hard block. No CSV inputs.",
        "",
    ]
    mp = out_dir / "summary.md"
    # placeholder; enriched after run
    mp.write_text("\n".join(lines), encoding="utf-8")
    paths["summary.md"] = mp
    (out_dir / "DECISION.txt").write_text(
        f"{dec.get('primary')}\n{dec.get('sizing')}\n{dec.get('tp_selection')}\n",
        encoding="utf-8",
    )
    return paths

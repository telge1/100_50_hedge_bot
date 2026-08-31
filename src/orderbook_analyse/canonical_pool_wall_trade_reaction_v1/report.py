"""Summaries and report writers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.contracts import (
    ANALYSIS_END,
    ANALYSIS_START,
    EATEN_TRADE_FRAC,
    FORWARD_SECONDS,
    OUT_ROOT,
    PULLED_TRADE_FRAC,
    REJECT_REVERSAL_ZONE_FRAC,
    TOUCH_TOLERANCE_BPS,
    WALL_IN_ZONE_MIN_FRAC,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def membership_bucket(p: int) -> str:
    if p <= 1:
        return "SINGLETON_P1"
    if p == 2:
        return "PAIR_P2"
    if p <= 4:
        return "CLUSTER_P3_4"
    if p <= 8:
        return "CLUSTER_P5_8"
    return "CLUSTER_P9_PLUS"


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["membership"] = out["maximum_P"].fillna(0).astype(int).map(membership_bucket)
    out["has_multi_tf"] = out["class_tags_str"].fillna("").str.contains("MULTI_TF_OVERLAP")
    out["has_parent"] = out["class_tags_str"].fillna("").str.contains("PARENT_15M_30M")
    return out


def crosstab_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Structure × wall_in_pool × reaction rates."""
    touched = df[df["touched"] == True].copy()  # noqa: E712
    if touched.empty:
        return pd.DataFrame()
    rows = []
    for (tf, side, mem, wall), g in touched.groupby(
        ["timeframe", "side", "membership", "wall_in_pool"], dropna=False
    ):
        n = len(g)
        rows.append(
            {
                "timeframe": tf,
                "side": side,
                "membership": mem,
                "wall_in_pool": wall,
                "n_touched": n,
                "n_rejected": int((g["reaction"] == "REJECTED").sum()),
                "n_passed": int((g["reaction"] == "PASSED_THROUGH").sum()),
                "n_ambiguous": int((g["reaction"] == "AMBIGUOUS").sum()),
                "reject_rate": round(float((g["reaction"] == "REJECTED").mean()), 4),
                "pass_rate": round(float((g["reaction"] == "PASSED_THROUGH").mean()), 4),
                "n_eaten": int((g["wall_fate"] == "EATEN").sum()),
                "n_pulled": int((g["wall_fate"] == "PULLED").sum()),
                "n_held": int((g["wall_fate"] == "HELD").sum()),
                "eaten_rate": round(float((g["wall_fate"] == "EATEN").mean()), 4),
                "pulled_rate": round(float((g["wall_fate"] == "PULLED").mean()), 4),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["n_touched", "reject_rate"], ascending=[False, False]
    )


def wall_fate_by_reaction(df: pd.DataFrame) -> pd.DataFrame:
    touched = df[df["touched"] == True].copy()  # noqa: E712
    if touched.empty:
        return pd.DataFrame()
    rows = []
    for (fate, reaction), g in touched.groupby(["wall_fate", "reaction"]):
        rows.append({"wall_fate": fate, "reaction": reaction, "n": len(g)})
    return pd.DataFrame(rows).sort_values("n", ascending=False)


def write_outputs(
    df: pd.DataFrame,
    market_meta: dict[str, Any],
    freeze: dict[str, Any],
    *,
    smoke: bool,
) -> dict[str, Any]:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    df = enrich(df)
    cross = crosstab_summary(df)
    fate = wall_fate_by_reaction(df)

    episodes_path = OUT_ROOT / "episode_reactions.csv"
    cross_path = OUT_ROOT / "crosstab_structure_wall_reaction.csv"
    fate_path = OUT_ROOT / "wall_fate_by_reaction.csv"
    df.to_csv(episodes_path, index=False)
    cross.to_csv(cross_path, index=False)
    fate.to_csv(fate_path, index=False)

    touched = df[df["touched"] == True]  # noqa: E712
    summary = {
        "generated_at": _now(),
        "smoke": smoke,
        "analysis_start": ANALYSIS_START,
        "analysis_end": ANALYSIS_END,
        "n_episodes": int(len(df)),
        "n_with_feature_data": int((df["feature_seconds"] > 0).sum()),
        "n_wall_in_pool_yes": int((df["wall_in_pool"] == "YES").sum()),
        "n_touched": int(len(touched)),
        "n_rejected": int((touched["reaction"] == "REJECTED").sum()) if len(touched) else 0,
        "n_passed": int((touched["reaction"] == "PASSED_THROUGH").sum()) if len(touched) else 0,
        "n_ambiguous": int((touched["reaction"] == "AMBIGUOUS").sum()) if len(touched) else 0,
        "reject_rate_touched": round(float((touched["reaction"] == "REJECTED").mean()), 4)
        if len(touched)
        else None,
        "thresholds": {
            "WALL_IN_ZONE_MIN_FRAC": WALL_IN_ZONE_MIN_FRAC,
            "TOUCH_TOLERANCE_BPS": TOUCH_TOLERANCE_BPS,
            "FORWARD_SECONDS": FORWARD_SECONDS,
            "EATEN_TRADE_FRAC": EATEN_TRADE_FRAC,
            "PULLED_TRADE_FRAC": PULLED_TRADE_FRAC,
            "REJECT_REVERSAL_ZONE_FRAC": REJECT_REVERSAL_ZONE_FRAC,
        },
        "market_meta": market_meta,
        "structural_freeze": {
            "structural_analysis_spec_sha256": freeze.get("structural_analysis_spec_sha256"),
            "structural_class_bundle_sha256": freeze.get("structural_class_bundle_sha256"),
        },
        "outputs": {
            "episode_reactions": str(episodes_path),
            "crosstab": str(cross_path),
            "wall_fate_by_reaction": str(fate_path),
        },
    }
    (OUT_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    # Top lines for humans
    top = cross.head(25) if not cross.empty else pd.DataFrame()
    lines = [
        "# Canonical Pool × Wall × Trade Reaction V1",
        "",
        f"Generated: `{summary['generated_at']}`",
        f"Smoke: `{smoke}`",
        "",
        "## Scope",
        "",
        "- Pools: frozen `raw_pool_episodes.parquet` (canonical LLD)",
        "- Walls: dominant 1s wall from `orderbook_features_1s_v2` (NOT full L2 depth)",
        "- Trades: `public_trades_canonical` aggregated to 1s",
        "- No PnL, no entries, no TP/SL, no strategy",
        "- `orderbook_deltas` not used (broken; no alias)",
        "",
        "## Counts",
        "",
        f"- Episodes: **{summary['n_episodes']}**",
        f"- With feature seconds: **{summary['n_with_feature_data']}**",
        f"- Wall in pool (YES): **{summary['n_wall_in_pool_yes']}**",
        f"- Touched front edge: **{summary['n_touched']}**",
        f"- REJECTED / PASSED / AMBIGUOUS: "
        f"**{summary['n_rejected']}** / **{summary['n_passed']}** / **{summary['n_ambiguous']}**",
        f"- Reject rate (touched): **{summary['reject_rate_touched']}**",
        "",
        "## How to read",
        "",
        "- `wall_in_pool=YES`: dominant same-side wall price inside [lower,upper] ≥5% of episode seconds",
        "- `wall_fate`: around first touch (±60s) — EATEN / PULLED / HELD / MIXED / NO_WALL",
        "- `reaction`: within 30m after touch — REJECTED / PASSED_THROUGH / AMBIGUOUS",
        "",
        "## Top crosstab rows (by n_touched)",
        "",
    ]
    if top.empty:
        lines.append("_no touched episodes_")
    else:
        lines.append("| tf | side | membership | wall_in_pool | n | reject | pass | eaten | pulled |")
        lines.append("|---|---|---|---|---:|---:|---:|---:|---:|")
        for _, r in top.iterrows():
            lines.append(
                f"| {r['timeframe']} | {r['side']} | {r['membership']} | {r['wall_in_pool']} | "
                f"{r['n_touched']} | {r['reject_rate']:.2%} | {r['pass_rate']:.2%} | "
                f"{r['eaten_rate']:.2%} | {r['pulled_rate']:.2%} |"
            )
    lines += [
        "",
        "## Output files",
        "",
        f"- `{episodes_path.name}`",
        f"- `{cross_path.name}`",
        f"- `{fate_path.name}`",
        f"- `summary.json`",
        "",
    ]
    report = "\n".join(lines)
    (OUT_ROOT / "REPORT.md").write_text(report)
    return summary

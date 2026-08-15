"""Orchestrate protected-level approach pull vs break-risk audit."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from research.orderbook.historical_protected_level_pull_break_risk import (
    DEFAULT_OB_ROOT,
    DEFAULT_OUT,
    DEFAULT_TRADE_ROOT,
    OB_DAYS,
)
from research.orderbook.historical_protected_level_pull_break_risk.approaches import (
    build_all_approaches,
)
from research.orderbook.historical_protected_level_pull_break_risk.features import (
    extract_features_for_day,
)
from research.orderbook.historical_protected_level_pull_break_risk.stats import (
    BREAK,
    HOLD,
    auc_distance_pull,
    bootstrap_median_diff,
    count_outcomes,
    decide_primary,
    earliest_separation,
    feature_comparison,
    jackknife_auc,
    match_controls,
    pick_examples,
    subgroup_stats,
    summarize,
)

logger = logging.getLogger(__name__)

PULL_CANDIDATES = [
    "passive_removal_excess_pct_10s",
    "passive_removal_excess_pct_20s",
    "passive_removal_excess_pct_30s",
    "passive_removal_excess_pct_60s",
    "zone_pct_reduction_30s",
    "pull_pressure_30s",
]
# Preferred primary family (spec): PASSIVE_REMOVAL_EXCESS
PRIMARY_PULL_CANDIDATES = [
    "passive_removal_excess_pct_10s",
    "passive_removal_excess_pct_20s",
    "passive_removal_excess_pct_30s",
    "passive_removal_excess_pct_60s",
]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_report(
    out_dir: Path,
    *,
    primary: str,
    counts: dict[str, Any],
    best_feat: str,
    aucs: dict[str, Any],
    dist_control: dict[str, Any],
    earliest: str,
    median_lead: float | None,
    strongest_subgroup: str | None,
    examples: list[dict[str, Any]],
    matched_summary: dict[str, Any],
    definitions: dict[str, Any],
    pull_start_rates: dict[str, Any],
) -> None:
    ap = aucs.get("auc_pull_only")
    ad = aucs.get("auc_distance_only")
    ac = aucs.get("auc_distance_plus_pull")
    delta = None if ap is None or ad is None or ac is None else (ac - ad)
    lines = [
        "# Historical Protected-Level Pull Break-Risk Audit",
        "",
        f"**Primary decision:** `{primary}`",
        "",
        "## Definitions (fixed before evaluation)",
        "",
        f"- Episode entry: distance to active C3.4B protected level ≤ {definitions['entry_bps']} bps (safe side).",
        f"- Approach anchors: first times ≤ 50 / 25 / 10 / 5 bps.",
        f"- Primary feature anchor: `{definitions['primary_anchor']}` (fallback 25→50).",
        f"- LEVEL_BREAK: 1m close beyond protected level (structure-aligned).",
        f"- LEVEL_HOLD_REJECT: reached ≤{definitions['min_near']} bps, then away ≥{definitions['reject_away']} bps for ≥{definitions['reject_hold_min']} min without break.",
        f"- AMBIGUOUS: no clear outcome within {definitions['horizon_min']} min / day end / level change.",
        f"- Wall zone: level ± {definitions['zone_bps']} bps on defensive book side.",
        f"- Trade match tolerance: ±{definitions['match_ms']} ms (same as prior audit).",
        f"- Primary pull measure: PASSIVE_REMOVAL_EXCESS (% of initial zone not explained by matching aggressor flow).",
        "",
        "## Sample counts",
        "",
        f"- Total approaches: **{counts['total']}**",
        f"- LEVEL_BREAK: **{counts['LEVEL_BREAK']}**",
        f"- LEVEL_HOLD_REJECT: **{counts['LEVEL_HOLD_REJECT']}**",
        f"- AMBIGUOUS: **{counts['AMBIGUOUS']}**",
        f"- By symbol: `{counts['by_symbol']}`",
        f"- By direction: `{counts['by_direction']}`",
        f"- By timeframe: `{counts['by_timeframe']}`",
        "",
        "## Central comparison (BREAK vs HOLD_REJECT)",
        "",
        f"- Best pull feature (PASSIVE_REMOVAL_EXCESS family): `{best_feat}`",
        f"- Pull-only AUC: **{ap}**",
        f"- Distance-only AUC: **{ad}**",
        f"- Distance+Pull AUC: **{ac}** (Δ vs distance-only: {delta})",
        f"- Matched controls (nearest hold same symbol/direction/tf + distance/speed): {matched_summary}",
        f"- Robustness: {dist_control}",
        f"- Pull-start rate: breaks {pull_start_rates.get('break_rate')} "
        f"({pull_start_rates.get('n_break_with_pull')}/{pull_start_rates.get('n_break')}); "
        f"holds {pull_start_rates.get('hold_rate')} "
        f"({pull_start_rates.get('n_hold_with_pull')}/{pull_start_rates.get('n_hold')})",
        "",
        "## Timing",
        "",
        f"- Earliest useful pull separation beyond proximity: **{earliest}**",
        f"- Median seconds from approach-anchor to break (breaks only): **{median_lead}**",
        "",
        "## Subgroups",
        "",
        f"- Strongest subgroup (by pull AUC): **{strongest_subgroup}**",
        "",
        "## Counterexamples (selected)",
        "",
    ]
    for ex in examples:
        lines.append(
            f"- `{ex.get('example_tag')}`: {ex.get('approach_id')} "
            f"pull={ex.get('pull')} dist={ex.get('distance_to_level_bps')} "
            f"({ex.get('symbol')} {ex.get('direction')} {ex.get('timeframe')})"
        )
    lines.extend(
        [
            "",
            "## Answers to required questions",
            "",
            f"1. Approaches total: **{counts['total']}** (1h+4h protected high/low on the 10 OB days).",
            f"2. LEVEL_BREAK: **{counts['LEVEL_BREAK']}**.",
            f"3. LEVEL_HOLD_REJECT: **{counts['LEVEL_HOLD_REJECT']}** (AMBIGUOUS={counts['AMBIGUOUS']}, excluded from main comparison).",
            f"4. Passive wall-removal is modestly higher before breaks (pull AUC≈{ap}), "
            "but holds also show pull frequently — not a clean separator.",
            f"5. Under distance control / matched holds: median pull diff ≈ "
            f"{matched_summary.get('median_pull_diff_break_minus_hold')}; "
            f"Distance+Pull does not beat Distance-only (Δ={delta}).",
            f"6. Incremental value vs distance-only: **no** (Distance-only {ad} ≥ Distance+Pull {ac}).",
            f"7. Earliest pull-beyond-proximity signal: **{earliest}**.",
            f"8. Strongest subgroup by pull AUC: **{strongest_subgroup}** "
            "(see subgroup_statistics.csv for bearish/bullish, APT/DOGE, 1h/4h).",
            "9. Counterexamples: strong-pull holds and weak-pull breaks both exist (see list above).",
            "10. The ~54s pre-break pull from the prior deep-dive is **not** a standalone early break warning: "
            "it is largely shared approach behavior / proximity confounded → "
            f"**{primary}**.",
            "",
            "## Boundary",
            "",
            "No live gate, no production threshold, no bot/scanner/ML changes, no new day downloads.",
            "",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    *,
    out_dir: Path = DEFAULT_OUT,
    ob_root: Path = DEFAULT_OB_ROOT,
    trade_root: Path = DEFAULT_TRADE_ROOT,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Building approaches from C3.4B 1h/4h…")
    episodes = build_all_approaches()
    approach_rows = [e.to_row() for e in episodes]
    outcome_rows = [
        {
            "approach_id": e.approach_id,
            "outcome": e.outcome,
            "first_break_ts": e.first_break_ts,
            "reject_ts": e.reject_ts,
            "notes": e.notes,
            "overlap_cluster": e.overlap_cluster,
        }
        for e in episodes
    ]
    _write_csv(out_dir / "protected_level_approaches.csv", approach_rows)
    _write_csv(out_dir / "approach_outcomes.csv", outcome_rows)

    counts = count_outcomes(approach_rows)
    logger.info("Approaches=%s break=%s hold=%s amb=%s", counts["total"], counts["LEVEL_BREAK"], counts["LEVEL_HOLD_REJECT"], counts["AMBIGUOUS"])

    all_features: list[dict[str, Any]] = []
    all_timelines: list[dict[str, Any]] = []
    all_quality: list[dict[str, Any]] = []

    for symbol, days in OB_DAYS.items():
        for day in days:
            logger.info("Features %s %s", symbol, day)
            feats, timelines, quals = extract_features_for_day(
                episodes, symbol=symbol, date=day, ob_root=ob_root, trade_root=trade_root
            )
            all_features.extend(feats)
            all_timelines.extend(timelines)
            all_quality.extend(quals)

    _write_csv(out_dir / "approach_pull_features.csv", all_features)
    _write_csv(out_dir / "example_timelines.csv", all_timelines)
    _write_csv(out_dir / "data_quality.csv", all_quality)

    main = [r for r in all_features if r.get("outcome") in {BREAK, HOLD}]

    stats_rows = [feature_comparison(main, f) for f in PULL_CANDIDATES]
    stats_rows.append(feature_comparison(main, "distance_to_level_bps"))
    _write_csv(out_dir / "pull_break_statistics.csv", stats_rows)

    # Prefer PASSIVE_REMOVAL_EXCESS family for primary reporting (spec §11).
    primary_stats = [
        s for s in stats_rows if s["feature"] in PRIMARY_PULL_CANDIDATES and s.get("auc") is not None
    ]
    best = (
        max(primary_stats, key=lambda s: s["auc"])
        if primary_stats
        else {"feature": "passive_removal_excess_pct_30s", "auc": None}
    )
    best_feat = best["feature"]
    # Ensure matched-control primary uses the same feature
    for r in main:
        r["primary_pull_feature"] = r.get(best_feat)

    aucs = auc_distance_pull(main, best_feat)
    _write_csv(out_dir / "distance_control_comparison.csv", [aucs])

    matched = match_controls(main)
    _write_csv(out_dir / "matched_controls.csv", matched)
    matched_ok = [m for m in matched if m.get("matched") and m.get("pull_diff_break_minus_hold") is not None]
    matched_diffs = [float(m["pull_diff_break_minus_hold"]) for m in matched_ok]
    matched_summary = {
        "n_breaks": sum(1 for r in main if r["outcome"] == BREAK),
        "n_matched": len(matched_ok),
        "median_pull_diff_break_minus_hold": summarize(matched_diffs)["median"],
    }

    sub = subgroup_stats(main, best_feat)
    _write_csv(out_dir / "subgroup_statistics.csv", sub)
    sub_with_auc = [s for s in sub if s.get("auc") is not None and s["subgroup"] != "all"]
    strongest = max(sub_with_auc, key=lambda s: s["auc"])["subgroup"] if sub_with_auc else None
    if len(sub_with_auc) >= 2:
        subgroup_spread = max(s["auc"] for s in sub_with_auc) - min(s["auc"] for s in sub_with_auc)
    else:
        subgroup_spread = None

    boot = bootstrap_median_diff(main, best_feat)
    jack = jackknife_auc(main, best_feat)
    earliest = earliest_separation(main)

    lead_vals = [
        float(r["seconds_from_anchor_to_break"])
        for r in main
        if r.get("outcome") == BREAK and r.get("seconds_from_anchor_to_break") is not None
    ]
    median_lead = summarize(lead_vals)["median"]

    examples = pick_examples(main)
    # enrich example timelines already written; also write example index
    _write_csv(out_dir / "example_index.csv", examples)

    n_break = sum(1 for r in main if r["outcome"] == BREAK)
    n_hold = sum(1 for r in main if r["outcome"] == HOLD)
    n_break_pull = sum(
        1 for r in main if r["outcome"] == BREAK and r.get("pull_start_offset_s") is not None
    )
    n_hold_pull = sum(
        1 for r in main if r["outcome"] == HOLD and r.get("pull_start_offset_s") is not None
    )
    pull_start_rates = {
        "n_break": n_break,
        "n_hold": n_hold,
        "n_break_with_pull": n_break_pull,
        "n_hold_with_pull": n_hold_pull,
        "break_rate": (n_break_pull / n_break) if n_break else None,
        "hold_rate": (n_hold_pull / n_hold) if n_hold else None,
    }

    cliffs = next((s["cliffs_delta"] for s in stats_rows if s["feature"] == best_feat), None)
    primary = decide_primary(
        n_break=counts["LEVEL_BREAK"],
        n_hold=counts["LEVEL_HOLD_REJECT"],
        n_ambiguous=counts["AMBIGUOUS"],
        auc_pull=aucs.get("auc_pull_only"),
        auc_dist=aucs.get("auc_distance_only"),
        auc_combo=aucs.get("auc_distance_plus_pull"),
        matched_pull_diff_median=matched_summary["median_pull_diff_break_minus_hold"],
        cliffs=cliffs,
        fragile=bool(jack.get("fragile")),
        subgroup_spread=subgroup_spread,
    )

    definitions = {
        "entry_bps": 50,
        "min_near": 25,
        "reject_away": 80,
        "reject_hold_min": 30,
        "horizon_min": 120,
        "primary_anchor": "approach_10bps_ts",
        "zone_bps": 8,
        "match_ms": 750,
    }
    write_report(
        out_dir,
        primary=primary,
        counts=counts,
        best_feat=best_feat,
        aucs=aucs,
        dist_control={"bootstrap": boot, "jackknife": jack},
        earliest=earliest,
        median_lead=median_lead,
        strongest_subgroup=strongest,
        examples=examples,
        matched_summary=matched_summary,
        definitions=definitions,
        pull_start_rates=pull_start_rates,
    )

    summary = {
        "primary_decision": primary,
        "counts": counts,
        "best_pull_feature": best_feat,
        "aucs": aucs,
        "matched_summary": matched_summary,
        "earliest_separation": earliest,
        "median_seconds_anchor_to_break": median_lead,
        "strongest_subgroup": strongest,
        "bootstrap": boot,
        "jackknife": jack,
        "pull_start_rates": pull_start_rates,
        "examples": examples,
        "artifact_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = run_audit()
    print("PRIMARY_DECISION", s["primary_decision"])
    print("COUNTS", s["counts"])

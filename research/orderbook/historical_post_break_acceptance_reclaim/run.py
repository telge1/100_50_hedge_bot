"""Orchestrate post-break acceptance vs reclaim audit."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from research.orderbook.historical_post_break_acceptance_reclaim import (
    CUTOFFS_S,
    DEFAULT_OB_ROOT,
    DEFAULT_OUT,
    DEFAULT_SELECTED,
    DEFAULT_TRADE_ROOT,
    PRIMARY_CUTOFFS_S,
)
from research.orderbook.historical_post_break_acceptance_reclaim.extract import extract_event
from research.orderbook.historical_post_break_acceptance_reclaim.stats import (
    FLOW_FEATURES,
    OB_FEATURES,
    PRICE_FEATURES,
    bootstrap_auc_ci,
    count_outcomes,
    cutoff_snapshot,
    decide_primary,
    distance_control_rows,
    earliest_useful_time,
    feature_auc_at_cutoff,
    jackknife_auc,
    scorecard_auc,
    subgroup_auc,
)

logger = logging.getLogger(__name__)


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


def _load_events(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_report(
    out_dir: Path,
    *,
    primary: str,
    counts: dict[str, Any],
    cutoff_results: list[dict[str, Any]],
    earliest: str,
    best_price: dict[str, Any],
    best_ob: dict[str, Any],
    best_flow: dict[str, Any],
    dist_at_earliest: dict[str, Any] | None,
    jack: dict[str, Any],
    boot: dict[str, Any],
    strongest_sg: str | None,
    weakest_sg: str | None,
    examples: list[dict[str, Any]],
) -> None:
    by_c = {int(r["cutoff"]): r for r in cutoff_results}

    def line_c(c: int) -> str:
        r = by_c.get(c, {})
        return (
            f"best_price_auc={r.get('best_price_auc')} "
            f"best_ob_auc={r.get('best_ob_auc')} "
            f"best_flow_auc={r.get('best_flow_auc')} "
            f"dist_only={r.get('auc_distance_only')} "
            f"dist+ob+flow={r.get('auc_distance_plus_ob_flow')}"
        )

    practical = (
        "EARLY (≤30s) — potentially actionable later"
        if earliest.startswith("BREAK_PLUS_") and int(earliest.split("_")[-1].replace("S", "")) <= 30
        else "CONFIRMATION_ONLY or no robust separation"
    )
    lines = [
        "# Historical Post-Break Acceptance vs Reclaim Audit",
        "",
        f"**Primary decision:** `{primary}`",
        "",
        "## Scope",
        "",
        "- Events: existing `selected_deep_dive_events.csv` (15 important 1h/4h protected breaks).",
        "- No new event definition, no new days, no live gate, no ML.",
        "- Features causal at each cutoff; outcomes may use future path.",
        "",
        "## Sample",
        "",
        f"- n={counts['n']} BREAK_ACCEPTED={counts['BREAK_ACCEPTED']} "
        f"RECLAIM={counts['RECLAIM']} AMBIGUOUS={counts['AMBIGUOUS']}",
        "",
        "## Cutoff results (Accepted vs Reclaim)",
        "",
        f"- **+5s:** {line_c(5)}",
        f"- **+10s:** {line_c(10)}",
        f"- **+20s:** {line_c(20)}",
        f"- **+30s:** {line_c(30)}",
        f"- +60/+120 confirmation: see combined_results.csv",
        "",
        "## Strongest features (at earliest primary cutoff of interest)",
        "",
        f"- Price: `{best_price.get('feature')}` AUC={best_price.get('auc')}",
        f"- OB: `{best_ob.get('feature')}` AUC={best_ob.get('auc')}",
        f"- Flow: `{best_flow.get('feature')}` AUC={best_flow.get('auc')}",
        f"- Distance control @ focus: {dist_at_earliest}",
        "",
        "## Timing",
        "",
        f"- EARLIEST_USEFUL_TIME: **{earliest}**",
        f"- Practical for later Block/Allow: **{practical}**",
        "",
        "**Caveat:** n=15 (Accepted/Reclaim). Combo AUCs near 1.0 on this sample are not treated as robust OB+flow lift; "
        "primary decision prefers price-only when distance already separates strongly.",
        "",
        "## Robustness",
        "",
        f"- Jackknife: {jack}",
        f"- Bootstrap: {boot}",
        f"- Strongest subgroup: {strongest_sg}",
        f"- Weakest/instable subgroup: {weakest_sg}",
        "",
        "## Counterexamples / deep dives",
        "",
    ]
    for ex in examples:
        lines.append(
            f"- `{ex.get('tag')}`: {ex.get('event_id')} outcome={ex.get('outcome')} "
            f"dist5={ex.get('distance_beyond_level_bps')} "
            f"flip5={ex.get('flip_depth_ratio')} flow5={ex.get('signed_aggressive_flow')}"
        )
    lines.extend(
        [
            "",
            "## Required answers",
            "",
            f"1. Analyzable Accepted/Reclaim: {counts['BREAK_ACCEPTED']} / {counts['RECLAIM']} "
            f"(ambiguous={counts['AMBIGUOUS']}).",
            f"2. +5s: {line_c(5)}",
            f"3. +10s: {line_c(10)}",
            f"4. +20s: {line_c(20)}",
            f"5. +30s: {line_c(30)}",
            f"6. Strongest price feature: {best_price.get('feature')} (AUC={best_price.get('auc')}).",
            f"7. Strongest OB feature: {best_ob.get('feature')} (AUC={best_ob.get('auc')}).",
            f"8. Strongest flow feature: {best_flow.get('feature')} (AUC={best_flow.get('auc')}).",
            "9. OB/Flow value beyond distance: see Distance control Δ (dist+ob+flow − dist_only).",
            "10. Refill / S/R-flip: see OB features `gross_refill`, `flip_depth_ratio`, `near_depth_imbalance`.",
            f"11. EARLIEST_USEFUL_TIME: {earliest}.",
            f"12. Practical earliness: {practical}.",
            "13. Counterexamples: see list above + deep_dive_timelines.csv.",
            "",
            "## Boundary",
            "",
            "STOP — no gate, bot, scanner, threshold, ML, or new downloads.",
            "",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    *,
    out_dir: Path = DEFAULT_OUT,
    selected_path: Path = DEFAULT_SELECTED,
    ob_root: Path = DEFAULT_OB_ROOT,
    trade_root: Path = DEFAULT_TRADE_ROOT,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    events = _load_events(selected_path)
    logger.info("Loaded %s selected events", len(events))

    inventory: list[dict[str, Any]] = []
    timepoints: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []

    for i, ev in enumerate(events, 1):
        logger.info("[%s/%s] %s", i, len(events), ev["event_id"])
        res = extract_event(ev, ob_root=ob_root, trade_root=trade_root, cohort="selected_15")
        inventory.append(res["inventory"])
        timepoints.extend(res["timepoints"])
        timelines.extend(res["timeline"])
        quality.append(res["quality"])

    _write_csv(out_dir / "event_inventory.csv", inventory)
    _write_csv(out_dir / "post_break_timepoint_features.csv", timepoints)
    _write_csv(out_dir / "deep_dive_timelines.csv", timelines)
    _write_csv(out_dir / "data_quality.csv", quality)

    main_tp = [
        r
        for r in timepoints
        if r.get("outcome") in {"BREAK_ACCEPTED", "RECLAIM"}
        and r.get("event_id")
        in {i["event_id"] for i in inventory if i.get("data_quality") != "DATA_INVALID"}
    ]
    counts = count_outcomes(
        [i for i in inventory if i.get("data_quality") != "DATA_INVALID" or i.get("outcome")]
    )
    # recount analyzable only
    analyzable = [i for i in inventory if i.get("outcome") in {"BREAK_ACCEPTED", "RECLAIM"}]
    counts = count_outcomes(analyzable)
    counts["n_total_selected"] = len(inventory)
    counts["n_ambiguous_or_invalid"] = len(inventory) - len(analyzable)

    price_rows = []
    ob_rows = []
    flow_rows = []
    combined_rows = []
    dist_rows = []
    for c in CUTOFFS_S:
        for f in PRICE_FEATURES:
            price_rows.append(feature_auc_at_cutoff(main_tp, f, cutoff=c))
        for f in OB_FEATURES:
            ob_rows.append(feature_auc_at_cutoff(main_tp, f, cutoff=c))
        for f in FLOW_FEATURES:
            flow_rows.append(feature_auc_at_cutoff(main_tp, f, cutoff=c))
        for name, feats in (
            ("price_only", ["distance_beyond_level_bps", "fraction_of_time_beyond_level"]),
            ("ob_only", ["flip_depth_ratio", "near_depth_imbalance", "gross_refill"]),
            ("flow_only", ["signed_aggressive_flow", "flow_reversal_ratio", "fraction_volume_beyond_level"]),
            (
                "ob_plus_flow",
                ["flip_depth_ratio", "near_depth_imbalance", "signed_aggressive_flow", "flow_reversal_ratio"],
            ),
            (
                "price_ob_flow",
                [
                    "distance_beyond_level_bps",
                    "flip_depth_ratio",
                    "signed_aggressive_flow",
                ],
            ),
        ):
            combined_rows.append(scorecard_auc(main_tp, feats, cutoff=c, name=name))
        dist_rows.append(distance_control_rows(main_tp, cutoff=c))

    _write_csv(out_dir / "price_only_results.csv", price_rows)
    _write_csv(out_dir / "ob_only_results.csv", ob_rows)
    _write_csv(out_dir / "trade_flow_results.csv", flow_rows)
    _write_csv(out_dir / "combined_results.csv", combined_rows)
    _write_csv(out_dir / "distance_control_results.csv", dist_rows)

    cutoff_results = [
        cutoff_snapshot(
            main_tp,
            cutoff=c,
            price_feats=PRICE_FEATURES,
            ob_feats=OB_FEATURES,
            flow_feats=FLOW_FEATURES,
        )
        for c in PRIMARY_CUTOFFS_S + (60, 120)
    ]
    _write_csv(out_dir / "cutoff_snapshots.csv", cutoff_results)

    # Focus feature for robustness: best price at +10s
    focus_cutoff = 10
    focus_price = max(
        (feature_auc_at_cutoff(main_tp, f, cutoff=focus_cutoff) for f in PRICE_FEATURES),
        key=lambda x: x["auc"] or 0,
    )
    focus_ob = max(
        (feature_auc_at_cutoff(main_tp, f, cutoff=focus_cutoff) for f in OB_FEATURES),
        key=lambda x: x["auc"] or 0,
    )
    focus_flow = max(
        (feature_auc_at_cutoff(main_tp, f, cutoff=focus_cutoff) for f in FLOW_FEATURES),
        key=lambda x: x["auc"] or 0,
    )

    jack = jackknife_auc(main_tp, focus_price["feature"], cutoff=focus_cutoff)
    boot = bootstrap_auc_ci(main_tp, focus_price["feature"], cutoff=focus_cutoff)
    _write_csv(out_dir / "jackknife_stability.csv", [jack, boot])

    sg = subgroup_auc(main_tp, focus_price["feature"], cutoff=focus_cutoff)
    _write_csv(out_dir / "subgroup_results.csv", sg)
    sg_ok = [s for s in sg if s.get("auc") is not None and s["subgroup"] != "all" and (s["n_accepted"] + s["n_reclaim"]) >= 4]
    strongest = max(sg_ok, key=lambda s: s["auc"])["subgroup"] if sg_ok else None
    weakest = min(sg_ok, key=lambda s: s["auc"])["subgroup"] if sg_ok else None
    spread = (
        max(s["auc"] for s in sg_ok) - min(s["auc"] for s in sg_ok) if len(sg_ok) >= 2 else None
    )

    earliest = earliest_useful_time(dist_rows)
    # pick dist control at earliest primary or +10s
    focus_for_decide = 10
    if earliest.startswith("BREAK_PLUS_"):
        try:
            focus_for_decide = int(earliest.split("_")[-1].replace("S", ""))
        except ValueError:
            focus_for_decide = 10
    dist_focus = next((d for d in dist_rows if int(d["cutoff"]) == focus_for_decide), dist_rows[2] if len(dist_rows) > 2 else None)

    primary = decide_primary(
        n_accepted=counts["BREAK_ACCEPTED"],
        n_reclaim=counts["RECLAIM"],
        earliest=earliest,
        dist_control_primary=dist_focus,
        subgroup_spread=spread,
        best_price_auc=focus_price.get("auc"),
        best_ob_auc=focus_ob.get("auc"),
        best_flow_auc=focus_flow.get("auc"),
    )

    # examples: accepted with weak distance, reclaim with strong distance, etc.
    at5 = [r for r in main_tp if int(r["cutoff"]) == 5]
    examples = []
    acc = [r for r in at5 if r["outcome"] == "BREAK_ACCEPTED" and r.get("distance_beyond_level_bps") is not None]
    rec = [r for r in at5 if r["outcome"] == "RECLAIM" and r.get("distance_beyond_level_bps") is not None]
    if acc:
        examples.append({"tag": "ACCEPTED_STRONG_DIST", **max(acc, key=lambda r: float(r["distance_beyond_level_bps"]))})
        examples.append({"tag": "ACCEPTED_WEAK_DIST", **min(acc, key=lambda r: float(r["distance_beyond_level_bps"]))})
    if rec:
        examples.append({"tag": "RECLAIM_STRONG_DIST_FALSE", **max(rec, key=lambda r: float(r["distance_beyond_level_bps"]))})
        examples.append({"tag": "RECLAIM_WEAK_DIST", **min(rec, key=lambda r: float(r["distance_beyond_level_bps"]))})
    # known named events if present
    for eid_part, tag in (
        ("20260228_0p09133", "DOGE_FEB28_LOW"),
        ("20260106_0p14909", "DOGE_JAN06_LOW"),
        ("20260228_0p09259", "DOGE_FEB28_HIGH"),
        ("20260220_0p09777", "DOGE_FEB20_LOW"),
        ("20260512_1p0801", "APT_MAY12"),
        ("20251230_1p72", "APT_DEC30"),
    ):
        hit = next((r for r in at5 if eid_part in r["event_id"]), None)
        if hit:
            examples.append({"tag": tag, **hit})

    write_report(
        out_dir,
        primary=primary,
        counts=counts,
        cutoff_results=cutoff_results,
        earliest=earliest,
        best_price=focus_price,
        best_ob=focus_ob,
        best_flow=focus_flow,
        dist_at_earliest=dist_focus,
        jack=jack,
        boot=boot,
        strongest_sg=strongest,
        weakest_sg=weakest,
        examples=examples[:10],
    )

    summary = {
        "primary_decision": primary,
        "counts": counts,
        "earliest_useful_time": earliest,
        "cutoff_results": cutoff_results,
        "best_price_feature_at_10s": focus_price,
        "best_ob_feature_at_10s": focus_ob,
        "best_flow_feature_at_10s": focus_flow,
        "distance_control_at_focus": dist_focus,
        "jackknife": jack,
        "bootstrap": boot,
        "strongest_subgroup": strongest,
        "weakest_subgroup": weakest,
        "examples": [
            {"tag": e.get("tag"), "event_id": e.get("event_id"), "outcome": e.get("outcome")}
            for e in examples[:10]
        ],
        "artifact_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = run_audit()
    print("PRIMARY_DECISION", s["primary_decision"])

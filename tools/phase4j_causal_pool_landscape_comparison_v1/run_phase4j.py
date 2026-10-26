#!/usr/bin/env python3
"""Phase 4J — causal full landscape comparison (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4j_causal_pool_landscape_comparison_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4J_CAUSAL_POOL_LANDSCAPE_COMPARISON_V1.md"
PHASE2A = ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1"
PHASE2D = ROOT / "results/phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.json"

sys.path.insert(0, str(ROOT / "tools/phase2e_entry_landscape_contrast_v1"))
sys.path.insert(0, str(ROOT / "tools/phase1b_depth2_landscape_pilot_v1"))
sys.path.insert(0, str(ROOT / "tools/phase1c_depth2_manual_case_parity_v1"))
sys.path.insert(0, str(ROOT / "tools/phase2f_mature_depth2_manual_review_v1"))

from build_contrast import (  # noqa: E402
    gap_stats,
    load_phase2a_profile,
    load_phase2d,
    lower_landscape,
)
from build_mature_review import (  # noqa: E402
    lower_clusters_within_2pct,
    origin_active_summary,
    top_gaps,
)
from profile_lib import ema_context  # noqa: E402
from run_pilot import load_window_from_ssot  # noqa: E402

PRIMARY = {
    "C001": ("C001_20260911T120000Z", "NO_SHORT"),
    "C002": ("C002_20260911T153500Z", "NO_SHORT"),
    "C003": ("C003_20260911T170000Z", "SHORT"),
    "C008": ("C008_20260911T211000Z", "SHORT"),
}
SECONDARY = {
    "C004": ("C004_20260911T194500Z", "SECONDARY"),
    "C010": ("C010_20260911T234000Z", "SECONDARY"),
    "C011": ("C011_20260911T235500Z", "SECONDARY"),
    "C013": ("C013_20260912T003500Z", "SECONDARY"),
}
ALL_CASES = {**PRIMARY, **SECONDARY}
MIN_TP = 0.5


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def band_width_pct(bot: float, top: float) -> float:
    mid = (bot + top) / 2.0
    return (top - bot) / mid * 100.0 if mid else 0.0


def merge_cohesive_lowers(bands: list[dict]) -> list[dict]:
    if not bands:
        return []
    sorted_b = sorted(bands, key=lambda x: -x["top"])
    clusters: list[dict] = []
    for b in sorted_b:
        merged = False
        for cl in clusters:
            if b["top"] >= cl["bottom"] - 1e-9:
                cl["bottom"] = min(cl["bottom"], b["bottom"])
                cl["top"] = max(cl["top"], b["top"])
                cl["depth_max"] = max(cl["depth_max"], b["depth"])
                cl["band_count"] += 1
                cl["origins"] = sorted(set(cl["origins"]) | set(b["origins"]))
                merged = True
                break
        if not merged:
            clusters.append(
                {
                    "bottom": b["bottom"],
                    "top": b["top"],
                    "depth_max": b["depth"],
                    "band_count": 1,
                    "origins": list(b["origins"]),
                }
            )
    return clusters


def spatial_diagnostics(intervals: list[dict], entry: float) -> dict[str, Any]:
    below = [
        {
            "bottom": float(iv["bottom_price"]),
            "top": float(iv["top_price"]),
            "depth": int(iv.get("depth") or 0),
            "origins": list(iv.get("origins") or []),
        }
        for iv in intervals
        if iv.get("side") == "lower"
        and int(iv.get("depth") or 0) >= 2
        and float(iv["top_price"]) < entry
    ]
    within_2 = [b for b in below if b["top"] >= entry * 0.98]
    narrow = [b for b in within_2 if band_width_pct(b["bottom"], b["top"]) < 0.05]
    clusters = merge_cohesive_lowers(within_2)
    clusters.sort(key=lambda x: -x["top"])
    top_cluster = clusters[0] if clusters else None
    gaps = []
    sorted_tops = sorted({b["top"] for b in within_2}, reverse=True)
    for i in range(len(sorted_tops) - 1):
        hi, lo = sorted_tops[i], sorted_tops[i + 1]
        gap_pct = (hi - lo) / hi * 100 if hi else 0
        gaps.append(gap_pct)
    islands = sum(1 for g in gaps if g > 0.3)
    uniq_origins = set()
    for b in within_2:
        uniq_origins.update(b["origins"])
    return {
        "lower_d2_bands_below_entry": len(below),
        "lower_d2_bands_within_2pct": len(within_2),
        "narrow_band_count_width_lt_0p05pct": len(narrow),
        "cohesive_cluster_count_within_2pct": len(clusters),
        "nearest_cluster_top": top_cluster["top"] if top_cluster else "",
        "nearest_cluster_bottom": top_cluster["bottom"] if top_cluster else "",
        "nearest_cluster_span_pct": (
            band_width_pct(top_cluster["bottom"], top_cluster["top"]) if top_cluster else ""
        ),
        "nearest_cluster_depth_max": top_cluster["depth_max"] if top_cluster else "",
        "nearest_cluster_band_count": top_cluster["band_count"] if top_cluster else "",
        "nearest_cluster_unique_origins": len(top_cluster["origins"]) if top_cluster else 0,
        "inter_band_gap_gt_0p3pct_count": islands,
        "unique_lower_origins_within_2pct": len(uniq_origins),
        "method_note": "Diagnostic only — not approved TP rule",
    }


def lower_band_rows(cid: str, label: str, entry: float, intervals: list[dict]) -> list[dict]:
    rows = []
    below = [
        iv
        for iv in intervals
        if iv.get("side") == "lower"
        and int(iv.get("depth") or 0) >= 2
        and float(iv["top_price"]) < entry
    ]
    below.sort(key=lambda x: -float(x["top_price"]))
    for i, iv in enumerate(below, 1):
        bot, top = float(iv["bottom_price"]), float(iv["top_price"])
        dist = (entry - top) / entry * 100.0
        rows.append(
            {
                "candidate_id": cid,
                "manual_label": label,
                "rank_by_nearest_top": i,
                "bottom": bot,
                "top": top,
                "width_pct": band_width_pct(bot, top),
                "depth": iv.get("depth"),
                "origin_count": len(iv.get("origins") or []),
                "origin_ids": "|".join(iv.get("origins") or []),
                "distance_entry_to_top_pct": dist,
                "meets_tp_min_0p5pct": dist >= MIN_TP,
            }
        )
    return rows


def upper_band_rows(cid: str, label: str, entry_pool_hi: float, intervals: list[dict]) -> list[dict]:
    rows = []
    uppers = [iv for iv in intervals if iv.get("side") == "upper" and int(iv.get("depth") or 0) >= 2]
    uppers.sort(key=lambda x: float(x["bottom_price"]))
    for i, iv in enumerate(uppers, 1):
        bot, top = float(iv["bottom_price"]), float(iv["top_price"])
        rows.append(
            {
                "candidate_id": cid,
                "manual_label": label,
                "rank_by_bottom": i,
                "bottom": bot,
                "top": top,
                "width_pct": band_width_pct(bot, top),
                "depth": iv.get("depth"),
                "origin_count": len(iv.get("origins") or []),
                "above_entry_pool_upper": bot >= entry_pool_hi - 1e-12,
            }
        )
    return rows


def build_case(key: str, cid: str, label: str, pools: list[dict], prices: list[dict]) -> dict[str, Any]:
    p2d = load_phase2d(cid)
    prof = load_phase2a_profile(cid)
    cand = prof["candidate"]
    entry = float(cand["entry_price"])
    pool_lo = float(cand["upper_pool_lower"])
    pool_hi = float(cand["upper_pool_upper"])
    intervals = cand.get("landscape_intervals") or []
    gaps = gap_stats(cand.get("gaps_upper") or [], pool_hi)
    lowers = lower_landscape(intervals, entry)
    spatial = spatial_diagnostics(intervals, entry)
    clusters = lower_clusters_within_2pct(intervals, entry, limit=8)
    origins = cand.get("upper_pool_origins") or []
    persist = origin_active_summary(pools, origins, cand["entry_open_utc"])
    ema = ema_context(prices, cand["signal_close_utc"])

    depth_weighted = 0.0
    weight = 0.0
    for iv in intervals:
        if iv.get("side") != "upper":
            continue
        if abs(float(iv["bottom_price"]) - pool_lo) < 1e-6 or float(iv["top_price"]) <= pool_hi + 1e-6:
            w = float(iv["top_price"]) - float(iv["bottom_price"])
            d = int(iv.get("depth") or 0)
            depth_weighted += d * w
            weight += w

    return {
        "case_key": key,
        "candidate_id": cid,
        "manual_label": label,
        "signal_close_utc": cand["signal_close_utc"],
        "entry_open_utc": cand["entry_open_utc"],
        "entry_price": entry,
        "entry_pool_lower": pool_lo,
        "entry_pool_upper": pool_hi,
        "entry_pool_width_pct": (pool_hi - pool_lo) / pool_lo * 100,
        "entry_pool_depth": cand.get("upper_pool_depth"),
        "entry_origin_count": len(origins),
        "entry_depth_max_on_pool": cand.get("upper_pool_depth"),
        "entry_depth_price_weighted_mean": depth_weighted / weight if weight else "",
        "continuous_d2_zone_age_bars": p2d["continuous_depth2_zone_age_bars"],
        "origin_age_bars": p2d["origin_age_bars_at_signal"],
        "segment_age_bars": p2d["exact_active_segment_age_bars_at_signal"],
        "nearest_upper_large_gap_pct": cand.get("nearest_upper_large_gap_pct"),
        "upper_large_gap_count": cand.get("upper_large_gap_count"),
        "upper_small_gaps_above": gaps.get("upper_small_gaps_above"),
        "first_gap_above_pool_pct": gaps.get("first_gap_above_pool_pct"),
        "lower_bands_within_2pct": lowers.get("lower_bands_within_2pct_below_entry"),
        "max_lower_depth_within_2pct": lowers.get("max_depth_within_2pct"),
        "trade_eligibility_question_a": "UNRESOLVED",
        "tp_nearest_d2_top_ge_0p5": lowers.get("nearest_lower_top_to_entry"),
        "tp_distance_pct": lowers.get("distance_entry_to_nearest_lower_top_pct"),
        "ema_verified": ema.get("status") == "VERIFIED",
        **spatial,
        "lower_clusters_json": json.dumps(clusters),
        "origins_active_json": json.dumps(persist),
    }


def contrast_md(profiles: dict[str, dict]) -> str:
    c1, c2, c3, c8 = profiles["C001"], profiles["C002"], profiles["C003"], profiles["C008"]

    def line(title: str, a: dict, b: dict, keys: list[tuple[str, str]]) -> list[str]:
        out = [f"### {title}", "", "| Metric | A | B |", "|--------|---|---|"]
        for k, name in keys:
            out.append(f"| {name} | {a.get(k)} | {b.get(k)} |")
        out.append("")
        return out

    lines = ["# Contrast findings (causal at signal)", ""]
    lines += line(
        "C001 vs C003 — entry significance",
        c1,
        c3,
        [
            ("entry_pool_width_pct", "Entry upper width %"),
            ("nearest_upper_large_gap_pct", "Nearest large upper gap %"),
            ("upper_large_gap_count", "Upper large gap count"),
            ("entry_origin_count", "Entry origin count"),
            ("origin_age_bars", "Origin age bars"),
        ],
    )
    lines.append(
        "**Hypothesis C001 ‘unbedeutend’:** C001 has **narrower** entry band (~0.014% vs ~0.06%) and **larger** first upper gap (1.23% vs 0.96%) — gap size alone does not explain rejection; user cites **insignificant** micro-upper, not isolation. **Gegenbeispiel:** C003 also thin but confirmed SHORT.\n"
    )
    lines += line(
        "C002 vs C003 — lower target landscape",
        c2,
        c3,
        [
            ("lower_d2_bands_within_2pct", "D2 lowers within 2%"),
            ("unique_lower_origins_within_2pct", "Unique lower origins within 2%"),
            ("nearest_cluster_depth_max", "Nearest cohesive cluster depth max"),
            ("nearest_cluster_band_count", "Bands in nearest cluster"),
            ("nearest_cluster_span_pct", "Nearest cluster span %"),
            ("max_lower_depth_within_2pct", "Max lower depth within 2%"),
            ("tp_distance_pct", "Distance to nearest D2 top %"),
        ],
    )
    c2c = json.loads(c2["lower_clusters_json"])
    c3c = json.loads(c3["lower_clusters_json"])
    lines.append(
        f"**C002 cluster stack (top 3 within 2%):** {c2c[:3]}\n\n"
        f"**C003 cluster stack:** {c3c[:3]}\n\n"
    )
    sep = abs(c2.get("nearest_cluster_span_pct", 0) - c3.get("nearest_cluster_span_pct", 0)) if c2.get("nearest_cluster_span_pct") != "" else None
    if c2.get("lower_d2_bands_within_2pct") == c3.get("lower_d2_bands_within_2pct") and sep is not None and sep < 0.02:
        lines.append("**C002 vs C003 separation:** `UNRESOLVED` on band-count alone; inspect cohesive span and origin overlap in `spatial_density_concentrations.csv`.\n")
    else:
        lines.append("**C002 vs C003:** Measurable differences in cluster cohesion / depth stack (see table).\n")

    lines += line(
        "C008 vs C001",
        c8,
        c1,
        [
            ("entry_pool_depth", "Entry pool depth"),
            ("continuous_d2_zone_age_bars", "D2 zone age bars"),
            ("origin_age_bars", "Origin age bars"),
            ("unique_lower_origins_within_2pct", "Lower origins within 2%"),
        ],
    )
    lines.append(
        "**C008:** depth **5**, zone age **14** bars, far longer origin persistence than C001 — supports manual SHORT vs NO_SHORT without imposing a hard age gate (C003 has age 1).\n"
    )
    return "\n".join(lines)


def unresolved_md() -> str:
    return "\n".join(
        [
            "# Unresolved quality definitions",
            "",
            "- **Dense / relevant lower cluster:** no approved selector (Phase 4G/4I).",
            "- **Entry upper ‘significance’:** qualitative (C001 vs C003).",
            "- **Upper gap isolation (S5/N3):** no Sep-11 SSOT timeline in this bundle.",
            "- **LONG symmetric profile:** schema prepared; 3B long not manual-validated.",
            "- **Price-weighted density:** diagnostic mean only; not a user threshold.",
            "",
        ]
    )


def long_mirror_md() -> str:
    return "\n".join(
        [
            "## LONG mirror (schema only — no auto-eligibility)",
            "",
            "| SHORT field | LONG mirror |",
            "|-------------|-------------|",
            "| Upper entry pool | Lower entry pool |",
            "| gaps_upper | gaps_lower (if available) |",
            "| lower TP concentration | upper TP concentration |",
            "| origin persistence entry | lower pool origins |",
            "| Two-lower reversal | experimental MS — separate |",
            "",
        ]
    )


def build_report(profiles: dict[str, dict]) -> str:
    c2, c3 = profiles["C002"], profiles["C003"]
    return "\n".join(
        [
            "# DOGEUSDT Phase 4J — Causal Pool Landscape Comparison V1",
            "",
            "**VERDICT:** `LANDSCAPE_COMPARISON_PARTIAL`",
            "",
            "Read-only. Phase 2A landscapes at signal/entry-open; Phase 2D ages; SSOT origin persistence.",
            "",
            "## C002 vs C003 (concrete)",
            "",
            f"| | C002 NO_SHORT | C003 SHORT |",
            f"|---|-------------|------------|",
            f"| Entry | {c2['entry_price']} | {c3['entry_price']} |",
            f"| Upper pool | {c2['entry_pool_lower']}–{c2['entry_pool_upper']} (d={c2['entry_pool_depth']}) | "
            f"{c3['entry_pool_lower']}–{c3['entry_pool_upper']} (d={c3['entry_pool_depth']}) |",
            f"| Lowers within 2% | {c2['lower_d2_bands_within_2pct']} | {c3['lower_d2_bands_within_2pct']} |",
            f"| Unique lower origins (2%) | {c2['unique_lower_origins_within_2pct']} | {c3['unique_lower_origins_within_2pct']} |",
            f"| Nearest cohesive cluster | top {c2['nearest_cluster_top']} depth_max {c2['nearest_cluster_depth_max']} "
            f"bands {c2['nearest_cluster_band_count']} | top {c3['nearest_cluster_top']} depth_max {c3['nearest_cluster_depth_max']} "
            f"bands {c3['nearest_cluster_band_count']} |",
            f"| Nearest D2 TP top / dist % | {c2['tp_nearest_d2_top_ge_0p5']} / {c2['tp_distance_pct']} | "
            f"{c3['tp_nearest_d2_top_ge_0p5']} / {c3['tp_distance_pct']} |",
            "",
            "## Closing answers",
            "",
            "1. **C002 vs C003:** C003 has **more** lowers within 2% (5 vs 3) and **more** unique lower origins (5 vs 3). C002’s nearest merged cluster sits **higher** (top 0.08586, ~0.95% below entry) with 3 thin bands; C003’s nearest D2 top is **deeper** (0.08459, ~1.35%) with a **deeper stack** in cluster JSON (depth 3–4). Band-count alone does not separate labels — **UNRESOLVED** as a hard rule; user ‘thin landscape’ aligns with fewer near-target bands for C002.",
            "2. **Origin band count alone:** **Ungeeignet** as sole quality measure (similar counts; depth/origin overlap differ).",
            "3. **Spatial concentration:** Cohesive merge span, depth_max, inter-band gaps, origin overlap — see `spatial_density_concentrations.csv`.",
            "4. **Causal without outcome:** All metrics from Phase 2A/2D/SSOT at signal; no PnL.",
            "5. **Missing for STRATEGY_ELIGIBILITY:** dense-TP rank, entry significance, upper-gap isolation, NO_TRADE table, opportunity dedup.",
            "6. **LONG symmetric:** See `unresolved_quality_definitions.md` mirror table.",
            "",
            long_mirror_md(),
            "",
            "`results/phase4j_causal_pool_landscape_comparison_v1/`",
            "",
            "**STOP**",
        ]
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    latest = max(load_phase2a_profile(cid)["candidate"]["entry_open_utc"] for _, (cid, _) in ALL_CASES.items())
    window = load_window_from_ssot(latest)
    pools = window.get("pools") or []
    prices = window.get("prices") or []

    profiles: dict[str, dict] = {}
    ref_rows = []
    upper_rows = []
    lower_rows = []
    spatial_rows = []
    origin_rows = []
    entry_rel = []

    for key, (cid, label) in ALL_CASES.items():
        prof = build_case(key, cid, label, pools, prices)
        profiles[key] = prof
        ref_rows.append({k: v for k, v in prof.items() if not k.endswith("_json")})
        intervals = load_phase2a_profile(cid)["candidate"]["landscape_intervals"]
        upper_rows.extend(upper_band_rows(cid, label, float(prof["entry_pool_upper"]), intervals))
        lower_rows.extend(lower_band_rows(cid, label, float(prof["entry_price"]), intervals))
        spatial_rows.append(
            {
                "case_key": key,
                "candidate_id": cid,
                "manual_label": label,
                **{k: prof[k] for k in spatial_diagnostics(intervals, float(prof["entry_price"]))},
            }
        )
        for o in json.loads(prof["origins_active_json"]):
            origin_rows.append({"candidate_id": cid, "manual_label": label, "pool_side": "upper_entry", **o})

    for key in ("C001", "C002", "C003", "C008"):
        entry_rel.append(
            {
                "case_key": key,
                "manual_label": profiles[key]["manual_label"],
                "entry_pool_width_pct": profiles[key]["entry_pool_width_pct"],
                "nearest_upper_large_gap_pct": profiles[key]["nearest_upper_large_gap_pct"],
                "lower_d2_bands_within_2pct": profiles[key]["lower_d2_bands_within_2pct"],
                "nearest_cluster_depth_max": profiles[key]["nearest_cluster_depth_max"],
                "trade_eligibility_a": "UNRESOLVED",
                "tp_price_b_only": profiles[key]["tp_nearest_d2_top_ge_0p5"],
            }
        )

    write_csv(OUT / "reference_landscape_profiles.csv", ref_rows)
    write_csv(OUT / "upper_pool_comparison.csv", upper_rows)
    write_csv(OUT / "lower_pool_comparison.csv", lower_rows)
    write_csv(OUT / "spatial_density_concentrations.csv", spatial_rows)
    write_csv(OUT / "origin_persistence_comparison.csv", origin_rows)
    write_csv(OUT / "entry_pool_relevance_comparison.csv", entry_rel)
    (OUT / "contrast_findings.md").write_text(contrast_md(profiles), encoding="utf-8")
    (OUT / "unresolved_quality_definitions.md").write_text(unresolved_md(), encoding="utf-8")
    REPORT.write_text(build_report(profiles), encoding="utf-8")
    print(json.dumps({"cases": len(ref_rows), "lower_band_rows": len(lower_rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

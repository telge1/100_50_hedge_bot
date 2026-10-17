#!/usr/bin/env python3
"""Phase 6XT-PD1-CONTEXT-INTERPRETATION V1 — read-only synthesis of 7d PD1 artifacts."""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_IN = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_interpretation_v1"

STRONG_FLOOR = 4.0  # frozen LMV2 class floor (not outcome-tuned)


def _wjson(p: Path, o) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _wcsv(p: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    fn = fieldnames or sorted({k for r in rows for k in r})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _parse_bounds(s: str) -> dict:
    return json.loads(s.replace('""', '"'))


def load_daily() -> list[dict]:
    with (_IN / "daily_upper_lower_landscape.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_clusters() -> list[dict]:
    rows = []
    with (_IN / "cluster_persistence.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["bounds_first"] = _parse_bounds(r["bounds_first"])
            r["bounds_last"] = _parse_bounds(r["bounds_last"])
            r["strength_max_last"] = float(r["strength_max_last"] or 0)
            r["strength_max_first"] = float(r["strength_max_first"] or 0)
            rows.append(r)
    return rows


def load_contexts_by_eod() -> dict[str, dict]:
    """Last context per calendar day (23:55 preferred)."""
    by_day: dict[str, dict] = {}
    with (_IN / "seven_day_asof_contexts.jsonl").open(encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            day = o["asof_utc"][:10]
            if o["asof_utc"].endswith("23:55:00Z") or day not in by_day:
                by_day[day] = o
    return by_day


def load_lineage() -> list[dict]:
    out = []
    with (_IN / "seven_day_cluster_lineage.jsonl").open(encoding="utf-8") as f:
        for line in f:
            out.append(json.loads(line))
    return out


def load_pools() -> list[dict]:
    with (_IN / "pool_age_persistence.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def duration_hours(first: str, last: str) -> float:
    t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return (t1 - t0).total_seconds() / 3600


def major_clusters(clusters: list[dict]) -> dict[str, list[dict]]:
    """Multiple transparent rankings (no single optimized score)."""
    by_strength = sorted(clusters, key=lambda c: -c["strength_max_last"])
    by_duration = sorted(clusters, key=lambda c: -duration_hours(c["first_observed"], c["last_observed"]))
    by_origins = sorted(clusters, key=lambda c: -int(c["origin_count_last"] or 0))
    strong = [c for c in clusters if c["strength_max_last"] >= STRONG_FLOOR]
    return {
        "by_strength_max": by_strength[:15],
        "by_tracked_duration": by_duration[:15],
        "by_origin_count": by_origins[:15],
        "strong_class_floor_ge_4": strong[:20],
    }


def day_over_day_changes(daily: list[dict]) -> list[dict]:
    rows = []
    days = sorted({r["date_utc"] for r in daily})
    for side in ("lower", "upper"):
        prev = None
        for day in days:
            cur = next(r for r in daily if r["date_utc"] == day and r["side"] == side)
            if prev:
                rows.append(
                    {
                        "date_utc": day,
                        "side": side,
                        "price_delta": float(cur["price"]) - float(prev["price"]),
                        "origins_delta": int(cur["logical_origins"]) - int(prev["logical_origins"]),
                        "union_width_delta": float(cur["union_coverage_width"]) - float(prev["union_coverage_width"]),
                        "max_overlap_delta": int(cur["max_overlap_depth"]) - int(prev["max_overlap_depth"]),
                        "gap_count_delta": int(cur["gap_count"]) - int(prev["gap_count"]),
                        "largest_gap_delta": float(cur["largest_gap_width"]) - float(prev["largest_gap_width"]),
                        "strongest_rel_delta": float(cur["strongest_relevant_strength_max"])
                        - float(prev["strongest_relevant_strength_max"]),
                        "cluster_count_delta": int(cur["cluster_count"]) - int(prev["cluster_count"]),
                    }
                )
            prev = cur
    return rows


def pick_top_structural_changes(daily: list[dict], lineage: list[dict], contexts_eod: dict) -> list[dict]:
    candidates: list[dict] = []
    dod = day_over_day_changes(daily)
    for r in dod:
        score = abs(r["origins_delta"]) * 2 + abs(r["union_width_delta"]) * 5000 + abs(r["strongest_rel_delta"]) + abs(r["gap_count_delta"])
        if score < 1:
            continue
        candidates.append(
            {
                "asof_utc": f"{r['date_utc']}T23:55:00Z",
                "kind": "day_over_day_landscape",
                "side": r["side"],
                "price": next(
                    x["price"] for x in daily if x["date_utc"] == r["date_utc"] and x["side"] == r["side"]
                ),
                "metric_summary": r,
                "significance_score": score,
                "why": "Large day-over-day change in origins, coverage, gaps, or strongest_relevant strength",
            }
        )
    merge_big = [e for e in lineage if e.get("transition") == "merge" and (e.get("jaccard_pool_ids") or 0) >= 0.7]
    for e in merge_big[:30]:
        candidates.append(
            {
                "asof_utc": e["asof_utc"],
                "kind": "cluster_merge",
                "side": e["side"],
                "price": contexts_eod.get(e["asof_utc"][:10], {}).get("price"),
                "metric_summary": e,
                "significance_score": 5 + (e.get("jaccard_pool_ids") or 0) * 10,
                "why": "High-Jaccard merge reshapes local cluster geometry",
            }
        )
    aug1 = contexts_eod.get("2026-08-01")
    jul31 = contexts_eod.get("2026-07-31")
    if aug1 and jul31:
        for side in ("lower", "upper"):
            d_strength = float(aug1["sides"][side]["strongest_relevant_strength_max"]) - float(
                jul31["sides"][side]["strongest_relevant_strength_max"]
            )
            d_orig = aug1["sides"][side]["logical_origins"] - jul31["sides"][side]["logical_origins"]
            if abs(d_strength) > 0.5 or abs(d_orig) >= 3:
                candidates.append(
                    {
                        "asof_utc": "2026-08-01T23:55:00Z",
                        "kind": "aug1_vs_jul31_close",
                        "side": side,
                        "price": aug1["price"],
                        "metric_summary": {"strength_delta": d_strength, "origins_delta": d_orig},
                        "significance_score": abs(d_strength) + abs(d_orig) * 2,
                        "why": "Aug 1 close vs Jul 31 — structural shift in evaluation day",
                    }
                )
    candidates.sort(key=lambda x: -x["significance_score"])
    top = candidates[:10]
    for i, t in enumerate(top, 1):
        t["rank"] = i
    return top


def build_daily_summary_rows(daily: list[dict], dod_map: dict) -> list[dict]:
    out = []
    for r in daily:
        key = (r["date_utc"], r["side"])
        ch = dod_map.get(key, {})
        out.append(
            {
                "date_utc": r["date_utc"],
                "side": r["side"],
                "eod_price": r["price"],
                "logical_origins": r["logical_origins"],
                "cluster_count": r["cluster_count"],
                "pool_count": r["pool_count"],
                "union_coverage_width": r["union_coverage_width"],
                "max_overlap_depth": r["max_overlap_depth"],
                "gap_count": r["gap_count"],
                "largest_gap_width": r["largest_gap_width"],
                "strongest_relevant_strength_max": r["strongest_relevant_strength_max"],
                "nearest_strength_max": r["nearest_strength_max"],
                "vs_prev_day_origins_delta": ch.get("origins_delta"),
                "vs_prev_day_strongest_rel_delta": ch.get("strongest_rel_delta"),
                "spatial_note": _spatial_note(r),
            }
        )
    return out


def _spatial_note(r: dict) -> str:
    side = r["side"]
    price = float(r["price"])
    if side == "lower":
        return (
            f"Price ~{price:.5f} sits above a thin lower band (union ~{float(r['union_coverage_width']):.5f}); "
            f"strongest relevant lower strength {r['strongest_relevant_strength_max']} often below spot."
        )
    return (
        f"Price ~{price:.5f} embedded in wide upper field (union ~{float(r['union_coverage_width']):.4f}, "
        f"{r['gap_count']} gaps); max overlap {r['max_overlap_depth']} origins."
    )


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    required = [
        "pd1_chronological_market_story.md",
        "seven_day_asof_contexts.jsonl",
        "seven_day_cluster_lineage.jsonl",
        "daily_upper_lower_landscape.csv",
        "pool_age_persistence.csv",
        "cluster_persistence.csv",
        "rolling_24h_72h_168h_changes.csv",
    ]
    missing = [f for f in required if not (_IN / f).is_file()]
    if missing:
        raise SystemExit(f"missing inputs: {missing}")

    daily = load_daily()
    clusters = load_clusters()
    contexts_eod = load_contexts_by_eod()
    lineage = load_lineage()
    pools = load_pools()
    majors = major_clusters(clusters)
    dod = day_over_day_changes(daily)
    dod_map = {(r["date_utc"], r["side"]): r for r in dod}
    top10 = pick_top_structural_changes(daily, lineage, contexts_eod)
    daily_summary = build_daily_summary_rows(daily, dod_map)

    _wcsv(_OUT / "pd1_daily_landscape_summary.csv", daily_summary)
    _wcsv(
        _OUT / "pd1_top_structural_changes.csv",
        [
            {
                "rank": t["rank"],
                "asof_utc": t["asof_utc"],
                "side": t["side"],
                "price": t.get("price"),
                "kind": t["kind"],
                "why_structurally_significant": t["why"],
                "details_json": json.dumps(t["metric_summary"], default=str),
            }
            for t in top10
        ],
    )

    # Major cluster timelines markdown
    tl = ["# PD1 major cluster timelines\n\n", "Separate rankings (frozen metrics); no single composite score.\n\n"]
    for label, items in majors.items():
        tl.append(f"## Ranking: {label}\n\n")
        for c in items[:8]:
            dur = duration_hours(c["first_observed"], c["last_observed"])
            tl.append(
                f"- **{c['side']}** `{c['membership_fp']}` "
                f"{c['bounds_last']['bottom']:.6f}–{c['bounds_last']['top']:.6f} · "
                f"strength_max {c['strength_max_last']:.3f} · origins {c['origin_count_last']} · "
                f"tracked {dur:.1f}h ({c['first_observed']} → {c['last_observed']})\n"
            )
        tl.append("\n")
    (_OUT / "pd1_major_cluster_timelines.md").write_text("".join(tl), encoding="utf-8")

    # Upper/lower relationships
    rel = ["# PD1 upper–lower landscape relationships\n\n"]
    for day in sorted(contexts_eod):
        ctx = contexts_eod[day]
        p = ctx["price"]
        lo, up = ctx["sides"]["lower"], ctx["sides"]["upper"]
        rel.append(f"## {day} (EOD price {p})\n")
        rel.append(
            f"- **Persistence asymmetry**: lower strongest_relevant {lo['strongest_relevant_strength_max']} vs "
            f"upper {up['strongest_relevant_strength_max']}; upper carries {up['logical_origins']} origins vs "
            f"lower {lo['logical_origins']}.\n"
        )
        rel.append(
            f"- **Coverage**: lower union {lo['union_coverage_width']} vs upper {up['union_coverage_width']} "
            f"(upper field ~{float(up['union_coverage_width'])/max(float(lo['union_coverage_width']),1e-9):.0f}× wider).\n"
        )
        rel.append(
            f"- **Nearest vs relevant**: lower nearest {lo['nearest_strength_max']} vs strongest_relevant "
            f"{lo['strongest_relevant_strength_max']} — spot-adjacent pools can be weak while deeper band stays strong.\n\n"
        )
    rel.append(
        "## System observations (descriptive only)\n"
        "- Deep lower **strongest_relevant** (≈7.57) recurs on multiple days while price trades higher; not proof of support.\n"
        "- Upper origin count **rose** toward 219 by 31 Jul while lower origins stayed ~12–25.\n"
        "- No bridge pools flagged in daily CSV (empty bridge_pool_ids throughout).\n"
        "- Causal lineage: many **merge** events on upper side during fast 25 Jul rally.\n"
    )
    (_OUT / "pd1_upper_lower_relationships.md").write_text("".join(rel), encoding="utf-8")

    limits = """# PD1 context limitations (interpretation phase)

1. **No pool-ID in lineage JSONL** — structural events cite membership_fp / cluster_id only; pool IDs come from cluster_persistence bounds matching, not reloaded SSOT.
2. **Cluster age ≠ pool age** — `pool_age_persistence.csv` vs `cluster_persistence.csv` must be read separately.
3. **Spatial detail** — daily/context aggregates; sub-cluster geometry within merged bands is summarized, not every pool plotted.
4. **Window independence PARTIAL** — 168h replay required for rolling 24/72/168h and lineage; documented in source phase.
5. **No outcome labels** — structural significance scores are heuristic for narrative selection, not PnL.
6. **August 1** — known development day; focal zone discussed only in case-study section.
"""
    (_OUT / "pd1_context_limitations.md").write_text(limits, encoding="utf-8")

    # Eight-day interpretation narrative
    interp = ["# PD1 eight-day market interpretation (25 Jul – 1 Aug 2026)\n\n"]
    interp.append("Read-only synthesis of `phase6xt_7d_pd1_context_v1`. PD1 only; 5m price for position.\n\n")
    for day in sorted({r["date_utc"] for r in daily}):
        interp.append(f"## {day}\n\n")
        lo = next(r for r in daily if r["date_utc"] == day and r["side"] == "lower")
        up = next(r for r in daily if r["date_utc"] == day and r["side"] == "upper")
        ch_lo = dod_map.get((day, "lower"))
        ch_up = dod_map.get((day, "upper"))
        interp.append(f"**Price (EOD)** ~{lo['price']}.\n\n")
        interp.append(
            f"**Lower landscape**: {lo['pool_count']} pools, {lo['logical_origins']} origins, "
            f"{lo['cluster_count']} merged clusters, union width {lo['union_coverage_width']}, "
            f"max overlap {lo['max_overlap_depth']}. Strongest relevant strength **{lo['strongest_relevant_strength_max']}** "
            f"(deep band below spot). Largest gap {lo['largest_gap_width']}.\n\n"
        )
        interp.append(
            f"**Upper landscape**: {up['pool_count']} pools, {up['logical_origins']} origins, "
            f"{up['cluster_count']} clusters across ~{float(up['union_coverage_width']):.4f} price width, "
            f"overlap depth {up['max_overlap_depth']}. Strongest relevant upper **{up['strongest_relevant_strength_max']}** "
            f"near/adjacent to trade zone; many weak nearest-upper pools at spot (nearest {up['nearest_strength_max']}).\n\n"
        )
        if ch_lo or ch_up:
            interp.append("**Vs previous day**: ")
            parts = []
            if ch_lo:
                parts.append(f"lower origins Δ{ch_lo['origins_delta']}, strongest_rel Δ{ch_lo['strongest_rel_delta']:.3f}")
            if ch_up:
                parts.append(f"upper origins Δ{ch_up['origins_delta']}, strongest_rel Δ{ch_up['strongest_rel_delta']:.3f}")
            interp.append("; ".join(parts) + ".\n\n")
        strong_lo = [c for c in clusters if c["side"] == "lower" and c["strength_max_last"] >= STRONG_FLOOR and c["last_observed"][:10] == day]
        strong_up = [c for c in clusters if c["side"] == "upper" and c["strength_max_last"] >= STRONG_FLOOR and c["last_observed"][:10] >= day and c["first_observed"][:10] <= day]
        if strong_lo:
            interp.append("**Notable lower concentrations (strength≥4, active this day)**: ")
            interp.append(
                ", ".join(
                    f"{c['bounds_last']['bottom']:.5f}–{c['bounds_last']['top']:.5f} (max {c['strength_max_last']:.2f})"
                    for c in sorted(strong_lo, key=lambda x: -x["strength_max_last"])[:4]
                )
                + ".\n\n"
            )
        if strong_up:
            interp.append("**Notable upper concentrations (strength≥4)**: ")
            interp.append(
                ", ".join(
                    f"{c['bounds_last']['bottom']:.5f}–{c['bounds_last']['top']:.5f} (max {c['strength_max_last']:.2f})"
                    for c in sorted(strong_up, key=lambda x: -x["strength_max_last"])[:4]
                )
                + ".\n\n"
            )

    interp.append("## Case study — known deep lower band (historical, post-chronology)\n\n")
    interp.append(
        "Band ~**0.067634–0.068870** (membership evolves; see `aug01_focal_pool_history.md`). "
        "**strength.max 7.57** and up to **10 origins** by 1 Aug; price **0.07017** at 16:00 (~1.85% above band). "
        "Other same-period flags: upper **0.07065–0.07076** hit **5.83** on 25 Jul (strength ranking); "
        "distant upper **0.07446+** clusters persisted all 8 days at low strength.\n"
    )
    (_OUT / "pd1_eight_day_market_interpretation.md").write_text("".join(interp), encoding="utf-8")

    # Optional simple charts
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        charts = _OUT / "charts"
        charts.mkdir(exist_ok=True)
        days = sorted({r["date_utc"] for r in daily})
        for side, col in (("lower", "tab:blue"), ("upper", "tab:orange")):
            ys = [int(next(r for r in daily if r["date_utc"] == d and r["side"] == side)["logical_origins"]) for d in days]
            fig, ax = plt.subplots(figsize=(9, 4))
            ax.plot(days, ys, marker="o", color=col)
            ax.set_title(f"PD1 {side} logical origins (EOD)")
            ax.tick_params(axis="x", rotation=45)
            fig.tight_layout()
            fig.savefig(charts / f"origins_{side}_eod.png", dpi=120)
            plt.close(fig)
    except Exception:
        pass

    report = """# PD1 Context Interpretation V1 — closure

## A. Hauptentwicklung 25.07–01.08
Upper-Feld wächst in **Origins** (~186→219) und **Coverage**; Lower bleibt schmal, aber **strongest_relevant** tiefer Block (~7.57) wiederholt. Preis schwankt ~0.0696–0.0734 mit Rallye 25.–26.07, dann Rückkehr Richtung 0.070–0.069 bis 1.08.

## B. Mehr-Tage-Persistenz
Cluster mit **strength≥4** und langer `tracked_duration` in `pd1_major_cluster_timelines.md` — z. B. tiefer Lower-Fingerprint **58ce7c9a…** (6 origins, 7.57) und ferne Upper **0.07446+** (bis 1.08).

## C. Neu / ACTIVE-Verlust
Tägliche births/deaths in Chronik; 1.08 Lower schrumpft (5 origins EOD), focal pool inactive 23:55.

## D. Gaps / Coverage / Origins
Upper **largest_gap** ~0.00389 stabil; lower gaps kleiner. Upper union ~0.022 vs lower ~0.0015–0.002.

## E. Was 7d-Lookback liefert
Lineage merges/splits, Pool- vs Cluster-Alter, 168h rolling deltas — nicht in Einzel-As-of.

## F. Zuverlässig heute
Kausale ACTIVE-Membership, frozen strength/contrast metrics, symmetric upper/lower daily stats.

## G. Noch nicht zuverlässig
Kausal **Preis→Pool**; bridge detection (leer); eindeutige Cluster-Identität über Merges ohne membership_fp.

## H. Kohärente Geschichte?
**Ja, deskriptiv** mit obigen Grenzen (`pd1_context_limitations.md`).

STOP
"""
    (_OUT / "report.md").write_text(report, encoding="utf-8")
    _wjson(
        _OUT / "job_status.json",
        {
            "status": "COMPLETE",
            "inputs": str(_IN),
            "outputs": [p.name for p in sorted(_OUT.iterdir())],
            "interpretation_only": True,
        },
    )


if __name__ == "__main__":
    main()

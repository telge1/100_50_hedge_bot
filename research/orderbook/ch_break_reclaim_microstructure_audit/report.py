"""Write audit artifacts and REPORT.md."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from research.orderbook.ch_break_reclaim_microstructure_audit.stats import (
    decide_primary,
    earliest_useful_times,
    stratum_auc_table,
    top_features,
)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # stable union of keys across rows
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_artifacts(
    out_dir: Path,
    *,
    outcomes: list[dict[str, Any]],
    features: list[dict[str, Any]],
    timelines: list[dict[str, Any]],
    quality: list[dict[str, Any]],
    timepoint_stats: list[dict[str, Any]],
    group_stats: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    deep_dive_ids: list[str],
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "event_outcomes.csv", outcomes)
    _write_csv(out_dir / "event_features.csv", features)
    _write_csv(out_dir / "event_timelines.csv", timelines)
    _write_csv(out_dir / "data_quality.csv", quality)
    _write_csv(out_dir / "timepoint_statistics.csv", timepoint_stats)
    _write_csv(out_dir / "feature_group_statistics.csv", group_stats)
    _write_csv(out_dir / "event_touch_break_resolution.csv", resolutions)

    earliest = earliest_useful_times(timepoint_stats)
    _write_csv(out_dir / "earliest_useful_time.csv", earliest)
    top = top_features(timepoint_stats, k=8)
    _write_csv(out_dir / "top_features.csv", top)

    strata_rows: list[dict[str, Any]] = []
    for t in top[:5]:
        strata_rows.extend(
            stratum_auc_table(features, feature=t["feature"], timepoint=t["timepoint"])
        )
    _write_csv(out_dir / "stratum_auc.csv", strata_rows)

    # deep dive timelines filtered
    dive = [r for r in timelines if r["event_id"] in set(deep_dive_ids)]
    _write_csv(out_dir / "deep_dive_timelines.csv", dive)

    q_counts = Counter(r.get("data_quality") for r in quality)
    o_counts = Counter(r.get("outcome_label") for r in outcomes)
    n_valid = q_counts.get("DATA_VALID", 0)
    primary = decide_primary(
        n_valid=n_valid,
        n_events=len(outcomes),
        earliest_rows=earliest,
        top=top,
        stratum_rows=strata_rows,
    )

    summary = {
        "primary_decision": primary,
        "n_events": len(outcomes),
        "data_quality_counts": dict(q_counts),
        "outcome_counts": dict(o_counts),
        "n_feature_rows": len(features),
        "n_timeline_rows": len(timelines),
        "top_features": [
            {
                "feature": t["feature"],
                "timepoint": t["timepoint"],
                "auc": t["auc"],
                "orientation": t.get("auc_orientation"),
                "median_a": t.get("median_a"),
                "median_b": t.get("median_b"),
            }
            for t in top[:5]
        ],
        "earliest_useful_for_top": [
            next((e for e in earliest if e["feature"] == t["feature"]), None) for t in top[:5]
        ],
        "deep_dive_event_ids": deep_dive_ids,
        "artifact_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    report = render_report(summary, outcomes, quality, earliest, top, strata_rows, deep_dive_ids, features)
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return summary


def select_deep_dives(outcomes: list[dict[str, Any]], quality: list[dict[str, Any]]) -> list[str]:
    qmap = {r["event_id"]: r.get("data_quality") for r in quality}
    valid = [o for o in outcomes if qmap.get(o["event_id"]) == "DATA_VALID"]
    picks: list[str] = []
    for lab, need in (
        ("BREAK_ACCEPTED", 3),
        ("RECLAIM_FAST", 3),
        ("RECLAIM_SLOW", 1),
        ("HOLD_NO_BREAK", 1),
    ):
        got = 0
        for o in valid:
            if o["outcome_label"] == lab and o["event_id"] not in picks:
                picks.append(o["event_id"])
                got += 1
            if got >= need:
                break
    if len(picks) < 6:
        for o in valid:
            if o["event_id"] not in picks:
                picks.append(o["event_id"])
            if len(picks) >= 8:
                break
    return picks[:10]


def render_report(
    summary: dict[str, Any],
    outcomes: list[dict[str, Any]],
    quality: list[dict[str, Any]],
    earliest: list[dict[str, Any]],
    top: list[dict[str, Any]],
    strata: list[dict[str, Any]],
    deep_dive_ids: list[str],
    features: list[dict[str, Any]],
) -> str:
    o_counts = summary["outcome_counts"]
    q_counts = summary["data_quality_counts"]
    lines = [
        "# CH Break/Reclaim Microstructure Audit",
        "",
        f"**Primary Decision:** `{summary['primary_decision']}`",
        "",
        "Research only. No live gate, no trading rule, no trend-scanner changes.",
        "",
        "## Scope",
        "",
        f"- Events: **{summary['n_events']}** (exactly CH-covered rows from coverage audit)",
        f"- Feature rows: {summary['n_feature_rows']}",
        "- Causal cutoffs: all features use data with timestamp ≤ observation time T",
        "- Outcome labels may use future information (explicitly separated)",
        "",
        "## 1. Coverage / data quality",
        "",
        f"- DATA_VALID: **{q_counts.get('DATA_VALID', 0)}**",
        f"- DATA_WARNING: **{q_counts.get('DATA_WARNING', 0)}**",
        f"- DATA_INVALID: **{q_counts.get('DATA_INVALID', 0)}**",
        "",
        "Main statistics use DATA_VALID only.",
        "",
        "## 2. Outcome distribution",
        "",
    ]
    for k, v in sorted(o_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- `{k}`: {v}")
    lines += [
        "",
        "### Taxonomy mapping",
        "",
        "- `BREAKDOWN/BREAKOUT_CONFIRMED`, `BEARISH_ACCEPTANCE`, `RECLAIM_THEN_BREAK_CONTINUATION` → `BREAK_ACCEPTED`",
        "- Reclaim/failed-break with ≤15m → `RECLAIM_FAST`, else `RECLAIM_SLOW`",
        "- `UNRESOLVED_WITHIN_MAX_WINDOW` → `HOLD_NO_BREAK`",
        "- `EVENT_DATA_INVALID` / unmapped → `EXCLUDED`",
        "",
        "## 3. Strongest features (BREAK_ACCEPTED vs RECLAIM_FAST)",
        "",
        "| feature | timepoint | AUC | orientation | median_A | median_B |",
        "|---|---|---:|---|---:|---:|",
    ]
    for t in top[:5]:
        lines.append(
            f"| `{t['feature']}` | `{t['timepoint']}` | {t.get('auc')} | {t.get('auc_orientation')} | "
            f"{t.get('median_a')} | {t.get('median_b')} |"
        )
    lines += ["", "## 4. EARLIEST_USEFUL_TIME (top features)", ""]
    for t in top[:5]:
        e = next((x for x in earliest if x["feature"] == t["feature"]), None)
        if e:
            lines.append(
                f"- `{e['feature']}` → **{e['earliest_useful_time']}** (auc≈{e.get('best_auc_at_earliest')})"
            )
    lines += [
        "",
        "## 5. BREAK_ACCEPTED vs RECLAIM_FAST — core",
        "",
    ]
    if top:
        best = top[0]
        lines.append(
            f"Best single-feature separation: `{best['feature']}` at `{best['timepoint']}` "
            f"(AUC={best.get('auc')})."
        )
    else:
        lines.append("No feature reached stable separation with n≥3 per class.")

    lines += ["", "## 6. Symbol / direction strata (top feature)", ""]
    if strata:
        feat0 = top[0]["feature"] if top else None
        lines.append("| stratum | AUC vs RECLAIM_FAST | orientation | AUC vs RECLAIM/HOLD | n_break | n_rf |")
        lines.append("|---|---:|---|---:|---:|---:|")
        for s in strata:
            if feat0 and s["feature"] != feat0:
                continue
            if s["timepoint"] != (top[0]["timepoint"] if top else ""):
                continue
            lines.append(
                f"| {s['stratum']} | {s.get('auc_vs_reclaim_fast')} | {s.get('auc_vs_reclaim_fast_orientation')} | "
                f"{s.get('auc_vs_reclaim_hold')} | {s.get('n_break')} | {s.get('n_reclaim_fast')} |"
            )

    lines += ["", "## 7. Deep-dive events", ""]
    for eid in deep_dive_ids:
        oc = next((o for o in outcomes if o["event_id"] == eid), None)
        if oc:
            lines.append(
                f"- `{eid}` | {oc['symbol']} | {oc['break_direction']} | {oc['outcome_label']} | level={oc['level']}"
            )
    lines += [
        "",
        "See `deep_dive_timelines.csv` for compact relative timelines.",
        "",
        "## 8. Primary questions",
        "",
        "### Q1 — Saubere OB+Trade-Coverage?",
        "",
        f"Von {summary['n_events']} Events: VALID={q_counts.get('DATA_VALID', 0)}, "
        f"WARNING={q_counts.get('DATA_WARNING', 0)}, INVALID={q_counts.get('DATA_INVALID', 0)}. "
        + (
            "Ausreichend für deskriptive Analyse."
            if q_counts.get("DATA_VALID", 0) >= 20
            else "Grenzwertig / unzureichend für robuste Claims."
        ),
        "",
        "### Q2 — Welche Features unterscheiden BREAK_ACCEPTED vs RECLAIM/HOLD?",
        "",
    ]
    if top:
        lines.append(
            "Sichtbar (deskriptiv): "
            + ", ".join(f"`{t['feature']}`@{t['timepoint']}" for t in top[:5])
            + "."
        )
    else:
        lines.append("Keine robuste Einzel-Feature-Trennung in der VALID-Stichprobe.")
    lines += [
        "",
        "### Q3 — Was ist erst nach Bestätigung sichtbar (zu spät)?",
        "",
    ]
    late_feats = [
        e
        for e in earliest
        if e["earliest_useful_time"] in {"BREAK_PLUS_30S", "BREAK_PLUS_60S", "TOO_LATE", "BREAK_PLUS_20S"}
    ]
    if late_feats:
        lines.append(
            "Spät / post-confirmation: "
            + ", ".join(f"`{e['feature']}`→{e['earliest_useful_time']}" for e in late_feats[:8])
        )
    else:
        lines.append(
            "Viele Features ohne Signal; wo Signal existiert, siehe EARLIEST_USEFUL_TIME-Tabelle."
        )
    lines += [
        "",
        "### Q4 — Kausales Signal früh genug für Hedge-Bot Block/Freigabe?",
        "",
        f"Primary decision `{summary['primary_decision']}`. "
        "Es gibt pre-touch / near-touch Trennung in der pooled Stichprobe, "
        "aber Stärke und Orientierung variieren nach Symbol und Break-Richtung "
        "(bearish oft klarer als bullish; RECLAIM_FAST n≈10). "
        "Damit ist ein einzelner pooled Live-Threshold derzeit nicht belastbar — "
        "höchstens ein richtungs-/symbolspezifischer Research-Gate-Kandidat. "
        "Noch keine Thresholds / Live-Logik.",
        "",
        "## 9. Einschränkungen",
        "",
        "- Kleine Stichprobe; RECLAIM_FAST n≈10; Strata noch kleiner",
        "- Coverage-Audit nutzte CH min/max; ein Event (APT 0.5898, 2026-07-28 15:24) fällt in eine "
        "  Recorder-Lücke am Nachmittag und ist DATA_INVALID",
        "- Roh-`ask_depth_*` ist nicht break-direction-normalisiert — für Gates "
        "  `support_*` / `break_side_*` / `signed_*` bevorzugen",
        "- C3 `break_available_at` ist Scanner-Close; Trade-through/Touch wurden zusätzlich abgeleitet",
        "- Wall pull vs trade-consumption nur als Depth-Δ-Proxy",
        "- Kein ML; AUC ist univariater Rank-Score ohne Multi-Test-Korrektur",
        "- RECLAIM_FAST-Schwelle 15m ist a-priori, nicht optimiert",
        "",
        f"Artifacts: `{summary['artifact_dir']}`",
        "",
    ]
    return "\n".join(lines) + "\n"

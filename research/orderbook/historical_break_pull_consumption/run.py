"""Orchestrate pull/consumption deep dive for the 15 selected events."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from research.orderbook.historical_break_pull_consumption.analyze import analyze_event

logger = logging.getLogger(__name__)

DEFAULT_EVENTS = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_structure_break_ob_deep_dive_20260808/selected_deep_dive_events.csv"
)
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_break_pull_consumption_deep_dive_20260808"
)


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


def load_events(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def decide_primary(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "DATA_INSUFFICIENT"
    valid = [s for s in summaries if s.get("data_quality") != "DATA_INVALID"]
    if len(valid) < 5:
        return "DATA_INSUFFICIENT"
    classes = Counter(s.get("mechanism_class") for s in valid)
    accepted = [s for s in valid if s.get("outcome") == "BREAK_ACCEPTED"]
    reclaim = [s for s in valid if s.get("outcome") == "RECLAIM_OR_HOLD"]
    acc_c = Counter(s.get("mechanism_class") for s in accepted)
    rec_c = Counter(s.get("mechanism_class") for s in reclaim)

    clear = sum(
        classes[k]
        for k in (
            "PULL_DOMINANT",
            "CONSUMPTION_DOMINANT",
            "MIXED_PULL_CONSUMPTION",
            "REFILL_ABSORPTION",
        )
    )
    if clear < max(3, len(valid) // 3):
        return "NO_CLEAR_PULL_CONSUMPTION_PATTERN"

    # Distinguish reclaim via absorption?
    if reclaim and rec_c.get("REFILL_ABSORPTION", 0) >= max(2, len(reclaim) // 2):
        if accepted and (
            acc_c.get("PULL_DOMINANT", 0) + acc_c.get("CONSUMPTION_DOMINANT", 0) + acc_c.get("MIXED_PULL_CONSUMPTION", 0)
        ) >= max(2, len(accepted) // 2):
            return "REFILL_ABSORPTION_DISTINGUISHES_RECLAIMS"

    if accepted:
        top_acc = acc_c.most_common(1)[0][0] if acc_c else None
        if top_acc == "PULL_DOMINANT" and acc_c["PULL_DOMINANT"] >= max(2, (len(accepted) + 1) // 2):
            return "PULL_DOMINATES_ACCEPTED_BREAKS"
        if top_acc == "CONSUMPTION_DOMINANT" and acc_c["CONSUMPTION_DOMINANT"] >= max(
            2, (len(accepted) + 1) // 2
        ):
            return "CONSUMPTION_DOMINATES_ACCEPTED_BREAKS"
        if top_acc == "MIXED_PULL_CONSUMPTION":
            return "MIXED_PULL_CONSUMPTION_PATTERN_VISIBLE"

    if classes.get("MIXED_PULL_CONSUMPTION", 0) >= clear // 2:
        return "MIXED_PULL_CONSUMPTION_PATTERN_VISIBLE"
    return "PULL_CONSUMPTION_PATTERNS_VISIBLE_BUT_MIXED"


def run_deep_dive(*, events_csv: Path = DEFAULT_EVENTS, out_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    events = load_events(events_csv)
    logger.info("Loaded %s deep-dive events", len(events))

    summaries: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    lifecycles: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []

    # Group by day for logging; still one analyze each (OB stream per event —
    # could optimize later; 15 events ok)
    for i, ev in enumerate(events, 1):
        logger.info("[%s/%s] %s", i, len(events), ev["event_id"])
        res = analyze_event(ev)
        summaries.append(res["summary"])
        matches.extend(res["matches"])
        lifecycles.extend(res["lifecycle"])
        timelines.extend(res["timeline"])
        quality_rows.append(res["quality"])

    # Accepted vs reclaim summary
    by_outcome: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in summaries:
        by_outcome[s.get("outcome") or "UNKNOWN"].append(s)
    avr_rows = []
    for outcome, rows in sorted(by_outcome.items()):
        c = Counter(r.get("mechanism_class") for r in rows)
        pulls = [r.get("pull_start_seconds_before_break") for r in rows if r.get("pull_start_seconds_before_break") is not None]
        cons = [
            r.get("consumption_start_seconds_before_break")
            for r in rows
            if r.get("consumption_start_seconds_before_break") is not None
        ]
        avr_rows.append(
            {
                "outcome": outcome,
                "n": len(rows),
                "mechanism_counts": json.dumps(dict(c)),
                "pull_dominant_n": c.get("PULL_DOMINANT", 0),
                "consumption_dominant_n": c.get("CONSUMPTION_DOMINANT", 0),
                "mixed_n": c.get("MIXED_PULL_CONSUMPTION", 0),
                "refill_absorption_n": c.get("REFILL_ABSORPTION", 0),
                "no_clear_n": c.get("NO_CLEAR_MECHANISM", 0),
                "median_pull_start_s_before": (
                    sorted(pulls)[len(pulls) // 2] if pulls else None
                ),
                "median_consumption_start_s_before": (
                    sorted(cons)[len(cons) // 2] if cons else None
                ),
            }
        )

    primary = decide_primary(summaries)
    class_counts = Counter(s.get("mechanism_class") for s in summaries)
    conf_counts = Counter(s.get("confidence") for s in summaries)
    clear_n = sum(
        1
        for s in summaries
        if s.get("mechanism_class") not in {"NO_CLEAR_MECHANISM", None}
        and s.get("confidence") != "LOW"
    )
    # also count medium+ as determined
    determined = sum(
        1
        for s in summaries
        if s.get("mechanism_class") not in {"NO_CLEAR_MECHANISM", None}
    )

    # clearest examples
    clearest = sorted(
        [s for s in summaries if s.get("mechanism_class") != "NO_CLEAR_MECHANISM"],
        key=lambda s: (
            0 if s.get("confidence") == "HIGH" else 1 if s.get("confidence") == "MEDIUM" else 2,
            -(s.get("gross_removal_qty") or 0),
        ),
    )[:5]

    counterexamples = []
    accepted_pull = [s for s in summaries if s.get("outcome") == "BREAK_ACCEPTED" and s.get("mechanism_class") == "REFILL_ABSORPTION"]
    reclaim_pull = [s for s in summaries if s.get("outcome") == "RECLAIM_OR_HOLD" and s.get("mechanism_class") in {"PULL_DOMINANT", "CONSUMPTION_DOMINANT"}]
    counterexamples.extend(accepted_pull)
    counterexamples.extend(reclaim_pull)

    summary = {
        "primary_decision": primary,
        "n_events": len(summaries),
        "mechanism_counts": dict(class_counts),
        "confidence_counts": dict(conf_counts),
        "n_mechanism_determined": determined,
        "n_medium_or_better_clear": clear_n,
        "quality_counts": dict(Counter(q.get("data_quality") for q in quality_rows)),
        "accepted_vs_reclaim": avr_rows,
        "clearest_events": [
            f"{s['event_id']} → {s['mechanism_class']} ({s.get('confidence')})" for s in clearest
        ],
        "counterexamples": [
            f"{s['event_id']} outcome={s.get('outcome')} mech={s.get('mechanism_class')}"
            for s in counterexamples
        ],
        "match_tolerance_ms": 750,
        "note_feed_alignment": (
            "OB deltas and public trades are separate feeds; ±750ms match window is "
            "descriptive sync tolerance, not microsecond causality."
        ),
        "artifact_dir": str(out_dir),
    }

    _write_csv(out_dir / "event_mechanisms.csv", summaries)
    _write_csv(out_dir / "wall_trade_matches.csv", matches)
    _write_csv(out_dir / "wall_lifecycles_with_trades.csv", lifecycles)
    _write_csv(out_dir / "combined_event_timelines.csv", timelines)
    _write_csv(out_dir / "accepted_vs_reclaim_summary.csv", avr_rows)
    _write_csv(out_dir / "data_quality.csv", quality_rows)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )

    # REPORT
    lines = [
        "# Historical Break Pull vs Consumption Deep Dive",
        "",
        f"**Primary Decision:** `{primary}`",
        "",
        "Mode: Historical OB + Public Trades on the same 15 deep-dive structure breaks.",
        "No AUC / ML / live gate. Matching tolerance ±750ms (cross-feed).",
        "",
        "## 1. Mechanism determination",
        "",
        f"- Events with a non-`NO_CLEAR_MECHANISM` class: **{determined}/15**",
        f"- Confidence: {dict(conf_counts)}",
        "",
        "## 2. Mechanism counts",
        "",
        f"- {dict(class_counts)}",
        "",
        "## 3. Accepted breaks (typical)",
        "",
    ]
    acc = by_outcome.get("BREAK_ACCEPTED", [])
    if acc:
        lines.append(f"- n={len(acc)} · {dict(Counter(s.get('mechanism_class') for s in acc))}")
        for s in acc[:5]:
            lines.append(
                f"  - `{s['event_id']}`: {s.get('mechanism_class')} "
                f"ratio={s.get('consumption_ratio')} "
                f"pull_s={s.get('pull_start_seconds_before_break')} "
                f"cons_s={s.get('consumption_start_seconds_before_break')}"
            )
    else:
        lines.append("- none classified BREAK_ACCEPTED via +60s beyond / prior label")
    lines += ["", "## 4. Reclaim / Hold (typical)", ""]
    rec = by_outcome.get("RECLAIM_OR_HOLD", [])
    if rec:
        lines.append(f"- n={len(rec)} · {dict(Counter(s.get('mechanism_class') for s in rec))}")
        for s in rec[:5]:
            lines.append(
                f"  - `{s['event_id']}`: {s.get('mechanism_class')} "
                f"refill={s.get('gross_refill_qty')} agg_pre={s.get('aggressive_qty_pre_break')}"
            )
    else:
        lines.append("- none")
    pulls = [
        s.get("pull_start_seconds_before_break")
        for s in summaries
        if s.get("pull_start_seconds_before_break") is not None
    ]
    cons = [
        s.get("consumption_start_seconds_before_break")
        for s in summaries
        if s.get("consumption_start_seconds_before_break") is not None
    ]
    lines += [
        "",
        "## 5–6. Timing before first_break",
        "",
        f"- Events with pull_start: {len(pulls)}; median seconds before break: "
        f"{(sorted(pulls)[len(pulls)//2] if pulls else 'n/a')}",
        f"- Events with consumption_start: {len(cons)}; median seconds before break: "
        f"{(sorted(cons)[len(cons)//2] if cons else 'n/a')}",
        "",
        "## 7. Refill / Absorption",
        "",
        f"- REFILL_ABSORPTION count: {class_counts.get('REFILL_ABSORPTION', 0)}",
        "- Role: distinguishes some reclaim/hold cases where aggressive flow hits the level "
        "but zone liquidity refills and acceptance fails.",
        "",
        "## 8. Clearest events",
        "",
    ]
    for c in summary["clearest_events"]:
        lines.append(f"- {c}")
    lines += ["", "## 9. Counterexamples", ""]
    if summary["counterexamples"]:
        for c in summary["counterexamples"]:
            lines.append(f"- {c}")
    else:
        lines.append("- none strong")
    lines += [
        "",
        "## 10. Robust enough for a later statistical test (not run)",
        "",
        "- Pre-break wall-zone depletion timing (seconds_to_break) vs outcome",
        "- matched_aggressive_qty / gross_removal (consumption ratio) distance-conditioned",
        "- REFILL_ABSORPTION vs accepted mechanisms — still descriptive only here",
        "",
        f"Quality: {summary['quality_counts']}",
        f"Artifacts: `{out_dir}`",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary

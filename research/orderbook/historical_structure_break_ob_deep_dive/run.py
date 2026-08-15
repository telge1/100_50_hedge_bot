"""Orchestrate historical structure-break OB deep dive."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from research.orderbook.historical_structure_break_ob_deep_dive.inventory import (
    build_inventory,
    prioritize_events,
)
from research.orderbook.historical_structure_break_ob_deep_dive.ob_extract import (
    process_events_for_day,
)

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/historical_structure_break_ob_deep_dive_20260808"
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


def decide_primary(
    *,
    n_events: int,
    n_selected: int,
    classifications: list[str],
    n_valid: int,
) -> str:
    if n_events == 0:
        return "HISTORICAL_BREAK_EVENTS_INSUFFICIENT"
    if n_selected == 0 or n_valid == 0:
        return "DATA_INSUFFICIENT"
    c = Counter(classifications)
    clear = sum(
        c[k]
        for k in (
            "WALL_PULLED_BEFORE_BREAK",
            "WALL_CONSUMED_OR_REMOVED_BREAK",
            "REFILL_THEN_RECLAIM",
            "BREAK_ACCEPTED_NO_QUICK_RECLAIM",
            "WALL_HELD_OR_RECLAIM",
        )
    )
    mixed = c.get("MIXED", 0) + c.get("NO_CLEAR_WALL_BEHAVIOR", 0)
    if clear >= max(3, n_valid // 2) and len([k for k, v in c.items() if v > 0 and k not in {"MIXED", "NO_CLEAR_WALL_BEHAVIOR", "DATA_INVALID"}]) >= 2:
        return "HISTORICAL_BREAK_OB_PATTERNS_VISIBLE"
    if clear >= 2:
        return "HISTORICAL_BREAK_OB_PATTERNS_MIXED"
    if mixed >= n_valid // 2:
        return "HISTORICAL_BREAK_OB_NO_CLEAR_PATTERN"
    return "HISTORICAL_BREAK_OB_PATTERNS_MIXED"


def write_report(out_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Historical Structure-Break Orderbook Deep Dive",
        "",
        f"**Primary Decision:** `{summary['primary_decision']}`",
        "",
        "Mode: **ORDERBOOK_ONLY** (no historical public trades for these days).",
        "Scanner: C3.4B `protected_medium` on feather 5m (+1h/4h aggregate). No new structure definition.",
        "",
        "## 1. How many important structure breaks on the 10 OB days?",
        "",
        f"- Clustered important events: **{summary['n_clustered_events']}**",
        f"- Raw rising-edge prints before clustering: {summary['n_raw_events']}",
        "",
        "## 2. Selected for deep dive",
        "",
    ]
    for e in summary.get("selected_events", []):
        lines.append(
            f"- `{e['event_id']}` | {e['symbol']} {e['direction']} {e['structure_type']} "
            f"TF={e['timeframe']} level={e['level']} avail={e['available_at']}"
        )
    lines += [
        "",
        "## 3. Timeframes / structure types",
        "",
        f"- Clustered types: {summary.get('type_counts')}",
        f"- Clustered TFs: {summary.get('tf_counts_clustered')}",
        f"- Selected TFs: {summary.get('selected_tf_counts')}",
        f"- Selected directions: {summary.get('selected_dir_counts')}",
        f"- Selected symbols: {summary.get('selected_sym_counts')}",
        "",
        "## 4. Orderbook BEFORE breaks",
        "",
        summary.get("pre_break_narrative", ""),
        "",
        "## 5. Orderbook AT break",
        "",
        summary.get("at_break_narrative", ""),
        "",
        "## 6. Continue vs reclaim/hold",
        "",
        summary.get("behavior_narrative", ""),
        f"- accepted_n={summary.get('accepted_n')} reclaim_hold_n={summary.get('reclaim_hold_n')}",
        "",
        "## 7–8. Wall-lifecycle patterns / cases",
        "",
        f"- Classification counts: {summary.get('classification_counts')}",
        f"- pulled_before_break={summary.get('pulled_before_break_n')}",
        f"- consumed_or_removed={summary.get('consumed_or_removed_n')}",
        f"- refill/reclaim or held={summary.get('reclaim_hold_n')}",
        "",
        "## 9. Visible before break",
        "",
        summary.get("pre_break_narrative", ""),
        "",
        "## 10. Visible only after break",
        "",
        summary.get("post_break_narrative", ""),
        "",
        "## 11. Especially informative events",
        "",
    ]
    for eid in summary.get("highlight_events", []):
        lines.append(f"- {eid}")
    lines += [
        "",
        "## 12. Data problems",
        "",
        f"- Quality: {summary.get('quality_counts')}",
        f"- Trades: ORDERBOOK_ONLY (no historical trade files for these days)",
        "",
        "## 13. Logical next test (not executed)",
        "",
        summary.get("next_test", ""),
        "",
        f"Artifacts: `{out_dir}`",
        "",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_deep_dive(*, out_dir: Path = DEFAULT_OUT, max_events: int = 15) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Phase A/B: building C3.4B inventory on historical OB days")
    clustered, raw = build_inventory()
    for e in clustered:
        e["historical_ob_available"] = True
        e["status"] = e.get("status") or "CANDIDATE"
    _write_csv(out_dir / "structure_break_events.csv", clustered)
    _write_csv(out_dir / "structure_break_events_raw.csv", raw)

    selected = prioritize_events(clustered, max_n=max_events)
    _write_csv(out_dir / "selected_deep_dive_events.csv", selected)

    all_timepoints: list[dict[str, Any]] = []
    all_life: list[dict[str, Any]] = []
    all_timeline: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    class_rows: list[dict[str, Any]] = []

    by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ev in selected:
        by_day.setdefault((ev["symbol"], ev["date"]), []).append(ev)

    done = 0
    for (symbol, date), day_events in sorted(by_day.items()):
        logger.info(
            "OB day pass %s %s (%s events)",
            symbol,
            date,
            len(day_events),
        )
        results = process_events_for_day(day_events)
        for ev in day_events:
            done += 1
            res = results[ev["event_id"]]
            logger.info("[%s/%s] %s → %s", done, len(selected), ev["event_id"], res.get("classification"))
            q = res["quality"]
            quality_rows.append(q)
            ev["first_touch_ts"] = res.get("resolved_first_touch")
            ev["first_break_ts"] = res.get("resolved_first_break")
            ev["ob_classification"] = res.get("classification")
            class_rows.append(
                {
                    "event_id": ev["event_id"],
                    "symbol": ev["symbol"],
                    "direction": ev["direction"],
                    "structure_type": ev["structure_type"],
                    "timeframe": ev["timeframe"],
                    "level": ev["level"],
                    "available_at": ev["available_at"],
                    "first_touch_ts": ev["first_touch_ts"],
                    "first_break_ts": ev["first_break_ts"],
                    "observed_behavior": res.get("classification"),
                    "data_quality": q.get("data_quality"),
                    "mode": "ORDERBOOK_ONLY",
                }
            )
            all_timepoints.extend(res["timepoints"])
            all_life.extend(res["lifecycle"])
            all_timeline.extend(res["timeline"])

    _write_csv(out_dir / "selected_deep_dive_events.csv", selected)
    _write_csv(out_dir / "event_ob_timepoints.csv", all_timepoints)
    _write_csv(out_dir / "event_wall_lifecycles.csv", all_life)
    _write_csv(out_dir / "event_timelines.csv", all_timeline)
    _write_csv(out_dir / "event_classification.csv", class_rows)
    _write_csv(out_dir / "data_quality.csv", quality_rows)

    classes = [r["observed_behavior"] for r in class_rows]
    qcounts = Counter(r.get("data_quality") for r in quality_rows)
    n_valid = qcounts.get("DATA_VALID", 0)
    primary = decide_primary(
        n_events=len(clustered),
        n_selected=len(selected),
        classifications=classes,
        n_valid=n_valid,
    )

    # narratives from classifications + timepoint deltas
    pulled = [r for r in class_rows if r["observed_behavior"] == "WALL_PULLED_BEFORE_BREAK"]
    removed = [r for r in class_rows if r["observed_behavior"] == "WALL_CONSUMED_OR_REMOVED_BREAK"]
    reclaim = [r for r in class_rows if r["observed_behavior"] in {"REFILL_THEN_RECLAIM", "WALL_HELD_OR_RECLAIM"}]
    accepted = [r for r in class_rows if r["observed_behavior"] == "BREAK_ACCEPTED_NO_QUICK_RECLAIM"]

    def _tp(eid: str, marker: str) -> dict[str, Any] | None:
        for row in all_timepoints:
            if row.get("event_id") == eid and row.get("marker") == marker:
                return row
        return None

    pre_bits: list[str] = []
    at_bits: list[str] = []
    post_bits: list[str] = []
    for r in class_rows:
        eid = r["event_id"]
        pre10 = _tp(eid, "PRE_10S")
        br = _tp(eid, "FIRST_BREAK")
        post60 = _tp(eid, "POST_60S")
        if pre10 and br:
            d_sup = (br.get("support_wall_notional") or 0) - (pre10.get("support_wall_notional") or 0)
            pre_bits.append(
                f"{eid}: support_wall Δ(−10s→break)={d_sup:.0f} notional; "
                f"dist_pre10={pre10.get('distance_to_level_bps')}"
            )
            at_bits.append(
                f"{eid}: at break mid={br.get('mid')} beyond={br.get('bbo_beyond_level')} "
                f"support_wall={br.get('support_wall_notional')}"
            )
        if br and post60:
            post_bits.append(
                f"{eid}: +60s beyond={post60.get('bbo_beyond_level')} "
                f"support_wall={post60.get('support_wall_notional')} class={r['observed_behavior']}"
            )

    behavior = (
        f"Across {len(class_rows)} deep-dive events (ORDERBOOK_ONLY): "
        f"pulled-before-break={len(pulled)}, removed-at-break={len(removed)}, "
        f"reclaim/hold={len(reclaim)}, accepted-no-quick-reclaim={len(accepted)}, "
        f"mixed/unclear={sum(1 for c in classes if c in {'MIXED','NO_CLEAR_WALL_BEHAVIOR'})}. "
        "Without trades, pull vs consumption cannot be separated — size drops are pull/consume proxies."
    )
    pre = "Before break (examples):\n" + ("\n".join(f"- {b}" for b in pre_bits[:8]) or "- n/a")
    post = "After break (examples):\n" + ("\n".join(f"- {b}" for b in post_bits[:8]) or "- n/a")
    at_break = "At break (examples):\n" + ("\n".join(f"- {b}" for b in at_bits[:8]) or "- n/a")
    highlights = [
        r["event_id"] + " → " + r["observed_behavior"]
        for r in class_rows
        if r["observed_behavior"] not in {"MIXED", "NO_CLEAR_WALL_BEHAVIOR", "DATA_INVALID"}
    ][:8]
    next_test = (
        "Next (not run): attach historical trades for the same 10 days to separate pull vs consumption; "
        "then compare WALL_PULLED_BEFORE_BREAK vs BREAK_ACCEPTED_NO_QUICK_RECLAIM with "
        "distance-conditioned residuals — still no live gate."
    )

    summary = {
        "primary_decision": primary,
        "n_raw_events": len(raw),
        "n_clustered_events": len(clustered),
        "n_selected": len(selected),
        "type_counts": dict(Counter(e["structure_type"] for e in clustered)),
        "tf_counts_clustered": dict(Counter(e["timeframe"] for e in clustered)),
        "selected_tf_counts": dict(Counter(e["timeframe"] for e in selected)),
        "selected_dir_counts": dict(Counter(e["direction"] for e in selected)),
        "selected_sym_counts": dict(Counter(e["symbol"] for e in selected)),
        "classification_counts": dict(Counter(classes)),
        "quality_counts": dict(qcounts),
        "selected_events": [
            {
                "event_id": e["event_id"],
                "symbol": e["symbol"],
                "direction": e["direction"],
                "structure_type": e["structure_type"],
                "timeframe": e["timeframe"],
                "level": e["level"],
                "available_at": e["available_at"],
                "first_break_ts": e.get("first_break_ts"),
                "ob_classification": e.get("ob_classification"),
            }
            for e in selected
        ],
        "behavior_narrative": behavior,
        "at_break_narrative": at_break,
        "pre_break_narrative": pre,
        "post_break_narrative": post,
        "highlight_events": highlights,
        "next_test": next_test,
        "mode": "ORDERBOOK_ONLY",
        "artifact_dir": str(out_dir),
        "pulled_before_break_n": len(pulled),
        "consumed_or_removed_n": len(removed),
        "reclaim_hold_n": len(reclaim),
        "accepted_n": len(accepted),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    write_report(out_dir, summary)
    return summary

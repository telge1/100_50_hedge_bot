"""Markdown / CSV report helpers for the six-event pilot."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def render_dry_run_report(dry_results: list[dict[str, Any]]) -> str:
    lines = [
        "# Dry-Run Report — MP ↔ Canonical QDH",
        "",
        "No ClickHouse mutations. Wall selection + planned windows only.",
        "",
    ]
    for r in dry_results:
        link = r.get("link") or {}
        lines.extend(
            [
                f"## `{r.get('event_id')}`",
                "",
                f"- Pair/case: `{json.dumps(r.get('case') or link.get('case') or {})}`",
                f"- Epoch: `{link.get('replay_epoch')}`",
                f"- Zone: `{link.get('zone_id')}` role=`{link.get('event_role')}`",
                f"- Candidates (top): `{json.dumps(link.get('candidates_top') or [])[:500]}`",
                f"- Selected wall: `{link.get('wall_id')}` side=`{link.get('wall_side')}` "
                f"price=`{link.get('wall_price')}` size@touch=`{link.get('wall_size_at_zone_touch')}`",
                f"- Band: [{link.get('canonical_band_low')}, {link.get('canonical_band_high')}] "
                f"(ticks=`{link.get('band_ticks')}`)",
                f"- Coverage OK: `{link.get('coverage_ok')}` blocker=`{link.get('blocker_reason')}`",
                f"- Baseline: `{link.get('baseline_window')}`",
                f"- Decision: `{link.get('decision_window')}`",
                f"- Forensic tail: `{link.get('forensic_tail_window')}`",
                f"- Planned call: `{r.get('planned_qdh_call')}`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def _snap(r: dict[str, Any]) -> dict[str, Any]:
    return r.get("decision_snapshot") or {}


def _link(r: dict[str, Any]) -> dict[str, Any]:
    return r.get("link") or {}


def render_pair_report(*, pair_id: str, pair_label: str, results: list[dict[str, Any]]) -> str:
    pair = [r for r in results if (r.get("case") or {}).get("pair_id") == pair_id]
    lines = [
        f"# {pair_id} — {pair_label}",
        "",
        "Outcome classes are reported separately and do **not** drive wall selection or QDH.",
        "",
    ]
    for r in pair:
        case = r.get("case") or {}
        link = _link(r)
        snap = _snap(r)
        pre = [b for b in (r.get("buckets") or []) if not b.get("post_decision")]
        fill = sum(float(b.get("attributed_fill_qty") or 0) for b in pre)
        pull = sum(float(b.get("residual_pull_qty") or 0) for b in pre)
        refill = sum(float(b.get("refill_qty") or 0) for b in pre)
        lines.extend(
            [
                f"## {case.get('outcome_class')} — `{r.get('event_id')}`",
                "",
                f"- Wall selected: `{bool(link.get('wall_id'))}` id=`{link.get('wall_id')}` "
                f"side=`{link.get('wall_side')}` price=`{link.get('wall_price')}`",
                f"- Visible at touch: `{link.get('wall_visible_at_zone_touch')}` "
                f"size=`{link.get('wall_size_at_zone_touch')}`",
                f"- Attributed fill / residual pull / refill (decision window sums): "
                f"{fill:.4f} / {pull:.4f} / {refill:.4f}",
                f"- Queue remaining @ cutoff: `{snap.get('canonical_qdh_queue_remaining')}`",
                f"- QDH_base @ cutoff: `{snap.get('canonical_qdh_base')}` valid=`{snap.get('qdh_valid')}`",
                f"- Queue runway: `{snap.get('queue_runway_seconds')}` state=`{snap.get('queue_state')}`",
                f"- Persistence: `{snap.get('persistence_ratio')}`",
                f"- Impact efficiency (raw): `{snap.get('impact_efficiency_pct_per_musd')}`",
                f"- Mid / micro @ cutoff: `{snap.get('midprice')}` / `{snap.get('microprice')}`",
                f"- Attribution confidence: `{snap.get('attribution_confidence')}`",
                f"- Coverage OK: `{r.get('coverage_ok')}` blocker=`{r.get('blocker_reason') or link.get('blocker_reason')}`",
                f"- Leakage check: `{snap.get('leakage_check_passed')}` "
                f"max_avail=`{snap.get('max_signal_feature_available_at')}`",
                f"- Outcome (separate): `{case.get('outcome_class')}`",
                "",
            ]
        )
    if len(pair) >= 2:
        a, b = pair[0], pair[1]
        sa, sb = _snap(a), _snap(b)
        lines.extend(
            [
                "## Pair questions (descriptive)",
                "",
                "1. Mechanically similar: both reuse the same QDH attribution + band_ticks=5 contract.",
                f"2. Pre-cutoff QDH_base: `{sa.get('canonical_qdh_base')}` vs `{sb.get('canonical_qdh_base')}`.",
                f"3. Queue survived: `{sa.get('canonical_qdh_queue_remaining')}` vs `{sb.get('canonical_qdh_queue_remaining')}`.",
                "4. Fill vs pull: see per-case residual_pull vs attributed_fill sums above.",
                f"5. Persistence: `{sa.get('persistence_ratio')}` vs `{sb.get('persistence_ratio')}`.",
                f"6. Raw impact efficiency: `{sa.get('impact_efficiency_pct_per_musd')}` vs "
                f"`{sb.get('impact_efficiency_pct_per_musd')}`.",
                "7. Microprice reclaim: see mechanism_flags / microprice series (OFI = NOT_IMPLEMENTED).",
                "8. Causal differences: only features with available_at ≤ trigger_ts.",
                "9. Forensic-only differences: post_decision=true buckets (excluded from decision snapshot).",
                "10. Hypothesis readiness: no thresholds tuned on these six; classify INSUFFICIENT_EVIDENCE "
                "if either side lacks coverage/attribution.",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def write_pair_comparison_csv(path: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        pid = (r.get("case") or {}).get("pair_id") or "?"
        by_pair.setdefault(pid, []).append(r)
    fieldnames = [
        "pair_id",
        "a_event_id",
        "a_outcome",
        "b_event_id",
        "b_outcome",
        "a_wall_price",
        "b_wall_price",
        "a_qdh_base",
        "b_qdh_base",
        "a_queue_rem",
        "b_queue_rem",
        "a_persistence",
        "b_persistence",
        "a_impact",
        "b_impact",
        "a_ok",
        "b_ok",
        "core_diff_note",
        "note",
    ]
    for pid, items in sorted(by_pair.items()):
        if len(items) != 2:
            rows.append({k: None for k in fieldnames} | {"pair_id": pid, "note": f"expected 2 got {len(items)}"})
            continue
        a, b = items[0], items[1]
        sa, sb = _snap(a), _snap(b)
        la, lb = _link(a), _link(b)
        rows.append(
            {
                "pair_id": pid,
                "a_event_id": a.get("event_id"),
                "a_outcome": (a.get("case") or {}).get("outcome_class"),
                "b_event_id": b.get("event_id"),
                "b_outcome": (b.get("case") or {}).get("outcome_class"),
                "a_wall_price": la.get("wall_price"),
                "b_wall_price": lb.get("wall_price"),
                "a_qdh_base": sa.get("canonical_qdh_base"),
                "b_qdh_base": sb.get("canonical_qdh_base"),
                "a_queue_rem": sa.get("canonical_qdh_queue_remaining"),
                "b_queue_rem": sb.get("canonical_qdh_queue_remaining"),
                "a_persistence": sa.get("persistence_ratio"),
                "b_persistence": sb.get("persistence_ratio"),
                "a_impact": sa.get("impact_efficiency_pct_per_musd"),
                "b_impact": sb.get("impact_efficiency_pct_per_musd"),
                "a_ok": a.get("ok"),
                "b_ok": b.get("ok"),
                "core_diff_note": _core_diff(sa, sb),
                "note": None,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return rows


def _core_diff(sa: dict[str, Any], sb: dict[str, Any]) -> str:
    parts = []
    for k, label in (
        ("canonical_qdh_base", "qdh"),
        ("canonical_qdh_queue_remaining", "queue"),
        ("persistence_ratio", "persist"),
        ("impact_efficiency_pct_per_musd", "impact"),
    ):
        va, vb = sa.get(k), sb.get(k)
        if va is None and vb is None:
            continue
        try:
            if va is not None and vb is not None and abs(float(va) - float(vb)) > 1e-9:
                parts.append(f"{label}:{va}->{vb}")
        except (TypeError, ValueError):
            if va != vb:
                parts.append(f"{label}:{va}->{vb}")
    return "; ".join(parts) if parts else "similar_or_missing"


def render_canonical_report(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    legacy_rows: list[dict[str, Any]],
    pair_rows: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> str:
    lines = [
        "# Canonical QDH Pilot Report",
        "",
        f"**VERDICT:** `{summary.get('verdict')}`",
        "",
        f"- QDH source: `{summary.get('qdh_source_module')}`",
        f"- QDH reimplemented: `{summary.get('qdh_reimplemented')}`",
        f"- Events: {summary.get('pilot_events')} | unique wall: {summary.get('events_with_unique_wall')} | "
        f"full attribution: {summary.get('events_with_full_attribution')} | blocked: {summary.get('events_blocked')}",
        f"- Blockers: `{summary.get('blocker_reasons')}`",
        f"- Defended band: `{summary.get('defended_band_contract')}`",
        f"- Public trades: `{summary.get('public_trade_source')}`",
        f"- Unique trade IDs / dupes removed: {summary.get('unique_trade_ids')} / {summary.get('duplicates_removed')}",
        f"- Fill / pull / refill / unknown sums: "
        f"{summary.get('attributed_fill_sum')} / {summary.get('residual_pull_sum')} / "
        f"{summary.get('refill_sum')} / {summary.get('unknown_sum')}",
        f"- QDH valid / exhausted / near-zero warnings: "
        f"{summary.get('qdh_valid_events')} / {summary.get('queue_exhausted_events')} / "
        f"{summary.get('near_zero_queue_warnings')}",
        f"- Causality / mass-balance violations: "
        f"{summary.get('causality_violations')} / {summary.get('mass_balance_violations')}",
        f"- Legacy material diffs (hit|pull): {summary.get('legacy_material_diffs')}",
        f"- Runtime: {runtime.get('elapsed_s')}s peak_ram={runtime.get('peak_ram_mb')} MB",
        "",
        "## Pair core diffs",
        "",
    ]
    for pr in pair_rows:
        lines.append(
            f"- {pr.get('pair_id')}: {pr.get('a_outcome')} vs {pr.get('b_outcome')} → `{pr.get('core_diff_note')}`"
        )
    lines.extend(["", "## Legacy vs canonical", ""])
    for row in legacy_rows:
        lines.append(
            f"- `{row.get('event_id')}` hit=`{row.get('cmp_hit')}` pull=`{row.get('cmp_pull')}` "
            f"add_vs_refill=`{row.get('cmp_add_vs_refill')}`"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Enrichment HIT/PULL remain `LEGACY_ZONE_PROXY`; canonical values use `canonical_qdh_*`.",
            "- No WallStateClassifier / Signal V2 / threshold tuning in this pilot.",
            "- OFI reclaim = NOT_IMPLEMENTED; normalized impact / absorption / vacuum = NOT_CALIBRATED.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"

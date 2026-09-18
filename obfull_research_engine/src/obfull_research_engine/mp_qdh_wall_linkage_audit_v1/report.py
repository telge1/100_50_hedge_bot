"""Reports and pair comparison for wall-linkage audit."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def write_pair_comparison(path: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        pid = (r.get("case") or {}).get("pair_id") or "?"
        by.setdefault(pid, []).append(r)
    rows: list[dict[str, Any]] = []
    fields = [
        "pair_id",
        "a_event_id",
        "a_outcome",
        "a_linkage",
        "b_event_id",
        "b_outcome",
        "b_linkage",
        "a_queue_touch",
        "b_queue_touch",
        "a_fill",
        "b_fill",
        "a_pull",
        "b_pull",
        "a_qdh_trigger",
        "b_qdh_trigger",
        "judgement",
        "note",
    ]
    for pid, items in sorted(by.items()):
        if len(items) != 2:
            rows.append({k: None for k in fields} | {"pair_id": pid, "judgement": "DATA_QUALITY_BLOCKED", "note": f"n={len(items)}"})
            continue
        a, b = items[0], items[1]
        ra, rb = a.get("reconciliation") or {}, b.get("reconciliation") or {}
        both_attr = bool(a.get("funnel")) and bool(b.get("funnel"))
        if not both_attr:
            judgement = "INSUFFICIENT_EVIDENCE"
        else:
            # Descriptive only — no edge claim
            diffs = []
            for key in ("linkage_status",):
                if a.get(key) != b.get(key):
                    diffs.append(key)
            for key in ("queue_at_touch", "pre_trigger_fill", "pre_trigger_pull", "qdh_at_trigger"):
                va, vb = ra.get(key), rb.get(key)
                try:
                    if va is not None and vb is not None and abs(float(va) - float(vb)) > 1e-9:
                        diffs.append(key)
                except (TypeError, ValueError):
                    if va != vb:
                        diffs.append(key)
            judgement = "DESCRIPTIVE_DIFFERENCE" if diffs else "NO_MATERIAL_DIFFERENCE"
        rows.append(
            {
                "pair_id": pid,
                "a_event_id": a.get("event_id"),
                "a_outcome": (a.get("case") or {}).get("outcome_class"),
                "a_linkage": a.get("linkage_status"),
                "b_event_id": b.get("event_id"),
                "b_outcome": (b.get("case") or {}).get("outcome_class"),
                "b_linkage": b.get("linkage_status"),
                "a_queue_touch": ra.get("queue_at_touch"),
                "b_queue_touch": rb.get("queue_at_touch"),
                "a_fill": ra.get("pre_trigger_fill"),
                "b_fill": rb.get("pre_trigger_fill"),
                "a_pull": ra.get("pre_trigger_pull"),
                "b_pull": rb.get("pre_trigger_pull"),
                "a_qdh_trigger": ra.get("qdh_at_trigger"),
                "b_qdh_trigger": rb.get("qdh_at_trigger"),
                "judgement": judgement,
                "note": None,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return rows


def render_audit_report(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    pair_rows: list[dict[str, Any]],
    runtime: dict[str, Any],
    phase0: dict[str, Any],
) -> str:
    lines = [
        "# Wall Linkage & Public-Trade Attribution Audit",
        "",
        f"**VERDICT:** `{summary.get('verdict')}`",
        "",
        "## Executive Summary",
        "",
        f"- Six pilot events audited against existing canonical QDH path.",
        f"- Linkage status counts: `{summary.get('status_counts')}`",
        f"- Full attribution funnels: {summary.get('events_with_full_attribution')}/6",
        f"- Unique trades / in-band qty / attributed fill: "
        f"{summary.get('total_unique_trades')} / {summary.get('in_band_trade_qty')} / {summary.get('attributed_fill_qty')}",
        f"- Attribution rate vs in-band: {summary.get('attribution_rate_vs_in_band_pct')}%",
        f"- Pull / refill (pre-trigger): {summary.get('pull_sum')} / {summary.get('refill_sum')}",
        f"- Fill share of book decrease: {summary.get('fill_share_of_decrease')}",
        "",
        "## Source tables & code paths",
        "",
        "- Silver LC/metrics: `research_full_ob_silver_v1_3.ob_level_changes_v1_3` / `ob_metrics_100ms_v1_3`",
        "- Public trades: `orderbook_analysis.public_trades_canonical`",
        "- Wall selection prior: `mp_qdh_canonical_integration_v1.wall_select.select_defense_wall`",
        "- Attribution: `level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution.attribute_intervals`",
        "- Mass balance: `...mass_balance.decompose_mass_balance`",
        "- QDH: `...queue_depletion_hazard.update_qdh` (not reimplemented)",
        "",
        "## Phase-0 contract (as coded)",
        "",
        "```json",
        json.dumps(phase0, indent=2)[:4000],
        "```",
        "",
        "## Why the pilot reported WALL_SELECTION_UNRESOLVED for two events",
        "",
        "Pilot `select_defense_wall` only accepts **positive queue at touch** inside zone±5 ticks. "
        "If the defense book was already empty at the MP edge at `first_touch_ts`, candidates=[], "
        "blocker=`NO_VISIBLE_DEFENSE_LEVEL_AT_ZONE_TOUCH`. This audit classifies those cases as "
        "historical DEPLETED/PULLED/MOVED/NO_CANONICAL without widening the search zone.",
        "",
        "## Blocked events detail",
        "",
    ]
    for eid in ("mpe_e13a0ab36c241caf4c30", "mpe_aa9edc2d4c8dd2bb21a3"):
        r = next((x for x in results if x.get("event_id") == eid), None)
        if not r:
            lines.append(f"- `{eid}`: missing from results")
            continue
        sel = r.get("selected") or {}
        lines.extend(
            [
                f"### `{eid}`",
                f"- Role / expected book side: `{r.get('event_role')}` / `{r.get('expected_book_side')}`",
                f"- Label / trade_side (must not flip side): `{r.get('label_price_only')}` / `{r.get('trade_side')}`",
                f"- Linkage: `{r.get('linkage_status')}` — {r.get('detail')}",
                f"- Candidates: {len(r.get('candidates') or [])}",
                f"- Selected historical wall: `{sel.get('candidate_wall_id')}` @ `{sel.get('wall_price_first')}` "
                f"max_q=`{sel.get('max_queue')}` q_touch=`{sel.get('queue_at_touch')}`",
                "",
            ]
        )

    lines.extend(
        [
            "## Why attributed fill ≪ unique trades",
            "",
            "Unique trade counts include **all** public trades loaded in the coverage window "
            "(warmup + pre-touch + decision + forensic). Canonical fills require:",
            "1. price inside `wall±5 ticks`,",
            "2. matching aggressor side,",
            "3. exchange time inside a book interval after wall visibility,",
            "4. trade_id not already consumed.",
            "Most volume fails (1)–(2). Engine does **not** hard-cap fill to book decrease "
            "(excess implies refill in mass-balance identity).",
            "",
            "## Per-event reconciliation",
            "",
        ]
    )
    for r in results:
        recon = r.get("reconciliation") or {}
        fun = r.get("funnel") or {}
        lines.append(
            f"- `{r.get('event_id')}` status=`{r.get('linkage_status')}` "
            f"unique={fun.get('total_unique_trade_count')} in_band_qty={fun.get('in_band_trade_qty')} "
            f"correct_qty={fun.get('correct_aggressor_trade_qty')} fill={fun.get('attributed_fill_qty')} "
            f"pull={recon.get('pre_trigger_pull')} refill={recon.get('pre_trigger_refill')}"
        )

    lines.extend(["", "## Pair judgements", ""])
    for p in pair_rows:
        lines.append(
            f"- {p.get('pair_id')}: `{p.get('judgement')}` "
            f"({p.get('a_outcome')} `{p.get('a_linkage')}` vs {p.get('b_outcome')} `{p.get('b_linkage')}`)"
        )

    lines.extend(
        [
            "",
            "## Legacy vs canonical",
            "",
            "Legacy enrichment HIT/PULL remain `LEGACY_ZONE_PROXY` (zone±bps aggregates). "
            "Canonical fills are wall-band public-trade attributions. Do not mix columns.",
            "",
            "## Causality & mass balance",
            "",
            f"- Causality violations (feature avail ≤ bucket start flags): {summary.get('causality_violations')}",
            f"- Mass-balance violations (engine identity): {summary.get('mass_balance_violations')}",
            "- Wall selection uses only LC with `event_time_ns ≤ first_touch_ts_ns`.",
            "- `POST_TRIGGER_FORENSIC` is explanatory only.",
            "",
            "## Open risks",
            "",
            "- Narrow zone±5 ticks may leave true nearby liquidity unclassified (by contract).",
            "- Receive-time often proxied when collector_received_at missing.",
            "- Microprice in this audit is `MICROPRICE_PROXY` (mid when sizes unknown).",
            "- Historical DEPLETED/PULLED uses fill-share heuristic (0.5) — documented, not trading threshold.",
            "",
            "## Recommendation (next step)",
            "",
            "1. Keep ±5-tick contract; treat DEPLETED/PULLED as first-class linkage outcomes.",
            "2. Report funnel rates (in-band / correct-aggressor) alongside raw unique trade counts.",
            "3. Only then consider WallStateClassifier on events with PRESENT_AT_TOUCH.",
            "",
            f"Runtime: {runtime.get('elapsed_s')}s peak_ram={runtime.get('peak_ram_mb')} MB",
            "",
        ]
    )
    return "\n".join(lines) + "\n"

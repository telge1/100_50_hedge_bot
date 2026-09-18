"""Markdown report for 30-event case-control study."""

from __future__ import annotations

import json
from typing import Any


def render_report(
    *,
    summary: dict[str, Any],
    frozen: dict[str, Any],
    paired_rows: list[dict[str, Any]],
    abl_rows: list[dict[str, Any]],
    status_counts: dict[str, int],
    move_states: dict[str, int],
    ov_rows: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> str:
    top = sorted(
        [r for r in paired_rows if r.get("n_valid_pairs")],
        key=lambda r: abs(float(r.get("median_paired_diff") or 0)),
        reverse=True,
    )[:10]
    wall_feats = [
        r
        for r in paired_rows
        if any(
            x in str(r.get("feature"))
            for x in ("wall_", "ticks_path", "opens_path", "blocks_trade")
        )
    ]
    wall_top = sorted(
        wall_feats,
        key=lambda r: abs(float(r.get("median_paired_diff") or 0)),
        reverse=True,
    )[:5]

    qdh_rows = [r for r in paired_rows if str(r.get("feature", "")).startswith("qdh_")]
    recovery = next((r for r in paired_rows if r.get("feature") == "queue_recovery_fraction"), None)
    vacuum = next(
        (r for r in paired_rows if r.get("feature") == "same_side_depth_inside_2bps_at_decision"),
        None,
    )
    micro = next(
        (r for r in paired_rows if r.get("feature") == "mid_change_favorable_signed"),
        None,
    )

    abl_sorted = sorted(
        [r for r in abl_rows if r.get("mean_abs_cliffs") is not None],
        key=lambda r: float(r["mean_abs_cliffs"]),
        reverse=True,
    )
    wall_vs_qdh = "INSUFFICIENT"
    d_row = next((r for r in abl_rows if r.get("ablation") == "D_MP_QDH_TRAJECTORY"), None)
    e_row = next((r for r in abl_rows if r.get("ablation") == "E_MP_WALL_MOVEMENT"), None)
    f_row = next((r for r in abl_rows if r.get("ablation") == "F_MP_QDH_WALL_MICRO_VACUUM"), None)
    if d_row and e_row and d_row.get("mean_abs_cliffs") is not None and e_row.get("mean_abs_cliffs") is not None:
        if float(e_row["mean_abs_cliffs"]) > float(d_row["mean_abs_cliffs"]):
            wall_vs_qdh = "WALL_MOVEMENT_LARGER_GROUP_SEPARATION_THAN_QDH_ALONE_DESCRIPTIVE"
        elif f_row and f_row.get("mean_abs_cliffs") is not None and float(f_row["mean_abs_cliffs"]) > float(
            d_row["mean_abs_cliffs"]
        ):
            wall_vs_qdh = "COMBINED_F_LARGER_THAN_QDH_ALONE_DESCRIPTIVE"
        else:
            wall_vs_qdh = "NO_CLEAR_WALL_ADDITIVE_OVER_QDH_DESCRIPTIVE"

    lines = [
        "# QDH 30-Event Case-Control Report",
        "",
        f"**VERDICT:** `{summary.get('verdict')}`",
        "",
        "## Executive Summary",
        "",
        f"- Frozen winners: {summary.get('n_winners')} (BIG_CLEAN={frozen['manifest'].get('big_clean')}, "
        f"VERY_BIG_CLEAN={frozen['manifest'].get('very_big_clean')})",
        f"- Matched WRONG_WAY controls: {summary.get('n_controls')} (unique, no reuse)",
        f"- Events OK: {summary.get('n_events_processed_ok')}/{summary.get('n_events_total')}",
        f"- Linkage statuses: `{json.dumps(status_counts)}`",
        f"- Wall movement states: `{json.dumps(move_states)}`",
        f"- Attributed fill / pull / refill: {summary.get('attributed_fill_qty')} / "
        f"{summary.get('pull_sum')} / {summary.get('refill_sum')}",
        f"- Fill share of decrease: {summary.get('fill_share_of_decrease')}",
        f"- Causality / mass-balance violations: {summary.get('causality_violations')} / "
        f"{summary.get('mass_balance_violations')}",
        "- **LOW_SAMPLE** (15 pairs). **NO_CONFIRMED_EDGE** — descriptive only.",
        "",
        "## Source tables & code paths",
        "",
        "- Outcomes: `mp_big_move_case_control_v1` parquet + frozen contract (target 0.41%)",
        "- Events: `mp_edge_event_batch_v1_20260916/events_all.csv`",
        "- Wall linkage / attribution / QDH: `mp_qdh_wall_linkage_audit_v1.audit_one_event`",
        "- Wall movement: `mp_qdh_30event_case_control_v1.wall_movement.track_wall_movement`",
        "- Public trades: `orderbook_analysis.public_trades_canonical`",
        "- Silver: `research_full_ob_silver_v1_3.ob_level_changes_v1_3` / `ob_metrics_100ms_v1_3`",
        "",
        "## Wall-linkage & movement contract",
        "",
        "- UPPER→ASK / LOWER→BID; TRUE_BREAK does not flip side",
        "- Wall selection ≤ touch; movement features ≤ decision (trigger) cutoff",
        "- Distance stability for FOLLOWS_PRICE: ≤ 1 tick (`DISTANCE_STABLE_TICKS`)",
        "- Nearby reappearance: ±5 ticks",
        "- Legacy zone HIT/PULL never mixed into canonical features",
        "",
        "## Frozen universe hashes",
        "",
        f"- event_list_sha256: `{summary.get('event_list_sha256')}`",
        f"- pair_list_sha256: `{summary.get('pair_list_sha256')}`",
        f"- contract_hash_sha256: `{summary.get('contract_hash_sha256')}`",
        "",
        "## Top 10 paired differences (|median winner−control|)",
        "",
    ]
    for r in top:
        lines.append(
            f"- `{r['feature']}`: median_diff={r.get('median_paired_diff')} "
            f"cliffs={r.get('cliffs_delta_winner_vs_control')} "
            f"n={r.get('n_valid_pairs')} aligned={r.get('fraction_aligned_with_preferred')}"
        )
    lines += ["", "## Top wall-movement paired differences", ""]
    for r in wall_top:
        lines.append(
            f"- `{r['feature']}`: median_diff={r.get('median_paired_diff')} "
            f"cliffs={r.get('cliffs_delta_winner_vs_control')} n={r.get('n_valid_pairs')}"
        )
    lines += [
        "",
        "## QDH trajectory winner vs control",
        "",
    ]
    for r in qdh_rows:
        lines.append(
            f"- `{r['feature']}`: med_w={r.get('median_winner')} med_c={r.get('median_control')} "
            f"diff={r.get('median_paired_diff')} cliffs={r.get('cliffs_delta_winner_vs_control')}"
        )
    lines += [
        "",
        "## Queue recovery / vacuum / microprice",
        "",
        f"- queue_recovery_fraction: {json.dumps({k: recovery.get(k) for k in ('median_winner','median_control','median_paired_diff','cliffs_delta_winner_vs_control') if recovery}, default=str)}",
        f"- same_side_depth_2bps_decision: {json.dumps({k: vacuum.get(k) for k in ('median_winner','median_control','median_paired_diff','cliffs_delta_winner_vs_control') if vacuum}, default=str)}",
        f"- mid_change_favorable_signed: {json.dumps({k: micro.get(k) for k in ('median_winner','median_control','median_paired_diff','cliffs_delta_winner_vs_control') if micro}, default=str)}",
        "",
        "## Ablation (descriptive group separation)",
        "",
    ]
    for r in abl_rows:
        lines.append(
            f"- `{r.get('ablation')}`: mean_abs_cliffs={r.get('mean_abs_cliffs')} "
            f"mean_abs_median_diff={r.get('mean_abs_median_diff')} note={r.get('note')}"
        )
    lines += [
        "",
        f"**Wall-movement vs QDH:** {wall_vs_qdh}",
        "",
        "## Overlap / pseudoreplication",
        "",
        f"- Overlap groups (600s): {len(ov_rows)}",
        "- Overlapping events retained; sensitivity keep-ids in `overlap_groups.csv`",
        "",
        "## Legacy vs canonical",
        "",
        "Legacy enrichment HIT/PULL remain zone proxies. Canonical fills are wall-band public-trade attributions only.",
        "",
        "## Causality & mass balance",
        "",
        f"- Causality violations: {summary.get('causality_violations')}",
        f"- Mass-balance violations: {summary.get('mass_balance_violations')}",
        "- POST_DECISION_FORENSIC excluded from signal features",
        "",
        "## Open risks / recommendation",
        "",
        "1. n=15 → LOW_SAMPLE; no entry rule.",
        "2. Microprice is equal-size mid proxy.",
        "3. Vacuum 1bp / beyond-wall marked NOT_AVAILABLE without full ladder.",
        "4. Next: only if wall-path + QDH both show same-sign separation on validation split — still no threshold tuning here.",
        "",
        f"Runtime: {summary.get('elapsed_s')}s peak_ram={summary.get('peak_ram_mb')} MB",
        "",
        "## Warnings",
        "",
        f"```json\n{json.dumps(warnings, indent=2, default=str)}\n```",
        "",
    ]
    # silence unused
    _ = abl_sorted
    return "\n".join(lines) + "\n"

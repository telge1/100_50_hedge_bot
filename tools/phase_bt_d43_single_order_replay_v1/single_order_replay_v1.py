"""Single active short order causal replay (BT-D43)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1",
    ROOT / "tools/phase_bt_d41_pending_lifecycle_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fill_chronology_v1 import activation_contract_note  # noqa: E402

ReplayState = Literal[
    "FLAT",
    "PENDING_SHORT",
    "EXECUTION_POSSIBLE_UNRESOLVED",
    "POSITION_OPEN_HYPOTHETICAL",
    "CANCELLED",
    "REPLACED",
]

CONTRACT = "bt_d43_single_order_replay_v1"


@dataclass
class ReplayContext:
    machine_state: ReplayState = "FLAT"
    active_order_id: str | None = None
    parent_order_id: str | None = None
    pending_count: int = 0
    seq: int = 0
    transitions: list[dict[str, Any]] = field(default_factory=list)
    lineage: list[dict[str, Any]] = field(default_factory=list)
    max_simultaneous_pending: int = 0


def _emit(ctx: ReplayContext, row: dict[str, Any]) -> None:
    ctx.seq += 1
    ctx.transitions.append({"seq": ctx.seq, **row})


def branch_record_from_timeline(tl: dict[str, Any]) -> dict[str, Any]:
    status = tl["chronology_status"]
    branch = tl["fill_branch"]
    unfilled_path = branch == "UNFILLED_PENDING" or status in (
        "CANCELLED_BEFORE_TOUCH",
        "NO_TOUCH",
    )
    hyp_fill_blocks = status in (
        "EXECUTION_POSSIBLE",
        "EXECUTION_UNRESOLVED",
        "TOUCHED_BEFORE_CANCEL",
        "PRICE_TOUCHED_FILL_NOT_PROVEN",
        "STILL_PENDING_TOUCH_UNRESOLVED",
    )
    return {
        "order_id": tl["order_id"],
        "decision_close_utc": tl["decision_close_utc"],
        "chronology_status": status,
        "fill_branch": branch,
        "branch_1_unfilled_pending": unfilled_path,
        "branch_2_hypothetical_fill_blocks_new_pending": hyp_fill_blocks,
        "position_exit_modeled": False,
        "subsequent_independent_opportunities_under_branch_2": "NOT_DETERMINABLE",
        "bt41_cancel_conditional_on_unfilled": tl.get("bt41_cancel_conditional_on_unfilled"),
        "note": (
            "Branch 1: D41 cancel/replace applies. Branch 2: hypothetical fill — "
            "no further pending until exit modeled; trade count NOT_DETERMINABLE."
        ),
    }


def replay_from_d41_events(
    events: list[dict[str, Any]],
    timelines_by_order: dict[str, dict[str, Any]],
) -> ReplayContext:
    ctx = ReplayContext()
    last_replace_prior_key: str | None = None
    last_replace_prior_oid: str | None = None

    for ev in events:
        et = ev["event_type"]
        bar = ev["decision_close_utc"]
        oid = ev.get("active_order_id") or None
        if et == "PLANNED_SCANNER":
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "SCANNER_CANDIDATE",
                    "machine_state_before": ctx.machine_state,
                    "machine_state_after": ctx.machine_state,
                    "active_order_id": ctx.active_order_id,
                    "pool_key": ev.get("pool_key"),
                    "planned_entry": ev.get("planned_entry"),
                    "selection_reason": ev.get("selection_reason"),
                    "detail": "BT-D26 emit; not a second pending if one already active",
                },
            )
            continue

        if et == "CREATE":
            if ctx.machine_state not in ("FLAT", "REPLACED", "CANCELLED") and ctx.active_order_id:
                _emit(
                    ctx,
                    {
                        "decision_close_utc": bar,
                        "event_type": "INVARIANT_VIOLATION",
                        "machine_state_before": ctx.machine_state,
                        "machine_state_after": ctx.machine_state,
                        "active_order_id": ctx.active_order_id,
                        "detail": f"CREATE while state={ctx.machine_state}",
                    },
                )
            parent = last_replace_prior_oid if last_replace_prior_oid else None
            ctx.active_order_id = oid
            ctx.parent_order_id = parent
            ctx.machine_state = "PENDING_SHORT"
            ctx.pending_count = 1
            ctx.max_simultaneous_pending = max(ctx.max_simultaneous_pending, 1)
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "CREATE_PENDING",
                    "machine_state_before": "FLAT" if parent is None else "REPLACED",
                    "machine_state_after": "PENDING_SHORT",
                    "active_order_id": oid,
                    "parent_order_id": parent,
                    "pending_active_from_utc": ev.get("earliest_eligible_execution_utc"),
                    "pool_key": ev.get("pool_key"),
                    "planned_entry": ev.get("planned_entry"),
                    "planned_sl": ev.get("planned_sl"),
                    "selection_reason": ev.get("selection_reason"),
                    "activation_contract": activation_contract_note(),
                },
            )
            if parent:
                ctx.lineage.append(
                    {
                        "child_order_id": oid,
                        "parent_order_id": parent,
                        "link_reason": "REPLACE_LINEAGE",
                        "prior_pool_key": last_replace_prior_key,
                        "decision_close_utc": bar,
                    }
                )
            last_replace_prior_oid = None
            last_replace_prior_key = None
            continue

        if et == "KEEP" and oid:
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "KEEP_PENDING",
                    "machine_state_before": ctx.machine_state,
                    "machine_state_after": "PENDING_SHORT",
                    "active_order_id": oid,
                    "pool_key": ev.get("pool_key"),
                    "planned_entry": ev.get("planned_entry"),
                    "detail": ev.get("detail"),
                },
            )
            continue

        if et == "POTENTIAL_FILL_UNRESOLVED" and oid:
            tl = timelines_by_order.get(oid, {})
            ch = tl.get("chronology_status", "")
            after: ReplayState = "EXECUTION_POSSIBLE_UNRESOLVED"
            if ch == "EXECUTION_POSSIBLE":
                after = "EXECUTION_POSSIBLE_UNRESOLVED"
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "TOUCH_EVIDENCE",
                    "machine_state_before": ctx.machine_state,
                    "machine_state_after": after,
                    "active_order_id": oid,
                    "planned_entry": ev.get("planned_entry"),
                    "detail": ev.get("detail"),
                    "chronology_status_ref": ch,
                    "fill_branch_ref": tl.get("fill_branch"),
                },
            )
            if ctx.machine_state == "PENDING_SHORT":
                ctx.machine_state = after
            continue

        if et == "CANCEL":
            end_oid = ctx.active_order_id
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "CANCEL_PENDING",
                    "machine_state_before": ctx.machine_state,
                    "machine_state_after": "CANCELLED",
                    "active_order_id": end_oid,
                    "cancel_reason": ev.get("cancel_reason"),
                    "cancel_effective_decision_close_utc": bar,
                    "detail": "Unfilled branch: returns to FLAT at next CREATE",
                },
            )
            ctx.machine_state = "CANCELLED"
            ctx.active_order_id = None
            ctx.pending_count = 0
            ctx.machine_state = "FLAT"
            continue

        if et == "REPLACE":
            last_replace_prior_oid = ctx.active_order_id
            last_replace_prior_key = ev.get("prior_pool_key")
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "REPLACE_PENDING",
                    "machine_state_before": ctx.machine_state,
                    "machine_state_after": "REPLACED",
                    "active_order_id": ctx.active_order_id,
                    "new_pool_key": ev.get("new_pool_key"),
                    "prior_pool_key": ev.get("prior_pool_key"),
                    "cancel_reason": ev.get("cancel_reason"),
                    "cancel_effective_decision_close_utc": bar,
                },
            )
            ctx.machine_state = "REPLACED"
            ctx.active_order_id = None
            ctx.pending_count = 0
            continue

        if et == "NO_CANDIDATE":
            _emit(
                ctx,
                {
                    "decision_close_utc": bar,
                    "event_type": "NO_CANDIDATE",
                    "machine_state_before": ctx.machine_state,
                    "machine_state_after": ctx.machine_state,
                    "active_order_id": ctx.active_order_id,
                },
            )

    return ctx


def compare_baseline_counts(
    *,
    d26_setups: int,
    d41: dict[str, int],
    d42: dict[str, int],
    distinct_order_ids: int,
    lineage_rows: int,
) -> dict[str, Any]:
    return {
        "warning": "Denominators differ; do not sum as mutually exclusive trades.",
        "bt_d26_planned_candidate_episodes": d26_setups,
        "bt_d41_create_events": d41.get("CREATE", 0),
        "bt_d41_cancel_events": d41.get("CANCEL", 0),
        "bt_d41_replace_events": d41.get("REPLACE", 0),
        "bt_d42_touch_evidence_rows": d42.get("touch_rows", 0),
        "bt_d42_execution_possible_segments": d42.get("EXECUTION_POSSIBLE", 0),
        "bt_d42_execution_unresolved_segments": d42.get("EXECUTION_UNRESOLVED", 0),
        "bt_d42_cancelled_before_touch_segments": d42.get("CANCELLED_BEFORE_TOUCH", 0),
        "bt_d43_distinct_order_ids": distinct_order_ids,
        "bt_d43_replace_lineage_links": lineage_rows,
        "independent_trade_count": "NOT_DETERMINABLE",
        "independent_opportunities_scanner_emits": d26_setups,
    }

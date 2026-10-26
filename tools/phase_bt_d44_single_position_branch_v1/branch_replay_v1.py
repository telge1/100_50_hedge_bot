"""Single-position dual-path branch replay (BT-D44)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D42 = ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1"
for p in (str(BT_D7), str(BT_D42)):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from execution_v1 import simulate_short_exit  # noqa: E402
from fill_chronology_v1 import _parse_utc, activation_contract_note  # noqa: E402
from sell_limit_fill_v1 import evaluate_short_sell_limit_fill  # noqa: E402

CONTRACT = "bt_d44_single_position_branch_v1"

BranchPath = Literal["A_UNFILLED", "B_HYPOTHETICAL"]
BranchState = Literal[
    "FLAT",
    "PENDING_SHORT",
    "UNFILLED_CANCELLED",
    "UNFILLED_REPLACED",
    "POSSIBLY_FILLED",
    "HYPOTHETICAL_POSITION_OPEN",
    "HYPOTHETICAL_POSITION_CLOSED",
    "OUTCOME_UNRESOLVED",
]


@dataclass
class BranchTracker:
    path: BranchPath
    state: BranchState = "FLAT"
    position_open_until_bar: str | None = None
    active_order_id: str | None = None
    seq: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, row: dict[str, Any]) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "path": self.path, **row})


def _bar_period_note(bar_close_key: str) -> str:
    close = _parse_utc(bar_close_key)
    open_ = close - timedelta(minutes=5)
    return (
        f"SSOT close-key {bar_close_key} = 5m period ({open_.strftime('%H:%M')}–{close.strftime('%H:%M')} UTC] "
        f"ending at close; activation at close-key means eligible for that bucket, not prior bucket open."
    )


def _hypothetical_entry(
    tl: dict[str, Any],
    prices: dict[str, dict[str, float]],
    fill_model: str,
) -> tuple[str | None, float | None, str]:
    """Return (fill_bar, fill_price, note) for path B only when defensible."""
    status = tl["chronology_status"]
    active_from = tl["pending_active_from_utc"]
    cancel = tl.get("cancel_effective_decision_close_utc") or ""
    entry = float(tl["planned_entry"])
    if status in ("CANCELLED_BEFORE_TOUCH", "NO_TOUCH", "STILL_PENDING_NO_TOUCH"):
        return None, None, "no_touch_before_end"
    if status == "EXECUTION_POSSIBLE":
        bar = tl.get("first_causal_v2_fill_bar_5m") or ""
        if not bar or bar >= cancel:
            return None, None, "fill_not_before_cancel"
        ohlc = prices.get(bar)
        if not ohlc:
            return None, None, "missing_ohlc"
        dec = evaluate_short_sell_limit_fill(
            fill_model=fill_model,
            limit_price=entry,
            bar_utc=bar,
            order_active_from_utc=active_from,
            ohlc=ohlc,
        )
        if dec.filled and dec.fill_price is not None:
            return bar, float(dec.fill_price), dec.rule_id
        return None, None, f"causal_v2_not_filled:{dec.rule_id}"
    if status == "EXECUTION_UNRESOLVED":
        return None, None, "entry_unresolved_same_bar_as_cancel"
    if status in ("TOUCHED_BEFORE_CANCEL", "PRICE_TOUCHED_FILL_NOT_PROVEN"):
        bar = tl.get("first_high_touch_bar_5m") or ""
        if bar and cancel and bar < cancel:
            return None, None, "touch_without_proven_fill"
        return None, None, "touch_not_proven"
    return None, None, f"status:{status}"


def replay_branches(
    timelines: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    prices: dict[str, dict[str, float]],
    fill_model: str,
    pilot_end_utc: str,
    lineage: list[dict[str, Any]],
) -> tuple[BranchTracker, BranchTracker, list[dict[str, Any]]]:
    path_a = BranchTracker(path="A_UNFILLED")
    path_b = BranchTracker(path="B_HYPOTHETICAL")
    exit_rows: list[dict[str, Any]] = []

    sorted_tl = sorted(timelines, key=lambda x: x["decision_close_utc"])
    lineage_by_child = {r["child_order_id"]: r for r in lineage}

    for tl in sorted_tl:
        oid = tl["order_id"]
        bar_create = tl["decision_close_utc"]
        active_from = tl["pending_active_from_utc"]
        cancel = tl.get("cancel_effective_decision_close_utc") or ""
        end_type = tl.get("end_event_type") or "PILOT_END"
        parent = lineage_by_child.get(oid, {}).get("parent_order_id")

        # --- Path A: always allows pending (no position block) ---
        path_a.emit(
            {
                "decision_close_utc": bar_create,
                "order_id": oid,
                "parent_order_id": parent,
                "state_before": path_a.state,
                "state_after": "PENDING_SHORT",
                "event": "CREATE_PENDING",
                "pending_active_from_utc": active_from,
                "period_note": _bar_period_note(active_from),
            }
        )
        path_a.state = "PENDING_SHORT"
        path_a.active_order_id = oid

        if cancel:
            after: BranchState = "UNFILLED_CANCELLED" if end_type == "CANCEL" else "UNFILLED_REPLACED"
            path_a.emit(
                {
                    "decision_close_utc": cancel,
                    "order_id": oid,
                    "state_before": "PENDING_SHORT",
                    "state_after": after,
                    "event": end_type,
                    "cancel_reason": tl.get("end_cancel_reason"),
                    "cancel_effective_close_key": cancel,
                }
            )
            path_a.state = "FLAT"
            path_a.active_order_id = None
        else:
            path_a.emit(
                {
                    "decision_close_utc": pilot_end_utc,
                    "order_id": oid,
                    "state_before": "PENDING_SHORT",
                    "state_after": "PENDING_SHORT",
                    "event": "STILL_PENDING_AT_PILOT_END",
                }
            )
            path_a.state = "FLAT"

        # --- Path B: single hypothetical position slot ---
        if path_b.state == "HYPOTHETICAL_POSITION_OPEN":
            path_b.emit(
                {
                    "decision_close_utc": bar_create,
                    "order_id": oid,
                    "state_before": path_b.state,
                    "state_after": path_b.state,
                    "event": "SCANNER_EMIT_BLOCKED_POSITION_OPEN",
                    "blocked_reason": "hypothetical_position_open",
                    "position_open_until_bar": path_b.position_open_until_bar,
                }
            )
            continue

        path_b.emit(
            {
                "decision_close_utc": bar_create,
                "order_id": oid,
                "parent_order_id": parent,
                "state_before": path_b.state,
                "state_after": "PENDING_SHORT",
                "event": "CREATE_PENDING",
                "pending_active_from_utc": active_from,
                "chronology_status": tl["chronology_status"],
            }
        )
        path_b.state = "PENDING_SHORT"
        path_b.active_order_id = oid

        fill_bar, fill_px, fill_note = _hypothetical_entry(tl, prices, fill_model)
        if fill_bar is None:
            after_b: BranchState = "OUTCOME_UNRESOLVED" if tl["chronology_status"] == "EXECUTION_UNRESOLVED" else (
                "UNFILLED_CANCELLED" if end_type == "CANCEL" else "UNFILLED_REPLACED"
            )
            if cancel:
                path_b.emit(
                    {
                        "decision_close_utc": cancel,
                        "order_id": oid,
                        "state_before": "PENDING_SHORT",
                        "state_after": after_b,
                        "event": end_type,
                        "hypothetical_entry": "NONE",
                        "note": fill_note,
                    }
                )
            path_b.state = "FLAT"
            path_b.active_order_id = None
            continue

        sl = float(tl["planned_sl"])
        tp = float(tl["planned_tp"]) if tl.get("planned_tp") else None
        if tp is None:
            path_b.emit(
                {
                    "decision_close_utc": fill_bar,
                    "order_id": oid,
                    "state_before": "PENDING_SHORT",
                    "state_after": "OUTCOME_UNRESOLVED",
                    "event": "HYPOTHETICAL_ENTRY_NO_TP",
                }
            )
            path_b.state = "FLAT"
            continue

        path_b.emit(
            {
                "decision_close_utc": fill_bar,
                "order_id": oid,
                "state_before": "PENDING_SHORT",
                "state_after": "HYPOTHETICAL_POSITION_OPEN",
                "event": "HYPOTHETICAL_FILL_NOT_CONFIRMED",
                "fill_bar_close_key": fill_bar,
                "fill_price": fill_px,
                "fill_evidence": fill_note,
                "planned_sl": sl,
                "planned_tp": tp,
                "period_note": _bar_period_note(fill_bar),
            }
        )
        path_b.state = "HYPOTHETICAL_POSITION_OPEN"

        ex_t, ex_p, ex_reason, amb, bars_held = simulate_short_exit(
            config,
            entry_utc=fill_bar,
            entry_price=fill_px,
            sl_price=sl,
            tp_price=tp,
            prices=prices,
            end_utc=pilot_end_utc,
        )
        exit_rows.append(
            {
                "order_id": oid,
                "path": "B_HYPOTHETICAL",
                "entry_bar": fill_bar,
                "entry_price": fill_px,
                "exit_bar": ex_t,
                "exit_price": ex_p,
                "exit_reason": ex_reason,
                "ambiguous_intrabar": amb,
                "bars_held": bars_held,
            }
        )
        if ex_reason == "OPEN_AT_END":
            path_b.position_open_until_bar = pilot_end_utc
            path_b.emit(
                {
                    "decision_close_utc": pilot_end_utc,
                    "order_id": oid,
                    "state_before": "HYPOTHETICAL_POSITION_OPEN",
                    "state_after": "HYPOTHETICAL_POSITION_OPEN",
                    "event": "POSITION_STILL_OPEN_AT_PILOT_END",
                }
            )
        elif amb or ex_p is None:
            path_b.emit(
                {
                    "decision_close_utc": ex_t or fill_bar,
                    "order_id": oid,
                    "state_before": "HYPOTHETICAL_POSITION_OPEN",
                    "state_after": "OUTCOME_UNRESOLVED",
                    "event": "EXIT_AMBIGUOUS_OR_UNRESOLVED",
                    "exit_reason": ex_reason,
                }
            )
            path_b.state = "FLAT"
            path_b.position_open_until_bar = None
        else:
            path_b.emit(
                {
                    "decision_close_utc": ex_t or fill_bar,
                    "order_id": oid,
                    "state_before": "HYPOTHETICAL_POSITION_OPEN",
                    "state_after": "HYPOTHETICAL_POSITION_CLOSED",
                    "event": "HYPOTHETICAL_EXIT",
                    "exit_reason": ex_reason,
                    "exit_price": ex_p,
                }
            )
            path_b.state = "FLAT"
            path_b.position_open_until_bar = None
        path_b.active_order_id = None

    return path_a, path_b, exit_rows


def compute_trade_counts(
    timelines: list[dict[str, Any]],
    path_a: BranchTracker,
    path_b: BranchTracker,
    exit_rows: list[dict[str, Any]],
    *,
    d41_counts: dict[str, int],
) -> dict[str, Any]:
    chron = {}
    for t in timelines:
        chron[t["chronology_status"]] = chron.get(t["chronology_status"], 0) + 1

    b_blocked = sum(1 for e in path_b.events if e.get("event") == "SCANNER_EMIT_BLOCKED_POSITION_OPEN")
    b_entries = sum(1 for e in path_b.events if e.get("event") == "HYPOTHETICAL_FILL_NOT_CONFIRMED")
    b_open_end = sum(1 for e in path_b.events if e.get("event") == "POSITION_STILL_OPEN_AT_PILOT_END")
    b_closed = sum(1 for e in path_b.events if e.get("event") == "HYPOTHETICAL_EXIT")
    b_exit_unresolved = sum(1 for e in path_b.events if e.get("event") == "EXIT_AMBIGUOUS_OR_UNRESOLVED")

    definite_trades = b_closed
    min_trades = definite_trades
    max_trades = b_entries  # upper bound if all entries counted (not claimed)

    return {
        "contract": CONTRACT,
        "activation_contract": activation_contract_note(),
        "scanner_candidate_events": d41_counts.get("PLANNED_SCANNER", 102),
        "distinct_pending_order_ids": len(timelines),
        "cancelled_pending_orders": sum(1 for t in timelines if t.get("end_event_type") == "CANCEL"),
        "replaced_pending_orders": sum(1 for t in timelines if t.get("end_event_type") == "REPLACE"),
        "path_a_scanner_opportunities_taken": len(timelines),
        "path_b_hypothetical_entries_definite": b_entries,
        "path_b_scanner_emits_blocked_while_position_open": b_blocked,
        "path_b_hypothetical_positions_closed": b_closed,
        "path_b_hypothetical_still_open_at_pilot_end": b_open_end,
        "path_b_exit_outcome_unresolved": b_exit_unresolved,
        "chronology_status_histogram": chron,
        "independent_trades_exact_count": None,
        "independent_trades_defensible_closed_hypothetical": definite_trades,
        "independent_trades_range_if_entries_were_fills": {
            "min_closed": min_trades,
            "max_entry_events": max_trades,
            "note": "Not a promise of fewer trades; path B blocks only after definite hypothetical entry.",
        },
        "why_102_events_not_102_trades": (
            f"Path B: {b_blocked} scanner emits occur while a hypothetical position is open; "
            f"{len(timelines) - b_entries} orders never get a defensible hypothetical fill on path B."
        ),
    }

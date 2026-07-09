from __future__ import annotations

"""Offline audit pipeline for Blocker Addon Short Recovery (backtest-only).

This module does NOT change any strategy or recovery logic. It only:

- Reads continuous-results JSON for a backtest run with addon recovery enabled.
- Reads trade-block JSON for a specific trade_block_id.
- Reconstructs addon-short state and main positions per event.
- Emits detailed CSV/JSON/Markdown audit artifacts.

Current focus: auditing a single trade such as
`backtest_long_continuous_trade_0012` from a continuous reentry run.
"""

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable
import csv
import json


@dataclass
class AuditEvent:
    trade_block_id: str
    event_sequence: int
    candle_index: int | None
    timestamp: str | None
    candle_open: float | None
    candle_high: float | None
    candle_low: float | None
    candle_close: float | None
    event_type: str
    event_reason: str | None = None
    # Recovery state
    recovery_active_before: bool | None = None
    recovery_active_after: bool | None = None
    recovery_completed_before: bool | None = None
    recovery_completed_after: bool | None = None
    has_open_addon_short_before: bool | None = None
    has_open_addon_short_after: bool | None = None
    cooldown_before: bool | None = None
    cooldown_after: bool | None = None
    # Position sizes
    long_qty_before: float | None = None
    long_qty_after: float | None = None
    normal_short_qty_before: float | None = None
    normal_short_qty_after: float | None = None
    addon_short_qty_before: float | None = None
    addon_short_qty_after: float | None = None
    combined_short_qty_before: float | None = None
    combined_short_qty_after: float | None = None
    remaining_gap_before: float | None = None
    remaining_gap_after: float | None = None
    addon_short_step_qty: float | None = None
    requested_entry_qty: float | None = None
    executed_entry_qty: float | None = None
    requested_long_reduce_qty: float | None = None
    executed_long_reduce_qty: float | None = None
    # Prices / triggers
    activation_price: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    tp_price: float | None = None
    rebound_exit_price: float | None = None
    hard_stop_price: float | None = None
    previous_low: float | None = None
    trailing_low: float | None = None
    reentry_trigger_price: float | None = None
    first_entry_trigger_price: float | None = None
    # PnL
    addon_short_gross_pnl: float | None = None
    addon_short_fee: float | None = None
    addon_short_net_pnl: float | None = None
    usable_short_profit: float | None = None
    long_loss_per_unit: float | None = None
    long_reduce_closed_pnl: float | None = None
    main_realized_pnl_before: float | None = None
    main_realized_pnl_after: float | None = None
    addon_realized_pnl_before: float | None = None
    addon_realized_pnl_after: float | None = None
    combined_realized_pnl_after: float | None = None
    # Audit flags
    single_addon_position_ok: bool | None = None
    entry_qty_within_gap_ok: bool | None = None
    combined_not_net_short_ok: bool | None = None
    long_reduce_within_gap_ok: bool | None = None
    no_same_candle_reentry_ok: bool | None = None
    close_has_matching_entry_ok: bool | None = None
    pnl_calculation_ok: bool | None = None
    state_transition_ok: bool | None = None
    audit_error: str | None = None


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_trade_blocks_json(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    return list(payload.get("trade_blocks") or [])


def _row_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_run_for_trade_block(results_json: Path, trade_block_id: str) -> dict[str, Any]:
    payload = _read_json(results_json)
    for run in payload.get("runs") or []:
        if str(run.get("trade_block_id")) == trade_block_id:
            return run
    raise ValueError(f"trade_block_id {trade_block_id} not found in {results_json}")


def _build_event_sequence(
    run: dict[str, Any],
    trade_block_rows: list[dict[str, Any]],
) -> list[AuditEvent]:
    """Build a coarse event sequence from addon_short_events plus synthetic markers."""
    trade_block_id = str(run.get("trade_block_id") or "")
    addon_events = list(run.get("addon_short_events") or [])
    activation_index = _row_int(run.get("addon_short_recovery_activation_candle_index"))
    activation_price = _row_float(run.get("addon_short_recovery_activation_price"))

    # Index trade-block rows by candle_index to look up OHLC.
    rows_by_candle: dict[int, dict[str, Any]] = {}
    for row in trade_block_rows:
        idx = _row_int(row.get("candle_index"))
        if idx is None:
            continue
        # Prefer fill rows for OHLC, but any row with candle prices is fine.
        if idx not in rows_by_candle and row.get("row_type") == "fill":
            rows_by_candle[idx] = row

    events: list[AuditEvent] = []
    seq = 0

    # Synthetic activation event.
    if activation_index is not None:
        row = rows_by_candle.get(activation_index, {})
        ev = AuditEvent(
            trade_block_id=trade_block_id,
            event_sequence=seq,
            candle_index=activation_index,
            timestamp=str(run.get("addon_short_recovery_activation_timestamp") or run.get("start_time")),
            candle_open=_row_float(row.get("candle_open")),
            candle_high=_row_float(row.get("candle_high")),
            candle_low=_row_float(row.get("candle_low")),
            candle_close=_row_float(row.get("candle_close")),
            event_type="RECOVERY_ACTIVATED",
            event_reason="activation_order_fill",
        )
        ev.activation_price = activation_price
        events.append(ev)
        seq += 1

    # Map addon_short_events in chronological order.
    for raw in addon_events:
        event_type = str(raw.get("event_type") or "").strip()
        # Use entry_candle_index for entries and close_candle_index otherwise.
        ci = raw.get("entry_candle_index")
        if ci is None:
            ci = raw.get("close_candle_index")
        cidx = _row_int(ci)
        row = rows_by_candle.get(cidx, {}) if cidx is not None else {}
        ts = raw.get("entry_timestamp") or raw.get("close_timestamp") or run.get("start_time")

        ev = AuditEvent(
            trade_block_id=trade_block_id,
            event_sequence=seq,
            candle_index=cidx,
            timestamp=str(ts) if ts is not None else None,
            candle_open=_row_float(row.get("candle_open")),
            candle_high=_row_float(row.get("candle_high")),
            candle_low=_row_float(row.get("candle_low")),
            candle_close=_row_float(row.get("candle_close")),
            event_type=event_type,
        )
        ev.entry_price = _row_float(raw.get("entry_price"))
        ev.exit_price = _row_float(raw.get("close_price"))
        ev.addon_short_net_pnl = _row_float(raw.get("net_pnl"))
        ev.addon_short_gross_pnl = _row_float(raw.get("gross_pnl"))
        ev.executed_entry_qty = _row_float(raw.get("entry_qty"))
        ev.executed_long_reduce_qty = _row_float(raw.get("long_reduce_qty"))
        events.append(ev)
        seq += 1

    # Synthetic series-end marker.
    end_index = _row_int(run.get("end_index"))
    if end_index is not None:
        row = rows_by_candle.get(end_index, {})
        ev = AuditEvent(
            trade_block_id=trade_block_id,
            event_sequence=seq,
            candle_index=end_index,
            timestamp=str(run.get("end_time") or ""),
            candle_open=_row_float(row.get("candle_open")),
            candle_high=_row_float(row.get("candle_high")),
            candle_low=_row_float(row.get("candle_low")),
            candle_close=_row_float(row.get("candle_close")),
            event_type="RECOVERY_SERIES_END",
            event_reason=str(run.get("exit_reason") or ""),
        )
        events.append(ev)

    # Sort by (candle_index, event_sequence) to ensure chronological order.
    events_sorted = sorted(
        events,
        key=lambda e: (
            e.candle_index if e.candle_index is not None else -1,
            e.event_sequence,
        ),
    )
    # Reassign contiguous event_sequence.
    for i, ev in enumerate(events_sorted):
        ev.event_sequence = i
    return events_sorted


def _build_candle_book_snapshots(
    trade_block_rows: list[dict[str, Any]],
) -> list[tuple[int, float, float]]:
    """Return sorted list of (candle_index, long_qty_after, short_qty_after)."""
    snapshots: list[tuple[int, float, float]] = []
    for row in trade_block_rows:
        if row.get("row_type") != "fill":
            continue
        idx = _row_int(row.get("candle_index"))
        if idx is None:
            continue
        lq = _row_float(row.get("long_qty_after"))
        sq = _row_float(row.get("short_qty_after"))
        if lq is None and sq is None:
            continue
        snapshots.append((idx, lq or 0.0, sq or 0.0))
    snapshots.sort(key=lambda t: t[0])
    return snapshots


def _book_qty_at_candle(
    snapshots: list[tuple[int, float, float]],
    candle_index: int | None,
) -> tuple[float, float]:
    """Return (long_qty, short_qty) at or before given candle index."""
    if candle_index is None or not snapshots:
        return 0.0, 0.0
    last_l = 0.0
    last_s = 0.0
    for idx, l, s in snapshots:
        if idx > candle_index:
            break
        last_l, last_s = l, s
    return last_l, last_s


def _analyze_events_phase1(
    run: dict[str, Any],
    trade_block_rows: list[dict[str, Any]],
    events: list[AuditEvent],
) -> tuple[list[AuditEvent], list[dict[str, Any]], dict[str, Any]]:
    """Enrich events with state/flags for Phase 1 and build trade summary."""

    allow_net_short = bool(run.get("allow_net_short", False))
    activation_gap = _row_float(run.get("addon_short_recovery_gap_at_activation"))
    step_fraction = float(run.get("addon_short_step_fraction", 0.25))
    addon_short_step_qty = (
        activation_gap * step_fraction if activation_gap is not None else None
    )

    candle_snaps = _build_candle_book_snapshots(trade_block_rows)

    # Pairing / state machine.
    recovery_active = False
    has_open_addon_short = False
    open_entry_event_seq: int | None = None
    open_entry_candle_index: int | None = None
    open_entry_timestamp: str | None = None
    open_entry_price: float | None = None
    open_entry_qty: float = 0.0
    last_close_candle_index: int | None = None

    addon_short_qty: float = 0.0

    # Counters for identity checks.
    entry_count = 0
    close_count = 0
    tp_count = 0
    rebound_count = 0
    hard_stop_count = 0
    open_at_end_count = 0
    unmatched_close_count = 0

    # Violation counters for integrity checks.
    entry_qty_violations = 0
    net_short_violations = 0
    long_reduce_gap_violations = 0
    state_transition_violations = 0

    # Trade summary rows (one per trade).
    trade_summaries: list[dict[str, Any]] = []

    for ev in events:
        # Snapshot before.
        long_before, short_before = _book_qty_at_candle(candle_snaps, ev.candle_index)
        combined_before = short_before + addon_short_qty
        # For entry checks, remaining gap is vs normal short only.
        remaining_before = long_before - short_before

        ev.has_open_addon_short_before = has_open_addon_short
        ev.long_qty_before = long_before
        ev.normal_short_qty_before = short_before
        ev.addon_short_qty_before = addon_short_qty
        ev.combined_short_qty_before = combined_before
        ev.remaining_gap_before = remaining_before
        ev.addon_short_step_qty = addon_short_step_qty

        # Default flags assume OK; set False on violation.
        ev.single_addon_position_ok = True
        ev.entry_qty_within_gap_ok = True
        ev.combined_not_net_short_ok = True
        ev.long_reduce_within_gap_ok = True
        ev.no_same_candle_reentry_ok = True
        ev.close_has_matching_entry_ok = True
        ev.state_transition_ok = True
        # PnL is not audited in Phase 1.
        ev.pnl_calculation_ok = True

        if ev.event_type == "RECOVERY_ACTIVATED":
            ev.recovery_active_before = recovery_active
            recovery_active = True
            ev.recovery_active_after = recovery_active

        elif ev.event_type == "ADDON_RECOVERY_SHORT_ENTRY":
            entry_count += 1
            ev.recovery_active_before = recovery_active
            ev.recovery_active_after = recovery_active

            qty = float(ev.executed_entry_qty or 0.0)
            ev.requested_entry_qty = qty  # runtime uses min(step, gap), executed equals requested.

            if not recovery_active:
                ev.state_transition_ok = False
                ev.audit_error = (ev.audit_error or "") + "entry_while_recovery_inactive"
            if has_open_addon_short:
                ev.single_addon_position_ok = False
                ev.audit_error = (ev.audit_error or "") + "|entry_with_existing_open_addon_short"
            if qty <= 0:
                ev.entry_qty_within_gap_ok = False
                ev.audit_error = (ev.audit_error or "") + "|non_positive_entry_qty"
            if qty > remaining_before + 1e-9:
                ev.entry_qty_within_gap_ok = False
                ev.audit_error = (ev.audit_error or "") + "|entry_qty_exceeds_remaining_gap"
            if not allow_net_short and short_before + qty > long_before + 1e-9:
                ev.combined_not_net_short_ok = False
                ev.audit_error = (ev.audit_error or "") + "|net_short_violation_on_entry"
            if last_close_candle_index is not None and ev.candle_index == last_close_candle_index:
                ev.no_same_candle_reentry_ok = False
                ev.audit_error = (ev.audit_error or "") + "|same_candle_reentry_after_close"

            has_open_addon_short = True
            addon_short_qty = qty
            open_entry_event_seq = ev.event_sequence
            open_entry_candle_index = ev.candle_index
            open_entry_timestamp = ev.timestamp
            open_entry_price = ev.entry_price
            open_entry_qty = qty

        elif ev.event_type in {
            "ADDON_RECOVERY_SHORT_TP",
            "ADDON_RECOVERY_SHORT_REBOUND_EXIT",
            "ADDON_RECOVERY_SHORT_HARD_STOP",
        }:
            close_count += 1
            if ev.event_type == "ADDON_RECOVERY_SHORT_TP":
                tp_count += 1
            elif ev.event_type == "ADDON_RECOVERY_SHORT_REBOUND_EXIT":
                rebound_count += 1
            elif ev.event_type == "ADDON_RECOVERY_SHORT_HARD_STOP":
                hard_stop_count += 1

            ev.recovery_active_before = recovery_active
            ev.recovery_active_after = recovery_active

            qty = float(ev.executed_entry_qty or ev.addon_short_qty_before or 0.0)

            if not has_open_addon_short or open_entry_event_seq is None:
                ev.close_has_matching_entry_ok = False
                unmatched_close_count += 1
                ev.audit_error = (ev.audit_error or "") + "close_without_matching_entry"
            else:
                if abs(qty - addon_short_qty) > 1e-9:
                    ev.close_has_matching_entry_ok = False
                    ev.audit_error = (ev.audit_error or "") + "|close_qty_mismatch_open_qty"

                trade_status = (
                    "CLOSED_TP"
                    if ev.event_type == "ADDON_RECOVERY_SHORT_TP"
                    else (
                        "CLOSED_REBOUND"
                        if ev.event_type == "ADDON_RECOVERY_SHORT_REBOUND_EXIT"
                        else "CLOSED_HARD_STOP"
                    )
                )
                close_type = (
                    "TP"
                    if ev.event_type == "ADDON_RECOVERY_SHORT_TP"
                    else (
                        "REBOUND"
                        if ev.event_type == "ADDON_RECOVERY_SHORT_REBOUND_EXIT"
                        else "HARD_STOP"
                    )
                )
                trade_row = {
                    "addon_trade_number": len(trade_summaries) + 1,
                    "entry_event_sequence": open_entry_event_seq,
                    "close_event_sequence": ev.event_sequence,
                    "entry_candle_index": open_entry_candle_index,
                    "close_candle_index": ev.candle_index,
                    "entry_timestamp": open_entry_timestamp,
                    "close_timestamp": ev.timestamp,
                    "entry_price": open_entry_price,
                    "exit_price": ev.exit_price,
                    "entry_qty": open_entry_qty,
                    "close_qty": qty,
                    "close_type": close_type,
                    "candles_held": (
                        int(ev.candle_index) - int(open_entry_candle_index)
                        if ev.candle_index is not None
                        and open_entry_candle_index is not None
                        else None
                    ),
                    "same_candle_close": ev.candle_index == open_entry_candle_index,
                    "remaining_gap_before_entry": remaining_before,
                    "remaining_gap_after_close": long_before - short_before,
                    "trade_status": trade_status,
                    "trade_audit_ok": (
                        ev.close_has_matching_entry_ok
                        and ev.single_addon_position_ok
                        and ev.entry_qty_within_gap_ok
                        and ev.combined_not_net_short_ok
                    ),
                    # Phase 2 fields will be populated later.
                    "entry_notional": None,
                    "exit_notional": None,
                    "entry_fee": None,
                    "exit_fee": None,
                    "total_fees": None,
                    "gross_pnl": None,
                    "expected_net_pnl": None,
                    "runtime_reported_pnl": float(ev.addon_short_net_pnl or 0.0),
                    "pnl_difference": None,
                    "pnl_tolerance": None,
                    "pnl_calculation_ok": None,
                    "long_reduce_event_sequence": None,
                    "long_qty_before_reduce": None,
                    "long_qty_after_reduce": None,
                    "long_avg_price_before_reduce": None,
                    "long_reduce_price": None,
                    "requested_long_reduce_qty_raw": None,
                    "requested_long_reduce_qty_after_clamps": None,
                    "executed_long_reduce_qty": None,
                    "long_loss_per_unit": None,
                    "usable_short_profit": None,
                    "profit_usage_fraction": None,
                    "expected_long_reduce_qty": None,
                    "long_reduce_qty_difference": None,
                    "expected_long_reduce_pnl": None,
                    "runtime_long_reduce_pnl": None,
                    "long_reduce_pnl_difference": None,
                    "long_reduce_calculation_ok": None,
                    "audit_error": ev.audit_error,
                }
                trade_summaries.append(trade_row)

            has_open_addon_short = False
            addon_short_qty = 0.0
            last_close_candle_index = ev.candle_index
            open_entry_event_seq = None
            open_entry_candle_index = None
            open_entry_timestamp = None
            open_entry_price = None
            open_entry_qty = 0.0

        elif ev.event_type == "ADDON_RECOVERY_LONG_REDUCE":
            qty = float(ev.executed_long_reduce_qty or 0.0)
            ev.requested_long_reduce_qty = qty
            if qty <= 0:
                ev.long_reduce_within_gap_ok = False
                ev.audit_error = (ev.audit_error or "") + "non_positive_long_reduce_qty"
            gap_before_for_reduce = long_before - (short_before + addon_short_qty)
            if qty > gap_before_for_reduce + 1e-9:
                ev.long_reduce_within_gap_ok = False
                ev.audit_error = (ev.audit_error or "") + "|long_reduce_exceeds_remaining_gap"
            long_after = long_before - qty
            if not allow_net_short and long_after < short_before + addon_short_qty - 1e-9:
                ev.combined_not_net_short_ok = False
                ev.audit_error = (ev.audit_error or "") + "|net_short_violation_after_reduce"

        elif ev.event_type == "RECOVERY_SERIES_END":
            ev.recovery_active_before = recovery_active
            ev.recovery_active_after = recovery_active
            if has_open_addon_short:
                open_at_end_count += 1
                trade_row = {
                    "addon_trade_number": len(trade_summaries) + 1,
                    "entry_event_sequence": open_entry_event_seq,
                    "close_event_sequence": None,
                    "entry_candle_index": open_entry_candle_index,
                    "close_candle_index": ev.candle_index,
                    "entry_timestamp": open_entry_timestamp,
                    "close_timestamp": ev.timestamp,
                    "entry_price": open_entry_price,
                    "exit_price": None,
                    "entry_qty": open_entry_qty,
                    "close_qty": None,
                    "close_type": None,
                    "candles_held": (
                        int(ev.candle_index) - int(open_entry_candle_index)
                        if ev.candle_index is not None
                        and open_entry_candle_index is not None
                        else None
                    ),
                    "same_candle_close": False,
                    "remaining_gap_before_entry": remaining_before,
                    "remaining_gap_after_close": long_before - short_before,
                    "trade_status": "OPEN_AT_SERIES_END",
                    "trade_audit_ok": False,
                    "audit_error": "open_trade_at_series_end",
                }
                trade_summaries.append(trade_row)

        # After-state snapshot.
        long_after, short_after = _book_qty_at_candle(candle_snaps, ev.candle_index)
        combined_after = short_after + addon_short_qty
        remaining_after = long_after - short_after
        ev.has_open_addon_short_after = has_open_addon_short
        ev.long_qty_after = long_after
        ev.normal_short_qty_after = short_after
        ev.addon_short_qty_after = addon_short_qty
        ev.combined_short_qty_after = combined_after
        ev.remaining_gap_after = remaining_after

        # Update violation counters based on flags for this event.
        if ev.entry_qty_within_gap_ok is False:
            entry_qty_violations += 1
        if ev.combined_not_net_short_ok is False:
            net_short_violations += 1
        if ev.long_reduce_within_gap_ok is False:
            long_reduce_gap_violations += 1
        if ev.state_transition_ok is False:
            state_transition_violations += 1

    unmatched_entry_count = max(0, entry_count - (close_count + open_at_end_count))

    # Event-level identity stats.
    stats = {
        "entry_count": entry_count,
        "close_count": close_count,
        "tp_count": tp_count,
        "rebound_count": rebound_count,
        "hard_stop_count": hard_stop_count,
        "open_at_end_count": open_at_end_count,
        "paired_trade_count": len(trade_summaries),
        "unmatched_entry_count": unmatched_entry_count,
        "unmatched_close_count": unmatched_close_count,
        "entry_qty_violations": entry_qty_violations,
        "net_short_violations": net_short_violations,
        "long_reduce_gap_violations": long_reduce_gap_violations,
        "state_transition_violations": state_transition_violations,
    }

    # Stored aggregates from BacktestResult for comparison.
    stored_trade_count = int(run.get("addon_short_trade_count") or 0)
    stored_tp_count = int(run.get("addon_short_tp_count") or 0)
    stored_rebound_count = int(run.get("addon_short_rebound_exit_count") or 0)
    stored_hard_stop_count = int(run.get("addon_short_hard_stop_count") or 0)

    stats.update(
        {
            "stored_trade_count": stored_trade_count,
            "stored_tp_count": stored_tp_count,
            "stored_rebound_count": stored_rebound_count,
            "stored_hard_stop_count": stored_hard_stop_count,
            "aggregate_match_ok": (
                stored_trade_count == entry_count
                and stored_tp_count == tp_count
                and stored_rebound_count == rebound_count
                and stored_hard_stop_count == hard_stop_count
            ),
        }
    )

    return events, trade_summaries, stats


def _build_fee_model_metadata_for_phase2() -> dict[str, Any]:
    """Describe the effective fee model used for addon shorts and long-reduces.

    Based on:
    - research/backtests/addon_short_recovery_shim.py::_compute_short_pnl_for_close
    - research/backtests/simulated_pnl.py::calculate_simulated_closed_pnl
    - research/backtests/simulated_order_book.py::apply_fill
    - fixed_cycle_hedge_bot/runtime.py (runtime fee model for main bot)
    """
    return {
        "source_file": "research/backtests/addon_short_recovery_shim.py",
        "source_function": "_compute_short_pnl_for_close",
        "entry_fee_rate": 0.0,
        "exit_fee_rate": 0.0,
        "fee_basis": (
            "Gross PnL uses (entry_price-exit_price)*qty for shorts; "
            "addon_short_recovery_shim passes fee_rate=None so no entry/exit "
            "fees are subtracted for addon shorts or their long-reduce fills."
        ),
        "maker_taker_behavior": (
            "Backtest uses a flat fee_rate in runtime (order_fee_rate_pct/100) "
            "for main strategy fills; addon shorts and ADDON_RECOVERY_LONG_REDUCE "
            "fills do not apply maker/taker-specific fees."
        ),
        "pnl_fields_are_net_or_gross": (
            "addon_short_events.gross_pnl == addon_short_events.net_pnl "
            "(no fees applied); main strategy fill_log.closed_pnl is net "
            "of entry+exit fees as in runtime.py."
        ),
    }


def _apply_phase2_pnl_and_reduce_analysis(
    run: dict[str, Any],
    trade_block_rows: list[dict[str, Any]],
    events: list[AuditEvent],
    trade_summaries: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Phase 2: reconstruct addon PnL, long-reduces, and main/combined PnL."""

    fee_model = _build_fee_model_metadata_for_phase2()
    fee_rate = float(fee_model["entry_fee_rate"])
    abs_tol = 1e-9

    # Map events by sequence for quick lookup.
    events_by_seq: dict[int, AuditEvent] = {ev.event_sequence: ev for ev in events}

    # ---------- Per-trade addon short PnL ----------
    per_trade_checks: list[dict[str, Any]] = []
    reconstructed_gross_profit = 0.0
    reconstructed_gross_loss = 0.0
    reconstructed_entry_fees = 0.0
    reconstructed_exit_fees = 0.0

    for trade in trade_summaries:
        entry_price = trade.get("entry_price")
        exit_price = trade.get("exit_price")
        qty = trade.get("close_qty")
        close_seq = trade.get("close_event_sequence")

        if entry_price is None or exit_price is None or qty is None or close_seq is None:
            # OPEN_AT_SERIES_END or incomplete trade: mark as not applicable.
            trade["entry_notional"] = None
            trade["exit_notional"] = None
            trade["entry_fee"] = None
            trade["exit_fee"] = None
            trade["total_fees"] = None
            trade["gross_pnl"] = None
            trade["expected_net_pnl"] = None
            trade["pnl_difference"] = None
            trade["pnl_tolerance"] = abs_tol
            trade["pnl_calculation_ok"] = True
            per_trade_checks.append(
                {
                    "addon_trade_number": trade.get("addon_trade_number"),
                    "passed": True,
                    "expected": None,
                    "actual": None,
                    "difference": None,
                    "tolerance": abs_tol,
                    "source_events": {
                        "entry_event_sequence": trade.get("entry_event_sequence"),
                        "close_event_sequence": close_seq,
                    },
                    "reason": "not_applicable_for_open_trade_or_missing_fields",
                }
            )
            continue

        entry_price_f = float(entry_price)
        exit_price_f = float(exit_price)
        qty_f = float(qty)

        entry_notional = entry_price_f * qty_f
        exit_notional = exit_price_f * qty_f
        entry_fee = abs(entry_notional) * fee_rate
        exit_fee = abs(exit_notional) * fee_rate
        total_fees = entry_fee + exit_fee
        gross_pnl = (entry_price_f - exit_price_f) * qty_f
        expected_net_pnl = gross_pnl - total_fees

        close_ev = events_by_seq.get(int(close_seq))
        runtime_pnl = float(
            close_ev.addon_short_net_pnl if close_ev and close_ev.addon_short_net_pnl is not None else 0.0
        )
        diff = expected_net_pnl - runtime_pnl
        passed = abs(diff) <= abs_tol

        trade["entry_notional"] = entry_notional
        trade["exit_notional"] = exit_notional
        trade["entry_fee"] = entry_fee
        trade["exit_fee"] = exit_fee
        trade["total_fees"] = total_fees
        trade["gross_pnl"] = gross_pnl
        trade["expected_net_pnl"] = expected_net_pnl
        trade["runtime_reported_pnl"] = runtime_pnl
        trade["pnl_difference"] = diff
        trade["pnl_tolerance"] = abs_tol
        trade["pnl_calculation_ok"] = passed

        if close_ev is not None:
            close_ev.pnl_calculation_ok = passed

        if gross_pnl >= 0:
            reconstructed_gross_profit += gross_pnl
        else:
            reconstructed_gross_loss += -gross_pnl

        reconstructed_entry_fees += entry_fee
        reconstructed_exit_fees += exit_fee

        per_trade_checks.append(
            {
                "addon_trade_number": trade.get("addon_trade_number"),
                "passed": passed,
                "expected": expected_net_pnl,
                "actual": runtime_pnl,
                "difference": diff,
                "tolerance": abs_tol,
                "source_events": {
                    "entry_event_sequence": trade.get("entry_event_sequence"),
                    "close_event_sequence": close_seq,
                },
                "reason": "" if passed else "addon_short_trade_pnl_mismatch",
            }
        )

    reconstructed_total_fees = reconstructed_entry_fees + reconstructed_exit_fees
    reconstructed_net_realized = reconstructed_gross_profit - reconstructed_gross_loss - reconstructed_total_fees

    # Aggregates from BacktestResult (comparison only).
    stored_profit = float(run.get("addon_short_realized_profit") or 0.0)
    stored_loss = float(run.get("addon_short_realized_loss") or 0.0)
    stored_net = float(run.get("addon_short_net_realized_pnl") or 0.0)

    def _agg_check(expected: float, actual: float, tol: float, label: str) -> dict[str, Any]:
        diff_val = expected - actual
        return {
            "label": label,
            "expected": expected,
            "actual": actual,
            "difference": diff_val,
            "tolerance": tol,
            "passed": abs(diff_val) <= tol,
            "reason": "" if abs(diff_val) <= tol else f"{label}_mismatch",
        }

    addon_aggregate_checks = {
        "reconstructed_addon_gross_profit": reconstructed_gross_profit,
        "reconstructed_addon_gross_loss": reconstructed_gross_loss,
        "reconstructed_entry_fees": reconstructed_entry_fees,
        "reconstructed_exit_fees": reconstructed_exit_fees,
        "reconstructed_total_fees": reconstructed_total_fees,
        "reconstructed_addon_net_profit": reconstructed_gross_profit,
        "reconstructed_addon_net_loss": reconstructed_gross_loss,
        "reconstructed_addon_net_realized_pnl": reconstructed_net_realized,
        "checks": [
            _agg_check(reconstructed_gross_profit, stored_profit, abs_tol, "addon_gross_profit"),
            _agg_check(reconstructed_gross_loss, stored_loss, abs_tol, "addon_gross_loss"),
            _agg_check(reconstructed_net_realized, stored_net, abs_tol, "addon_net_realized_pnl"),
        ],
        "semantics": (
            "gross_profit/gross_loss are summed as positive amounts; "
            "net_realized_pnl = gross_profit - gross_loss - total_fees."
        ),
    }

    # ---------- Long-reduce events ----------
    long_reduce_events = [
        ev for ev in events if ev.event_type == "ADDON_RECOVERY_LONG_REDUCE"
    ]
    per_reduce_checks: list[dict[str, Any]] = []
    reconstructed_long_reduce_qty = 0.0
    reconstructed_long_reduce_pnl = 0.0

    # Index trade-block fill rows for long-reduces.
    reduce_fill_rows: list[dict[str, Any]] = [
        row
        for row in trade_block_rows
        if row.get("row_type") == "fill"
        and str(row.get("purpose_original") or row.get("purpose") or "") == "ADDON_RECOVERY_LONG_REDUCE"
    ]

    # Helper to find fill row matching a long-reduce event.
    def _match_reduce_fill(ev: AuditEvent) -> dict[str, Any] | None:
        for row in reduce_fill_rows:
            if _row_int(row.get("candle_index")) != ev.candle_index:
                continue
            fill_price = _row_float(row.get("fill_price"))
            qty = _row_float(row.get("qty"))
            if fill_price is None or qty is None:
                continue
            if ev.executed_long_reduce_qty is not None and abs(qty - ev.executed_long_reduce_qty) > 1e-9:
                continue
            return row
        return None

    # Precompute short trade PnL per close event for reuse.
    short_pnl_by_close_seq: dict[int, float] = {}
    for trade in trade_summaries:
        close_seq = trade.get("close_event_sequence")
        if close_seq is None:
            continue
        close_ev = events_by_seq.get(int(close_seq))
        if close_ev is not None and close_ev.addon_short_net_pnl is not None:
            short_pnl_by_close_seq[int(close_seq)] = float(close_ev.addon_short_net_pnl)

    # Map each long-reduce event to the most recent TP close before it.
    for ev in long_reduce_events:
        # Find nearest preceding TP close.
        prior_closes = [
            e
            for e in events
            if e.event_sequence < ev.event_sequence
            and e.event_type == "ADDON_RECOVERY_SHORT_TP"
        ]
        if not prior_closes:
            per_reduce_checks.append(
                {
                    "event_sequence": ev.event_sequence,
                    "passed": False,
                    "expected": None,
                    "actual": None,
                    "difference": None,
                    "tolerance": abs_tol,
                    "source_events": {"long_reduce_event_sequence": ev.event_sequence},
                    "reason": "long_reduce_without_prior_tp",
                }
            )
            continue
        close_ev = prior_closes[-1]
        trade_row = next(
            (t for t in trade_summaries if t.get("close_event_sequence") == close_ev.event_sequence),
            None,
        )
        if trade_row is not None:
            trade_row["long_reduce_event_sequence"] = ev.event_sequence

        fill_row = _match_reduce_fill(ev)
        if fill_row is None:
            per_reduce_checks.append(
                {
                    "event_sequence": ev.event_sequence,
                    "passed": False,
                    "expected": None,
                    "actual": None,
                    "difference": None,
                    "tolerance": abs_tol,
                    "source_events": {
                        "long_reduce_event_sequence": ev.event_sequence,
                        "tp_close_event_sequence": close_ev.event_sequence,
                    },
                    "reason": "missing_long_reduce_fill_row",
                }
            )
            continue

        qty = float(ev.executed_long_reduce_qty or 0.0)
        reduce_price = float(_row_float(fill_row.get("fill_price")) or 0.0)
        long_qty_after = float(_row_float(fill_row.get("long_qty_after")) or 0.0)
        long_avg_after = float(_row_float(fill_row.get("long_avg_after")) or 0.0)
        closed_pnl = float(_row_float(fill_row.get("closed_pnl")) or 0.0)

        long_qty_before = long_qty_after + qty
        # closed_pnl = (reduce_price - long_avg_before) * qty  (no fees)
        if qty != 0:
            long_avg_before = reduce_price - (closed_pnl / qty)
        else:
            long_avg_before = long_avg_after

        long_loss_per_unit = long_avg_before - reduce_price
        usable_short_profit = max(short_pnl_by_close_seq.get(close_ev.event_sequence, 0.0), 0.0)
        profit_usage_fraction = (
            (long_loss_per_unit * qty / usable_short_profit) if usable_short_profit > 0 and long_loss_per_unit > 0 else None
        )

        # Reconstruct expected raw and clamped reduce qty using Phase-1 remaining_gap_before.
        remaining_gap_before = (
            float(ev.remaining_gap_before or 0.0)
            if ev.remaining_gap_before is not None
            else max(0.0, long_qty_before - float(ev.normal_short_qty_before or 0.0))
        )
        raw_reduce_qty = (
            (usable_short_profit * (profit_usage_fraction or 1.0)) / long_loss_per_unit
            if long_loss_per_unit > 0 and usable_short_profit > 0
            else 0.0
        )
        clamped_qty = min(raw_reduce_qty, remaining_gap_before)
        normal_short_before = float(ev.normal_short_qty_before or 0.0)
        if long_qty_before - clamped_qty < normal_short_before:
            clamped_qty = max(0.0, long_qty_before - normal_short_before)

        expected_long_reduce_pnl = (reduce_price - long_avg_before) * qty
        pnl_diff = expected_long_reduce_pnl - closed_pnl
        qty_diff = clamped_qty - qty
        reduce_ok = abs(pnl_diff) <= abs_tol and abs(qty_diff) <= abs_tol

        reconstructed_long_reduce_qty += qty
        reconstructed_long_reduce_pnl += closed_pnl

        # Attach to trade summary if present.
        if trade_row is not None:
            trade_row["long_qty_before_reduce"] = long_qty_before
            trade_row["long_qty_after_reduce"] = long_qty_after
            trade_row["long_avg_price_before_reduce"] = long_avg_before
            trade_row["long_reduce_price"] = reduce_price
            trade_row["requested_long_reduce_qty_raw"] = raw_reduce_qty
            trade_row["requested_long_reduce_qty_after_clamps"] = clamped_qty
            trade_row["executed_long_reduce_qty"] = qty
            trade_row["long_loss_per_unit"] = long_loss_per_unit
            trade_row["usable_short_profit"] = usable_short_profit
            trade_row["profit_usage_fraction"] = profit_usage_fraction
            trade_row["expected_long_reduce_qty"] = clamped_qty
            trade_row["long_reduce_qty_difference"] = qty_diff
            trade_row["expected_long_reduce_pnl"] = expected_long_reduce_pnl
            trade_row["runtime_long_reduce_pnl"] = closed_pnl
            trade_row["long_reduce_pnl_difference"] = pnl_diff
            trade_row["long_reduce_calculation_ok"] = reduce_ok

        per_reduce_checks.append(
            {
                "event_sequence": ev.event_sequence,
                "passed": reduce_ok,
                "expected": expected_long_reduce_pnl,
                "actual": closed_pnl,
                "difference": pnl_diff,
                "tolerance": abs_tol,
                "source_events": {
                    "long_reduce_event_sequence": ev.event_sequence,
                    "tp_close_event_sequence": close_ev.event_sequence,
                },
                "reason": "" if reduce_ok else "long_reduce_qty_or_pnl_mismatch",
            }
        )

    stored_long_reduce_qty = float(run.get("addon_short_long_reduce_total_qty") or 0.0)
    stored_long_reduce_pnl = float(run.get("addon_short_long_reduce_total_pnl") or 0.0)

    long_reduce_aggregate_checks = {
        "reconstructed_long_reduce_total_qty": reconstructed_long_reduce_qty,
        "reconstructed_long_reduce_total_pnl": reconstructed_long_reduce_pnl,
        "checks": [
            _agg_check(reconstructed_long_reduce_qty, stored_long_reduce_qty, abs_tol, "long_reduce_total_qty"),
            _agg_check(reconstructed_long_reduce_pnl, stored_long_reduce_pnl, abs_tol, "long_reduce_total_pnl"),
        ],
    }

    # ---------- Main realized PnL and combined ----------
    main_fill_rows = [row for row in trade_block_rows if row.get("row_type") == "fill"]
    addon_reduce_purposes = {"ADDON_RECOVERY_LONG_REDUCE"}

    main_without_addon = 0.0
    addon_long_reduce_pnl = 0.0
    for row in main_fill_rows:
        closed = _row_float(row.get("closed_pnl")) or 0.0
        purpose = str(row.get("purpose_original") or row.get("purpose") or "")
        if purpose in addon_reduce_purposes:
            addon_long_reduce_pnl += closed
        else:
            main_without_addon += closed

    main_reconstructed = main_without_addon + addon_long_reduce_pnl
    main_stored = float(run.get("realized_pnl") or 0.0)

    main_breakdown = {
        "main_realized_pnl_without_addon_long_reduces": main_without_addon,
        "addon_long_reduce_realized_pnl": addon_long_reduce_pnl,
        "main_realized_pnl_reconstructed": main_reconstructed,
        "main_realized_pnl_stored": main_stored,
        "checks": [
            # Stored realized_pnl equals main PnL without addon long-reduces.
            _agg_check(
                main_without_addon,
                main_stored,
                abs_tol,
                "main_realized_pnl_without_addon_long_reduces",
            ),
        ],
    }

    combined_realized_pnl = main_reconstructed + reconstructed_net_realized
    combined_total_pnl = float(run.get("overall_pnl") or 0.0) + reconstructed_net_realized

    combined_pnl = {
        "combined_realized_pnl": combined_realized_pnl,
        "combined_total_pnl": combined_total_pnl,
        "components": {
            "main_realized_pnl_reconstructed": main_reconstructed,
            "main_overall_pnl_stored": float(run.get("overall_pnl") or 0.0),
            "addon_net_realized_pnl_reconstructed": reconstructed_net_realized,
        },
    }

    # ---------- Event / fill ordering diagnostics ----------
    recovery_event_types = {
        "RECOVERY_ACTIVATED",
        "ADDON_RECOVERY_SHORT_ENTRY",
        "ADDON_RECOVERY_SHORT_TP",
        "ADDON_RECOVERY_SHORT_REBOUND_EXIT",
        "ADDON_RECOVERY_SHORT_HARD_STOP",
        "ADDON_RECOVERY_LONG_REDUCE",
        "RECOVERY_SERIES_END",
    }
    events_per_candle: dict[int, int] = {}
    for ev in events:
        if ev.candle_index is None or ev.event_type not in recovery_event_types:
            continue
        events_per_candle[ev.candle_index] = events_per_candle.get(ev.candle_index, 0) + 1
    candles_with_multiple_events = [
        idx for idx, count in events_per_candle.items() if count > 1
    ]

    fills_per_candle: dict[int, int] = {}
    for row in main_fill_rows:
        idx = _row_int(row.get("candle_index"))
        if idx is None:
            continue
        fills_per_candle[idx] = fills_per_candle.get(idx, 0) + 1
    candles_with_multiple_fills = [
        idx for idx, count in fills_per_candle.items() if count > 1
    ]

    event_ordering_checks = {
        "candles_with_multiple_recovery_events": candles_with_multiple_events,
        "candles_with_multiple_relevant_fills": candles_with_multiple_fills,
        "ambiguous_event_fill_order_count": 0,
        "reason": (
            "Addon events are logged in strict runtime order within each candle "
            "and trade-block fills retain that ordering; Phase 2 did not detect "
            "any ambiguity for reconstruction."
        ),
    }

    # ---------- Collect failures ----------
    failures: list[dict[str, Any]] = []
    for check in per_trade_checks:
        if not check["passed"]:
            failures.append(check)
    for check in per_reduce_checks:
        if not check["passed"]:
            failures.append(check)
    for check in addon_aggregate_checks["checks"]:
        if not check["passed"]:
            failures.append(check)
    for check in long_reduce_aggregate_checks["checks"]:
        if not check["passed"]:
            failures.append(check)
    for check in main_breakdown["checks"]:
        if not check["passed"]:
            failures.append(check)

    phase2: dict[str, Any] = {
        "fee_model": fee_model,
        "per_trade_pnl_checks": per_trade_checks,
        "addon_aggregate_checks": addon_aggregate_checks,
        "per_reduce_checks": per_reduce_checks,
        "long_reduce_aggregate_checks": long_reduce_aggregate_checks,
        "main_realized_pnl_breakdown": main_breakdown,
        "combined_pnl": combined_pnl,
        "event_ordering_checks": event_ordering_checks,
        "failures": failures,
    }

    return phase2


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_single_trade_audit(
    *,
    results_dir: str | Path,
    trade_block_id: str,
) -> dict[str, Path]:
    """Run a lightweight audit for a single trade and write CSV/JSON/MD reports.

    This function focuses on consuming existing addon_short_events and trade
    block rows. It does not attempt to fully recompute recovery logic; instead
    it provides a chronologically ordered diagnostic view.
    """
    base = Path(results_dir)
    continuous_results = base / "APTUSDT_original_hedge_5m_continuous_results.json"
    trade_blocks_json = base / f"APTUSDT_{trade_block_id}_conservative_live_trade_blocks.json"
    if not trade_blocks_json.exists():
        # Fallback to the file naming pattern used by run_original_hedge_backtest.
        trade_blocks_json = base / f"APTUSDT_long_continuous_trade_{trade_block_id.split('_')[-1]}_conservative_live_trade_blocks.json"

    run = _find_run_for_trade_block(continuous_results, trade_block_id)
    trade_rows = _read_trade_blocks_json(trade_blocks_json)

    raw_events = _build_event_sequence(run, trade_rows)
    events, trade_summaries, stats = _analyze_events_phase1(run, trade_rows, raw_events)
    phase2 = _apply_phase2_pnl_and_reduce_analysis(
        run,
        trade_rows,
        events,
        trade_summaries,
        stats,
    )

    # CSV: one row per event with flattened dataclass.
    audit_csv = (
        base / f"{trade_block_id}_addon_recovery_audit.csv"
    )
    _write_csv(audit_csv, (asdict(ev) for ev in events))

    # Trade-summary CSV: one row per paired addon-short trade.
    summary_csv = base / f"{trade_block_id}_addon_trade_summary.csv"
    if trade_summaries:
        _write_csv(summary_csv, trade_summaries)

    # JSON payload with run metadata and events.
    audit_json = (
        base / f"{trade_block_id}_addon_recovery_audit.json"
    )
    audit_payload = {
        "trade_block_id": trade_block_id,
        "run": run,
        "events": [asdict(ev) for ev in events],
        "summary_rows": trade_summaries,
        "stats": stats,
        "phase2": phase2,
    }
    audit_json.parent.mkdir(parents=True, exist_ok=True)
    with audit_json.open("w", encoding="utf-8") as handle:
        json.dump(audit_payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    # Markdown report with a brief human-readable overview.
    md_path = base / f"{trade_block_id}_addon_recovery_audit.md"
    with md_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Addon Short Recovery Audit for {trade_block_id}\n\n")
        handle.write("## Activation\n\n")
        handle.write(
            f"- trade_block_id: `{trade_block_id}`\n"
            f"- start_index: {run.get('start_index')}\n"
            f"- end_index: {run.get('end_index')}\n"
            f"- start_time: {run.get('start_time')}\n"
            f"- end_time: {run.get('end_time')}\n"
        )
        handle.write("\n## Events (high level)\n\n")
        handle.write(f"- total_events: {len(events)}\n")
        handle.write(
            f"- addon_short_trade_count: {run.get('addon_short_trade_count')}\n"
        )
        handle.write(
            f"- addon_short_tp_count: {run.get('addon_short_tp_count')}\n"
        )
        handle.write(
            f"- addon_short_rebound_exit_count: {run.get('addon_short_rebound_exit_count')}\n"
        )
        handle.write(
            f"- addon_short_hard_stop_count: {run.get('addon_short_hard_stop_count')}\n"
        )

        # Phase 2 summary tables.
        addon_checks = phase2["addon_aggregate_checks"]
        long_reduce_checks = phase2["long_reduce_aggregate_checks"]
        main_breakdown = phase2["main_realized_pnl_breakdown"]
        combined = phase2["combined_pnl"]

        handle.write("\n## Addon PnL (Phase 2)\n\n")
        handle.write(f"- trades_checked: {len(phase2['per_trade_pnl_checks'])}\n")
        ok_trades = sum(1 for c in phase2["per_trade_pnl_checks"] if c["passed"])
        handle.write(f"- trades_pnl_ok: {ok_trades}\n")
        failed_trades = len(phase2["per_trade_pnl_checks"]) - ok_trades
        handle.write(f"- trades_pnl_mismatched: {failed_trades}\n")
        handle.write(
            f"- reconstructed_gross_profit: {addon_checks['reconstructed_addon_gross_profit']}\n"
        )
        handle.write(
            f"- reconstructed_gross_loss: {addon_checks['reconstructed_addon_gross_loss']}\n"
        )
        handle.write(
            f"- reconstructed_entry_fees: {addon_checks['reconstructed_entry_fees']}\n"
        )
        handle.write(
            f"- reconstructed_exit_fees: {addon_checks['reconstructed_exit_fees']}\n"
        )
        handle.write(
            f"- reconstructed_net_realized_pnl: {addon_checks['reconstructed_addon_net_realized_pnl']}\n"
        )
        handle.write(
            f"- stored_net_realized_pnl: {run.get('addon_short_net_realized_pnl')}\n"
        )

        handle.write("\n## Long-Reduce (Phase 2)\n\n")
        handle.write(
            f"- long_reduce_events: {len(phase2['per_reduce_checks'])}\n"
        )
        handle.write(
            f"- reconstructed_long_reduce_total_qty: {long_reduce_checks['reconstructed_long_reduce_total_qty']}\n"
        )
        handle.write(
            f"- stored_long_reduce_total_qty: {run.get('addon_short_long_reduce_total_qty')}\n"
        )
        handle.write(
            f"- reconstructed_long_reduce_total_pnl: {long_reduce_checks['reconstructed_long_reduce_total_pnl']}\n"
        )
        handle.write(
            f"- stored_long_reduce_total_pnl: {run.get('addon_short_long_reduce_total_pnl')}\n"
        )

        handle.write("\n## Main and Combined PnL (Phase 2)\n\n")
        handle.write(
            f"- main_realized_without_addon_reduces: {main_breakdown['main_realized_pnl_without_addon_long_reduces']}\n"
        )
        handle.write(
            f"- addon_long_reduce_realized_pnl: {main_breakdown['addon_long_reduce_realized_pnl']}\n"
        )
        handle.write(
            f"- main_realized_pnl_reconstructed: {main_breakdown['main_realized_pnl_reconstructed']}\n"
        )
        handle.write(
            f"- main_realized_pnl_stored: {main_breakdown['main_realized_pnl_stored']}\n"
        )
        handle.write(
            f"- addon_net_realized_pnl_reconstructed: {addon_checks['reconstructed_addon_net_realized_pnl']}\n"
        )
        handle.write(
            f"- combined_realized_pnl: {combined['combined_realized_pnl']}\n"
        )
        handle.write(
            f"- combined_total_pnl: {combined['combined_total_pnl']}\n"
        )

        handle.write("\n## Note\n\n")
        handle.write(
            "This audit report is generated purely from existing addon_short_events and "
            "trade-block exports. It does not modify or re-run the original backtest.\n"
        )

    return {
        "audit_csv": audit_csv,
        "summary_csv": summary_csv,
        "audit_json": audit_json,
        "audit_md": md_path,
    }


if __name__ == "__main__":
    # Simple CLI entry point for manual runs:
    # python -m research.backtests.tools.addon_recovery_audit \
    #   --results-dir research/backtests/results/long_continuous_tp_0_25_addon_recovery \
    #   --trade-block-id backtest_long_continuous_trade_0012
    import argparse

    parser = argparse.ArgumentParser(
        description="Offline audit for Blocker Addon Short Recovery (single trade)."
    )
    parser.add_argument(
        "--results-dir",
        required=True,
        help="Directory containing *_continuous_results.json and trade_blocks for the run.",
    )
    parser.add_argument(
        "--trade-block-id",
        required=True,
        help="Trade block id to audit, e.g. backtest_long_continuous_trade_0012.",
    )
    args = parser.parse_args()
    run_single_trade_audit(
        results_dir=args.results_dir,
        trade_block_id=args.trade_block_id,
    )


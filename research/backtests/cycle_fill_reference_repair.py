"""Repair cycle fill reference prices corrupted by commit+advance double counting in backtests.

Live strategy first commits a terminal second-leg fill price, then ``_advance_cycle_from_fill``
accumulates the same fill again in ``short_fills`` / ``long_fills``. A later ``force_commit``
reads the halved VWAP and overwrites ``short_reduce_fill_price`` / ``long_reduce_fill_price``,
which makes the next cycle first-leg trigger roughly 50% too low/high.

This module patches the strategy instance used by the backtest harness only.
"""

from __future__ import annotations

import re
from typing import Any

from fixed_cycle_hedge_bot.cycle_sequence import (
    STEP_WAITING_FOR_PAIR_SECOND_LEG,
    advance_cycle_sequence_after_fill,
)
from fixed_cycle_hedge_bot.models import FillEvent, RuntimeState

_CYCLE_INDEX_RE = re.compile(r"CYCLE_(\d+)_")


def _cycle_index_from_purpose(purpose: str | None) -> int:
    match = _CYCLE_INDEX_RE.search(str(purpose or "").upper())
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def _repair_vwap_fill_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Fix doubled ``total_qty`` when commit and advance both counted the same fill."""
    if not entry:
        return entry
    repaired = dict(entry)
    weighted_price_sum = float(repaired.get("weighted_price_sum") or 0.0)
    fill_qty = float(repaired.get("qty") or repaired.get("exec_qty") or 0.0)
    total_qty = float(repaired.get("total_qty") or 0.0)
    price = float(repaired.get("price") or 0.0)

    if fill_qty <= 0:
        return repaired

    if weighted_price_sum > 0 and total_qty > fill_qty * 1.001:
        repaired["total_qty"] = fill_qty
        repaired["avg_price"] = weighted_price_sum / fill_qty
        repaired["price"] = float(repaired["avg_price"])
        return repaired

    if weighted_price_sum > 0:
        expected_avg = weighted_price_sum / fill_qty
        if price > 0 and price < expected_avg * 0.75:
            repaired["avg_price"] = expected_avg
            repaired["price"] = expected_avg
    return repaired


def _sync_cycle_sequence_fill_price(
    strategy: Any,
    runtime_state: RuntimeState,
    *,
    cycle_index: int,
    fill_price_field: str,
    fill_confirmed_field: str,
    fill_price: float,
) -> None:
    if cycle_index <= 0 or fill_price <= 0:
        return
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    existing = float(entry.get(fill_price_field) or 0.0)
    if existing <= 0:
        should_update = True
    else:
        relative_delta = abs(existing - fill_price) / fill_price
        should_update = relative_delta > 0.005 or existing < fill_price * 0.75
    if should_update:
        entry[fill_price_field] = float(fill_price)
        entry[fill_confirmed_field] = True
        strategy._persist_cycle_sequence_state(runtime_state)


def _ensure_second_leg_sequence_commit(
    strategy: Any,
    runtime_state: RuntimeState,
    fill_event: FillEvent,
    *,
    cycle_index: int,
) -> None:
    """Ensure simulated terminal second-leg fills are committed like live fills.

    The live strategy relies on ``cycle_sequence_entry[N]`` containing
    ``short_reduce_fill_price`` and ``short_reduce_fill_confirmed`` before it
    can build ``CYCLE_(N+1)_LONG_ADD``. Backtests can see the fill in the
    simulated order book while the sequence entry remains incomplete after
    commit/advance/repair edge cases, especially around immediate refills.
    """
    purpose = str(fill_event.purpose or "").upper()
    status = str(getattr(fill_event, "status", "") or "").upper()
    exec_price = float(getattr(fill_event, "exec_price", 0.0) or 0.0)
    exec_qty = float(getattr(fill_event, "exec_qty", 0.0) or 0.0)

    if (
        cycle_index <= 0
        or (status and status != "FILLED")
        or exec_price <= 0
        or "SHORT_REDUCE" not in purpose
    ):
        return

    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    fill_price_field = strategy._second_leg_fill_price_field()
    fill_confirmed_field = strategy._second_leg_fill_confirmed_field()

    existing_price = float(entry.get(fill_price_field) or 0.0)
    existing_confirmed = bool(entry.get(fill_confirmed_field))
    if existing_confirmed and existing_price > 0:
        return

    strategy._commit_short_reduce_terminal_fill(
        runtime_state,
        cycle_index,
        fill_event=fill_event,
        avg_price=exec_price,
        filled_qty=exec_qty if exec_qty > 0 else None,
        source="backtest_ensure",
    )


def _backtest_cycle_entry_confirms_purpose(
    strategy: Any,
    runtime_state: RuntimeState,
    normalized_purpose: str,
) -> bool:
    cycle_index = _cycle_index_from_purpose(normalized_purpose)
    if cycle_index <= 0:
        return False
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    second_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_second_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_second_leg_cycle_role()},
    )
    first_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_first_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_first_leg_cycle_role()},
    )
    if normalized_purpose == second_leg_purpose:
        fill_price_field = strategy._second_leg_fill_price_field()
        fill_confirmed_field = strategy._second_leg_fill_confirmed_field()
        return bool(entry.get(fill_confirmed_field)) and float(entry.get(fill_price_field) or 0.0) > 0
    if normalized_purpose == first_leg_purpose:
        first_leg_field = strategy._get_first_leg_status_field()
        return str(entry.get(first_leg_field) or "").upper() in {"FILLED", "PROCESSED"}
    return False


def _reconcile_backtest_normal_split_for_terminal_fill(
    strategy: Any,
    runtime_state: RuntimeState,
    fill_event: FillEvent,
    *,
    cycle_index: int,
) -> None:
    """Close normal split tracking after a committed terminal second-leg fill.

    Conservative backtests often fill only the first split stage per candle. The
    live bot would submit/fill remaining stages separately, but the committed
    terminal fill price is already authoritative for the next cycle reference.
    """
    metadata = dict(fill_event.metadata or {})
    if not bool(metadata.get("normal_cycle_second_leg_split")):
        return

    state = runtime_state.strategy_state
    cycle_key = str(cycle_index)
    stage_count_map = state.get("normal_cycle_second_leg_split_stage_count") or {}
    stage_count = int(
        stage_count_map.get(cycle_key)
        or metadata.get("split_stage_count")
        or metadata.get("stage_count")
        or 0
    )
    if stage_count <= 1:
        return

    complete, _ = strategy._is_normal_cycle_second_leg_split_complete(state, cycle_index)
    if complete:
        return

    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    fill_confirmed_field = strategy._second_leg_fill_confirmed_field()
    fill_price_field = strategy._second_leg_fill_price_field()
    if not bool(entry.get(fill_confirmed_field)):
        return
    if float(entry.get(fill_price_field) or 0.0) <= 0:
        return

    filled_map = state.setdefault("normal_cycle_second_leg_split_filled_stages", {})
    filled_map[cycle_key] = list(range(1, stage_count + 1))
    state["normal_cycle_second_leg_split_filled_stages"] = filled_map
    state["cycle_short_tp_filled"] = True


def _sync_backtest_processed_cycle_purposes(
    strategy: Any,
    runtime_state: RuntimeState,
    *,
    cycle_index: int,
) -> None:
    state = runtime_state.strategy_state
    first_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_first_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_first_leg_cycle_role()},
    )
    second_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_second_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_second_leg_cycle_role()},
    )
    processed = {
        str(purpose or "").upper()
        for purpose in (state.get("processed_cycle_purposes") or [])
        if str(purpose or "")
    }
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    first_leg_field = strategy._get_first_leg_status_field()
    first_leg_done = str(entry.get(first_leg_field) or "").upper() in {"FILLED", "PROCESSED"}
    second_leg_done = strategy._get_second_leg_status(entry) in {"FILLED", "PROCESSED"}
    fill_confirmed = bool(entry.get(strategy._second_leg_fill_confirmed_field()))
    if first_leg_done:
        processed.add(first_leg_purpose)
    if second_leg_done or fill_confirmed:
        processed.add(second_leg_purpose)
    if processed:
        state["processed_cycle_purposes"] = sorted(processed)


def _advance_backtest_cycle_sequence_after_second_leg(
    strategy: Any,
    runtime_state: RuntimeState,
    fill_event: FillEvent,
    *,
    cycle_index: int,
) -> None:
    purpose = str(fill_event.purpose or "").upper()
    second_leg_purpose = strategy._normalize_cycle_purpose(
        strategy._get_second_leg_purpose(cycle_index),
        {"cycle_index": cycle_index, "cycle_role": strategy._get_second_leg_cycle_role()},
    )
    if purpose != second_leg_purpose:
        return
    if str(getattr(fill_event, "status", "") or "").upper() not in {"", "FILLED"}:
        return

    state = runtime_state.strategy_state
    entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
    if not bool(entry.get(strategy._second_leg_fill_confirmed_field())):
        return
    if float(entry.get(strategy._second_leg_fill_price_field()) or 0.0) <= 0:
        return

    _reconcile_backtest_normal_split_for_terminal_fill(
        strategy,
        runtime_state,
        fill_event,
        cycle_index=cycle_index,
    )

    active_cycle_index = int(state.get("active_cycle_index") or cycle_index)
    cycle_step = str(state.get("cycle_step") or "")
    if (
        active_cycle_index != cycle_index
        or cycle_step != STEP_WAITING_FOR_PAIR_SECOND_LEG
    ):
        if active_cycle_index > cycle_index:
            _sync_backtest_processed_cycle_purposes(strategy, runtime_state, cycle_index=cycle_index)
            return
        state["active_cycle_index"] = cycle_index
        state["cycle_step"] = STEP_WAITING_FOR_PAIR_SECOND_LEG

    sequence_result = advance_cycle_sequence_after_fill(
        purpose,
        state,
        strategy._sequence_config,
    )
    if sequence_result.get("success"):
        state["cycle_completed_count"] = max(
            int(state.get("cycle_completed_count") or 0),
            cycle_index,
        )
        state["cycle_pair_count"] = max(int(state.get("cycle_pair_count") or 0), cycle_index)
        state["cycle_waiting_for_short_tp"] = False
        state["short_tp_pending_cycle"] = 0
        _sync_backtest_processed_cycle_purposes(strategy, runtime_state, cycle_index=cycle_index)
        strategy._persist_cycle_sequence_state(runtime_state)


def repair_cycle_fill_maps_after_advance(
    strategy: Any,
    runtime_state: RuntimeState,
    fill_event: FillEvent,
) -> None:
    """Reconcile cycle fill maps after ``_advance_cycle_from_fill`` in backtests."""
    purpose = str(fill_event.purpose or "").upper()
    cycle_index = _cycle_index_from_purpose(purpose)
    if cycle_index <= 0:
        return

    _ensure_second_leg_sequence_commit(
        strategy,
        runtime_state,
        fill_event,
        cycle_index=cycle_index,
    )
    _advance_backtest_cycle_sequence_after_second_leg(
        strategy,
        runtime_state,
        fill_event,
        cycle_index=cycle_index,
    )

    cycle_state = strategy._ensure_cycle_state(runtime_state)

    if "_SHORT_" in purpose and ("SHORT_REDUCE" in purpose or "SHORT_TP" in purpose):
        fills = cycle_state.setdefault("short_fills", {})
        key = str(cycle_index)
        repaired = _repair_vwap_fill_entry(dict(fills.get(key) or {}))
        if repaired:
            fills[key] = repaired
            avg_price = float(repaired.get("avg_price") or repaired.get("price") or 0.0)
            _sync_cycle_sequence_fill_price(
                strategy,
                runtime_state,
                cycle_index=cycle_index,
                fill_price_field=strategy._second_leg_fill_price_field(),
                fill_confirmed_field=strategy._second_leg_fill_confirmed_field(),
                fill_price=avg_price,
            )
        return

    if "_LONG_" in purpose and ("LONG_REDUCE" in purpose or "LONG_ADD" in purpose):
        fills = cycle_state.setdefault("long_fills", {})
        key = str(cycle_index)
        repaired = _repair_vwap_fill_entry(dict(fills.get(key) or {}))
        if repaired:
            fills[key] = repaired
            avg_price = float(repaired.get("avg_price") or repaired.get("price") or 0.0)
            _sync_cycle_sequence_fill_price(
                strategy,
                runtime_state,
                cycle_index=cycle_index,
                fill_price_field=strategy._second_leg_fill_price_field(),
                fill_confirmed_field=strategy._second_leg_fill_confirmed_field(),
                fill_price=avg_price,
            )


def install_cycle_fill_reference_repair(strategy: Any) -> None:
    """Patch strategy fill commit/advance hooks for backtest-only reference repair."""
    if getattr(strategy, "_backtest_cycle_fill_reference_repair_installed", False):
        return

    original_commit = strategy._commit_short_reduce_terminal_fill
    original_advance = strategy._advance_cycle_from_fill
    original_confirmed_history_has_purpose = strategy._confirmed_history_has_purpose

    def wrapped_confirmed_history_has_purpose(
        runtime_state: RuntimeState,
        purpose: str,
        *,
        exchange_order_id: str | None = None,
    ) -> bool:
        normalized_purpose = str(purpose or "").strip().upper()
        if normalized_purpose and _backtest_cycle_entry_confirms_purpose(
            strategy,
            runtime_state,
            normalized_purpose,
        ):
            return True
        return original_confirmed_history_has_purpose(
            runtime_state,
            purpose,
            exchange_order_id=exchange_order_id,
        )

    def wrapped_commit(
        runtime_state: RuntimeState,
        cycle_index: int,
        *,
        fill_event: FillEvent | None = None,
        avg_price: float | None = None,
        filled_qty: float | None = None,
        source: str = "terminal_fill",
    ) -> None:
        fill_event_price = float(getattr(fill_event, "exec_price", 0.0) or 0.0)
        if fill_event is not None and fill_event_price > 0:
            return original_commit(
                runtime_state,
                cycle_index,
                fill_event=fill_event,
                avg_price=fill_event_price,
                filled_qty=float(getattr(fill_event, "exec_qty", 0.0) or 0.0) or filled_qty,
                source=source,
            )

        if source == "force_commit" and fill_event is None:
            entry = strategy._get_cycle_sequence_entry(runtime_state, cycle_index)
            fill_price_field = strategy._second_leg_fill_price_field()
            fill_confirmed_field = strategy._second_leg_fill_confirmed_field()
            existing_price = float(entry.get(fill_price_field) or 0.0)
            cycle_state = strategy._ensure_cycle_state(runtime_state)
            side_key = "short_fills" if fill_price_field == "short_reduce_fill_price" else "long_fills"
            fill_entry = dict((cycle_state.get(side_key) or {}).get(str(cycle_index)) or {})
            repaired = _repair_vwap_fill_entry(fill_entry)
            repaired_avg = float(repaired.get("avg_price") or repaired.get("price") or 0.0)
            repaired_qty = float(repaired.get("qty") or repaired.get("exec_qty") or 0.0)
            if (
                existing_price > 0
                and bool(entry.get(fill_confirmed_field))
                and repaired_avg > 0
                and abs(existing_price - repaired_avg) / repaired_avg <= 0.005
            ):
                return
            if repaired_avg > 0:
                if side_key in cycle_state and str(cycle_index) in cycle_state[side_key]:
                    cycle_state[side_key][str(cycle_index)] = repaired
                return original_commit(
                    runtime_state,
                    cycle_index,
                    avg_price=repaired_avg,
                    filled_qty=repaired_qty if repaired_qty > 0 else None,
                    source=source,
                )
        return original_commit(
            runtime_state,
            cycle_index,
            fill_event=fill_event,
            avg_price=avg_price,
            filled_qty=filled_qty,
            source=source,
        )

    def wrapped_advance(
        fill_event: FillEvent,
        runtime_state: RuntimeState,
        context: Any | None = None,
    ) -> None:
        original_advance(fill_event, runtime_state, context)
        repair_cycle_fill_maps_after_advance(strategy, runtime_state, fill_event)

    strategy._commit_short_reduce_terminal_fill = wrapped_commit
    strategy._advance_cycle_from_fill = wrapped_advance
    strategy._confirmed_history_has_purpose = wrapped_confirmed_history_has_purpose
    strategy._backtest_cycle_fill_reference_repair_installed = True

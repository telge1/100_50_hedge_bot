"""Research-only Safe Cycle Boundary Freeze policy (pure helpers).

Implements DirectionConfig-aware cycle-opener detection and PENDING→ACTIVE
activation predicates. Consumed by ``inventory_mtm_freeze_shim.py`` when
``InventoryMtmFreezeConfig.safe_cycle_boundary`` is enabled.

No live strategy / runtime / config files are modified by this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fixed_cycle_hedge_bot.direction_config import (
    LONG_PRIMARY_DIRECTION,
    SHORT_PRIMARY_DIRECTION,
    DirectionConfig,
    get_direction_config,
)

_CYCLE_LEG_RE = re.compile(r"^CYCLE_(\d+)_([A-Z_]+)$")

FREEZE_NORMAL = "normal"
FREEZE_PENDING = "pending"
FREEZE_ACTIVE = "active"

SAFE_BOUNDARY_VARIANTS = ("S0", "S1", "S2", "S3")


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_cycle_purpose(purpose: str) -> tuple[int | None, str | None]:
    match = _CYCLE_LEG_RE.match(str(purpose or "").strip().upper())
    if not match:
        return None, None
    return int(match.group(1)), match.group(2)


def direction_config_for_primary(primary_side: str) -> DirectionConfig:
    side = str(primary_side or "long").strip().lower()
    if side == "short":
        return SHORT_PRIMARY_DIRECTION
    return LONG_PRIMARY_DIRECTION


def first_leg_kind_from_direction(config: DirectionConfig) -> str:
    """Canonical first-leg purpose suffix from DirectionConfig (e.g. LONG_ADD / SHORT_REDUCE)."""
    return str(config.cycle_first_leg or "").strip().upper()


def second_leg_kind_from_direction(config: DirectionConfig) -> str:
    return str(config.cycle_second_leg or "").strip().upper()


def is_direction_aware_cycle_opener(
    purpose: str,
    *,
    primary_side: str,
    direction: DirectionConfig | None = None,
) -> bool:
    """True iff ``purpose`` is the DirectionConfig first-leg opener of some cycle.

    Long-primary: ``CYCLE_N_LONG_ADD`` (runtime long-reduce opener).
    Short-primary: ``CYCLE_N_SHORT_REDUCE`` (NOT ``SHORT_ADD`` — that was the
    legacy freeze-helper bug).
    """
    _cycle, kind = parse_cycle_purpose(purpose)
    if kind is None:
        return False
    cfg = direction or direction_config_for_primary(primary_side)
    return kind == first_leg_kind_from_direction(cfg)


def is_direction_aware_second_leg(
    purpose: str,
    *,
    primary_side: str,
    direction: DirectionConfig | None = None,
) -> bool:
    _cycle, kind = parse_cycle_purpose(purpose)
    if kind is None:
        return False
    cfg = direction or direction_config_for_primary(primary_side)
    return kind == second_leg_kind_from_direction(cfg)


def is_refill_purpose(purpose: str) -> bool:
    p = str(purpose or "").strip().upper()
    return p in {"REFILL_LONG", "REFILL_SHORT", "RECOVERY_REFILL_LONG", "RECOVERY_REFILL_SHORT"}


def is_basket_exit_purpose(purpose: str) -> bool:
    p = str(purpose or "").strip().upper()
    return p in {
        "LONG_TP_EXIT",
        "SHORT_SL_EXIT",
        "LONG_SL_EXIT",
        "SHORT_TP_EXIT",
    }


def legacy_short_opener_bug_would_match(purpose: str) -> bool:
    """Documents the old incorrect short-opener mapping (SHORT_ADD)."""
    _cycle, kind = parse_cycle_purpose(purpose)
    return kind == "SHORT_ADD"


def resolve_requested_cycle_at_trigger(strategy_state: dict[str, Any]) -> int:
    """Pick the in-flight cycle index that must finish before FREEZE_ACTIVE."""
    active = int(safe_float(strategy_state.get("active_cycle_index"), 1) or 1)
    step = str(strategy_state.get("cycle_step") or "")
    if step == "WAITING_FOR_PAIR_SECOND_LEG":
        return max(1, active)
    # After a just-completed pair, active often already points at next first-leg wait.
    completed = int(
        safe_float(
            strategy_state.get("cycle_completed_count"),
            safe_float(strategy_state.get("completed_cycle_count"), 0),
        )
        or 0
    )
    if completed > 0 and step == "WAITING_FOR_PAIR_FIRST_LEG":
        # Requested cycle is the one that just finished (completed).
        return max(1, completed)
    return max(1, active)


def _cycle_state_entry(strategy_state: dict[str, Any], cycle_index: int) -> dict[str, Any]:
    raw = strategy_state.get("cycle_states") or {}
    if not isinstance(raw, dict):
        return {}
    for key in (cycle_index, str(cycle_index), int(cycle_index)):
        try:
            entry = raw.get(key)
        except Exception:
            entry = None
        if isinstance(entry, dict):
            return entry
    return {}


def _staged_or_split_incomplete(strategy_state: dict[str, Any], cycle_index: int) -> bool:
    """Best-effort: True if staged/split second-leg maps still need fills for cycle N."""
    n = str(cycle_index)
    stage_count = strategy_state.get("normal_cycle_second_leg_split_stage_count") or {}
    filled = strategy_state.get("normal_cycle_second_leg_split_filled_stages") or {}
    if isinstance(stage_count, dict) and n in stage_count:
        need = int(safe_float(stage_count.get(n), 0) or 0)
        have = filled.get(n) if isinstance(filled, dict) else None
        have_n = len(have) if isinstance(have, list) else int(safe_float(have, 0) or 0)
        if need > 0 and have_n < need:
            return True
    staged_count = strategy_state.get("staged_second_leg_tp_stage_count") or {}
    staged_filled = strategy_state.get("staged_second_leg_tp_filled_stages") or {}
    if isinstance(staged_count, dict) and n in staged_count:
        need = int(safe_float(staged_count.get(n), 0) or 0)
        have = staged_filled.get(n) if isinstance(staged_filled, dict) else None
        have_n = len(have) if isinstance(have, list) else int(safe_float(have, 0) or 0)
        if need > 0 and have_n < need:
            return True
    if bool(strategy_state.get("cycle_waiting_for_short_tp")):
        pending = int(safe_float(strategy_state.get("short_tp_pending_cycle"), 0) or 0)
        if pending == int(cycle_index):
            return True
    if bool(strategy_state.get("cycle_waiting_for_long_reduce")):
        pending = int(safe_float(strategy_state.get("long_reduce_pending_cycle"), 0) or 0)
        if pending == int(cycle_index):
            return True
    return False


def _refill_incomplete(strategy_state: dict[str, Any], cycle_index: int) -> bool:
    if bool(strategy_state.get("refill_in_progress")) or bool(strategy_state.get("refill_pending")):
        return True
    if bool(strategy_state.get("refill_required")):
        last = int(safe_float(strategy_state.get("last_refill_completed_cycle_index"), -1) or -1)
        if last < int(cycle_index):
            return True
    return False


def safe_boundary_ready(
    strategy_state: dict[str, Any],
    *,
    requested_cycle: int,
    primary_side: str = "long",
    long_qty: float = 0.0,
    short_qty: float = 0.0,
    active_exit_purposes: list[str] | None = None,
) -> tuple[bool, str]:
    """Return ``(ready, reason)`` for PENDING → ACTIVE.

    Uses canonical strategy_state fields; does not mutate strategy state.
    """
    N = int(requested_cycle)
    if N < 1:
        return False, "invalid_requested_cycle"

    entry = _cycle_state_entry(strategy_state, N)
    complete_flag = bool(entry.get("complete"))
    completed_count = int(
        safe_float(
            strategy_state.get("cycle_completed_count"),
            safe_float(strategy_state.get("completed_cycle_count"), 0),
        )
        or 0
    )
    if not complete_flag and completed_count < N:
        return False, "cycle_not_complete"

    step = str(strategy_state.get("cycle_step") or "")
    if step and step != "WAITING_FOR_PAIR_FIRST_LEG":
        # Still inside a pair (waiting for second leg) → not ready.
        if step == "WAITING_FOR_PAIR_SECOND_LEG":
            return False, "waiting_for_second_leg"

    if _staged_or_split_incomplete(strategy_state, N):
        return False, "staged_or_split_incomplete"

    if _refill_incomplete(strategy_state, N):
        return False, "refill_incomplete"

    if bool(strategy_state.get("force_exit_rebuild")):
        return False, "force_exit_rebuild"

    signature = strategy_state.get("last_exit_signature")
    if signature is None or signature == "":
        # Flat book may omit exits.
        if abs(float(long_qty)) > 1e-9 or abs(float(short_qty)) > 1e-9:
            return False, "missing_last_exit_signature"

    if bool(strategy_state.get("pending_final_exit")) and (
        abs(float(long_qty)) > 1e-9 or abs(float(short_qty)) > 1e-9
    ):
        # Soft: some runs set this while exits exist; require signature already.
        if signature is None:
            return False, "pending_final_exit"

    # Next opener must not already be filled for N+1 (activation cleanliness).
    next_entry = _cycle_state_entry(strategy_state, N + 1)
    if next_entry:
        # If next first leg already marked filled/complete, grandfather path —
        # still allow activation but reason notes grandfather.
        la = str(next_entry.get("long_add_status") or next_entry.get("first_leg_status") or "").upper()
        if la in {"FILLED", "COMPLETE", "COMPLETED"}:
            return True, "ready_grandfathered_next_opener_started"

    purposes = [str(p or "").upper() for p in (active_exit_purposes or [])]
    flat = abs(float(long_qty)) <= 1e-9 and abs(float(short_qty)) <= 1e-9
    if not flat and signature and not purposes:
        # Signature without live orders can still be OK right after rebuild commit
        # if orders are about to be submitted in the same batch — allow.
        pass

    return True, "ready"


@dataclass
class SafeBoundaryRuntime:
    """Backtest-only bookkeeping for safe-boundary freeze (research shim)."""

    freeze_state: str = FREEZE_NORMAL
    freeze_requested_at_candle: int | None = None
    freeze_requested_cycle: int | None = None
    freeze_activated_at_candle: int | None = None
    freeze_activated_after_cycle: int | None = None
    blocked_opener_count: int = 0
    blocked_opener_purposes: list[str] = field(default_factory=list)
    allowed_current_cycle_action_count: int = 0
    safe_boundary_reason: str | None = None
    exit_signature_at_activation: Any = None
    events: list[dict[str, Any]] = field(default_factory=list)
    # Explicit stop-after-cycle modes (S2/S3): arm pending from entry.
    arm_mode: str = "mtm"  # mtm | stop_after_cycle
    stop_after_cycle: int | None = None
    armed_from_entry: bool = False

    def log(self, action: str, **payload: Any) -> None:
        self.events.append({"action": action, **payload})


def classify_allowed_pending_action(purpose: str, *, primary_side: str) -> str | None:
    """Return event suffix for allowed-while-pending logging, or None."""
    p = str(purpose or "").strip().upper()
    if is_direction_aware_second_leg(p, primary_side=primary_side):
        # Staged/split second legs reuse the same purpose string; callers may
        # refine via strategy_state. Default to second_leg.
        return "second_leg"
    if is_refill_purpose(p):
        return "refill"
    if is_basket_exit_purpose(p):
        return "exit"
    if "COVERAGE" in p or p.startswith("REPAIR_"):
        return "coverage"
    if p.startswith("CYCLE_") and not is_direction_aware_cycle_opener(p, primary_side=primary_side):
        return "coverage"
    return None


def is_next_cycle_first_leg_opener(
    purpose: str,
    *,
    primary_side: str,
    activated_after_cycle: int,
    direction: DirectionConfig | None = None,
) -> bool:
    """True if purpose opens a cycle strictly after ``activated_after_cycle``."""
    cycle, _kind = parse_cycle_purpose(purpose)
    if cycle is None:
        return False
    if not is_direction_aware_cycle_opener(purpose, primary_side=primary_side, direction=direction):
        return False
    return int(cycle) > int(activated_after_cycle)


def detect_invalid_partial_cycle(strategy_state: dict[str, Any]) -> bool:
    """Heuristic: first leg filled for some N but cycle not complete and second incomplete."""
    raw = strategy_state.get("cycle_states") or {}
    if not isinstance(raw, dict):
        return False
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if bool(entry.get("complete")):
            continue
        first = str(
            entry.get("long_add_status")
            or entry.get("first_leg_status")
            or entry.get("short_reduce_status")
            or ""
        ).upper()
        second = str(
            entry.get("short_reduce_status")
            or entry.get("second_leg_status")
            or entry.get("long_reduce_status")
            or ""
        ).upper()
        if first in {"FILLED", "COMPLETE", "COMPLETED"} and second not in {
            "FILLED",
            "COMPLETE",
            "COMPLETED",
        }:
            # Still waiting for second leg is only invalid if sequencer abandoned it.
            step = str(strategy_state.get("cycle_step") or "")
            if step == "WAITING_FOR_PAIR_FIRST_LEG":
                return True
            try:
                n = int(key)
            except (TypeError, ValueError):
                continue
            active = int(safe_float(strategy_state.get("active_cycle_index"), 0) or 0)
            if active > n and not bool(entry.get("complete")):
                return True
    return False

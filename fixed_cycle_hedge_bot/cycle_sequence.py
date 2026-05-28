from dataclasses import dataclass
import re
from typing import Any, Dict

STEP_WAITING_FOR_PAIR_FIRST_LEG = "WAITING_FOR_PAIR_FIRST_LEG"
STEP_WAITING_FOR_PAIR_SECOND_LEG = "WAITING_FOR_PAIR_SECOND_LEG"


@dataclass
class CycleSequenceConfig:
    cycle_prefix: str = "CYCLE"
    first_leg: str = "LONG_ADD"
    second_leg: str = "SHORT_REDUCE"


def _format_purpose(prefix: str, cycle_index: int, leg: str) -> str:
    return f"{prefix}_{cycle_index}_{leg}"


def derive_next_required_purpose(
    config: CycleSequenceConfig, active_cycle_index: int, cycle_step: str
) -> str | None:
    if cycle_step == STEP_WAITING_FOR_PAIR_FIRST_LEG:
        return _format_purpose(config.cycle_prefix, active_cycle_index, config.first_leg)
    if cycle_step == STEP_WAITING_FOR_PAIR_SECOND_LEG:
        return _format_purpose(config.cycle_prefix, active_cycle_index, config.second_leg)
    return None


def initialize_cycle_sequence_state(
    state: dict[str, Any], config: CycleSequenceConfig
) -> dict[str, Any]:
    active_cycle_index = int(state.get("active_cycle_index") or 1)
    state["active_cycle_index"] = active_cycle_index
    state["cycle_step"] = STEP_WAITING_FOR_PAIR_FIRST_LEG
    state["last_completed_purpose"] = None
    next_required = derive_next_required_purpose(
        config, active_cycle_index, state["cycle_step"]
    )
    state["next_required_purpose"] = next_required
    return {
        "active_cycle_index": active_cycle_index,
        "cycle_step": state["cycle_step"],
        "next_required_purpose": next_required,
        "last_completed_purpose": None,
    }


def get_cycle_sequence_state(
    state: dict[str, Any], config: CycleSequenceConfig
) -> dict[str, Any]:
    active_cycle_index = int(state.get("active_cycle_index") or 1)
    cycle_step = state.get("cycle_step") or STEP_WAITING_FOR_PAIR_FIRST_LEG
    next_required = state.get("next_required_purpose")
    if not next_required:
        next_required = derive_next_required_purpose(config, active_cycle_index, cycle_step)
        state["next_required_purpose"] = next_required
    return {
        "active_cycle_index": active_cycle_index,
        "cycle_step": cycle_step,
        "next_required_purpose": next_required,
        "last_completed_purpose": state.get("last_completed_purpose"),
    }


def is_attempted_purpose_matching_sequence(
    attempted_purpose: str, sequence_state: dict[str, Any]
) -> bool:
    attempted = str(attempted_purpose or "").upper()
    required = str(sequence_state.get("next_required_purpose") or "").upper()
    return attempted == required


def advance_cycle_sequence_after_fill(
    purpose: str, state: dict[str, Any], config: CycleSequenceConfig
) -> dict[str, Any]:
    normalized = str(purpose or "").upper()
    prefix = f"{config.cycle_prefix}_".upper()
    if not normalized.startswith(prefix):
        return {"success": False, "type": None, "payload": {}}
    remainder = normalized[len(prefix) :]
    if "_" not in remainder:
        return {"success": False, "type": None, "payload": {}}
    cycle_index_str, leg = remainder.split("_", 1)
    if not cycle_index_str.isdigit():
        return {"success": False, "type": None, "payload": {}}
    cycle_index = int(cycle_index_str)
    leg_upper = leg.upper()
    sequence_state = get_cycle_sequence_state(state, config)
    active = sequence_state["active_cycle_index"]
    step = sequence_state["cycle_step"]

    first_leg = config.first_leg.upper()
    second_leg = config.second_leg.upper()

    payload = {
        "active_cycle_index": active,
        "cycle_step": step,
        "next_required_purpose": sequence_state["next_required_purpose"],
        "attempted_purpose": normalized,
    }

    if leg_upper == first_leg:
        if cycle_index != active or step != STEP_WAITING_FOR_PAIR_FIRST_LEG:
            return {
                "success": False,
                "type": "unexpected_fill",
                "payload": {**payload, "cycle_index": cycle_index},
            }
        state["cycle_step"] = STEP_WAITING_FOR_PAIR_SECOND_LEG
        state["next_required_purpose"] = derive_next_required_purpose(
            config, active, state["cycle_step"]
        )
        state["last_completed_purpose"] = normalized
        return {
            "success": True,
            "type": "step_transition",
            "payload": {
                **payload,
                "cycle_step": state["cycle_step"],
                "next_required_purpose": state["next_required_purpose"],
                "last_completed_purpose": normalized,
            },
        }

    if leg_upper == second_leg:
        if cycle_index != active or step != STEP_WAITING_FOR_PAIR_SECOND_LEG:
            return {
                "success": False,
                "type": "unexpected_fill",
                "payload": {**payload, "cycle_index": cycle_index},
            }
        next_cycle = active + 1
        state["last_completed_purpose"] = normalized
        state["active_cycle_index"] = next_cycle
        state["cycle_step"] = STEP_WAITING_FOR_PAIR_FIRST_LEG
        state["next_required_purpose"] = derive_next_required_purpose(
            config, next_cycle, state["cycle_step"]
        )
        return {
            "success": True,
            "type": "pair_completed",
            "payload": {
                **payload,
                "active_cycle_index": state["active_cycle_index"],
                "cycle_step": state["cycle_step"],
                "next_required_purpose": state["next_required_purpose"],
                "last_completed_purpose": state["last_completed_purpose"],
            },
        }

    return {"success": False, "type": None, "payload": {}}

from __future__ import annotations

from typing import Any


def cycle_submit_identity(purpose_upper: str, metadata: dict[str, Any]) -> tuple:
    """
    Stable submit-identity for cycle orders (intents and open orders).

    - Normal cycle orders: (PURPOSE, "single")
    - Staged second-leg TPs: (PURPOSE, "staged", cycle_index, stage_index)
    - Normal second-leg splits: (PURPOSE, "normal_split", cycle_index, split_stage_index, split_stage_count)
    """
    if not purpose_upper.startswith("CYCLE_"):
        return (purpose_upper,)

    staged_flag = bool(metadata.get("is_staged_second_leg_tp"))
    normal_split_flag = bool(metadata.get("normal_cycle_second_leg_split"))

    if staged_flag:
        try:
            cycle_index = int(metadata.get("cycle_index"))
        except (TypeError, ValueError):
            cycle_index = None
        try:
            stage_index = int(metadata.get("stage_index"))
        except (TypeError, ValueError):
            stage_index = None
        if cycle_index is not None and stage_index is not None:
            return (purpose_upper, "staged", cycle_index, stage_index)
        return (purpose_upper, "staged_fallback")

    if normal_split_flag:
        split_cycle_val = metadata.get("split_cycle_index")
        if split_cycle_val is None:
            split_cycle_val = metadata.get("cycle_index")
        try:
            cycle_index = int(split_cycle_val)
        except (TypeError, ValueError):
            cycle_index = None
        try:
            split_stage_index = int(metadata.get("split_stage_index"))
        except (TypeError, ValueError):
            split_stage_index = None
        try:
            split_stage_count = int(
                metadata.get("split_stage_count") or metadata.get("stage_count")
            )
        except (TypeError, ValueError):
            split_stage_count = None
        if (
            cycle_index is not None
            and split_stage_index is not None
            and split_stage_count is not None
        ):
            return (
                purpose_upper,
                "normal_split",
                cycle_index,
                split_stage_index,
                split_stage_count,
            )
        return (purpose_upper, "normal_split_fallback")

    return (purpose_upper, "single")

"""Purpose helpers that temporarily keep the existing long-bot strings."""

from __future__ import annotations


def cycle_long_add(cycle_index: int) -> str:
    # NOTE: The long cycle naming still uses "_LONG_ADD" even when the runtime
    # interprets the intent as a long reduce leg (side=Sell, reduce_only=True).
    return f"CYCLE_{cycle_index}_LONG_ADD"


def cycle_short_reduce(cycle_index: int) -> str:
    return f"CYCLE_{cycle_index}_SHORT_REDUCE"


def cycle_short_add(cycle_index: int) -> str:
    # NOTE: Despite the "_SHORT_ADD" suffix, these purposes represent short
    # reduce legs in the current runtime (side=Buy, reduce_only=True).
    return f"CYCLE_{cycle_index}_SHORT_ADD"


def cycle_long_reduce(cycle_index: int) -> str:
    return f"CYCLE_{cycle_index}_LONG_REDUCE"


def normalize_purpose(purpose: object) -> str:
    return str(purpose or "").upper()


def is_cycle_long_add(purpose: object) -> bool:
    # NOTE: The intent name ends with "_LONG_ADD" even though the runtime
    # enforces long reduce semantics for these purposes.
    normalized = normalize_purpose(purpose)
    return normalized.startswith("CYCLE_") and normalized.endswith("_LONG_ADD")


def is_cycle_short_reduce(purpose: object) -> bool:
    normalized = normalize_purpose(purpose)
    return normalized.startswith("CYCLE_") and normalized.endswith("_SHORT_REDUCE")


def is_cycle_short_add(purpose: object) -> bool:
    # NOTE: Historical naming keeps “_SHORT_ADD” here, but the runtime treats
    # such intents as short reduce legs.
    normalized = normalize_purpose(purpose)
    return normalized.startswith("CYCLE_") and normalized.endswith("_SHORT_ADD")


def is_cycle_long_reduce(purpose: object) -> bool:
    normalized = normalize_purpose(purpose)
    return normalized.startswith("CYCLE_") and normalized.endswith("_LONG_REDUCE")


def is_refill_long(purpose: object) -> bool:
    return normalize_purpose(purpose) == "REFILL_LONG"


def is_refill_short(purpose: object) -> bool:
    return normalize_purpose(purpose) == "REFILL_SHORT"


def is_long_exit(purpose: object) -> bool:
    return normalize_purpose(purpose) == "LONG_TP_EXIT"


def is_short_exit(purpose: object) -> bool:
    return normalize_purpose(purpose) == "SHORT_SL_EXIT"

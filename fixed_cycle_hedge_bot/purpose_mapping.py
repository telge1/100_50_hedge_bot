"""Purpose helpers that temporarily keep the existing long-bot strings."""

from __future__ import annotations


def cycle_long_add(cycle_index: int) -> str:
    return f"CYCLE_{cycle_index}_LONG_ADD"


def cycle_short_reduce(cycle_index: int) -> str:
    return f"CYCLE_{cycle_index}_SHORT_REDUCE"


def cycle_short_add(cycle_index: int) -> str:
    return f"CYCLE_{cycle_index}_SHORT_ADD"


def cycle_long_reduce(cycle_index: int) -> str:
    return f"CYCLE_{cycle_index}_LONG_REDUCE"


def normalize_purpose(purpose: object) -> str:
    return str(purpose or "").upper()


def is_cycle_long_add(purpose: object) -> bool:
    normalized = normalize_purpose(purpose)
    return normalized.startswith("CYCLE_") and normalized.endswith("_LONG_ADD")


def is_cycle_short_reduce(purpose: object) -> bool:
    normalized = normalize_purpose(purpose)
    return normalized.startswith("CYCLE_") and normalized.endswith("_SHORT_REDUCE")


def is_cycle_short_add(purpose: object) -> bool:
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

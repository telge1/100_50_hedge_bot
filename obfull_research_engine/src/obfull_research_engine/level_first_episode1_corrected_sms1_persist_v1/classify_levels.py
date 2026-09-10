"""Split ordinary level updates from refill classification."""

from __future__ import annotations

from typing import Any


def classify_level_kind(old_size: Any, new_size: Any) -> str:
    old = float(old_size or 0.0)
    new = float(new_size or 0.0)
    if old <= 0.0 and new > 0.0:
        return "LEVEL_ADD"
    if old > 0.0 and new <= 0.0:
        return "LEVEL_REMOVE"
    if new > old:
        return "LEVEL_INCREASE"
    if new < old:
        return "LEVEL_DECREASE"
    return "LEVEL_UNCHANGED"


def is_size_reduction(kind: str) -> bool:
    return kind in {"LEVEL_REMOVE", "LEVEL_DECREASE"}


def is_size_increase(kind: str) -> bool:
    return kind in {"LEVEL_ADD", "LEVEL_INCREASE"}

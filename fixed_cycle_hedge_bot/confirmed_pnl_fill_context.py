"""Recover cycle fill context for confirmed PnL history writes."""

from __future__ import annotations

import re
from typing import Any

from . import purpose_mapping
from .direction_config import DirectionConfig, LONG_PRIMARY_DIRECTION

_CYCLE_INDEX_RE = re.compile(r"CYCLE_(\d+)_", re.I)
_SPLIT_SUFFIX_RE = re.compile(r"-split\d+$")


def recover_purpose_from_client_order_id(client_id: str, strategy_name: str = "fixed_cycle") -> str:
    text = str(client_id or "").strip()
    if not text:
        return ""
    prefix = f"{strategy_name}-"
    recovered_prefix = f"recovered-{strategy_name}-"
    purpose_part = ""
    if text.startswith(prefix):
        remainder = text[len(prefix) :]
        if "-" in remainder:
            purpose_part = remainder.rsplit("-", 1)[0]
            purpose_part = _SPLIT_SUFFIX_RE.sub("", purpose_part)
    elif text.startswith(recovered_prefix):
        remainder = text[len(recovered_prefix) :]
        if remainder.startswith("-") and "--" in remainder[1:]:
            purpose_part = remainder[1:].split("--", 1)[0]
            purpose_part = _SPLIT_SUFFIX_RE.sub("", purpose_part)
        elif "-" in remainder:
            purpose_part = remainder.rsplit("-", 1)[0]
            purpose_part = _SPLIT_SUFFIX_RE.sub("", purpose_part)
    if purpose_part:
        return purpose_part.upper()
    return ""


def cycle_index_from_purpose(purpose: str) -> int:
    match = _CYCLE_INDEX_RE.search(str(purpose or "").strip())
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def cycle_role_from_purpose(purpose: str, direction: DirectionConfig | None = None) -> str:
    del direction  # reserved for direction-specific overrides
    purpose_upper = str(purpose or "").upper()
    if purpose_mapping.is_cycle_long_reduce(purpose) or purpose_mapping.is_cycle_long_add(purpose):
        return "long_reduce"
    if (
        purpose_mapping.is_cycle_short_reduce(purpose)
        or purpose_mapping.is_cycle_short_add(purpose)
        or (purpose_upper.startswith("CYCLE_") and purpose_upper.endswith("_SHORT_TP"))
    ):
        return "short_reduce"
    return ""


def classify_exit_fill_for_audit(
    purpose: str,
    metadata: dict[str, Any] | None,
    *,
    direction: DirectionConfig | None = None,
) -> tuple[str, int]:
    metadata = dict(metadata or {})
    purpose_upper = str(purpose or "").upper()
    cycle_index = 0
    try:
        cycle_index = int(metadata.get("cycle_index") or 0)
    except (TypeError, ValueError):
        cycle_index = 0
    if cycle_index <= 0:
        cycle_index = cycle_index_from_purpose(purpose_upper)

    cycle_role = str(metadata.get("cycle_role") or "").lower()
    if not cycle_role:
        cycle_role = cycle_role_from_purpose(purpose_upper, direction)

    if cycle_role == "long_reduce":
        return "cycle_long_reduce", cycle_index
    if cycle_role == "short_reduce":
        return "cycle_short_tp", cycle_index

    if purpose_mapping.is_cycle_long_reduce(purpose) or purpose_mapping.is_cycle_long_add(purpose):
        return "cycle_long_reduce", cycle_index_from_purpose(purpose_upper) or cycle_index
    if (
        purpose_mapping.is_cycle_short_reduce(purpose)
        or purpose_mapping.is_cycle_short_add(purpose)
        or (purpose_upper.startswith("CYCLE_") and purpose_upper.endswith("_SHORT_TP"))
    ):
        return "cycle_short_tp", cycle_index_from_purpose(purpose_upper) or cycle_index

    if (
        purpose_upper in {"LONG_TP_EXIT", "LONG_SL_EXIT"}
        or "LONG_TP" in purpose_upper
        or "LONG_SL" in purpose_upper
    ):
        return "final_long_exit", 0
    if (
        purpose_upper in {"SHORT_TP_EXIT", "SHORT_SL_EXIT", "SHORT_HARD_STOP", "SHORT_HARD_STOP_EXIT"}
        or "SHORT_TP" in purpose_upper
        or "SHORT_SL" in purpose_upper
        or "SHORT_HARD_STOP" in purpose_upper
    ):
        return "final_short_exit", 0
    return "ignore", 0


def enrich_fill_for_confirmed_pnl(
    fill_event: Any,
    *,
    strategy_name: str = "fixed_cycle",
    direction: DirectionConfig | None = None,
) -> None:
    metadata = dict(getattr(fill_event, "metadata", None) or {})
    purpose = str(getattr(fill_event, "purpose", None) or "").strip()
    client_id = str(getattr(fill_event, "client_order_id", None) or "").strip()
    recovered = recover_purpose_from_client_order_id(client_id, strategy_name) if client_id else ""
    if recovered and (
        not purpose
        or purpose.upper() in {"RECOVERED_ORDER", "RECOVERED_SHORT_REDUCE", "RECOVERED_LONG_REDUCE"}
        or (recovered.startswith("CYCLE_") and not purpose.upper().startswith("CYCLE_"))
    ):
        fill_event.purpose = recovered
        purpose = recovered

    cycle_index = cycle_index_from_purpose(purpose)
    if cycle_index > 0 and not metadata.get("cycle_index"):
        metadata["cycle_index"] = cycle_index
    if not metadata.get("cycle_role"):
        role = cycle_role_from_purpose(purpose, direction or LONG_PRIMARY_DIRECTION)
        if role:
            metadata["cycle_role"] = role
    fill_event.metadata = metadata


def purpose_requires_confirmed_history_row(purpose: str) -> bool:
    purpose_upper = str(purpose or "").upper()
    return purpose_upper.startswith("CYCLE_") and (
        purpose_mapping.is_cycle_long_reduce(purpose)
        or purpose_mapping.is_cycle_long_add(purpose)
        or purpose_mapping.is_cycle_short_reduce(purpose)
        or purpose_mapping.is_cycle_short_add(purpose)
        or purpose_upper.endswith("_SHORT_TP")
    )

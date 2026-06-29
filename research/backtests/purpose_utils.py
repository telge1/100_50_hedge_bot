"""Bot-purpose preservation helpers for backtest logs (read-only naming)."""

from __future__ import annotations

import re
from typing import Any

from fixed_cycle_hedge_bot.confirmed_pnl_fill_context import (
    cycle_index_from_purpose,
    cycle_role_from_purpose,
)

_CYCLE_PURPOSE_RE = re.compile(r"^CYCLE_\d+_", re.I)
_RECOVERY_RELOAD_RE = re.compile(r"^RECOVERY_RELOAD_(LONG|SHORT)_ENTRY$", re.I)


def preserve_bot_purpose(purpose: object) -> str:
    """Return the bot purpose string unchanged except for outer whitespace."""
    return str(purpose or "").strip()


def is_cycle_purpose(purpose: object) -> bool:
    return bool(_CYCLE_PURPOSE_RE.match(preserve_bot_purpose(purpose)))


def is_recovery_reload_purpose(purpose: object) -> bool:
    return bool(_RECOVERY_RELOAD_RE.match(preserve_bot_purpose(purpose)))


def enrich_purpose_metadata(purpose: object, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ensure metadata carries original bot purpose and cycle fields when known."""
    normalized_purpose = preserve_bot_purpose(purpose)
    enriched = dict(metadata or {})
    original = preserve_bot_purpose(enriched.get("purpose_original") or normalized_purpose)
    enriched["purpose_original"] = original
    enriched["purpose"] = normalized_purpose

    cycle_index = enriched.get("cycle_index")
    if cycle_index is None and normalized_purpose:
        parsed_index = cycle_index_from_purpose(normalized_purpose)
        if parsed_index > 0:
            enriched["cycle_index"] = parsed_index

    if not enriched.get("cycle_role") and normalized_purpose:
        role = cycle_role_from_purpose(normalized_purpose)
        if role:
            enriched["cycle_role"] = role

    return enriched


def purpose_log_fields(purpose: object, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build shared purpose fields for fill_log / order_log / debug output."""
    enriched = enrich_purpose_metadata(purpose, metadata)
    normalized_purpose = preserve_bot_purpose(enriched.get("purpose") or purpose)
    fields: dict[str, Any] = {
        "purpose": normalized_purpose,
        "purpose_original": preserve_bot_purpose(enriched.get("purpose_original") or normalized_purpose),
    }
    if enriched.get("cycle_index") is not None:
        try:
            cycle_index_value = int(enriched.get("cycle_index"))
        except (TypeError, ValueError):
            cycle_index_value = 0
        if cycle_index_value > 0:
            fields["cycle_index"] = cycle_index_value
    if enriched.get("cycle_role"):
        fields["cycle_role"] = enriched.get("cycle_role")
    return fields

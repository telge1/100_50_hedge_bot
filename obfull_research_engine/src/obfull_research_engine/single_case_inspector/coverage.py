"""Modality-aware case coverage (does not alter Full-OB analyze gate)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..timeparse import format_utc_z
from . import (
    CASE_COMPLETE,
    CASE_COMPLETE_OPTIONAL_MISSING,
    CASE_NOT_COMPLETE,
    FULL_OB_STATUS,
)


def evaluate_case_coverage(
    *,
    symbol: str,
    focus_ts: datetime,
    flags: dict[str, bool],
    modality_status: dict[str, str],
    missing_optional: list[str] | None = None,
    missing_required: list[str] | None = None,
) -> dict[str, Any]:
    """flags: with_footprint, with_avr, with_oi, with_liquidations."""
    focus_ts = focus_ts.astimezone(timezone.utc)
    required: list[str] = ["PUBLIC_TRADES", "PRICE"]
    if flags.get("with_footprint"):
        required.append("FOOTPRINT")
    if flags.get("with_avr"):
        required.append("AVR")
    if flags.get("with_oi"):
        required.append("OI")
    if flags.get("with_liquidations"):
        required.append("LIQUIDATIONS")

    missing_required = list(missing_required or [])
    for m in required:
        st = modality_status.get(m, "MISSING")
        if st not in {"COMPLETE", "COMPLETE_EMPTY_OK", "OK"}:
            if m not in missing_required:
                missing_required.append(m)

    missing_optional = list(missing_optional or [])
    # Full-OB always optional for this inspector
    if modality_status.get("FULL_OB") != "COMPLETE":
        if "FULL_OB" not in missing_optional:
            missing_optional.append("FULL_OB")

    if missing_required:
        overall = CASE_NOT_COMPLETE
    elif missing_optional:
        overall = CASE_COMPLETE_OPTIONAL_MISSING
    else:
        overall = CASE_COMPLETE

    return {
        "schema_version": "case_coverage_v1",
        "symbol": symbol.upper(),
        "focus_ts": format_utc_z(focus_ts),
        "required_modalities": required,
        "optional_modalities": ["FULL_OB"],
        "modality_status": {
            **modality_status,
            "FULL_OB": modality_status.get("FULL_OB", FULL_OB_STATUS),
        },
        "missing_required_modalities": missing_required,
        "missing_optional_modalities": missing_optional,
        "empty_trade_second_policy": "NOT_AUTOMATICALLY_SOURCE_GAP",
        "overall": overall,
        "note": "Independent of analyze Full-OB gate; Full-OB optional here.",
    }

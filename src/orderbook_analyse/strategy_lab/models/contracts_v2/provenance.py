"""Non-executable legacy provenance references for V2 contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyProvenanceRefV2:
    """Non-executable legacy source reference (metadata only)."""

    module: str
    path: str
    symbol: str | None
    notes: str | None

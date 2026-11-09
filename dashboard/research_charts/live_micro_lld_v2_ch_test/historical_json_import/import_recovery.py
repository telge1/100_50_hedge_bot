"""
Resume / STOP classification for partial historical imports (no auto-delete).

Used by tests and runbooks; does not mutate ClickHouse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RecoveryAction = Literal["SAFE_RESUME", "STOP_MANUAL", "STOP_CONFLICT", "COMPLETE"]


@dataclass(frozen=True)
class ImportPartialState:
    catalog_complete: bool
    sidecar_present: bool
    publish_present: bool
    provenance_canonical_match: bool | None = None
    is_pilot_line_overlap: bool = False


def classify_partial_import(state: ImportPartialState) -> tuple[RecoveryAction, str]:
    """
    A. sidecar yes, publish no → SAFE_RESUME (publish only after require_before_publish).
    B. catalog complete, sidecar no → STOP_MANUAL (re-run sidecar path; no publish).
    C. sidecar yes, catalog incomplete → STOP_MANUAL.
    D. publish yes, sidecar no (new batch line) → STOP_MANUAL / legacy pilot exempt.
    E/F. provenance match/mismatch handled by sidecar store on retry.
    """
    if state.publish_present and state.catalog_complete and state.sidecar_present:
        return ("COMPLETE", "batch close fully persisted")
    if state.is_pilot_line_overlap:
        return ("SAFE_RESUME", "pilot overlap: publish-only on production line; no meta/catalog rewrite")
    if state.sidecar_present and not state.publish_present and state.catalog_complete:
        return ("SAFE_RESUME", "sidecar verified; resume with publish ledger only")
    if state.catalog_complete and not state.sidecar_present:
        return ("STOP_MANUAL", "catalog without sidecar — do not publish; write sidecar first")
    if state.sidecar_present and not state.catalog_complete:
        return ("STOP_MANUAL", "sidecar without full catalog — investigate before publish")
    if state.publish_present and not state.sidecar_present:
        return ("STOP_MANUAL", "publish without sidecar on new batch path — legacy compat only for pilot")
    if state.provenance_canonical_match is False:
        return ("STOP_CONFLICT", "conflicting original provenance — manual forensic review")
    return ("SAFE_RESUME", "incomplete write; retry idempotent import path")

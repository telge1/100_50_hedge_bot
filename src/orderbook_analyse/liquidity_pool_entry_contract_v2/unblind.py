"""Separate outcome-unblind API — never called from mechanical."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_v2.mechanical import (
    MechanicalAuditError,
    atomic_write_json,
    payload_sha256,
)

TARGET_MECHANICAL_COMPLETE = 24


def run_outcome_unblind(
    mechanical_artifact: Path,
    outcome_source: Path | None,
    output_dir: Path,
    *,
    batch_release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail-closed unblind. Requires valid mechanical artifact + batch release.

    For expansion batches, batch_release must include:
      mechanical_complete_count == 24
      expansion_binding_ok == True
    """
    mechanical_artifact = Path(mechanical_artifact)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    marker = mechanical_artifact.parent / "mechanical_complete.marker"
    if not mechanical_artifact.is_file():
        raise MechanicalAuditError("MECHANICAL_UNBLIND_SEPARATION_FAILURE", "missing mechanical artifact")
    if not marker.is_file():
        raise MechanicalAuditError("MECHANICAL_UNBLIND_SEPARATION_FAILURE", "missing mechanical_complete.marker")

    mech = json.loads(mechanical_artifact.read_text(encoding="utf-8"))
    stored = mech.get("mechanical_payload_sha256")
    recomputed = payload_sha256(mech)
    if not stored or stored != recomputed:
        raise MechanicalAuditError("MECHANICAL_UNBLIND_SEPARATION_FAILURE", "payload sha mismatch")
    marker_sha = marker.read_text(encoding="utf-8").strip()
    if marker_sha != stored:
        raise MechanicalAuditError("MECHANICAL_UNBLIND_SEPARATION_FAILURE", "marker sha mismatch")

    release = batch_release or {}
    if not release.get("batch_release_granted"):
        raise MechanicalAuditError(
            "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
            "batch_release_granted required",
        )
    n = int(release.get("mechanical_complete_count") or 0)
    if n != TARGET_MECHANICAL_COMPLETE:
        raise MechanicalAuditError(
            "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
            f"mechanical_complete_count={n} != {TARGET_MECHANICAL_COMPLETE}",
        )

    # Intentionally do not open outcome_source in this freeze task even if granted —
    # keep API present but refuse actual reads until a later explicit unblind campaign.
    if outcome_source is not None:
        raise MechanicalAuditError(
            "MECHANICAL_UNBLIND_SEPARATION_FAILURE",
            "outcome file reads deferred; unblind API gates only in this freeze phase",
        )

    out = {
        "unblind_performed": False,
        "gate_passed": True,
        "mechanical_payload_sha256": stored,
        "mechanical_complete_count": n,
        "note": "unblind gated OK but outcome reads not executed in this freeze phase",
    }
    atomic_write_json(output_dir / "unblind_gate_result.json", out)
    return out

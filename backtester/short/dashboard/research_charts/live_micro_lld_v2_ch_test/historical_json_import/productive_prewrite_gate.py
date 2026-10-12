"""
Central productive pre-write gate — must pass before any ClickHouse INSERT on batch import.

Fails closed before ``execute_batch_import`` enters the locked write path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

from .batch_runner import BatchImportError, BatchSpec
from .controlled_run_manifest import ControlledRunManifest, resolve_manifest_stems
from .import_recovery import ImportPartialState, classify_partial_import
from .write_guards import InvalidTestRunIdError, require_valid_test_run_uuid

WRITE_ENV = "HISTORICAL_JSON_IMPORT_ALLOW_WRITE"
SIDECAR_WRITE_ENV = "HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE"


class ProductivePrewriteGateError(RuntimeError):
    """Productive import blocked before any database write."""


@dataclass(frozen=True)
class ProductivePrewriteGateResult:
    test_run_id: str
    write_gate: bool
    sidecar_gate: bool
    scope_checks: list[dict[str, Any]]


def assert_productive_env_gates() -> tuple[bool, bool]:
    write_ok = os.environ.get(WRITE_ENV) == "1"
    sidecar_ok = os.environ.get(SIDECAR_WRITE_ENV) == "1"
    if not write_ok or not sidecar_ok:
        missing = []
        if not write_ok:
            missing.append(WRITE_ENV)
        if not sidecar_ok:
            missing.append(SIDECAR_WRITE_ENV)
        raise ProductivePrewriteGateError(
            f"productive import gates not set (required before first CH insert): {', '.join(missing)}"
        )
    return write_ok, sidecar_ok


def classify_snapshot_layers(
    *,
    meta_complete: int,
    members: int,
    catalog: int,
    publish_any: int,
    sidecar_any: int,
    published_on_target_line: bool,
    is_pilot_overlap: bool = False,
) -> tuple[str, str]:
    catalog_complete = catalog > 0 and meta_complete > 0
    return classify_partial_import(
        ImportPartialState(
            catalog_complete=catalog_complete,
            sidecar_present=sidecar_any > 0,
            publish_present=publish_any > 0 and published_on_target_line,
            is_pilot_line_overlap=is_pilot_overlap,
        )
    )


def assess_scope_ch_states(
    snapshot_ids: Sequence[str],
    *,
    pipeline_line_id: str,
    published_on_line: set[str],
    layer_by_snapshot: dict[str, dict[str, int]],
    pilot_snapshot_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    pilot_snapshot_ids = pilot_snapshot_ids or set()
    out: list[dict[str, Any]] = []
    for sid in snapshot_ids:
        layers = layer_by_snapshot.get(sid) or {
            "meta_complete": 0,
            "members": 0,
            "catalog": 0,
            "publish_any": 0,
            "sidecar_any": 0,
        }
        on_line = sid in published_on_line
        action, reason = classify_snapshot_layers(
            meta_complete=int(layers.get("meta_complete", 0)),
            members=int(layers.get("members", 0)),
            catalog=int(layers.get("catalog", 0)),
            publish_any=int(layers.get("publish_any", 0)),
            sidecar_any=int(layers.get("sidecar_any", 0)),
            published_on_target_line=on_line,
            is_pilot_overlap=sid in pilot_snapshot_ids,
        )
        cat_n = int(layers.get("catalog", 0))
        side_n = int(layers.get("sidecar_any", 0))
        pub_n = int(layers.get("publish_any", 0))
        meta_n = int(layers.get("meta_complete", 0))
        if cat_n > 0 and side_n == 0 and pub_n == 0 and not on_line:
            action = "SAFE_RESUME"
            reason = "catalog complete; resume sidecar then publish (Phase 4J path)"
        elif side_n > 0 and pub_n == 0 and cat_n > 0 and meta_n > 0 and not on_line:
            action = "SAFE_RESUME"
            reason = "sidecar verified; resume publish ledger only"
        if on_line and action != "COMPLETE":
            action = "COMPLETE"
            reason = "published on target pipeline line"
        empty = not any(int(layers.get(k, 0)) for k in ("meta_complete", "members", "catalog", "publish_any", "sidecar_any"))
        if empty:
            action = "SAFE_RESUME"
            reason = "not present in CH — fresh import"
        out.append(
            {
                "snapshot_id": sid,
                "layers": layers,
                "recovery_action": action,
                "reason": reason,
            }
        )
    return out


def assert_scope_recovery_safe(scope_checks: Sequence[dict[str, Any]]) -> None:
    allowed = {"SAFE_RESUME", "COMPLETE"}
    bad = [c for c in scope_checks if c["recovery_action"] not in allowed]
    if bad:
        sample = bad[0]
        raise ProductivePrewriteGateError(
            f"unclassified or unsafe CH partial state for {sample['snapshot_id']}: "
            f"{sample['recovery_action']} — {sample['reason']}"
        )
    for c in scope_checks:
        layers = c.get("layers") or {}
        meta = int(layers.get("meta_complete", 0))
        members = int(layers.get("members", 0))
        catalog = int(layers.get("catalog", 0))
        sidecar = int(layers.get("sidecar_any", 0))
        publish = int(layers.get("publish_any", 0))
        if members > 0 and catalog == 0:
            raise ProductivePrewriteGateError(
                f"orphan members without catalog for {c['snapshot_id']}"
            )
        if meta and catalog == 0 and not sidecar and not publish:
            raise ProductivePrewriteGateError(
                f"meta without catalog for {c['snapshot_id']} — manual review"
            )
        if catalog > 0 and sidecar == 0 and publish == 0:
            # Phase 4J-style SAFE_RESUME: catalog complete, sidecar pending
            continue


def run_productive_prewrite_gate(
    *,
    test_run_id: str,
    pipeline_line_id: str,
    spec: BatchSpec | None = None,
    manifest: ControlledRunManifest | None = None,
    snapshot_ids: Sequence[str] | None = None,
    scope_ch_checks: Sequence[dict[str, Any]] | None = None,
    require_manifest_pipeline_match: bool = True,
) -> ProductivePrewriteGateResult:
    """
    Full productive gate. Call immediately before acquiring the writer lock / CH inserts.

    ``scope_ch_checks`` may be precomputed (orchestrator); when omitted and snapshot_ids
    set, caller must still pass classified checks for write paths that touch CH.
    """
    assert_productive_env_gates()
    try:
        run_id = require_valid_test_run_uuid(test_run_id, context="productive_prewrite_gate")
    except InvalidTestRunIdError as exc:
        raise ProductivePrewriteGateError(str(exc)) from exc

    if manifest is not None and require_manifest_pipeline_match:
        if manifest.pipeline_line_id != pipeline_line_id:
            raise ProductivePrewriteGateError("pipeline_line_id does not match manifest")
        if spec is not None:
            if spec.pipeline_line_id != manifest.pipeline_line_id:
                raise ProductivePrewriteGateError("batch spec pipeline_line_id != manifest")
            if spec.combo_folder != manifest.combo_folder:
                raise ProductivePrewriteGateError("batch spec combo_folder != manifest")

    checks = list(scope_ch_checks or [])
    if snapshot_ids and not checks:
        raise ProductivePrewriteGateError(
            "snapshot_ids provided without scope_ch_checks — classify CH state first"
        )
    if checks:
        assert_scope_recovery_safe(checks)

    return ProductivePrewriteGateResult(
        test_run_id=run_id,
        write_gate=True,
        sidecar_gate=True,
        scope_checks=checks,
    )


def load_scope_layer_map_for_snapshots(snapshot_ids: Sequence[str]) -> dict[str, dict[str, int]]:
    from .ch_import_state import ch_client_and_db
    from .orphan_audit import audit_snapshot_layers

    client, db = ch_client_and_db()
    try:
        return {sid: audit_snapshot_layers(client, db, sid) for sid in snapshot_ids}
    finally:
        client.close()

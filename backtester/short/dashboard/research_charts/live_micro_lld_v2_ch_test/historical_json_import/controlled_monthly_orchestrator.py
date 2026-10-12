"""
Phase 4I: controlled monthly import orchestrator (manifest-bound, sequential batches).

No unbounded catalog import. No DELETE/RESTORE. Writes only when env gates + explicit mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .batch_orchestrator import BatchOrchestrator, BatchOrchestratorResult
from .batch_progress import utc_now_iso, write_progress_report
from .batch_resume import assess_close_resume_states
from .batch_runner import BatchImportError, BatchSpec, estimate_batch_write_rows, validate_batch
from .ch_import_resource_monitor import (
    check_optional_limits,
    check_wall_clock_limits,
    collect_resource_baseline,
)
from .recovery_orchestrator_bridge import find_blocked_close_resumes
from .controlled_run_manifest import (
    ControlledRunManifest,
    load_controlled_run_manifest,
    resolve_manifest_stems,
    iter_manifest_batch_specs,
)
from .historical_import_lock import (
    HistoricalImportLockError,
    historical_import_write_lock,
)
from .import_recovery import ImportPartialState, classify_partial_import
from .pilot_overlap import pilot_snapshot_ids_from_catalog
from .write_guards import require_valid_test_run_uuid
from .backup_provenance import BackupProvenanceError, verify_and_record_backup_provenance
from .productive_prewrite_gate import (
    ProductivePrewriteGateError,
    assert_productive_env_gates,
    assess_scope_ch_states,
    load_scope_layer_map_for_snapshots,
    run_productive_prewrite_gate,
)
ControlledMode = Literal["dry_run", "write_and_readback"]


@dataclass
class ControlledOrchestratorResult:
    run_id: str
    status: str
    manifest_path: str | None
    stems: list[str]
    snapshot_ids: list[str]
    expected_writes: dict[str, int]
    batch_specs: list[dict[str, Any]]
    ch_preflight: dict[str, Any] | None
    resource_baseline_before: dict[str, Any]
    resource_baseline_after: dict[str, Any] | None
    resume_states: list[dict[str, Any]]
    batch_results: list[BatchOrchestratorResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_reason: str = ""


class ControlledMonthlyImportOrchestrator:
    """
    Manifest-scoped multi-batch runner.

    Holds a single ``historical_import_write_lock`` for the full write session to avoid
    nested ``flock`` deadlocks with ``execute_batch_import``.
    """

    def __init__(self, catalog_root: Path, *, progress_dir: Path | None = None) -> None:
        self.catalog_root = catalog_root
        self.progress_dir = progress_dir or (catalog_root / "_controlled_import_progress")
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        self._batch = BatchOrchestrator(catalog_root, progress_dir=self.progress_dir)

    def _persist_run_state(
        self,
        manifest: ControlledRunManifest,
        payload: dict[str, Any],
    ) -> Path:
        path = self.progress_dir / f"{manifest.run_id}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _load_prior_run_state(self, manifest: ControlledRunManifest) -> dict[str, Any] | None:
        path = self.progress_dir / f"{manifest.run_id}.json"
        if not path.is_file():
            return None
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior.get("scope_fingerprint") != manifest.scope_fingerprint:
            raise BatchImportError(
                "resume blocked: scope_fingerprint changed since prior orchestrator run"
            )
        return prior

    def dry_run(
        self,
        manifest: ControlledRunManifest,
        *,
        manifest_path: Path | None = None,
    ) -> ControlledOrchestratorResult:
        return self._run(manifest, mode="dry_run", manifest_path=manifest_path)

    def run_from_manifest_file(
        self,
        path: Path,
        *,
        mode: ControlledMode = "dry_run",
        test_run_id: str | None = None,
    ) -> ControlledOrchestratorResult:
        manifest = load_controlled_run_manifest(path)
        if mode == "write_and_readback":
            return self._run(
                manifest,
                mode=mode,
                manifest_path=path,
                test_run_id=test_run_id,
            )
        return self._run(manifest, mode="dry_run", manifest_path=path)

    def _run(
        self,
        manifest: ControlledRunManifest,
        *,
        mode: ControlledMode,
        manifest_path: Path | None = None,
        test_run_id: str | None = None,
    ) -> ControlledOrchestratorResult:
        errors: list[str] = []
        stems = resolve_manifest_stems(self.catalog_root, manifest)
        specs = iter_manifest_batch_specs(self.catalog_root, manifest, stems)

        vr_all: list[dict[str, Any]] = []
        snapshot_ids: list[str] = []
        for spec in specs:
            vr = validate_batch(self.catalog_root, spec, test_run_id=test_run_id)
            vr_all.append(vr)
            if vr["errors"]:
                errors.extend(vr["errors"])
            snapshot_ids.extend(vr.get("snapshot_ids") or [])

        expected = estimate_batch_write_rows(
            {
                "meta_rows": sum(int(v["summary"].get("meta_rows") or 0) for v in vr_all),
                "member_rows": sum(int(v["summary"].get("member_rows") or 0) for v in vr_all),
                "catalog_rows": sum(int(v["summary"].get("catalog_rows") or 0) for v in vr_all),
                "publish_rows": sum(int(v["summary"].get("publish_rows") or 0) for v in vr_all),
            }
        )
        expected["sidecar_rows"] = len(snapshot_ids)

        scope_ids = set(snapshot_ids)
        baseline_before = collect_resource_baseline(
            self.catalog_root,
            pipeline_line_id=manifest.pipeline_line_id,
            scope_snapshot_ids=scope_ids,
        )
        limit_errs = check_optional_limits(baseline_before)
        errors.extend(limit_errs)

        ch_preflight: dict[str, Any] | None = None
        resume_states: list[dict[str, Any]] = []
        scope_checks: list[dict[str, Any]] = []
        if baseline_before.get("clickhouse"):
            ch_preflight = baseline_before["clickhouse"]
            published = set()
            try:
                from .ch_import_state import ch_client_and_db, fetch_meta_complete_snapshot_ids
                from .ch_import_state import fetch_published_snapshot_ids

                client, db = ch_client_and_db()
                try:
                    published = fetch_published_snapshot_ids(
                        client, db, pipeline_line_id=manifest.pipeline_line_id
                    )
                    meta_ids = fetch_meta_complete_snapshot_ids(client, db)
                finally:
                    client.close()
                layer_map = load_scope_layer_map_for_snapshots(snapshot_ids)
                pilot_ids = pilot_snapshot_ids_from_catalog(self.catalog_root)
                scope_checks = assess_scope_ch_states(
                    snapshot_ids,
                    pipeline_line_id=manifest.pipeline_line_id,
                    published_on_line=published,
                    layer_by_snapshot=layer_map,
                    pilot_snapshot_ids=pilot_ids,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"CH resume classification failed: {exc}")
                published = set()
                meta_ids = set()
                pilot_ids = set()
            if not errors:
                pairs = list(zip(stems, snapshot_ids))
                for row in assess_close_resume_states(
                    pairs,
                    pipeline_line_id=manifest.pipeline_line_id,
                    published_on_line=published,
                    meta_snapshot_ids=meta_ids,
                    pilot_ids=pilot_ids,
                ):
                    resume_states.append(
                        {
                            "stem": row.stem,
                            "snapshot_id": row.snapshot_id,
                            "classification": row.classification,
                        }
                    )
                recovery_by_sid = {
                    c["snapshot_id"]: c["recovery_action"] for c in scope_checks
                }
                blocked = find_blocked_close_resumes(resume_states, recovery_by_sid)
                if blocked and mode == "write_and_readback":
                    sample = blocked[0]
                    errors.append(
                        f"scope snapshot blocked for write: close={sample['classification']} "
                        f"recovery={sample.get('recovery_action')}"
                    )

        prior = self._load_prior_run_state(manifest)
        if prior and int(prior.get("batches_completed") or 0) >= manifest.max_batches:
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="complete",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                errors=[],
                stopped_reason="prior run already completed max_batches",
            )

        if errors:
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="preflight_failed",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                errors=errors,
                stopped_reason="preflight",
            )

        if mode == "dry_run":
            self._persist_run_state(
                manifest,
                {
                    "run_id": manifest.run_id,
                    "scope_fingerprint": manifest.scope_fingerprint,
                    "mode": "dry_run",
                    "stems": list(stems),
                    "snapshot_ids": snapshot_ids,
                    "batches_planned": len(specs),
                    "batches_completed": 0,
                    "finished_at": utc_now_iso(),
                },
            )
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="dry_run_ok",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                errors=[],
                stopped_reason=f"dry_run complete; {len(stems)} stems in {len(specs)} batch(es)",
            )

        try:
            assert_productive_env_gates()
        except ProductivePrewriteGateError as exc:
            errors.append(str(exc))
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="blocked",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                errors=errors,
                stopped_reason="write gate",
            )

        run_uuid = require_valid_test_run_uuid(
            test_run_id or manifest.run_id,
            context="ControlledMonthlyImportOrchestrator",
        )

        try:
            if not scope_checks:
                layer_map = load_scope_layer_map_for_snapshots(snapshot_ids)
                from .ch_import_state import ch_client_and_db, fetch_published_snapshot_ids

                client, db = ch_client_and_db()
                try:
                    published_scope = fetch_published_snapshot_ids(
                        client, db, pipeline_line_id=manifest.pipeline_line_id
                    )
                finally:
                    client.close()
                scope_checks = assess_scope_ch_states(
                    snapshot_ids,
                    pipeline_line_id=manifest.pipeline_line_id,
                    published_on_line=published_scope,
                    layer_by_snapshot=layer_map,
                    pilot_snapshot_ids=pilot_snapshot_ids_from_catalog(self.catalog_root),
                )
            run_uuid = run_productive_prewrite_gate(
                test_run_id=run_uuid,
                pipeline_line_id=manifest.pipeline_line_id,
                manifest=manifest,
                scope_ch_checks=scope_checks,
            ).test_run_id
        except ProductivePrewriteGateError as exc:
            errors.append(str(exc))
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="preflight_failed",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                errors=errors,
                stopped_reason="productive pre-write gate",
            )

        try:
            backup_provenance = verify_and_record_backup_provenance(manifest)
        except BackupProvenanceError as exc:
            errors.append(str(exc))
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="preflight_failed",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                errors=errors,
                stopped_reason="backup provenance",
            )

        batch_results: list[BatchOrchestratorResult] = []
        completed = int(prior.get("batches_completed") or 0) if prior else 0
        try:
            with historical_import_write_lock():
                for idx, spec in enumerate(specs):
                    if idx < completed:
                        continue
                    if completed >= manifest.max_batches:
                        break
                    res = self._batch.run_batch(
                        spec,
                        mode="write_and_readback",
                        test_run_id=run_uuid,
                        write_lock_held=True,
                    )
                    batch_results.append(res)
                    timing = res.timing_seconds or {}
                    errors.extend(
                        check_wall_clock_limits(
                            batch_seconds=timing.get("total"),
                            readback_seconds=timing.get("readback"),
                        )
                    )
                    if res.status != "readback_ok":
                        errors.extend(res.errors or [f"batch {spec.batch_id} status={res.status}"])
                        break
                    if errors:
                        break
                    completed += 1
                    import time as _time

                    t_rc0 = _time.monotonic()
                    limit_after = check_optional_limits(
                        collect_resource_baseline(
                            self.catalog_root,
                            pipeline_line_id=manifest.pipeline_line_id,
                            scope_snapshot_ids=scope_ids,
                        )
                    )
                    rc_sec = _time.monotonic() - t_rc0
                    prog_path = self.progress_dir / f"{spec.batch_id}.json"
                    if prog_path.is_file():
                        from .batch_progress import load_progress_report, write_progress_report

                        prog = load_progress_report(prog_path)
                        prog.resource_check_seconds = rc_sec
                        write_progress_report(prog_path, prog)
                    if limit_after:
                        errors.extend(limit_after)
                        break
        except HistoricalImportLockError as exc:
            errors.append(str(exc))
            return ControlledOrchestratorResult(
                run_id=manifest.run_id,
                status="lock_failed",
                manifest_path=str(manifest_path) if manifest_path else None,
                stems=list(stems),
                snapshot_ids=snapshot_ids,
                expected_writes=expected,
                batch_specs=[_spec_summary(s) for s in specs],
                ch_preflight=ch_preflight,
                resource_baseline_before=baseline_before,
                resource_baseline_after=None,
                resume_states=resume_states,
                batch_results=batch_results,
                errors=errors,
                stopped_reason="concurrent writer",
            )

        baseline_after = collect_resource_baseline(
            self.catalog_root,
            pipeline_line_id=manifest.pipeline_line_id,
            scope_snapshot_ids=scope_ids,
        )
        final_status = "readback_ok" if not errors and completed == len(specs) else "failed"
        self._persist_run_state(
            manifest,
            {
                "run_id": manifest.run_id,
                "scope_fingerprint": manifest.scope_fingerprint,
                "mode": "write_and_readback",
                "batches_planned": len(specs),
                "batches_completed": completed,
                "test_run_id": run_uuid,
                "status": final_status,
                "errors": errors,
                "backup_provenance": backup_provenance.to_dict(),
                "finished_at": utc_now_iso(),
            },
        )
        return ControlledOrchestratorResult(
            run_id=manifest.run_id,
            status=final_status,
            manifest_path=str(manifest_path) if manifest_path else None,
            stems=list(stems),
            snapshot_ids=snapshot_ids,
            expected_writes=expected,
            batch_specs=[_spec_summary(s) for s in specs],
            ch_preflight=ch_preflight,
            resource_baseline_before=baseline_before,
            resource_baseline_after=baseline_after,
            resume_states=resume_states,
            batch_results=batch_results,
            errors=errors,
            stopped_reason="write session finished",
        )


def _spec_summary(spec: BatchSpec) -> dict[str, Any]:
    return {
        "batch_id": spec.batch_id,
        "import_batch_id": spec.import_batch_id,
        "stem_count": len(spec.stems),
        "first_stem": spec.stems[0],
        "last_stem": spec.stems[-1],
    }


def classify_close_layers_for_recovery(
    *,
    catalog_complete: bool,
    sidecar_present: bool,
    publish_present: bool,
    provenance_canonical_match: bool | None = None,
    is_pilot_line_overlap: bool = False,
) -> tuple[str, str]:
    """Thin wrapper for tests documenting crash points."""
    return classify_partial_import(
        ImportPartialState(
            catalog_complete=catalog_complete,
            sidecar_present=sidecar_present,
            publish_present=publish_present,
            provenance_canonical_match=provenance_canonical_match,
            is_pilot_line_overlap=is_pilot_line_overlap,
        )
    )

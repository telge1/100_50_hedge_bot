"""
Controlled historical batch orchestrator (validate → write → readback → progress).

Phase 4G: planning and dry-run hooks only unless explicit write env gates are set elsewhere.
No automatic DELETE/RESTORE.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .batch_progress import BatchProgressReport, CloseOutcome, write_progress_report, utc_now_iso
from .batch_resume import batch_success_criteria_met, reconcile_progress_with_ch
from .batch_runner import BatchImportError, BatchSpec, run_batch_readback_gate, validate_batch
from .batch_write import execute_batch_import
from .provenance_sidecar import InMemoryProvenanceRegistry
from .write_guards import require_valid_test_run_uuid
from .productive_prewrite_gate import (
    ProductivePrewriteGateError,
    assert_productive_env_gates,
)

OrchestratorMode = Literal["validate_only", "write_and_readback"]


@dataclass
class BatchOrchestratorResult:
    batch_id: str
    status: str
    validate: dict[str, Any]
    write: list[dict[str, Any]] | None
    readback: dict[str, Any] | None
    errors: list[str]
    timing_seconds: dict[str, float] = field(default_factory=dict)


class BatchOrchestrator:
    """
    Single-batch lifecycle with fail-closed readback before marking progress ``readback_ok``.

    Resume: re-run with same ``BatchSpec``; ``import_historical_close`` / stores skip existing rows.
    """

    def __init__(self, catalog_root: Path, *, progress_dir: Path | None = None) -> None:
        self.catalog_root = catalog_root
        self.progress_dir = progress_dir or (catalog_root / "_batch_progress")

    def run_batch(
        self,
        spec: BatchSpec,
        *,
        mode: OrchestratorMode = "validate_only",
        test_run_id: str | None = None,
        write_lock_held: bool = False,
    ) -> BatchOrchestratorResult:
        errors: list[str] = []
        started_at = utc_now_iso()
        t_batch = time.monotonic()
        t_v0 = time.monotonic()
        vr = validate_batch(self.catalog_root, spec, test_run_id=test_run_id)
        validate_sec = time.monotonic() - t_v0
        if vr["errors"]:
            timing = {
                "validate": validate_sec,
                "write": 0.0,
                "readback": 0.0,
                "total": validate_sec,
            }
            self._write_failed_progress(
                spec,
                vr,
                started_at=started_at,
                timing=timing,
                error_cause="validate_failed",
                errors=vr["errors"],
            )
            return BatchOrchestratorResult(
                spec.batch_id,
                "validate_failed",
                vr,
                None,
                None,
                vr["errors"],
                timing,
            )
        if mode == "validate_only":
            return BatchOrchestratorResult(spec.batch_id, "validated", vr, None, None, [])

        try:
            assert_productive_env_gates()
        except ProductivePrewriteGateError as exc:
            errors.append(str(exc))
            timing = {
                "validate": validate_sec,
                "write": 0.0,
                "readback": 0.0,
                "total": validate_sec,
            }
            return BatchOrchestratorResult(spec.batch_id, "blocked", vr, None, None, errors, timing)

        import uuid

        run_uuid = require_valid_test_run_uuid(
            test_run_id or str(uuid.uuid4()),
            context="BatchOrchestrator",
        )
        t0 = time.monotonic()
        write_out = execute_batch_import(
            self.catalog_root,
            spec,
            test_run_id=run_uuid,
            write_lock_held=write_lock_held,
        )
        write_sec = time.monotonic() - t0
        t1 = time.monotonic()
        rb = run_batch_readback_gate(
            list(spec.stems),
            catalog_root=self.catalog_root,
            combo_folder=spec.combo_folder,
            month=spec.month,
            pipeline_line_id=spec.pipeline_line_id,
            batch_spec=spec,
            require_sidecar=True,
        )
        readback_sec = time.monotonic() - t1
        total_sec = time.monotonic() - t_batch
        timing = {
            "validate": validate_sec,
            "write": write_sec,
            "readback": readback_sec,
            "total": total_sec,
        }
        bad_p = [r for r in rb["parity"] if not r.ok]
        bad_s = [r for r in rb["sidecar"] if not r.ok]
        if bad_p or bad_s:
            errors.append(f"readback parity failures={len(bad_p)} sidecar={len(bad_s)}")
            self._write_failed_progress(
                spec,
                vr,
                started_at=started_at,
                timing=timing,
                error_cause="readback_failed",
                errors=errors,
            )
            return BatchOrchestratorResult(
                spec.batch_id,
                "readback_failed",
                vr,
                write_out,
                rb,
                errors,
                timing,
            )

        reg = InMemoryProvenanceRegistry()
        for stem in spec.stems:
            from .sidecar_readback import expected_sidecar_for_stem

            reg.register(
                expected_sidecar_for_stem(self.catalog_root, spec, stem)
            )
        prog_path = self.progress_dir / f"{spec.batch_id}.json"
        prog = BatchProgressReport(
            batch_id=spec.batch_id,
            pipeline_line_id=spec.pipeline_line_id,
            combo_folder=spec.combo_folder,
            month=spec.month,
            import_batch_id=spec.import_batch_id,
            status="readback_ok",
            closes_planned=len(spec.stems),
            closes=[
                CloseOutcome(stem=r["stem"], snapshot_id=r["snapshot_id"], status="readback_ok")
                for r in write_out
            ],
            summary=vr["summary"],
            errors=errors,
            started_at=started_at,
            finished_at=utc_now_iso(),
            timing_seconds=timing,
        )
        write_progress_report(prog_path, prog)
        ok, crit_errs = batch_success_criteria_met(
            all_published_on_line=True,
            readback_ok=True,
            provenance_registry=reg,
            snapshot_ids=vr["snapshot_ids"],
            progress_written=True,
            pipeline_line_id=spec.pipeline_line_id,
        )
        if not ok:
            errors.extend(crit_errs)
            prog.status = "failed"
            write_progress_report(prog_path, prog)
        reconcile_progress_with_ch(
            prog_path,
            ch_published_count=len(spec.stems),
            expected_in_batch=len(spec.stems),
        )
        status = "readback_ok" if ok and not errors else "failed"
        if status != "readback_ok":
            prog.error_cause = "success_criteria_failed"
            prog.timing_seconds = timing
            write_progress_report(prog_path, prog)
        return BatchOrchestratorResult(
            spec.batch_id, status, vr, write_out, rb, errors, timing
        )

    def _write_failed_progress(
        self,
        spec: BatchSpec,
        vr: dict[str, Any],
        *,
        started_at: str,
        timing: dict[str, float],
        error_cause: str,
        errors: list[str],
    ) -> None:
        prog_path = self.progress_dir / f"{spec.batch_id}.json"
        prog = BatchProgressReport(
            batch_id=spec.batch_id,
            pipeline_line_id=spec.pipeline_line_id,
            combo_folder=spec.combo_folder,
            month=spec.month,
            import_batch_id=spec.import_batch_id,
            status="failed",
            closes_planned=len(spec.stems),
            closes=[],
            summary=vr.get("summary") or {},
            errors=errors,
            started_at=started_at,
            finished_at=utc_now_iso(),
            timing_seconds=timing,
            error_cause=error_cause,
        )
        write_progress_report(prog_path, prog)

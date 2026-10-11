"""
Controlled historical batch orchestrator (validate → write → readback → progress).

Phase 4G: planning and dry-run hooks only unless explicit write env gates are set elsewhere.
No automatic DELETE/RESTORE.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .batch_progress import BatchProgressReport, CloseOutcome, write_progress_report, utc_now_iso
from .batch_resume import batch_success_criteria_met, reconcile_progress_with_ch
from .batch_runner import BatchImportError, BatchSpec, run_batch_readback_gate, validate_batch
from .batch_write import execute_batch_import
from .provenance_sidecar import InMemoryProvenanceRegistry
from .write_guards import require_valid_test_run_uuid

OrchestratorMode = Literal["validate_only", "write_and_readback"]


@dataclass
class BatchOrchestratorResult:
    batch_id: str
    status: str
    validate: dict[str, Any]
    write: list[dict[str, Any]] | None
    readback: dict[str, Any] | None
    errors: list[str]


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
    ) -> BatchOrchestratorResult:
        errors: list[str] = []
        vr = validate_batch(self.catalog_root, spec, test_run_id=test_run_id)
        if vr["errors"]:
            return BatchOrchestratorResult(
                spec.batch_id,
                "validate_failed",
                vr,
                None,
                None,
                vr["errors"],
            )
        if mode == "validate_only":
            return BatchOrchestratorResult(spec.batch_id, "validated", vr, None, None, [])

        if os.environ.get("HISTORICAL_JSON_IMPORT_ALLOW_WRITE") != "1":
            errors.append("HISTORICAL_JSON_IMPORT_ALLOW_WRITE not set")
            return BatchOrchestratorResult(spec.batch_id, "blocked", vr, None, None, errors)

        import uuid

        run_uuid = require_valid_test_run_uuid(
            test_run_id or str(uuid.uuid4()),
            context="BatchOrchestrator",
        )
        write_out = execute_batch_import(
            self.catalog_root,
            spec,
            test_run_id=run_uuid,
        )
        rb = run_batch_readback_gate(
            list(spec.stems),
            catalog_root=self.catalog_root,
            combo_folder=spec.combo_folder,
            month=spec.month,
            pipeline_line_id=spec.pipeline_line_id,
            batch_spec=spec,
            require_sidecar=True,
        )
        bad_p = [r for r in rb["parity"] if not r.ok]
        bad_s = [r for r in rb["sidecar"] if not r.ok]
        if bad_p or bad_s:
            errors.append(f"readback parity failures={len(bad_p)} sidecar={len(bad_s)}")
            return BatchOrchestratorResult(
                spec.batch_id, "readback_failed", vr, write_out, rb, errors
            )

        reg = InMemoryProvenanceRegistry()
        for stem in spec.stems:
            from .sidecar_readback import expected_sidecar_for_stem

            reg.register(
                expected_sidecar_for_stem(self.catalog_root, spec, stem)
            )
        ok, crit_errs = batch_success_criteria_met(
            all_published_on_line=True,
            readback_ok=True,
            provenance_registry=reg,
            snapshot_ids=vr["snapshot_ids"],
            progress_written=False,
            pipeline_line_id=spec.pipeline_line_id,
        )
        if not ok:
            errors.extend(crit_errs)

        prog_path = self.progress_dir / f"{spec.batch_id}.json"
        prog = BatchProgressReport(
            batch_id=spec.batch_id,
            pipeline_line_id=spec.pipeline_line_id,
            combo_folder=spec.combo_folder,
            month=spec.month,
            import_batch_id=spec.import_batch_id,
            status="readback_ok" if ok else "failed",
            closes_planned=len(spec.stems),
            closes=[
                CloseOutcome(stem=r["stem"], snapshot_id=r["snapshot_id"], status="readback_ok")
                for r in write_out
            ],
            summary=vr["summary"],
            errors=errors,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
        )
        write_progress_report(prog_path, prog)
        reconcile_progress_with_ch(
            prog_path,
            ch_published_count=len(spec.stems),
            expected_in_batch=len(spec.stems),
        )
        status = "readback_ok" if ok and not errors else "failed"
        return BatchOrchestratorResult(spec.batch_id, status, vr, write_out, rb, errors)

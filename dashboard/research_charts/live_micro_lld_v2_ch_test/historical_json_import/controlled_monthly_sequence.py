"""
Sequential multi-manifest controlled import (one month, multiple combos).

Holds ordering and stop-on-failure semantics for a full-month run.
Writes only when mode is ``write_and_readback`` and env gates are set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .batch_runner import BatchImportError
from .controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
    ControlledOrchestratorResult,
)
from .controlled_run_manifest import (
    ControlledRunManifest,
    load_controlled_run_manifest,
)
from .post_import_verification import verify_manifest_scope_ch_counts

SequenceMode = Literal["dry_run", "write_and_readback", "plan_only"]


@dataclass
class MonthlySequenceStep:
    combo_folder: str
    manifest_path: str
    manifest: ControlledRunManifest


@dataclass
class MonthlySequencePlan:
    schema_version: int
    approved_month: str
    backup_recovery_id: str
    combo_order: tuple[str, ...]
    steps: tuple[MonthlySequenceStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approved_month": self.approved_month,
            "backup_recovery_id": self.backup_recovery_id,
            "combo_order": list(self.combo_order),
            "steps": [
                {
                    "combo_folder": s.combo_folder,
                    "manifest_path": s.manifest_path,
                    "run_id": s.manifest.run_id,
                    "scope_fingerprint": s.manifest.scope_fingerprint,
                    "max_snapshots": s.manifest.max_snapshots,
                    "max_batches": s.manifest.max_batches,
                    "pipeline_line_id": s.manifest.pipeline_line_id,
                }
                for s in self.steps
            ],
        }


@dataclass
class MonthlySequenceResult:
    status: str
    mode: SequenceMode
    steps: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_at_combo: str | None = None
    stopped_reason: str = ""


def load_monthly_sequence_plan(path: Path) -> MonthlySequencePlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema_version", 0)) != 1:
        raise BatchImportError(f"unsupported monthly plan schema: {data.get('schema_version')}")
    combo_order = tuple(str(c) for c in data["combo_order"])
    steps: list[MonthlySequenceStep] = []
    backup_id = str(data["backup_recovery_id"])
    for entry in data["manifests"]:
        combo = str(entry["combo_folder"])
        mpath = Path(entry["manifest_path"])
        if not mpath.is_file():
            raise BatchImportError(f"missing manifest for {combo}: {mpath}")
        manifest = load_controlled_run_manifest(mpath)
        if manifest.backup_recovery_id != backup_id:
            raise BatchImportError(
                f"{combo} manifest backup_recovery_id != plan backup_recovery_id"
            )
        if manifest.approved_month != str(data["approved_month"]):
            raise BatchImportError(f"{combo} month mismatch")
        steps.append(
            MonthlySequenceStep(
                combo_folder=combo,
                manifest_path=str(mpath),
                manifest=manifest,
            )
        )
    ordered = [s.combo_folder for s in steps]
    if tuple(ordered) != combo_order:
        raise BatchImportError(f"manifest order {ordered} != combo_order {combo_order}")
    return MonthlySequencePlan(
        schema_version=1,
        approved_month=str(data["approved_month"]),
        backup_recovery_id=backup_id,
        combo_order=combo_order,
        steps=tuple(steps),
    )


class ControlledMonthlySequenceRunner:
    """Run manifest list in fixed order; abort entire month on first combo failure."""

    def __init__(self, catalog_root: Path, *, progress_dir: Path) -> None:
        self.catalog_root = catalog_root
        self.progress_dir = progress_dir
        self.progress_dir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        plan: MonthlySequencePlan,
        *,
        mode: SequenceMode,
        resume_state_path: Path | None = None,
    ) -> MonthlySequenceResult:
        if mode == "plan_only":
            return MonthlySequenceResult(
                status="plan_ok",
                mode=mode,
                steps=[{"combo": s.combo_folder, "manifest": s.manifest_path} for s in plan.steps],
            )

        state = self._load_state(resume_state_path)
        start_idx = int(state.get("next_combo_index") or 0)
        results: list[dict[str, Any]] = []
        errors: list[str] = []

        for idx, step in enumerate(plan.steps):
            if idx < start_idx:
                continue
            orch = ControlledMonthlyImportOrchestrator(
                self.catalog_root,
                progress_dir=self.progress_dir / step.combo_folder,
            )
            mpath = Path(step.manifest_path)
            if mode == "dry_run":
                res = orch.dry_run(step.manifest, manifest_path=mpath)
            elif mode == "write_and_readback":
                res = orch.run_from_manifest_file(mpath, mode="write_and_readback")
            else:
                raise BatchImportError(f"unsupported mode {mode}")

            step_payload = {
                "combo_folder": step.combo_folder,
                "manifest_path": step.manifest_path,
                "orchestrator_status": res.status,
                "stems": len(res.stems),
                "batches": len(res.batch_specs),
                "expected_writes": res.expected_writes,
                "errors": res.errors,
            }
            if mode == "write_and_readback" and res.status == "readback_ok":
                counts = verify_manifest_scope_ch_counts(
                    self.catalog_root, step.manifest, chunk_size=100
                )
                step_payload["post_combo_ch_counts"] = counts
            results.append(step_payload)

            if res.status not in ("dry_run_ok", "readback_ok"):
                errors.extend(res.errors or [res.stopped_reason or res.status])
                self._save_state(
                    resume_state_path,
                    {
                        "next_combo_index": idx,
                        "last_failed_combo": step.combo_folder,
                        "status": "stopped",
                        "errors": errors,
                    },
                )
                return MonthlySequenceResult(
                    status="failed",
                    mode=mode,
                    steps=results,
                    errors=errors,
                    stopped_at_combo=step.combo_folder,
                    stopped_reason=res.stopped_reason or res.status,
                )

            self._save_state(
                resume_state_path,
                {
                    "next_combo_index": idx + 1,
                    "last_completed_combo": step.combo_folder,
                    "status": "in_progress" if idx + 1 < len(plan.steps) else "complete",
                },
            )

        final = "dry_run_ok" if mode == "dry_run" else "readback_ok"
        return MonthlySequenceResult(
            status=final,
            mode=mode,
            steps=results,
            errors=[],
            stopped_reason="month sequence finished",
        )

    def _load_state(self, path: Path | None) -> dict[str, Any]:
        if path is None or not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_state(self, path: Path | None, payload: dict[str, Any]) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

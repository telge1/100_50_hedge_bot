"""Jun–Sep 2026 overnight multi-month import (sequential months, fail-closed)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .batch_runner import BatchImportError
from .controlled_monthly_sequence import (
    ControlledMonthlySequenceRunner,
    load_monthly_sequence_plan,
)
from .post_import_verification import verify_manifest_file

OvernightMode = Literal["dry_run", "write_and_readback"]


@dataclass
class OvernightMonthStep:
    month: str
    progress_subdir: str
    monthly_plan_path: str


@dataclass
class OvernightImportPlan:
    schema_version: int
    overnight_run_id: str
    backup_recovery_id: str
    months: tuple[OvernightMonthStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overnight_run_id": self.overnight_run_id,
            "backup_recovery_id": self.backup_recovery_id,
            "months": [
                {
                    "month": m.month,
                    "progress_subdir": m.progress_subdir,
                    "monthly_plan_path": m.monthly_plan_path,
                }
                for m in self.months
            ],
        }


@dataclass
class OvernightSequenceResult:
    status: str
    mode: OvernightMode
    months: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_at_month: str | None = None
    stopped_reason: str = ""


def load_overnight_import_plan(path: Path) -> OvernightImportPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema_version", 0)) != 1:
        raise BatchImportError(f"unsupported overnight plan schema: {data.get('schema_version')}")
    months = tuple(
        OvernightMonthStep(
            month=str(m["month"]),
            progress_subdir=str(m["progress_subdir"]),
            monthly_plan_path=str(m["monthly_plan_path"]),
        )
        for m in data["months"]
    )
    return OvernightImportPlan(
        schema_version=1,
        overnight_run_id=str(data["overnight_run_id"]),
        backup_recovery_id=str(data["backup_recovery_id"]),
        months=months,
    )


class ControlledOvernightSequenceRunner:
    def __init__(self, catalog_root: Path, *, progress_root: Path) -> None:
        self.catalog_root = catalog_root
        self.progress_root = progress_root
        self.progress_root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        plan: OvernightImportPlan,
        *,
        mode: OvernightMode,
        state_path: Path,
        log_root: Path,
    ) -> OvernightSequenceResult:
        state = self._load_state(state_path)
        start_idx = int(state.get("next_month_index") or 0)
        results: list[dict[str, Any]] = []
        errors: list[str] = []

        for idx, month_step in enumerate(plan.months):
            if idx < start_idx:
                continue
            month_plan = load_monthly_sequence_plan(Path(month_step.monthly_plan_path))
            if month_plan.backup_recovery_id != plan.backup_recovery_id:
                raise BatchImportError(
                    f"{month_step.month} monthly plan backup id != overnight plan"
                )
            progress = self.progress_root / month_step.progress_subdir
            runner = ControlledMonthlySequenceRunner(self.catalog_root, progress_dir=progress)
            month_state = progress / "monthly_resume_state.json"
            seq_res = runner.run(
                month_plan,
                mode=mode,
                resume_state_path=month_state,
            )
            month_payload: dict[str, Any] = {
                "month": month_step.month,
                "progress_subdir": month_step.progress_subdir,
                "sequence_status": seq_res.status,
                "steps": seq_res.steps,
                "errors": seq_res.errors,
            }
            if mode == "write_and_readback" and seq_res.status == "readback_ok":
                final_rb: dict[str, Any] = {"combos": []}
                for step in month_plan.steps:
                    final_rb["combos"].append(
                        verify_manifest_file(
                            self.catalog_root,
                            Path(step.manifest_path),
                            test_run_id=step.manifest.run_id,
                        )
                    )
                month_payload["month_readback_ok"] = all(c.get("ok") for c in final_rb["combos"])
                report_path = log_root / f"{month_step.progress_subdir}_report.json"
                report_path.write_text(
                    json.dumps({"month": month_step.month, "readback": final_rb}, indent=2),
                    encoding="utf-8",
                )
                month_payload["month_report_path"] = str(report_path)
            results.append(month_payload)

            self._save_state(
                state_path,
                {
                    "next_month_index": idx,
                    "last_month": month_step.month,
                    "last_sequence_status": seq_res.status,
                    "status": "in_progress",
                },
            )

            if seq_res.status not in ("dry_run_ok", "readback_ok"):
                errors.extend(seq_res.errors or [seq_res.stopped_reason or seq_res.status])
                return OvernightSequenceResult(
                    status="failed",
                    mode=mode,
                    months=results,
                    errors=errors,
                    stopped_at_month=month_step.month,
                    stopped_reason=seq_res.stopped_reason or seq_res.status,
                )

            self._save_state(
                state_path,
                {
                    "next_month_index": idx + 1,
                    "last_completed_month": month_step.month,
                    "status": "complete" if idx + 1 >= len(plan.months) else "in_progress",
                },
            )

        final = "dry_run_ok" if mode == "dry_run" else "readback_ok"
        return OvernightSequenceResult(
            status=final,
            mode=mode,
            months=results,
            errors=[],
            stopped_reason="overnight sequence finished",
        )

    def _load_state(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_state(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

"""Atomic artifact writers with RUNNING → COMPLETE | FAILED status model."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import AnalysisFailure
from .hashing import canonical_json, report_content_hash
from .models import XRayResult

ARTIFACTS = (
    "manifest.json",
    "readiness.json",
    "report.json",
    "minute_metrics.json",
    "trade_sequence.json",
    "trade_dedup_report.json",
    "wall_lifecycle.json",
    "breakout_states.json",
    "coverage_report.json",
    "run.log",
)

OUTPUT_DIR_ALREADY_EXISTS = "OUTPUT_DIR_ALREADY_EXISTS"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETE = "COMPLETE"
STATUS_FAILED = "FAILED"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


class RunOutputSession:
    """Write into ``<output>.running.<id>`` then rename to ``output`` or ``.failed.<id>``."""

    def __init__(self, output_dir: Path, *, allow_existing: bool = False):
        self.final_dir = Path(output_dir)
        self.run_id = _new_run_id()
        self.running_dir = self.final_dir.parent / (
            f"{self.final_dir.name}.running.{self.run_id}"
        )
        self.failed_dir = self.final_dir.parent / (
            f"{self.final_dir.name}.failed.{self.run_id}"
        )
        if self.final_dir.exists() and not allow_existing:
            raise RuntimeError(OUTPUT_DIR_ALREADY_EXISTS)
        self.running_dir.mkdir(parents=True, exist_ok=False)
        self.status = STATUS_RUNNING
        self._manifest_extra: dict[str, Any] = {
            "run_id": self.run_id,
        }

    def write_running_marker(self) -> None:
        _write_json(
            self.running_dir / "run_status.json",
            {"status": STATUS_RUNNING, "run_id": self.run_id},
        )

    def commit_complete(
        self,
        result: XRayResult,
        *,
        log_lines: list[str],
        pre_rename_check: Callable[[], None] | None = None,
        manifest_extra: dict[str, Any] | None = None,
    ) -> str:
        digest = _write_all_artifacts(
            self.running_dir,
            result,
            log_lines=log_lines,
            status=STATUS_COMPLETE,
            failure=None,
            manifest_extra={**self._manifest_extra, **(manifest_extra or {})},
        )
        if pre_rename_check is not None:
            pre_rename_check()
        # Atomic replace onto final path
        os.replace(str(self.running_dir), str(self.final_dir))
        self.status = STATUS_COMPLETE
        return digest

    def abort_failed(
        self,
        result: XRayResult | None,
        *,
        log_lines: list[str],
        failure: AnalysisFailure,
        manifest_extra: dict[str, Any] | None = None,
    ) -> Path:
        # Prefer writing into running dir then rename to failed.
        target_staging = self.running_dir if self.running_dir.exists() else self.failed_dir
        if not target_staging.exists():
            target_staging.mkdir(parents=True, exist_ok=True)
        if result is None:
            # Minimal failure shell
            from .models import (
                AnalysisWindow,
                BaselineBookState,
                ReferenceLevel,
                ReferenceSource,
                RunMode,
                WallThresholdSpec,
                XRayResult as XR,
            )
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            result = XR(
                mode=RunMode.MANUAL_WINDOW,
                window=AnalysisWindow(
                    symbol="UNKNOWN",
                    start_utc=now,
                    end_utc=now,
                    start_ns=0,
                    end_ns=0,
                    chain_version="",
                    chain_hash="",
                ),
                reference=ReferenceLevel(
                    price=None,
                    side=None,
                    source=ReferenceSource.NONE,
                    known_as_of_utc=None,
                    causal_reference=False,
                ),
                wall_threshold=WallThresholdSpec(
                    method="none",
                    window_start=now,
                    window_end=now,
                    known_as_of_utc=now,
                    causal=False,
                ),
                baseline=BaselineBookState(
                    source="failed",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason=failure.failure_reason,
                ),
                sections={"failure": failure.to_dict()},
                data_quality={"failure": failure.to_dict()},
            )
        _write_all_artifacts(
            target_staging,
            result,
            log_lines=log_lines,
            status=STATUS_FAILED,
            failure=failure,
            manifest_extra={**self._manifest_extra, **(manifest_extra or {})},
        )
        if target_staging != self.failed_dir:
            if self.failed_dir.exists():
                raise RuntimeError("FAILED_DIR_ALREADY_EXISTS")
            os.replace(str(target_staging), str(self.failed_dir))
        self.status = STATUS_FAILED
        return self.failed_dir


def _write_all_artifacts(
    out_dir: Path,
    result: XRayResult,
    *,
    log_lines: list[str],
    status: str,
    failure: AnalysisFailure | None,
    manifest_extra: dict[str, Any],
) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    # Strip volatile lock probes from content hash
    digest = report_content_hash(payload)
    result.report_hash = digest
    payload["report_hash"] = digest
    payload["run_status"] = status
    if failure is not None:
        payload["failure"] = failure.to_dict()

    sections = result.sections
    manifest = {
        "status": status,
        "mode": result.mode.value,
        "reference_source": result.reference.source.value,
        "causal_reference": result.reference.causal_reference,
        "start_utc": result.window.start_utc.isoformat().replace("+00:00", "Z"),
        "end_utc": result.window.end_utc.isoformat().replace("+00:00", "Z"),
        "symbol": result.window.symbol,
        "chain_version": result.window.chain_version,
        "chain_hash": result.window.chain_hash,
        "reference": result.reference.to_dict(),
        "wall_threshold": result.wall_threshold.to_dict(),
        "baseline": result.baseline.to_dict(),
        "package_report_hash": digest,
        **(sections.get("mp_manifest") or {}),
        **manifest_extra,
    }
    if failure is not None:
        manifest["failure"] = failure.to_dict()
    manifest["status"] = status
    _write_json(out_dir / "manifest.json", manifest)
    _write_json(out_dir / "readiness.json", sections.get("readiness") or {})
    _write_json(out_dir / "report.json", payload)
    _write_json(out_dir / "minute_metrics.json", sections.get("minute_metrics") or [])
    _write_json(out_dir / "trade_sequence.json", sections.get("trade_sequence") or [])
    _write_json(out_dir / "trade_dedup_report.json", sections.get("trade_dedup") or {})
    _write_json(out_dir / "wall_lifecycle.json", sections.get("wall_lifecycles") or [])
    _write_json(out_dir / "breakout_states.json", sections.get("breakout_states"))
    _write_json(out_dir / "coverage_report.json", sections.get("coverage") or {})
    _write_json(
        out_dir / "run_status.json",
        {"status": status, "run_id": manifest_extra.get("run_id"), "report_hash": digest},
    )
    (out_dir / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return digest


def write_xray_artifacts(out_dir: Path, result: XRayResult, *, log_lines: list[str]) -> str:
    """Backward-compatible helper for check-only / unit tests (direct write).

    Prefer ``RunOutputSession`` for live/full runs.
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        # Tests often reuse tmp_path subdirs; allow empty or create leaf.
        pass
    out_dir.mkdir(parents=True, exist_ok=True)
    return _write_all_artifacts(
        out_dir,
        result,
        log_lines=log_lines,
        status=STATUS_COMPLETE,
        failure=None,
        manifest_extra={"run_id": "legacy_direct", "status": STATUS_COMPLETE},
    )

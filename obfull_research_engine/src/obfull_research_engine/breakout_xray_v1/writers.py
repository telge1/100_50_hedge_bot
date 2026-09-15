"""Artifact writers for Breakout X-Ray V1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def write_xray_artifacts(out_dir: Path, result: XRayResult, *, log_lines: list[str]) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    digest = report_content_hash(payload)
    result.report_hash = digest
    payload["report_hash"] = digest

    sections = result.sections
    manifest = {
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
    }
    _write_json(out_dir / "manifest.json", manifest)
    _write_json(out_dir / "readiness.json", sections.get("readiness") or {})
    _write_json(out_dir / "report.json", payload)
    _write_json(out_dir / "minute_metrics.json", sections.get("minute_metrics") or [])
    _write_json(out_dir / "trade_sequence.json", sections.get("trade_sequence") or [])
    _write_json(out_dir / "trade_dedup_report.json", sections.get("trade_dedup") or {})
    _write_json(out_dir / "wall_lifecycle.json", sections.get("wall_lifecycles") or [])
    _write_json(out_dir / "breakout_states.json", sections.get("breakout_states"))
    _write_json(out_dir / "coverage_report.json", sections.get("coverage") or {})
    (out_dir / "run.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return digest

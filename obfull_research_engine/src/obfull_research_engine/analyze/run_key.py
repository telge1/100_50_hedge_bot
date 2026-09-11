"""Run-key and run-directory helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import ENGINE_ROOT
from . import ORCHESTRATOR_VERSION

ANALYSIS_RUNS_ROOT = ENGINE_ROOT / "results" / "analysis_runs_v1"


def format_window_id(start: datetime, end: datetime) -> str:
    def _c(dt: datetime) -> str:
        return dt.astimezone().strftime("%Y-%m-%dT%H%M%SZ") if False else (
            dt.strftime("%Y-%m-%dT%H%M%SZ")
        )

    # Always UTC Z compact (inputs already UTC)
    s = start.strftime("%Y-%m-%dT%H%M%SZ")
    e = end.strftime("%Y-%m-%dT%H%M%SZ")
    return f"{s}__{e}"


def compute_run_key(
    *,
    symbol: str,
    start_z: str,
    end_z: str,
    state_schema_hash: str,
    candidate_config_hash: str,
    grouper_config_hash: str,
    outcome_config_hash: str,
    orchestrator_version: str = ORCHESTRATOR_VERSION,
    with_avr: bool = False,
    avr_contract_version: str | None = None,
    avr_config_hash: str | None = None,
    avr_source_code_hash: str | None = None,
    with_avr_multiscale: bool = False,
    multiscale_contract_version: str | None = None,
    multiscale_windows: list[int] | tuple[int, ...] | None = None,
    footprint_config_hash: str | None = None,
    oi_liq_join_version: str | None = None,
    coverage_policy: str | None = None,
    coverage_policy_version: str | None = None,
    coverage_policy_config_hash: str | None = None,
    warmup_anchor_policy: str | None = None,
    usable_spans_hash: str | None = None,
    base_analysis_eligible_start: str | None = None,
    final_analysis_eligible_start: str | None = None,
) -> str:
    # Legacy run-keys must stay identical when AVR is off: do not fold
    # with_avr=false into the identity payload.
    payload: dict[str, Any] = {
        "symbol": symbol.upper(),
        "start": start_z,
        "end": end_z,
        "state_schema_hash": state_schema_hash,
        "candidate_config_hash": candidate_config_hash,
        "grouper_config_hash": grouper_config_hash,
        "outcome_config_hash": outcome_config_hash,
        "orchestrator_version": orchestrator_version,
    }
    if with_avr or with_avr_multiscale:
        payload["with_avr"] = True
        payload["avr_contract_version"] = avr_contract_version
        payload["avr_config_hash"] = avr_config_hash
        payload["avr_source_code_hash"] = avr_source_code_hash
    if with_avr_multiscale:
        payload["with_avr_multiscale"] = True
        payload["multiscale_contract_version"] = multiscale_contract_version
        payload["multiscale_windows"] = list(multiscale_windows or [])
        payload["footprint_config_hash"] = footprint_config_hash
        payload["oi_liq_join_version"] = oi_liq_join_version
    # Only fold coverage policy into identity when not legacy strict default
    if coverage_policy and coverage_policy != "STRICT_WHOLE_WINDOW":
        payload["coverage_policy"] = coverage_policy
        payload["coverage_policy_version"] = coverage_policy_version
        payload["coverage_policy_config_hash"] = coverage_policy_config_hash
    # Localized + AVR only. Strict / core / legacy keys stay byte-identical.
    if warmup_anchor_policy:
        payload["warmup_anchor_policy"] = warmup_anchor_policy
        if usable_spans_hash:
            payload["usable_spans_hash"] = usable_spans_hash
        if base_analysis_eligible_start:
            payload["base_analysis_eligible_start"] = base_analysis_eligible_start
        if final_analysis_eligible_start:
            payload["final_analysis_eligible_start"] = final_analysis_eligible_start
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def run_dir_for(*, symbol: str, start: datetime, end: datetime, run_key: str) -> Path:
    return ANALYSIS_RUNS_ROOT / symbol.upper() / format_window_id(start, end) / f"rk_{run_key[:16]}"


def file_sha256(path: Path) -> str | None:
    if not path.exists() or path.name.endswith(".tmp"):
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: Path, obj: Any) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

"""Checkpoint handoff inventory and contract audit (research only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_memory_state import CHECKPOINT_VERSION, MarketMemoryState, SCHEMA_CODE_VERSION

HANDOFF_REQUIRED_TOP_LEVEL = (
    "version",
    "asof_utc",
    "watermark_utc",
    "schema_code_version",
    "origins",
    "upper_origins",
    "spatial_steps",
    "drop_episodes",
    "active_pool_ids",
    "seen_origins",
    "main_last_step_bottom",
    "main_step_seq",
    "main_step_initialized",
    "price_processor",
    "price_timeline",
    "processed_publications",
    "event_cursor",
)

HANDOFF_PRICE_PROCESSOR_FIELDS = (
    "watermark_5m",
    "bar_ring",
    "drop_episodes",
    "processed_bars",
)

Classification = str  # "A" | "B" | "C"


def _file_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    st = path.stat()
    row: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "bytes": st.st_size,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if path.suffix == ".json" and st.st_size < 50_000_000:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                row["checkpoint_version"] = data.get("version")
                row["asof_utc"] = data.get("asof_utc")
                row["watermark_utc"] = data.get("watermark_utc")
                row["schema_code_version"] = data.get("schema_code_version")
                row["semantic_state_hash"] = data.get("semantic_state_hash") or data.get("state_hash")
                row["has_price_timeline"] = bool(data.get("price_timeline"))
                row["price_timeline_len"] = len(data.get("price_timeline") or [])
        except (json.JSONDecodeError, OSError):
            row["parse_error"] = True
    return row


def inventory_checkpoint_files(repo: Path) -> list[dict[str, Any]]:
    patterns = [
        repo / "logs/historical_import/overnight_2026/phase6xt_90d_integrated_price_pool_memory_v1/rerun_20261016T182520Z",
        repo / "logs/historical_import/overnight_2026/phase6xt_90d_market_memory_bootstrap_catchup_v1",
        repo / "logs/historical_import/overnight_2026/phase6xt_continuous_landscape_paper_runner_v1",
        repo / "logs/historical_import/overnight_2026/phase6xt_market_memory_price_stream_recovery_v1",
    ]
    files: list[dict[str, Any]] = []
    for base in patterns:
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            name = p.name.lower()
            if "checkpoint" in name or name.endswith(".bin.json"):
                files.append(_file_meta(p))
    return files


def classify_validated_source(
    validated_dir: Path,
    validated_hash: str | None,
    inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """A=Path-A persistiert, B=semantisch identisch B/C persistiert, C=nur Hashes/Summaries."""
    persisted_a = None
    persisted_b = None
    for row in inventory:
        if not row.get("exists"):
            continue
        p = row.get("path", "")
        if "rerun_20261016T182520Z" in p:
            continue
        h = row.get("semantic_state_hash")
        if h and validated_hash and h == validated_hash:
            if row.get("asof_utc", "").startswith("2026-08-19"):
                persisted_a = row
            else:
                persisted_b = persisted_b or row

    rerun_has_cp = any(
        r.get("exists") and "rerun_20261016T182520Z" in r.get("path", "") and "checkpoint" in r.get("path", "")
        for r in inventory
    )
    parity_files = [
        validated_dir / "checkpoint_restart_semantic_parity.json",
        validated_dir / "multiple_restart_parity.json",
        validated_dir / "full_replay_state_summary.json",
    ]
    has_hashes = all(p.is_file() for p in parity_files)

    if persisted_a:
        klass = "A"
    elif persisted_b:
        klass = "B"
    else:
        klass = "C"

    return {
        "classification": klass,
        "definition": {
            "A": "Full Path-A-compatible checkpoint persisted at boot end",
            "B": "Semantically identical Path-B/C checkpoint (hash match, not necessarily Path-A label)",
            "C": "Hashes and summaries only; no restorable full state in validated rerun dir",
        }[klass],
        "validated_rerun_persisted_checkpoint_file": rerun_has_cp,
        "hash_summary_artifacts_present": has_hashes,
        "candidate_matching_validated_hash": persisted_a or persisted_b,
        "validated_semantic_hash": validated_hash,
    }


def audit_state_completeness(data: dict[str, Any]) -> dict[str, Any]:
    missing_top = [k for k in HANDOFF_REQUIRED_TOP_LEVEL if k not in data]
    pp = data.get("price_processor") or {}
    missing_pp = [k for k in HANDOFF_PRICE_PROCESSOR_FIELDS if k not in pp]
    issues: list[str] = []
    ver = data.get("version")
    if ver != CHECKPOINT_VERSION:
        issues.append(f"checkpoint version {ver} expected {CHECKPOINT_VERSION}")
    if data.get("schema_code_version") != SCHEMA_CODE_VERSION:
        issues.append(f"schema {data.get('schema_code_version')} != {SCHEMA_CODE_VERSION}")
    for pd in ("1", "10"):
        if pd not in (data.get("origins") or {}):
            issues.append(f"missing origins PD{pd}")
        if pd not in (data.get("upper_origins") or {}):
            issues.append(f"missing upper_origins PD{pd}")
    loadable = False
    load_error = None
    try:
        st = MarketMemoryState.from_checkpoint(data)
        loadable = True
        recomputed = st.semantic_state_hash()
        stored = data.get("semantic_state_hash")
        if stored and stored != recomputed:
            issues.append("stored semantic_state_hash does not recompute from payload")
    except Exception as e:
        load_error = str(e)

    return {
        "missing_top_level_keys": missing_top,
        "missing_price_processor_fields": missing_pp,
        "issues": issues,
        "paper_runner_loadable": loadable,
        "load_error": load_error,
        "complete_per_contract": not missing_top and not missing_pp and loadable and not issues,
    }


def reconstruction_plan(validated_hash: str | None) -> dict[str, Any]:
    return {
        "requires_full_90d_replay": False,
        "approved_minimal_path": [
            "Re-run integrated validation with PHASE6XT_EXPORT_HANDOFF=1 to write path_a_boot_end.checkpoint.json "
            "(single write at end of Path A; ~50m wall, separate operator approval).",
            "Alternative: persist cp_snap at catchup + Path-B catchup only if Path-A export unavailable "
            "(still requires interleaved hist→catchup checkpoint once).",
        ],
        "explicitly_forbidden_without_approval": [
            "Full 90d SSOT+price replay disguised as handoff",
            "Treating paper_runner foundation_checkpoint.json as Path-A when semantic hash differs",
        ],
        "validated_target_hash": validated_hash,
        "paper_runner_foundation_acceptable_as": "INTERNAL_BOOT_END_REFERENCE_ONLY until hash matches",
    }

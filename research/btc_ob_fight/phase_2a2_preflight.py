"""Phase 2A.2 consistency preflight (read-only audit before changes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_phase_2a2_preflight(
    *,
    repo_root: Path | None = None,
    run_014_path: Path | None = None,
) -> dict[str, Any]:
    """Document reclaim source paths and known historical bugs."""
    import subprocess

    root = repo_root or Path(__file__).resolve().parents[2]
    run_dir = run_014_path or root / "results/btc_ob_fight_cases/20260831T190000Z/run_014"

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())

    run_files = sorted(p.name for p in run_dir.iterdir()) if run_dir.is_dir() else []

    return {
        "preflight_version": "phase_2a2_consistency_v1",
        "repository": str(root),
        "branch": branch,
        "head": head,
        "dirty": dirty,
        "run_014_present": run_dir.is_dir(),
        "run_014_file_count": len(run_files),
        "reclaim_output_paths": {
            "canonical": "reclaim_events.csv",
            "deprecated_removed": "reclaim_events_corrected.csv",
            "legacy_bug_function": "fight_facts._build_reclaim_events (removed in 2A.2)",
            "canonical_builder": "outside_reclaim.build_canonical_reclaim_pipeline",
        },
        "competing_sources_before_fix": [
            "fight_facts._build_reclaim_events (DEPRECATED_INVALID_GLOBAL_FIRST_BUG)",
            "fight_sequence.reclaim_events_corrected (duplicate corrected layer)",
        ],
        "single_source_of_truth_after_fix": "reclaim_events.csv from reclaim_event_contract_v2",
        "edge_visit_output": "edge_visits.csv",
        "cluster_sensitivity_output": "fight_cluster_sensitivity.csv",
        "known_gap0_invariant_violation": "cluster_count(gap=0) < edge_visit_count due to gap<=0 merge",
        "historical_run_012_reclaim_status": "KNOWN_INVALID_GLOBAL_FIRST_BUG",
        "historical_run_014_reclaim_status": "CORRECTED_LAYER_ONLY_IN_reclaim_events_corrected.csv",
        "legacy_global_first_reclaim_enabled": False,
    }

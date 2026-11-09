"""
Align close-level resume labels with layer-based recovery actions for orchestrator writes.

``assess_close_resume_states`` uses meta/publish sets only; ``assess_scope_ch_states`` uses
full CH layers. Both must agree before a productive multi-batch run proceeds.
"""

from __future__ import annotations

from typing import Mapping

# Layer recovery actions from ``productive_prewrite_gate.assess_scope_ch_states``.
SAFE_LAYER_ACTIONS = frozenset({"SAFE_RESUME", "COMPLETE"})

# Close-level labels from ``batch_runner.classify_close_resume``.
CLOSE_PUBLISH_SKIP = "publish_skip"
CLOSE_PILOT_OVERLAP = "pilot_overlap_publish_only"
CLOSE_READY = "ready"
CLOSE_META_NO_PUBLISH = "meta_without_publish"


def close_resume_allowed_for_write(
    close_classification: str,
    recovery_action: str | None,
) -> bool:
    """
    Return True when the orchestrator may include this close in a write batch.

    ``meta_without_publish`` is allowed when layer recovery is SAFE_RESUME (e.g. catalog
    without sidecar — Phase 4J) or COMPLETE (already on pipeline line).
    """
    if recovery_action in SAFE_LAYER_ACTIONS:
        return True
    if close_classification in (CLOSE_PUBLISH_SKIP, CLOSE_PILOT_OVERLAP, CLOSE_READY):
        return True
    if close_classification == CLOSE_META_NO_PUBLISH and recovery_action in SAFE_LAYER_ACTIONS:
        return True
    return False


def find_blocked_close_resumes(
    resume_states: list[dict],
    recovery_by_snapshot: Mapping[str, str],
) -> list[dict]:
    blocked: list[dict] = []
    for row in resume_states:
        sid = str(row["snapshot_id"])
        action = recovery_by_snapshot.get(sid)
        if not close_resume_allowed_for_write(str(row["classification"]), action):
            blocked.append(
                {
                    **row,
                    "recovery_action": action,
                }
            )
    return blocked

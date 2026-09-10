"""Classify outcome-path boundaries without reinterpreting frozen mb_state_1s_v1 fields.

``replay_epoch`` in mb_state_1s_v1 is incremented on every periodic checkpoint (and on
exchange snapshots) by the state builder. It is **not** a proven hard continuity break.

Hard breaks for Full-OB outcomes use existing state signals:
- ``sequence_gap_count > 0`` → true sequence gap
- ``resync_flag != 0`` → true resync (existing state heuristic)
- missing 1s price point → gap / source end (handled by caller)

A ``replay_epoch`` change with ``resync_flag==0`` and ``sequence_gap_count==0`` and a
monotonic +1 step relative to the previous path point is treated as ``CHECKPOINT_ONLY``.
Any other epoch discontinuity without hard signals is ``UNKNOWN_FAIL_CLOSED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prices import PricePoint


class PathBoundaryKind(str, Enum):
    NONE = "NONE"
    CHECKPOINT_ONLY = "CHECKPOINT_ONLY"
    TRUE_SEQUENCE_GAP = "TRUE_SEQUENCE_GAP"
    TRUE_RESYNC = "TRUE_RESYNC"
    SOURCE_END = "SOURCE_END"
    UNKNOWN_FAIL_CLOSED = "UNKNOWN_FAIL_CLOSED"


# Existing episode_outcome_v1 censor_reason contract values
CENSOR_REPLAY_GAP = "REPLAY_GAP"
CENSOR_RESYNC_BOUNDARY = "RESYNC_BOUNDARY"
CENSOR_SOURCE_WINDOW_END = "SOURCE_WINDOW_END"
CENSOR_EPOCH_BOUNDARY = "EPOCH_BOUNDARY"  # retained only for UNKNOWN_FAIL_CLOSED

QUALITY_CHECKPOINT_BOUNDARY_CROSSED = "CHECKPOINT_BOUNDARY_CROSSED"
QUALITY_SEQUENCE_GAP_ON_PATH = "SEQUENCE_GAP_ON_PATH"


@dataclass(frozen=True)
class PathBoundaryDecision:
    kind: PathBoundaryKind
    censor_reason: str | None
    quality_flag: str | None

    @property
    def is_hard(self) -> bool:
        return self.censor_reason is not None


def classify_path_boundary(
    *,
    prev: "PricePoint | None",
    current: "PricePoint",
    anchor_replay_epoch: int,
) -> PathBoundaryDecision:
    """Classify continuity at ``current`` relative to ``prev`` / anchor.

    Order: explicit hard signals first, then checkpoint-vs-unknown epoch changes.
    """
    if int(current.sequence_gap_count) > 0:
        return PathBoundaryDecision(
            kind=PathBoundaryKind.TRUE_SEQUENCE_GAP,
            censor_reason=CENSOR_REPLAY_GAP,
            quality_flag=QUALITY_SEQUENCE_GAP_ON_PATH,
        )
    if int(current.resync_flag) != 0:
        return PathBoundaryDecision(
            kind=PathBoundaryKind.TRUE_RESYNC,
            censor_reason=CENSOR_RESYNC_BOUNDARY,
            quality_flag=None,
        )

    cur_ep = int(current.replay_epoch)
    anchor_ep = int(anchor_replay_epoch)
    if prev is None:
        # First path sample after anchor: epoch may already differ if checkpoint landed
        # between anchor availability and first path second — still checkpoint-only if soft.
        if cur_ep == anchor_ep:
            return PathBoundaryDecision(kind=PathBoundaryKind.NONE, censor_reason=None, quality_flag=None)
        if cur_ep == anchor_ep + 1:
            return PathBoundaryDecision(
                kind=PathBoundaryKind.CHECKPOINT_ONLY,
                censor_reason=None,
                quality_flag=QUALITY_CHECKPOINT_BOUNDARY_CROSSED,
            )
        return PathBoundaryDecision(
            kind=PathBoundaryKind.UNKNOWN_FAIL_CLOSED,
            censor_reason=CENSOR_EPOCH_BOUNDARY,
            quality_flag=None,
        )

    prev_ep = int(prev.replay_epoch)
    if cur_ep == prev_ep:
        # Continuity vs previous path sample unchanged (flag already set at the crossing).
        return PathBoundaryDecision(kind=PathBoundaryKind.NONE, censor_reason=None, quality_flag=None)

    # Epoch changed vs previous path point.
    if cur_ep == prev_ep + 1:
        return PathBoundaryDecision(
            kind=PathBoundaryKind.CHECKPOINT_ONLY,
            censor_reason=None,
            quality_flag=QUALITY_CHECKPOINT_BOUNDARY_CROSSED,
        )

    # Epoch reset, jump, or decrease (e.g. new hour partition) without hard flags.
    return PathBoundaryDecision(
        kind=PathBoundaryKind.UNKNOWN_FAIL_CLOSED,
        censor_reason=CENSOR_EPOCH_BOUNDARY,
        quality_flag=None,
    )


def classify_missing_point(
    *,
    requested_available_at,
    last_available_at,
) -> PathBoundaryDecision:
    """Missing 1s grid point: source end vs internal gap."""
    if last_available_at is not None and requested_available_at > last_available_at:
        return PathBoundaryDecision(
            kind=PathBoundaryKind.SOURCE_END,
            censor_reason=CENSOR_SOURCE_WINDOW_END,
            quality_flag=None,
        )
    return PathBoundaryDecision(
        kind=PathBoundaryKind.TRUE_SEQUENCE_GAP,
        censor_reason=CENSOR_REPLAY_GAP,
        quality_flag=QUALITY_SEQUENCE_GAP_ON_PATH,
    )

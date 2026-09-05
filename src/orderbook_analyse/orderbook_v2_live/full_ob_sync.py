"""Bybit Full-OB synchronization contract (pure, testable).

Implements https://bybit-exchange.github.io/docs/v5/websocket/public/full-ob
synchronization procedure. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeltaOutcome(str, Enum):
    APPLIED = "applied"
    IGNORED_STALE_U = "ignored_stale_u"  # u < local
    IGNORED_DUP_U = "ignored_dup_u"  # u == local
    IGNORED_DECREASING_SEQ = "ignored_decreasing_seq"
    GAP = "gap"  # u > local + 1
    U_RESET = "u_reset"  # u == 1 → full resync
    NOT_READY = "not_ready"  # book has no snapshot yet


class AlignStatus(str, Enum):
    ALIGNED = "aligned"
    NEED_NEWER_SNAPSHOT = "need_newer_snapshot"
    NEED_MORE_DELTAS = "need_more_deltas"
    EMPTY_BUFFER_APPLY_SNAPSHOT = "empty_buffer_apply_snapshot"
    SEQ_U_MISMATCH = "seq_u_mismatch"


@dataclass(frozen=True)
class DeltaMeta:
    u: int
    seq: int
    payload: dict[str, Any]


@dataclass
class BufferState:
    """Pre-snapshot delta buffer with continuity checks."""

    items: list[DeltaMeta] = field(default_factory=list)
    last_u: int | None = None
    last_seq: int | None = None
    cleared_for_gap: int = 0
    discarded_decreasing_seq: int = 0

    def clear(self) -> None:
        self.items.clear()
        self.last_u = None
        self.last_seq = None

    def push(self, *, u: int, seq: int, payload: dict[str, Any]) -> str:
        """Return 'accepted' | 'discarded_seq' | 'cleared_gap'."""
        cleared = False
        if self.last_seq is not None and seq < self.last_seq:
            self.discarded_decreasing_seq += 1
            return "discarded_seq"
        if self.last_u is not None:
            if u == 1:
                self.clear()
                self.cleared_for_gap += 1
                cleared = True
            elif u != self.last_u + 1:
                if u <= self.last_u:
                    return "discarded_seq"
                self.clear()
                self.cleared_for_gap += 1
                cleared = True
        self.items.append(DeltaMeta(u=u, seq=seq, payload=payload))
        self.last_u = u
        self.last_seq = seq
        return "cleared_gap" if cleared else "accepted"


@dataclass(frozen=True)
class AlignResult:
    status: AlignStatus
    matched_index: int | None = None  # index in buffer of matching delta
    remaining: tuple[DeltaMeta, ...] = ()
    reason: str = ""


def extract_u_seq(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    data = payload.get("data") or {}
    u = data.get("u")
    seq = data.get("seq")
    try:
        u_i = int(u) if u is not None else None
    except (TypeError, ValueError):
        u_i = None
    try:
        seq_i = int(seq) if seq is not None else None
    except (TypeError, ValueError):
        seq_i = None
    return u_i, seq_i


def align_snapshot_to_buffer(
    *,
    snap_u: int,
    snap_seq: int,
    buffer: list[DeltaMeta],
) -> AlignResult:
    """Match REST snapshot against buffered WS deltas per Bybit procedure."""
    if not buffer:
        return AlignResult(
            status=AlignStatus.EMPTY_BUFFER_APPLY_SNAPSHOT,
            remaining=(),
            reason="no_buffered_deltas",
        )

    first = buffer[0]
    if snap_seq < first.seq:
        return AlignResult(
            status=AlignStatus.NEED_NEWER_SNAPSHOT,
            reason=f"snap_seq={snap_seq}<first_delta_seq={first.seq}",
        )

    # Discard deltas with seq < snap_seq
    kept = [d for d in buffer if d.seq >= snap_seq]
    if not kept:
        # Snapshot ahead of all buffered deltas — apply snap, no remaining.
        return AlignResult(
            status=AlignStatus.ALIGNED,
            matched_index=None,
            remaining=(),
            reason="snapshot_ahead_of_buffer",
        )

    # Find delta with seq == snap_seq
    match_idxs = [i for i, d in enumerate(kept) if d.seq == snap_seq]
    if not match_idxs:
        # snap_seq sits in a hole between buffered seqs — wait for matching delta
        if snap_seq > kept[-1].seq:
            return AlignResult(
                status=AlignStatus.ALIGNED,
                matched_index=None,
                remaining=(),
                reason="snapshot_ahead_partial",
            )
        return AlignResult(
            status=AlignStatus.NEED_MORE_DELTAS,
            reason=f"no_delta_with_seq={snap_seq}",
        )

    matched = kept[match_idxs[0]]
    if matched.u != snap_u:
        return AlignResult(
            status=AlignStatus.SEQ_U_MISMATCH,
            reason=f"seq={snap_seq} snap_u={snap_u} delta_u={matched.u}",
        )

    # Remaining = deltas after the matched one
    remaining = tuple(kept[match_idxs[0] + 1 :])
    return AlignResult(
        status=AlignStatus.ALIGNED,
        matched_index=match_idxs[0],
        remaining=remaining,
        reason="seq_u_matched",
    )


def classify_live_delta(*, local_u: int | None, event_u: int, event_seq: int | None, local_seq: int | None) -> DeltaOutcome:
    """Classify a live delta relative to an initialized book."""
    if local_u is None:
        return DeltaOutcome.NOT_READY
    if event_u == 1 and local_u != 1:
        return DeltaOutcome.U_RESET
    if event_seq is not None and local_seq is not None and event_seq < local_seq:
        return DeltaOutcome.IGNORED_DECREASING_SEQ
    if event_u < local_u:
        return DeltaOutcome.IGNORED_STALE_U
    if event_u == local_u:
        return DeltaOutcome.IGNORED_DUP_U
    if event_u > local_u + 1:
        return DeltaOutcome.GAP
    return DeltaOutcome.APPLIED

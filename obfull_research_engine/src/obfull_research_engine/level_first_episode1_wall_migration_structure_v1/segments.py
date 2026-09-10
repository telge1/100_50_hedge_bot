"""Epoch-isolated book segments — never bridge 4→5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import (
    _as_dt,
    _apply_change,
    _apply_reset,
    merge_book_events,
    normalize_book_resets,
)
from ..level_first_episode1_corrected_sms1_persist_v1.reader import load_table, replay_payload_from_tables
from ..timeparse import format_utc_z
from . import (
    EPOCH4,
    EPOCH4_COVERAGE_END,
    EPOCH4_COVERAGE_START,
    EPOCH5,
    EPOCH5_CHECKPOINT,
)


class SegmentError(RuntimeError):
    """Fail-closed segment / epoch isolation error."""


@dataclass(frozen=True)
class EpochSegment:
    name: str
    replay_epoch: int
    start_exclusive_anchor: datetime | None  # book events with et >= start (inclusive for coverage)
    coverage_start: datetime
    coverage_end: datetime
    anchor_mode: str  # "continuous_coverage" | "checkpoint_reanchor"
    checkpoint_event_time: datetime | None = None


def epoch4_segment() -> EpochSegment:
    return EpochSegment(
        name="epoch4_continuous_coverage",
        replay_epoch=EPOCH4,
        start_exclusive_anchor=None,
        coverage_start=_as_dt(EPOCH4_COVERAGE_START),
        coverage_end=_as_dt(EPOCH4_COVERAGE_END),
        anchor_mode="continuous_coverage",
    )


def epoch5_segment() -> EpochSegment:
    cp = _as_dt(EPOCH5_CHECKPOINT)
    # Independent segment: from checkpoint through last available ep5 book in persist,
    # but analysis horizon still cannot claim continuity with ep4.
    return EpochSegment(
        name="epoch5_checkpoint_reanchor",
        replay_epoch=EPOCH5,
        start_exclusive_anchor=cp,
        coverage_start=cp,
        coverage_end=_as_dt("2026-09-06T20:21:00Z"),  # last state available_at in persist
        anchor_mode="checkpoint_reanchor",
        checkpoint_event_time=cp,
    )


def load_payload(persist_dir: Path) -> dict[str, Any]:
    return replay_payload_from_tables(Path(persist_dir))


def assert_no_epoch_bridge(a: EpochSegment, b: EpochSegment) -> None:
    if a.replay_epoch == b.replay_epoch:
        return
    # Explicit hard rule documentation helper
    if {a.replay_epoch, b.replay_epoch} == {EPOCH4, EPOCH5}:
        raise SegmentError("forbidden: epoch 4→5 bridge")


def book_state_at(
    payload: dict[str, Any],
    *,
    until: datetime,
    require_epoch: int | None = None,
) -> dict[str, Any]:
    """Reconstruct asks/bids for event_time < until; optional epoch check on last event."""
    until = _as_dt(until)
    resets = normalize_book_resets(payload.get("book_resets"), None)
    events = merge_book_events(payload["level_changes"], resets)
    bids = dict(payload["initial_bids"])
    asks = dict(payload["initial_asks"])
    epoch = payload.get("initial_replay_epoch")
    last_et = None
    for et, _o, _k, _i, kind, pev in events:
        if et >= until:
            break
        if kind == "reset":
            _apply_reset(bids, asks, pev)
        else:
            _apply_change(bids, asks, pev)
        if pev.get("replay_epoch") is not None:
            epoch = int(pev["replay_epoch"])
        last_et = et
    if require_epoch is not None and epoch is not None and int(epoch) != int(require_epoch):
        raise SegmentError(f"book epoch {epoch} != required {require_epoch} at {format_utc_z(until)}")
    bb = max(bids) if bids else None
    ba = min(asks) if asks else None
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": bb,
        "best_ask": ba,
        "replay_epoch": int(epoch) if epoch is not None else None,
        "last_event_time": format_utc_z(last_et) if last_et else None,
        "until_exclusive": format_utc_z(until),
    }


def iter_merged_events(payload: dict[str, Any]):
    resets = normalize_book_resets(payload.get("book_resets"), None)
    return merge_book_events(payload["level_changes"], resets)


def load_states(persist_dir: Path) -> list[dict[str, Any]]:
    return load_table(Path(persist_dir), "states_100ms", require=True)


def segment_to_dict(seg: EpochSegment) -> dict[str, Any]:
    return {
        "name": seg.name,
        "replay_epoch": seg.replay_epoch,
        "coverage_start": format_utc_z(seg.coverage_start),
        "coverage_end": format_utc_z(seg.coverage_end),
        "anchor_mode": seg.anchor_mode,
        "checkpoint_event_time": format_utc_z(seg.checkpoint_event_time)
        if seg.checkpoint_event_time
        else None,
    }

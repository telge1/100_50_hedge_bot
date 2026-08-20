"""Per-sequence data-quality flags for batch outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class DataQuality:
    data_quality_status: str  # OK | DEGRADED | FAIL
    data_quality_flags: list[str] = field(default_factory=list)
    outcome_eligible: bool = True
    exclusion_reason: str | None = None

    def to_row(self, sequence_id: str, symbol: str) -> dict[str, Any]:
        return {
            "wall_sequence_id": sequence_id,
            "symbol": symbol,
            "data_quality_status": self.data_quality_status,
            "data_quality_flags": "|".join(self.data_quality_flags),
            "outcome_eligible": self.outcome_eligible,
            "exclusion_reason": self.exclusion_reason,
        }


def assess_data_quality(
    *,
    sequence_id: str,
    symbol: str,
    n_level_events: int,
    n_incomplete_initial: int,
    n_snapshot_boundaries: int,
    n_ticker_samples: int,
    n_trades_loaded: int,
    update_id_gap: bool,
    ended_by_segment: bool,
    forward_any_complete: bool,
    bucket_ok: bool,
) -> DataQuality:
    flags: list[str] = []
    if n_level_events == 0:
        flags.append("no_level_updates_in_band")
    if n_incomplete_initial > 0 and n_incomplete_initial >= max(1, n_level_events // 2):
        flags.append("missing_or_incomplete_initial_state")
    elif n_incomplete_initial > 0:
        flags.append("partial_incomplete_initial_state")
    if n_ticker_samples == 0:
        flags.append("missing_ticker_data")
    elif n_ticker_samples < 3:
        flags.append("sparse_ticker_data")
    if n_trades_loaded == 0:
        flags.append("missing_or_empty_trade_window")
    if n_snapshot_boundaries > 0:
        flags.append("snapshot_boundary_in_window")
    if update_id_gap:
        flags.append("update_id_gap")
    if ended_by_segment:
        flags.append("segment_boundary_end")
    if not forward_any_complete:
        flags.append("incomplete_forward_window")
    if not bucket_ok:
        flags.append("unclear_bucket_reconstruction")

    fail_flags = {
        "no_level_updates_in_band",
        "unclear_bucket_reconstruction",
        "missing_ticker_data",
    }
    hard = [f for f in flags if f in fail_flags]
    if hard:
        return DataQuality(
            data_quality_status="FAIL",
            data_quality_flags=flags,
            outcome_eligible=False,
            exclusion_reason=";".join(hard),
        )
    if flags:
        # incomplete forward alone does not exclude toxicity summary, but
        # outcome rows mark completeness; exclude from hold/break rates if
        # no complete forward at all.
        eligible = "incomplete_forward_window" not in flags or n_ticker_samples > 0
        if "incomplete_forward_window" in flags and n_ticker_samples == 0:
            eligible = False
        status = "DEGRADED"
        excl = None
        if not eligible:
            excl = "incomplete_forward_window"
            status = "FAIL"
        return DataQuality(
            data_quality_status=status,
            data_quality_flags=flags,
            outcome_eligible=eligible,
            exclusion_reason=excl,
        )
    return DataQuality(
        data_quality_status="OK",
        data_quality_flags=[],
        outcome_eligible=True,
        exclusion_reason=None,
    )


def detect_update_id_gap(level_events: Sequence[Any]) -> bool:
    """Heuristic: large non-monotonic jumps in update_id on same price stream."""
    last: int | None = None
    for ev in level_events:
        uid = int(getattr(ev, "update_id", 0))
        if last is not None and uid > last + 50_000:
            return True
        if last is None or uid > last:
            last = uid
    return False

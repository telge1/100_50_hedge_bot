"""168h pool lookback gate for causal SSOT replay."""

from __future__ import annotations

from dataclasses import dataclass

from contracts_v1 import BARS_PER_HOUR_5M, MIN_POOL_LOOKBACK_HOURS


@dataclass(frozen=True)
class PoolHistoryGateResultV1:
    ok: bool
    reason_code: str
    bars_available: int
    bars_required: int


def evaluate_pool_history_gate(*, close_index: int) -> PoolHistoryGateResultV1:
    required = MIN_POOL_LOOKBACK_HOURS * BARS_PER_HOUR_5M
    if close_index < required:
        return PoolHistoryGateResultV1(
            ok=False,
            reason_code="INSUFFICIENT_POOL_LOOKBACK_168H",
            bars_available=close_index,
            bars_required=required,
        )
    return PoolHistoryGateResultV1(
        ok=True,
        reason_code="POOL_HISTORY_OK",
        bars_available=close_index,
        bars_required=required,
    )

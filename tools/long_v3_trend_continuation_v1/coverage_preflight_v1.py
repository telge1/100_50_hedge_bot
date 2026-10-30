"""Coverage preflight — 168h pool history before first evaluated close."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts_v1 import BARS_PER_HOUR_5M, MIN_POOL_LOOKBACK_HOURS
from pool_history_gate_v1 import evaluate_pool_history_gate


@dataclass(frozen=True)
class CoveragePreflightResultV1:
    ok: bool
    reason_code: str
    first_decision_close_utc: str | None
    closes_available: int
    bars_before_first: int
    required_bars: int
    transport: str
    detail: dict[str, Any]


def run_coverage_preflight(
    *,
    decision_closes: list[str],
    transport: str,
    bundle_first_close: str | None = None,
) -> CoveragePreflightResultV1:
    if not decision_closes:
        return CoveragePreflightResultV1(
            ok=False,
            reason_code="NO_DECISION_CLOSES",
            first_decision_close_utc=None,
            closes_available=0,
            bars_before_first=0,
            required_bars=MIN_POOL_LOOKBACK_HOURS * BARS_PER_HOUR_5M,
            transport=transport,
            detail={},
        )
    first = decision_closes[0]
    eval_index = min(len(decision_closes) - 1, MIN_POOL_LOOKBACK_HOURS * BARS_PER_HOUR_5M)
    gate = evaluate_pool_history_gate(close_index=eval_index)
    ok = len(decision_closes) > gate.bars_required and gate.ok
    reason = gate.reason_code if ok else "INSUFFICIENT_POOL_LOOKBACK_168H"
    if bundle_first_close and first == bundle_first_close and eval_index < gate.bars_required:
        ok = False
        reason = "MONTH_BUNDLE_START_NO_168H_LOOKBACK"
    return CoveragePreflightResultV1(
        ok=ok,
        reason_code=reason,
        first_decision_close_utc=first,
        closes_available=len(decision_closes),
        bars_before_first=eval_index,
        required_bars=gate.bars_required,
        transport=transport,
        detail={"bundle_first_close": bundle_first_close},
    )

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .state_machine import _sanitize_routed_state


ALLOWED_ENTRY_STATES = {
    "mid_exhaustion_long",
    "pullback_in_long_context",
}


@dataclass(slots=True)
class DecisionPolicyResult:
    decision: str
    decision_reason: str
    confidence: float | None
    confidence_source: str
    range_unclear_diagnosis: str | None
    entry_allowed: bool


def classify_range_unclear_diagnosis(
    *,
    transition_reason: Sequence[str] | None,
    routed_transition_reason: Sequence[str] | None,
) -> str | None:
    all_reasons = [
        str(item)
        for item in [*(transition_reason or []), *(routed_transition_reason or [])]
        if str(item)
    ]
    if not all_reasons:
        return None
    if any(reason.startswith("awaiting_confirmation:") for reason in all_reasons):
        return "waiting_for_confirmation"
    if any(reason in {"true_range_or_unclear_context", "range_or_unclear_context"} for reason in all_reasons):
        return "true_range_context"
    if any(
        reason.startswith("blocked_transition:")
        or "ambiguous" in reason
        or "conflict" in reason
        for reason in all_reasons
    ):
        return "conflicting_candidates"
    if any(reason in {"same_ts_guard", "cooldown_active"} for reason in all_reasons):
        return "guard_or_holdover"
    return "no_signal_confirmed"


def evaluate_entry_decision(
    *,
    state: str | None,
    confidence: float | None,
    confidence_source: str | None,
    range_unclear_diagnosis: str | None,
) -> DecisionPolicyResult:
    routed_state = _sanitize_routed_state(state)
    normalized_confidence_source = str(confidence_source or "missing")

    if routed_state == "range_unclear":
        diagnosis = str(range_unclear_diagnosis or "other")
        return DecisionPolicyResult(
            decision="SKIP",
            decision_reason=f"range_unclear_{diagnosis}",
            confidence=confidence,
            confidence_source=normalized_confidence_source,
            range_unclear_diagnosis=diagnosis,
            entry_allowed=False,
        )

    if routed_state in ALLOWED_ENTRY_STATES:
        return DecisionPolicyResult(
            decision="ALLOW",
            decision_reason=f"allowed_state_{routed_state}",
            confidence=confidence,
            confidence_source=normalized_confidence_source,
            range_unclear_diagnosis=range_unclear_diagnosis,
            entry_allowed=True,
        )

    return DecisionPolicyResult(
        decision="WATCHLIST",
        decision_reason="state_not_whitelisted",
        confidence=confidence,
        confidence_source=normalized_confidence_source,
        range_unclear_diagnosis=range_unclear_diagnosis,
        entry_allowed=False,
    )

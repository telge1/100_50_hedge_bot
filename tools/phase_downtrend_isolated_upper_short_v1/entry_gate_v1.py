"""Fail-closed entry gate combining regime, eligibility, pause, baseline."""

from __future__ import annotations

from typing import Any

from contracts_v1 import EntryGateDecisionV1, PauseState, RegimeContextV1
from isolated_upper_eligibility_v1 import evaluate_isolated_upper_eligibility
from regime_classifier_v1 import RegimeClassifierV1
from short_pause_state_v1 import ShortPauseControllerV1


def evaluate_entry_gate(
    *,
    context: RegimeContextV1,
    pause: ShortPauseControllerV1,
    baseline_signal: Any | None,
    snapshot: dict[str, Any],
    structural_context: dict[str, Any] | None = None,
    classifier: RegimeClassifierV1 | None = None,
) -> EntryGateDecisionV1:
    clf = classifier or RegimeClassifierV1()
    regime = clf.regime_at_close(context)
    eligibility = evaluate_isolated_upper_eligibility(
        decision_close_utc=context.decision_close_utc,
        baseline_signal=baseline_signal,
        snapshot=snapshot,
        structural_context=structural_context,
    )
    reasons: list[str] = []
    if regime.blocks_new_short_intent or regime.regime != "DOWNTREND":
        reasons.append(f"REGIME_{regime.regime}")
    if not eligibility.eligible:
        reasons.append(f"ISOLATED_{eligibility.reason}")
    if pause.blocks_new_short_intent():
        reasons.append(f"PAUSE_{pause.pause_state}")
    if baseline_signal is None:
        reasons.append("BASELINE_NO_SIGNAL")

    allow = (
        regime.regime == "DOWNTREND"
        and not regime.blocks_new_short_intent
        and eligibility.eligible
        and pause.pause_state == "READY"
        and baseline_signal is not None
    )
    return EntryGateDecisionV1(
        allow_new_short_intent=allow,
        reasons=reasons,
        regime=regime,
        eligibility=eligibility,
        pause_state=pause.pause_state,
    )

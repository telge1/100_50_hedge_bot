"""Fail-closed entry gate combining EMA confirmation, eligibility, pause, baseline."""

from __future__ import annotations

from typing import Any

from contracts_v1 import EntryGateDecisionV1, PauseState, PureEmaConfirmationV1, RegimeContextV1
from isolated_upper_eligibility_v1 import evaluate_isolated_upper_eligibility
from pure_ema_confirmation_v1 import PureEmaConfirmationGateV1
from regime_classifier_v1 import RegimeClassifierV1
from short_pause_state_v1 import ShortPauseControllerV1


def _isolated_cfg(config_bundle: dict[str, Any] | None) -> dict[str, Any]:
    if not config_bundle:
        return {}
    return config_bundle.get("isolated_strategy_v1") or {}


def evaluate_entry_gate(
    *,
    context: RegimeContextV1,
    pause: ShortPauseControllerV1,
    baseline_signal: Any | None,
    snapshot: dict[str, Any],
    structural_context: dict[str, Any] | None = None,
    classifier: RegimeClassifierV1 | None = None,
    ema_gate: PureEmaConfirmationGateV1 | None = None,
    isolated_strategy_config: dict[str, Any] | None = None,
) -> EntryGateDecisionV1:
    isolated = _isolated_cfg(isolated_strategy_config)
    ema_cfg = isolated.get("pure_ema_confirmation") or {}
    ema_enabled = bool(ema_cfg.get("enabled"))
    research_pass = bool((isolated.get("isolated_upper_eligibility") or {}).get("research_pass_on_baseline_signal"))

    clf = classifier or RegimeClassifierV1()
    regime = clf.regime_at_close(context)
    eligibility = evaluate_isolated_upper_eligibility(
        decision_close_utc=context.decision_close_utc,
        baseline_signal=baseline_signal,
        snapshot=snapshot,
        structural_context=structural_context,
        research_pass_on_baseline_signal=research_pass,
    )

    ema_confirmation: PureEmaConfirmationV1 | None = None
    if ema_enabled:
        gate = ema_gate or PureEmaConfirmationGateV1()
        ema_confirmation = gate.at_close(context.decision_close_utc)

    reasons: list[str] = []
    pool_candidate_present = baseline_signal is not None

    if not pool_candidate_present:
        reasons.append("BASELINE_NO_SIGNAL")

    if not eligibility.eligible:
        reasons.append(f"ISOLATED_{eligibility.reason}")

    if pause.blocks_new_short_intent():
        reasons.append(f"PAUSE_{pause.pause_state}")

    if ema_enabled:
        if ema_confirmation is None or ema_confirmation.decision != "SHORT_OK":
            dec = ema_confirmation.decision if ema_confirmation else "UNCERTAIN"
            reasons.append(f"EMA_{dec}")
            if ema_confirmation:
                reasons.append(f"EMA_REASON_{ema_confirmation.reason_code}")
    else:
        if regime.blocks_new_short_intent or regime.regime != "DOWNTREND":
            reasons.append(f"REGIME_{regime.regime}")

    ema_allows = ema_enabled and ema_confirmation is not None and ema_confirmation.authorizes_new_pending
    regime_allows = not ema_enabled and regime.regime == "DOWNTREND" and not regime.blocks_new_short_intent

    allow = (
        pool_candidate_present
        and eligibility.eligible
        and pause.pause_state == "READY"
        and (ema_allows or regime_allows)
    )
    return EntryGateDecisionV1(
        allow_new_short_intent=allow,
        reasons=reasons,
        regime=regime,
        eligibility=eligibility,
        pause_state=pause.pause_state,
        ema_confirmation=ema_confirmation,
        pool_candidate_present=pool_candidate_present,
    )

"""Isolated upper-pool eligibility — skeleton rejects all candidates."""

from __future__ import annotations

from typing import Any

from contracts_v1 import IsolatedEligibilityResultV1, UpperCandidateDiagnosticV1

NOT_YET_VALIDATED = "NOT_YET_VALIDATED"


def build_candidate_diagnostic(
    *,
    decision_close_utc: str,
    snapshot: dict[str, Any],
    baseline_signal: Any | None,
) -> UpperCandidateDiagnosticV1:
    prov = {
        "pipeline_line_id": snapshot.get("pipeline_line_id"),
        "provider_config_hash": snapshot.get("config_hash"),
        "decision_close_utc": decision_close_utc,
        "data_provenance": snapshot.get("data_provenance"),
    }
    if baseline_signal is None:
        return UpperCandidateDiagnosticV1(
            decision_close_utc=decision_close_utc,
            pool_lower=None,
            pool_upper=None,
            pool_key=None,
            origin_ids=None,
            baseline_signal_present=False,
            density_provenance=prov,
        )
    return UpperCandidateDiagnosticV1(
        decision_close_utc=decision_close_utc,
        pool_lower=float(baseline_signal.pool_lower),
        pool_upper=float(baseline_signal.pool_upper),
        pool_key=str(baseline_signal.pool_key),
        origin_ids=str(getattr(baseline_signal, "origin_ids", "") or ""),
        baseline_signal_present=True,
        density_provenance=prov,
    )


def evaluate_isolated_upper_eligibility(
    *,
    decision_close_utc: str,
    baseline_signal: Any | None,
    snapshot: dict[str, Any],
    structural_context: dict[str, Any] | None = None,
    research_pass_on_baseline_signal: bool = False,
) -> IsolatedEligibilityResultV1:
    """Fail-closed until pool-growth / stack rules are validated (unless BT-D66 research flag)."""
    candidate = build_candidate_diagnostic(
        decision_close_utc=decision_close_utc,
        snapshot=snapshot,
        baseline_signal=baseline_signal,
    )
    _ = structural_context  # reserved for research hooks (D51/D50)
    if research_pass_on_baseline_signal and baseline_signal is not None:
        return IsolatedEligibilityResultV1(
            eligible=True,
            reason="BASELINE_POOL_CANDIDATE_PRESENT",
            candidate=candidate,
        )
    return IsolatedEligibilityResultV1(
        eligible=False,
        reason=NOT_YET_VALIDATED,
        candidate=candidate,
    )

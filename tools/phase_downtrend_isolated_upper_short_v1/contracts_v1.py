"""Shared types for downtrend_isolated_upper_short_v1 (research skeleton)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RegimeState = Literal["DOWNTREND", "SIDEWAYS", "UNCERTAIN"]
PauseState = Literal["READY", "SHORT_PAUSED"]
EmaStructureDecision = Literal["SHORT_OK", "SHORT_NO", "UNCERTAIN"]

CONTRACT = "downtrend_isolated_upper_short_v1"
RUNNER_CONTRACT = "isolated_research_runner_v1"


@dataclass(frozen=True)
class RegimeContextV1:
    """Causal inputs available at decision close (no future bars)."""

    decision_close_utc: str
    ohlc_to_close: dict[str, float] | None = None
    ema_features: dict[str, Any] | None = None
    structural_notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RegimeClassificationV1:
    regime: RegimeState
    evidence: list[str]
    blocks_new_short_intent: bool


@dataclass(frozen=True)
class UpperCandidateDiagnosticV1:
    """Read-only view of baseline nearest-upper candidate (if any)."""

    decision_close_utc: str
    pool_lower: float | None
    pool_upper: float | None
    pool_key: str | None
    origin_ids: str | None
    baseline_signal_present: bool
    density_provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IsolatedEligibilityResultV1:
    eligible: bool
    reason: str
    candidate: UpperCandidateDiagnosticV1 | None = None


@dataclass(frozen=True)
class PureEmaConfirmationV1:
    decision: EmaStructureDecision
    reason_code: str
    chart_focus: str | None
    provenance: dict[str, Any]
    authorizes_new_pending: bool


@dataclass(frozen=True)
class EntryGateDecisionV1:
    allow_new_short_intent: bool
    reasons: list[str]
    regime: RegimeClassificationV1
    eligibility: IsolatedEligibilityResultV1
    pause_state: PauseState
    ema_confirmation: PureEmaConfirmationV1 | None = None
    pool_candidate_present: bool = False


@dataclass(frozen=True)
class StopLossPauseEventV1:
    exit_bar_utc: str
    position_id: str | None
    causal_note: str


@dataclass(frozen=True)
class RequalificationResultV1:
    """Future causal requalification — never unlocks in skeleton v1."""

    unlocked: bool
    reason: str
    evidence: list[str]

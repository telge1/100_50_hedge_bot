"""Candidate evidence state machine — NOT_CALIBRATED, no trading rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import DECISION_STATUS, FIRST_CANDIDATE_ELIGIBLE_SECONDS, FIRST_EARLY_EVIDENCE_SECONDS
from .schema import CandidateState, ThresholdSide, iso_z


@dataclass
class EvidenceScores:
    continuation: float = 0.0
    mean_reversion: float = 0.0
    continuation_fields: dict[str, Any] = field(default_factory=dict)
    mean_reversion_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateFSM:
    threshold_side: ThresholdSide
    state: CandidateState = "UNRESOLVED"
    transitions: list[dict[str, Any]] = field(default_factory=list)
    not_calibrated: bool = True
    decision_status: str = DECISION_STATUS

    def _set(self, new_state: CandidateState, *, at: datetime, reason: str, evidence: dict[str, Any] | None = None) -> None:
        if new_state == self.state:
            return
        self.transitions.append(
            {
                "from": self.state,
                "to": new_state,
                "at": iso_z(at),
                "reason": reason,
                "evidence": evidence or {},
            }
        )
        self.state = new_state

    def update(
        self,
        *,
        elapsed_s: float,
        coverage_ok: bool,
        archive_ok: bool,
        features: dict[str, Any],
        now: datetime,
    ) -> CandidateState:
        if not archive_ok:
            self._set("BLOCKED_RAW_ARCHIVE", at=now, reason="archive_coverage_invalid")
            return self.state
        if not coverage_ok:
            self._set("BLOCKED_COVERAGE", at=now, reason="queue_or_sequence_invalid")
            return self.state

        scores = score_evidence(self.threshold_side, features)
        if elapsed_s < FIRST_EARLY_EVIDENCE_SECONDS:
            self._set("UNRESOLVED", at=now, reason="before_early_evidence")
            return self.state
        if elapsed_s < FIRST_CANDIDATE_ELIGIBLE_SECONDS:
            self._set(
                "EARLY_EVIDENCE",
                at=now,
                reason="early_window",
                evidence=scores.__dict__,
            )
            return self.state

        # After 5s: evidence / candidate / conflicting — still NOT_CALIBRATED
        cont = scores.continuation
        mr = scores.mean_reversion
        if cont > 0 and mr > 0 and abs(cont - mr) < 0.25:
            self._set("CONFLICTING", at=now, reason="both_sides", evidence=scores.__dict__)
            return self.state
        if cont > mr and cont >= 2:
            label: CandidateState = (
                "CONTINUATION_CANDIDATE"
                if cont >= 3
                else "CONTINUATION_EVIDENCE"
            )
            self._set(label, at=now, reason="continuation_dominant", evidence=scores.__dict__)
            return self.state
        if mr > cont and mr >= 2:
            label = "MEAN_REVERSION_CANDIDATE" if mr >= 3 else "MEAN_REVERSION_EVIDENCE"
            self._set(label, at=now, reason="mean_reversion_dominant", evidence=scores.__dict__)
            return self.state
        self._set("EARLY_EVIDENCE", at=now, reason="insufficient_evidence", evidence=scores.__dict__)
        return self.state

    def research_candidate_label(self) -> str | None:
        """Map FSM state to threshold-specific research labels."""
        above = self.threshold_side == "ABOVE_EMA_THRESHOLD"
        below = self.threshold_side == "BELOW_EMA_THRESHOLD"
        if self.state in {"CONTINUATION_CANDIDATE", "CONTINUATION_EVIDENCE"}:
            if above:
                return "CONTINUATION_LONG_CANDIDATE"
            if below:
                return "CONTINUATION_SHORT_CANDIDATE"
        if self.state in {"MEAN_REVERSION_CANDIDATE", "MEAN_REVERSION_EVIDENCE"}:
            if above:
                return "MEAN_REVERSION_SHORT_CANDIDATE"
            if below:
                return "MEAN_REVERSION_LONG_CANDIDATE"
        if self.state == "CONFLICTING":
            return "CONFLICTING"
        if self.state == "UNRESOLVED":
            return "UNRESOLVED"
        return self.state

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "research_candidate": self.research_candidate_label(),
            "not_calibrated": self.not_calibrated,
            "decision_status": self.decision_status,
            "threshold_side": self.threshold_side,
            "transitions": list(self.transitions),
        }


def score_evidence(side: ThresholdSide, features: dict[str, Any]) -> EvidenceScores:
    """Heuristic uncalibrated counters — NOT trading weights."""
    cont_f: dict[str, Any] = {}
    mr_f: dict[str, Any] = {}
    cont = 0.0
    mr = 0.0
    pers = float(features.get("persistence_ratio") or 0.0)
    ie = float(features.get("impact_efficiency") or 0.0)
    qdh = float((features.get("qdh") or {}).get("qdh_base") or 0.0)
    refill = float((features.get("mass") or {}).get("refill") or 0.0)
    pull = float((features.get("mass") or {}).get("residual_pull") or 0.0)
    fill = float((features.get("mass") or {}).get("attributed_fill_capped") or 0.0)
    mp_chg = float(features.get("microprice_change_hint") or 0.0)

    if side == "ABOVE_EMA_THRESHOLD":
        if pers > 1.0:
            cont += 1
            cont_f["persistence"] = pers
        else:
            mr += 1
            mr_f["persistence_loss"] = pers
        if fill > 0 and qdh > 0:
            cont += 1
            cont_f["ask_depletion"] = qdh
        if ie > 0:
            cont += 1
            cont_f["positive_ie"] = ie
        else:
            mr += 1
            mr_f["aggression_without_progress"] = ie
        if refill > fill and fill > 0:
            mr += 1
            mr_f["ask_refill"] = refill
        if pull > 0:
            cont += 0.5
            cont_f["ask_pull"] = pull
        if mp_chg > 0:
            cont += 1
            cont_f["microprice_up"] = mp_chg
        elif mp_chg < 0:
            mr += 1
            mr_f["microprice_down"] = mp_chg
    elif side == "BELOW_EMA_THRESHOLD":
        # mirrored
        if pers > 1.0:
            cont += 1
            cont_f["persistence"] = pers
        else:
            mr += 1
            mr_f["persistence_loss"] = pers
        if fill > 0 and qdh > 0:
            cont += 1
            cont_f["bid_depletion"] = qdh
        if ie > 0:
            cont += 1
            cont_f["positive_ie"] = ie
        else:
            mr += 1
            mr_f["aggression_without_progress"] = ie
        if refill > fill and fill > 0:
            mr += 1
            mr_f["bid_refill"] = refill
        if mp_chg < 0:
            cont += 1
            cont_f["microprice_down"] = mp_chg
        elif mp_chg > 0:
            mr += 1
            mr_f["microprice_up"] = mp_chg
    return EvidenceScores(
        continuation=cont,
        mean_reversion=mr,
        continuation_fields=cont_f,
        mean_reversion_fields=mr_f,
    )

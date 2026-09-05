"""Candidate state machine — append-only, causal timestamps, no outcome feedback."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    REGISTERED_CANDIDATE_STATES,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING


@dataclass
class StateTransition:
    window_id: str
    observed_at: str
    decision_at: str
    evidence_available_until: str
    previous_state: str
    new_state: str
    reason_codes: str
    data_sources: str
    quality_status: str
    notes: str = ""


@dataclass
class StateMachine:
    window_id: str
    transitions: list[StateTransition] = field(default_factory=list)
    current: str = "no_trade"

    def append(
        self,
        *,
        new_state: str,
        observed_at: str,
        decision_at: str,
        evidence_available_until: str,
        reason_codes: list[str] | str,
        data_sources: list[str] | str,
        quality_status: str,
        notes: str = "",
    ) -> None:
        if new_state not in REGISTERED_CANDIDATE_STATES:
            raise ValueError(f"unregistered candidate state: {new_state}")
        # Causality: decision_at must not precede last needed evidence
        if decision_at not in (None, MISSING) and evidence_available_until not in (None, MISSING):
            if decision_at < evidence_available_until:
                decision_at = evidence_available_until
        rc = reason_codes if isinstance(reason_codes, str) else "|".join(reason_codes)
        ds = data_sources if isinstance(data_sources, str) else "|".join(data_sources)
        prev = self.current
        self.transitions.append(
            StateTransition(
                window_id=self.window_id,
                observed_at=observed_at,
                decision_at=decision_at,
                evidence_available_until=evidence_available_until,
                previous_state=prev,
                new_state=new_state,
                reason_codes=rc,
                data_sources=ds,
                quality_status=quality_status,
                notes=notes,
            )
        )
        self.current = new_state


def map_primary_to_candidate(
    *,
    data_incomplete: bool,
    block_flat: bool,
    wait_next_zone: bool,
    primary_class: str,
    mechanism: str,
    possible_regime_flip: bool,
    full_regime_flip: bool,
    liquidity_pull_tagged: bool,
) -> tuple[str, list[str]]:
    """Map microstructure classification → registered candidate state (no outcomes)."""
    reasons: list[str] = []
    if data_incomplete:
        return "data_incomplete", ["DATA_INCOMPLETE"]
    if block_flat:
        return "block_flat_compression", ["BLOCK_FLAT_COMPRESSION"]
    if wait_next_zone:
        return "wait_next_zone_confirmation", ["WAIT_NEXT_ZONE_CONFIRMATION"]
    if full_regime_flip:
        return "full_regime_flip_confirmed", ["FULL_REGIME_FLIP"]
    if possible_regime_flip:
        return "possible_regime_flip", ["POSSIBLE_REGIME_FLIP"]

    if primary_class == "DEFENSE_REJECTION":
        reasons.append("DEFENSE_REJECTION")
        if mechanism in ("ASK_DEFENSE", "BID_DEFENSE"):
            reasons.append(mechanism)
        return "defense_rejection_confirmed", reasons

    if primary_class == "FALSE_BREAKOUT_RECLAIM":
        reasons.append("FALSE_BREAKOUT_RECLAIM")
        return "false_breakout_confirmed", reasons

    if primary_class in (
        "ABSORPTION_THEN_BREAKOUT",
        "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION",
    ):
        if liquidity_pull_tagged or mechanism == "LIQUIDITY_PULL":
            # Liquidity pull must not be valued as absorption breakout
            return "wait_microstructure_confirmation", ["LIQUIDITY_PULL_NOT_ABSORPTION"]
        reasons.append(primary_class)
        if mechanism in ("ASK_ABSORPTION", "BID_ABSORPTION"):
            reasons.append(mechanism)
        return "breakout_confirmed", reasons

    if primary_class == "LIQUIDITY_PULL_BREAKOUT":
        return "wait_microstructure_confirmation", ["LIQUIDITY_PULL_NOT_ABSORPTION"]

    if primary_class in ("NO_RELEVANT_ZONE_CONTACT",):
        return "no_trade", ["NO_RELEVANT_ZONE_CONTACT"]

    if primary_class in ("RANGE_AROUND_ZONE", "UNDETERMINED", "ATTACK_WITHOUT_RESOLUTION"):
        return "wait_microstructure_confirmation", [primary_class or "UNCLEAR"]

    if primary_class == "DATA_INCOMPLETE":
        return "data_incomplete", ["DATA_INCOMPLETE"]

    # Default unclear
    return "wait_microstructure_confirmation", ["UNCLEAR_MICROSTRUCTURE"]


def build_state_timeline(
    *,
    window_id: str,
    window_start: str,
    contact_at: str | None,
    classification_at: str | None,
    data_incomplete: bool,
    incomplete_reason: str,
    block_flat: bool,
    wait_next_zone: bool,
    primary_class: str,
    mechanism: str,
    possible_regime_flip: bool,
    full_regime_flip: bool,
    flip_clocks: dict[str, Any],
    evidence_until: str,
    quality_status: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Append-only timeline; outcome fields must never feed back into this."""
    sm = StateMachine(window_id=window_id)
    sources_base = [
        "candles_5m_closed",
        "orderbook_ob200_v3_raw",
        "public_trades_native",
        "open_interest_1m",
        "liquidations",
    ]

    # Always start watch when approaching / window opens
    obs0 = window_start
    sm.append(
        new_state="watch_zone",
        observed_at=obs0,
        decision_at=obs0,
        evidence_available_until=obs0,
        reason_codes=["ZONE_WATCH"],
        data_sources=["candles_5m_closed"],
        quality_status=quality_status,
    )

    if data_incomplete and not contact_at:
        sm.append(
            new_state="data_incomplete",
            observed_at=evidence_until,
            decision_at=evidence_until,
            evidence_available_until=evidence_until,
            reason_codes=["DATA_INCOMPLETE", incomplete_reason],
            data_sources=sources_base,
            quality_status="DATA_INCOMPLETE",
        )
        return [_t_dict(t) for t in sm.transitions], sm.current, ["DATA_INCOMPLETE"]

    if block_flat:
        at = classification_at or contact_at or window_start
        sm.append(
            new_state="block_flat_compression",
            observed_at=at,
            decision_at=at,
            evidence_available_until=at,
            reason_codes=["BLOCK_FLAT_COMPRESSION"],
            data_sources=["candles_5m_closed"],
            quality_status=quality_status,
        )
        return [_t_dict(t) for t in sm.transitions], sm.current, ["BLOCK_FLAT_COMPRESSION"]

    if contact_at:
        sm.append(
            new_state="wait_microstructure_confirmation",
            observed_at=contact_at,
            decision_at=contact_at,
            evidence_available_until=contact_at,
            reason_codes=["ZONE_TOUCH"],
            data_sources=sources_base,
            quality_status=quality_status,
        )

    liquidity_pull = mechanism == "LIQUIDITY_PULL"
    final, reasons = map_primary_to_candidate(
        data_incomplete=data_incomplete and primary_class == "DATA_INCOMPLETE",
        block_flat=False,
        wait_next_zone=wait_next_zone and primary_class not in (
            "DEFENSE_REJECTION",
            "ABSORPTION_THEN_BREAKOUT",
            "FALSE_BREAKOUT_RECLAIM",
        ),
        primary_class=primary_class,
        mechanism=mechanism,
        possible_regime_flip=possible_regime_flip,
        full_regime_flip=full_regime_flip,
        liquidity_pull_tagged=liquidity_pull,
    )

    # Evidence for this decision = last timestamp actually required (not full-day L2 end).
    evidence_parts = [
        p for p in [classification_at, contact_at] if p and p != MISSING
    ]
    if possible_regime_flip or full_regime_flip:
        for k in (
            "price_breakout_at",
            "wall_absorbed_at",
            "breakout_confirmed_at",
            "retest_at",
            "fast_ema_cross_confirmed_at",
            "full_regime_flip_confirmed_at",
        ):
            v = flip_clocks.get(k)
            if v and v != MISSING:
                evidence_parts.append(str(v))
    last_evidence = max(evidence_parts) if evidence_parts else (classification_at or window_start)
    # Cap by window-local availability marker if earlier (partial L2)
    if evidence_until and evidence_until != MISSING and evidence_until < last_evidence:
        last_evidence = evidence_until
    decision = classification_at or last_evidence
    if decision < last_evidence:
        decision = last_evidence

    if final != sm.current:
        notes = ""
        if liquidity_pull:
            notes = "liquidity_pull_explicit_not_absorption"
        sm.append(
            new_state=final,
            observed_at=classification_at or last_evidence,
            decision_at=decision,
            evidence_available_until=last_evidence,
            reason_codes=reasons,
            data_sources=sources_base,
            quality_status="DATA_INCOMPLETE" if data_incomplete else quality_status,
            notes=notes,
        )

    return [_t_dict(t) for t in sm.transitions], sm.current, reasons


def _t_dict(t: StateTransition) -> dict[str, Any]:
    return {
        "window_id": t.window_id,
        "observed_at": t.observed_at,
        "decision_at": t.decision_at,
        "evidence_available_until": t.evidence_available_until,
        "previous_state": t.previous_state,
        "new_state": t.new_state,
        "reason_codes": t.reason_codes,
        "data_sources": t.data_sources,
        "quality_status": t.quality_status,
        "notes": t.notes,
    }

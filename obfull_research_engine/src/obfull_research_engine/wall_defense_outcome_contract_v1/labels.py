"""Deterministic mechanical OutcomeLabel from OutcomeFacts only."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import (
    CONTRACT_VERSION,
    LABEL_ATTACK_NO_JOINT,
    LABEL_CENSORED,
    LABEL_INCONCLUSIVE,
    LABEL_JOINT_ATTACK_AT_H,
    LABEL_JOINT_RECLAIM_AT_H,
    LABEL_LAYERED_RECLAIM,
    LABEL_MULTI_RECROSS,
    LABEL_NO_RESOLUTION,
    LABEL_NO_WALL_TOUCH,
    LABEL_PULLED,
)
from .models import OutcomeFacts, OutcomeLabel

EPSILON = 1e-12


def _facts_fingerprint(facts: OutcomeFacts) -> str:
    # Stable subset used for label derivation
    keys = [
        "coverage_ok",
        "censor_reason",
        "wall_was_attacked",
        "joint_breach_observed",
        "joint_reclaim_observed",
        "end_price_side",
        "end_microprice_side",
        "recross_count",
        "attacked_chain_node_count",
        "pull_share_raw",
        "first_joint_breach_time",
        "first_joint_reclaim_time",
    ]
    d = facts.to_dict()
    payload = {k: d.get(k) for k in keys}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def label_from_facts(facts: OutcomeFacts) -> OutcomeLabel:
    """Pure function: OutcomeFacts → mechanical label. No feature inputs."""
    fp = _facts_fingerprint(facts)
    note = ""

    if not facts.coverage_ok or facts.censor_reason:
        return OutcomeLabel(
            outcome_contract_version=CONTRACT_VERSION,
            episode_id=facts.episode_id,
            anchor_type=facts.anchor_type,
            horizon_ms=facts.horizon_ms,
            label=LABEL_CENSORED,
            derivation_note=f"censor_reason={facts.censor_reason}",
            facts_fingerprint=fp,
        )

    attacked = bool(facts.wall_was_attacked)
    joint_breach = bool(facts.joint_breach_observed) or bool(facts.first_joint_breach_time)
    joint_reclaim = bool(facts.joint_reclaim_observed) or bool(facts.first_joint_reclaim_time)
    end_p = facts.end_price_side
    end_m = facts.end_microprice_side
    both_attack = end_p == "ATTACK" and end_m == "ATTACK"
    both_def = end_p == "DEFENDER" and end_m == "DEFENDER"
    recross = int(facts.recross_count or 0)
    attacked_nodes = int(facts.attacked_chain_node_count or 0)
    pull_share = facts.pull_share_raw

    if not attacked and not joint_breach:
        # No evidence of attack in horizon window from anchor
        lbl = LABEL_NO_WALL_TOUCH
        note = "no attack evidence"
    elif attacked and not joint_breach:
        lbl = LABEL_ATTACK_NO_JOINT
        note = "attack without joint breach"
    elif joint_breach and both_attack:
        lbl = LABEL_JOINT_ATTACK_AT_H
        note = "joint breach; both sides attack at horizon end"
    elif joint_breach and both_def and attacked_nodes >= 2:
        lbl = LABEL_LAYERED_RECLAIM
        note = ">=2 attacked chain nodes + joint reclaim at horizon"
    elif joint_breach and both_def:
        lbl = LABEL_JOINT_RECLAIM_AT_H
        note = "joint breach then both sides defender at horizon end"
    elif recross >= 2 and not (both_attack or both_def):
        lbl = LABEL_MULTI_RECROSS
        note = "multiple recrosses; unresolved joint side at horizon"
    elif (
        pull_share is not None
        and pull_share >= 0.0
        and float(facts.cumulative_attributed_hits or 0) <= EPSILON
        and float(facts.cumulative_residual_pulls or 0) > EPSILON
        and not joint_breach
    ):
        # Raw pull dominance without matching execution — no Episode-1 threshold;
        # require hits≈0 and pulls>0 as mechanical condition (not optimized).
        lbl = LABEL_PULLED
        note = "pulls without matching attributed hits (raw mechanical)"
    elif attacked or joint_breach:
        lbl = LABEL_NO_RESOLUTION
        note = "attack/breach present but no unique joint end-side"
    else:
        lbl = LABEL_INCONCLUSIVE
        note = "coverage ok but contradictory/unclear"

    # Mixed end sides with recrosses
    if lbl == LABEL_NO_RESOLUTION and recross >= 2 and end_p != end_m:
        lbl = LABEL_MULTI_RECROSS
        note = "recrosses with disagreeing price/micro end sides"

    return OutcomeLabel(
        outcome_contract_version=CONTRACT_VERSION,
        episode_id=facts.episode_id,
        anchor_type=facts.anchor_type,
        horizon_ms=facts.horizon_ms,
        label=lbl,
        label_is_trading_class=False,
        derivation_note=note,
        facts_fingerprint=fp,
    )

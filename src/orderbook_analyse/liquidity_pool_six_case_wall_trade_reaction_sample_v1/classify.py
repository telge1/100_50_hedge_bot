"""Mirror-symmetric evidence classification (rules fixed before run)."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    ACCEPT_SECONDS,
    MIN_ATTACK_NOTIONAL,
    STRONG_IMPACT_BPS,
)


def classify_case(feat: dict[str, Any]) -> dict[str, Any]:
    """Return evidence_class + specific_wall_reaction + pool_level_reaction.

    Rules (ASK/FROM_BELOW; BID/FROM_ABOVE mirrored):
    - Buy(ASK)/Sell(BID) attack with low |impact| + causal reclaim of entry edge
      → rejection / absorption evidence (only if specific wall meaningfully attacked).
    - Acceptance beyond pool component → breakout.
    - No meaningful wall attack → WALL_NOT_MEANINGFULLY_ATTACKED (no defense claim).
    - Cancel/move dominant without absorption → WALL_CANCEL_OR_MOVE_DOMINANT.
    - Rejection with wall switch / mixed wall story → POOL_REJECTION_MIXED_WALL_REACTION.
    """
    if feat.get("window_censored_active"):
        return {
            "evidence_class": "WINDOW_CENSORED_ACTIVE",
            "specific_wall_reaction": "NOT_ASSESSED_CENSORED",
            "pool_level_reaction": "CENSORED_ACTIVE",
            "rule": "cluster_still_active_at_300s_cap",
        }
    if feat.get("insufficient_data"):
        return {
            "evidence_class": "INSUFFICIENT_DATA",
            "specific_wall_reaction": "UNKNOWN",
            "pool_level_reaction": "UNKNOWN",
            "rule": "sparse_ob_or_trades",
        }

    side = feat["side"]
    wall_attacked = bool(feat.get("start_wall_meaningfully_attacked"))
    later_wall = bool(feat.get("later_wall_appeared"))
    later_attacked = bool(feat.get("later_wall_attacked"))
    cancel_dom = bool(feat.get("cancel_or_move_dominant"))
    consume_dom = bool(feat.get("trade_depletion_dominant"))
    refill = bool(feat.get("refill_supported"))
    rejected = bool(feat.get("pool_reclaimed_entry_side"))
    breakout = bool(feat.get("pool_accepted_beyond"))
    attack_n = float(feat.get("attack_notional") or 0.0)
    impact5 = feat.get("impact_5s_bps")
    low_impact = impact5 is not None and abs(float(impact5)) < STRONG_IMPACT_BPS
    high_flow = attack_n >= MIN_ATTACK_NOTIONAL

    # Specific vs pool-level labels (explicit separation)
    if wall_attacked and (consume_dom or refill or cancel_dom or later_wall):
        specific = "SPECIFIC_WALL_REACTION"
    elif wall_attacked:
        specific = "SPECIFIC_WALL_REACTION"
    else:
        specific = "NO_SPECIFIC_WALL_REACTION"

    if rejected or breakout:
        pool_lvl = "POOL_LEVEL_REACTION"
    else:
        pool_lvl = "NO_CLEAR_POOL_LEVEL_REACTION"

    if breakout and not rejected:
        return {
            "evidence_class": "POOL_BREAKOUT_WITH_ACCEPTANCE",
            "specific_wall_reaction": specific,
            "pool_level_reaction": pool_lvl,
            "rule": f"{side}_acceptance_beyond_component",
        }

    if not wall_attacked:
        if cancel_dom:
            return {
                "evidence_class": "WALL_CANCEL_OR_MOVE_DOMINANT",
                "specific_wall_reaction": "CANCEL_OR_MOVE_WITHOUT_ATTACK",
                "pool_level_reaction": pool_lvl,
                "rule": "cancel_move_without_meaningful_attack",
            }
        return {
            "evidence_class": "WALL_NOT_MEANINGFULLY_ATTACKED",
            "specific_wall_reaction": "NO_SPECIFIC_WALL_REACTION",
            "pool_level_reaction": pool_lvl,
            "rule": "no_defense_claim_without_attack",
        }

    if cancel_dom and not consume_dom and not (high_flow and low_impact and rejected):
        return {
            "evidence_class": "WALL_CANCEL_OR_MOVE_DOMINANT",
            "specific_wall_reaction": specific,
            "pool_level_reaction": pool_lvl,
            "rule": "cancel_or_move_dominant_on_attacked_wall",
        }

    if (
        wall_attacked
        and high_flow
        and low_impact
        and rejected
        and not breakout
        and not later_wall
        and not later_attacked
        and (refill or not consume_dom)
    ):
        return {
            "evidence_class": "POOL_REJECTION_WITH_ABSORPTION_EVIDENCE",
            "specific_wall_reaction": "SPECIFIC_WALL_REACTION",
            "pool_level_reaction": "POOL_LEVEL_REACTION",
            "rule": f"{side}_attack_low_impact_reclaim_no_later_wall",
        }

    if rejected and not breakout:
        return {
            "evidence_class": "POOL_REJECTION_MIXED_WALL_REACTION",
            "specific_wall_reaction": specific
            if wall_attacked
            else "NO_SPECIFIC_WALL_REACTION",
            "pool_level_reaction": "POOL_LEVEL_REACTION",
            "rule": f"{side}_rejection_with_mixed_or_switched_walls",
        }

    return {
        "evidence_class": "INSUFFICIENT_DATA",
        "specific_wall_reaction": specific,
        "pool_level_reaction": pool_lvl,
        "rule": "no_rule_fired",
    }

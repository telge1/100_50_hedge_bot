"""Counter-side initiative evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.aggressor_efficiency_flip.contracts import AEFConfig, counter_side
from orderbook_analyse.aggressor_efficiency_flip.impact import DualImpact
from orderbook_analyse.aggressor_efficiency_flip.timeutil import percentile_rank


@dataclass
class InitiativeDecision:
    confirmed: bool
    reason_code: str
    label: str
    notional: float
    dominant_share: float
    notional_rank: float
    share_rank: float
    contemp_impact_rank: float
    post_follow_rank: float
    ordinal_initiative_score: float
    directional_impact_bps: float


def initiative_notional(impact: DualImpact, side: str) -> float:
    return impact.flow.buy_notional if side == "Buy" else impact.flow.sell_notional


def evaluate_initiative(
    impact: DualImpact,
    *,
    direction: str,
    cfg: AEFConfig,
    past_notionals: list[float],
    past_shares: list[float],
    past_impacts: list[float],
    past_posts: list[float],
) -> InitiativeDecision:
    side = counter_side(direction)
    notion = initiative_notional(impact, side)
    share = impact.flow.dominant_share()
    dir_bps = impact.same_side_contemporaneous_bps  # measured with side=counter

    # Recompute using counter side explicitly — DualImpact was built with counter side.
    n_rank = percentile_rank(notion, past_notionals)
    s_rank = percentile_rank(share, past_shares)
    c_rank = percentile_rank(dir_bps, past_impacts)
    p_rank = percentile_rank(impact.post_same_side_followthrough_bps, past_posts)
    score = n_rank + s_rank + c_rank + p_rank + (0.1 if not impact.reclaim_flag else 0.0)

    if impact.flow.dominant_side() != side:
        return InitiativeDecision(
            False, "not_counter_dominant", "WRONG_SIDE", notion, share,
            n_rank, s_rank, c_rank, p_rank, score, dir_bps,
        )
    if notion < cfg.counter_min_notional_usdt or share < cfg.counter_min_dominant_share:
        return InitiativeDecision(
            False, "below_counter_min", "BUY_BURST_WITHOUT_IMPACT" if side == "Buy" else "SELL_BURST_WITHOUT_IMPACT",
            notion, share, n_rank, s_rank, c_rank, p_rank, score, dir_bps,
        )
    if dir_bps < cfg.counter_min_directional_impact_bps:
        # delayed path is diagnostic only — not confirmed for V1 flip
        if impact.post_same_side_followthrough_bps >= cfg.counter_min_directional_impact_bps:
            label = "BUY_BURST_WITH_DELAYED_IMPACT" if side == "Buy" else "SELL_BURST_WITH_DELAYED_IMPACT"
            return InitiativeDecision(
                False, "delayed_impact_only", label, notion, share,
                n_rank, s_rank, c_rank, p_rank, score, dir_bps,
            )
        label = "BUY_BURST_WITHOUT_IMPACT" if side == "Buy" else "SELL_BURST_WITHOUT_IMPACT"
        return InitiativeDecision(
            False, "no_contemporaneous_impact", label, notion, share,
            n_rank, s_rank, c_rank, p_rank, score, dir_bps,
        )
    if impact.post_counter_move_bps >= cfg.counter_max_immediate_reclaim_bps:
        label = "BUY_BURST_FAILED_RECLAIM" if side == "Buy" else "SELL_BURST_FAILED_RECLAIM"
        return InitiativeDecision(
            False, "immediate_reclaim", label, notion, share,
            n_rank, s_rank, c_rank, p_rank, score, dir_bps,
        )

    label = "BUY_INITIATIVE_CONFIRMED" if side == "Buy" else "SELL_INITIATIVE_CONFIRMED"
    return InitiativeDecision(
        True, "initiative_confirmed", label, notion, share,
        n_rank, s_rank, c_rank, p_rank, score, dir_bps,
    )

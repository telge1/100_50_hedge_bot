"""Compression classification with strong_same_side_impact_veto."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import AEFConfig, aggressor_side
from orderbook_analyse.aggressor_efficiency_flip.impact import DualImpact
from orderbook_analyse.aggressor_efficiency_flip.timeutil import invert_rank, percentile_rank


@dataclass
class CompressionDecision:
    allowed: bool
    reason_code: str
    strong_same_side_impact_veto: bool
    delayed_continuation_veto: bool
    notional: float
    dominant_share: float
    notional_rank: float
    share_rank: float
    contemp_impact_rank: float
    post_follow_rank: float
    ordinal_compression_score: float
    semantic_case: str


def compression_notional(impact: DualImpact, side: str) -> float:
    if side == "Sell":
        return impact.flow.sell_notional
    return impact.flow.buy_notional


def evaluate_compression(
    impact: DualImpact,
    *,
    direction: str,
    cfg: AEFConfig,
    past_notionals: list[float],
    past_shares: list[float],
    past_contemp_impacts: list[float],
    past_post_follows: list[float],
) -> CompressionDecision:
    side = aggressor_side(direction)
    if impact.flow.dominant_side() != side:
        return CompressionDecision(
            False, "not_aggressor_dominant", False, False,
            compression_notional(impact, side), impact.flow.dominant_share(),
            0.0, 0.0, 0.0, 0.0, 0.0, "E_or_wrong_side",
        )

    notion = compression_notional(impact, side)
    share = impact.flow.dominant_share()
    same_c = impact.same_side_contemporaneous_bps
    post_same = impact.post_same_side_followthrough_bps

    n_rank = percentile_rank(notion, past_notionals)
    s_rank = percentile_rank(share, past_shares)
    c_rank = percentile_rank(same_c, past_contemp_impacts)
    p_rank = percentile_rank(post_same, past_post_follows)

    strong_veto = same_c >= cfg.strong_same_side_impact_bps
    delayed_veto = post_same >= cfg.strong_post_followthrough_bps

    # ordinal: high notional/share + inverted impact ranks + reclaim bonus
    score = (
        n_rank
        + s_rank
        + invert_rank(c_rank)
        + invert_rank(p_rank)
        + (0.1 if impact.reclaim_flag else 0.0)
    )

    if strong_veto:
        return CompressionDecision(
            False, "strong_same_side_impact_veto", True, delayed_veto,
            notion, share, n_rank, s_rank, c_rank, p_rank, score,
            "C_successful_initiative_NOT_compression",
        )
    if delayed_veto:
        return CompressionDecision(
            False, "delayed_same_side_continuation_veto", False, True,
            notion, share, n_rank, s_rank, c_rank, p_rank, score,
            "D_delayed_initiative_NOT_confirmed_absorption",
        )
    if notion < cfg.min_notional_usdt or share < cfg.min_dominant_share:
        return CompressionDecision(
            False, "below_min_notional_or_share", False, False,
            notion, share, n_rank, s_rank, c_rank, p_rank, score, "E_low_notional",
        )
    if n_rank < cfg.min_notional_rank:
        return CompressionDecision(
            False, "below_min_notional_rank", False, False,
            notion, share, n_rank, s_rank, c_rank, p_rank, score, "E_low_rank",
        )
    if same_c > cfg.weak_contemporaneous_max_bps:
        return CompressionDecision(
            False, "contemporaneous_impact_not_weak", False, False,
            notion, share, n_rank, s_rank, c_rank, p_rank, score, "other",
        )

    case = "B_absorption_with_reclaim" if impact.reclaim_flag else "A_possible_absorption"
    return CompressionDecision(
        True, "compression_confirmed", False, False,
        notion, share, n_rank, s_rank, c_rank, p_rank, score, case,
    )

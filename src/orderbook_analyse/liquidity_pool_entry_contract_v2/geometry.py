"""ASK/BID edge geometry — single declarative mapping, no split thresholds."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.liquidity_pool_entry_contract_v2 import VALID_COMBINATIONS
from orderbook_analyse.liquidity_pool_entry_contract_v2.case_spec import (
    InvalidPoolApproachCombination,
)


@dataclass(frozen=True)
class PoolGeometry:
    pool_side: str
    approach: str
    lower: float
    upper: float
    front_edge: float
    back_edge: float
    # Trade direction implied by defense vs breakout at this contact
    defense_trade_direction: str  # LONG for BID, SHORT for ASK
    breakout_trade_direction: str  # SHORT for BID, LONG for ASK
    # Price direction of accepted breakout relative to mid
    breakout_beyond_back: str  # "below" for BID, "above" for ASK
    reclaim_toward_front: str  # "above" for BID, "below" for ASK
    wall_retreat_adverse: str  # "lower" for BID walls, "higher" for ASK walls
    attack_aggressor: str  # Sell attacks BID, Buy attacks ASK
    defense_counterflow: str  # Buy defends BID, Sell defends ASK


def resolve_geometry(*, pool_side: str, approach: str, lower: float, upper: float) -> PoolGeometry:
    side = pool_side.upper()
    appr = approach.upper()
    if (side, appr) not in VALID_COMBINATIONS:
        raise InvalidPoolApproachCombination(
            f"INVALID_POOL_APPROACH_COMBINATION: {side}/{appr}"
        )
    if upper <= lower:
        raise InvalidPoolApproachCombination(
            f"INVALID_POOL_APPROACH_COMBINATION: upper<=lower {lower}/{upper}"
        )
    if side == "BID":
        # FROM_ABOVE: front=upper, back=lower
        return PoolGeometry(
            pool_side=side,
            approach=appr,
            lower=lower,
            upper=upper,
            front_edge=upper,
            back_edge=lower,
            defense_trade_direction="LONG",
            breakout_trade_direction="SHORT",
            breakout_beyond_back="below",
            reclaim_toward_front="above",
            wall_retreat_adverse="lower",
            attack_aggressor="Sell",
            defense_counterflow="Buy",
        )
    # ASK / FROM_BELOW: front=lower, back=upper
    return PoolGeometry(
        pool_side=side,
        approach=appr,
        lower=lower,
        upper=upper,
        front_edge=lower,
        back_edge=upper,
        defense_trade_direction="SHORT",
        breakout_trade_direction="LONG",
        breakout_beyond_back="above",
        reclaim_toward_front="below",
        wall_retreat_adverse="higher",
        attack_aggressor="Buy",
        defense_counterflow="Sell",
    )


def mirror_price(price: float, *, pivot: float) -> float:
    """Reflect price around pivot for BID↔ASK symmetry fixtures."""
    return 2.0 * pivot - price


def mirror_side(side: str) -> str:
    return "ASK" if side.upper() == "BID" else "BID"


def mirror_approach(approach: str) -> str:
    return "FROM_BELOW" if approach.upper() == "FROM_ABOVE" else "FROM_ABOVE"


def mirror_aggressor(side: str) -> str:
    return "Buy" if side == "Sell" else "Sell"

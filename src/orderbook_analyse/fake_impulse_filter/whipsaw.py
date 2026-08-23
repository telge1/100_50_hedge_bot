from __future__ import annotations

from dataclasses import dataclass

from .states import Side
from .thresholds import ResearchExploreParams, DEFAULT_RESEARCH


@dataclass(frozen=True)
class WhipsawDecision:
    blocked: bool
    cooldown_s: int
    seconds_since_opposite: float | None
    reason: str


def evaluate_whipsaw(
    proposed_side: Side,
    last_opposite_impulse_age_s: float | None,
    opposite_was_active: bool,
    new_direction_independently_confirmed: bool,
    params: ResearchExploreParams = DEFAULT_RESEARCH,
    cooldown_s: int | None = None,
) -> WhipsawDecision:
    """Block opposite entries inside cooldown unless new direction re-confirmed."""
    cd = cooldown_s if cooldown_s is not None else params.primary_cooldown_s
    if not opposite_was_active or last_opposite_impulse_age_s is None:
        return WhipsawDecision(False, cd, last_opposite_impulse_age_s, "no_recent_opposite")
    if last_opposite_impulse_age_s > cd:
        return WhipsawDecision(False, cd, last_opposite_impulse_age_s, "cooldown_expired")
    if new_direction_independently_confirmed:
        return WhipsawDecision(False, cd, last_opposite_impulse_age_s, "independent_reconfirm_allows")
    return WhipsawDecision(
        True,
        cd,
        last_opposite_impulse_age_s,
        f"whipsaw_block_within_{cd}s_without_independent_confirm",
    )

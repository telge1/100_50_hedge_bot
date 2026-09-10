"""OutcomeFacts and OutcomeLabel typed models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import CONTRACT_VERSION


@dataclass
class OutcomeFacts:
    """Raw, non-interpreted future measurements for one (anchor, horizon)."""

    outcome_contract_version: str
    episode_id: str
    chain_id: str | None
    wall_generation_id: str | None
    symbol: str
    zone_id: str
    wall_side: str
    anchor_type: str
    anchor_time: str
    horizon_ms: int
    horizon_end: str
    max_input_available_at: str | None

    start_bid: float | None
    start_ask: float | None
    start_midprice: float | None
    start_microprice: float | None
    end_bid: float | None
    end_ask: float | None
    end_midprice: float | None
    end_microprice: float | None

    end_price_side: str | None
    end_microprice_side: str | None
    first_price_breach_time: str | None
    first_microprice_breach_time: str | None
    first_joint_breach_time: str | None
    first_price_reclaim_time: str | None
    first_microprice_reclaim_time: str | None
    first_joint_reclaim_time: str | None

    attack_side_dwell_ms: int | None
    defender_side_dwell_ms: int | None
    longest_attack_side_dwell_ms: int | None
    longest_defender_side_dwell_ms: int | None
    recross_count: int | None

    max_attack_excursion_ticks: float | None
    max_defender_excursion_ticks: float | None
    max_attack_excursion_bps: float | None
    max_defender_excursion_bps: float | None

    # Side-normalized progress at horizon end vs anchor start mid
    attack_progress_ticks: float | None
    defender_progress_ticks: float | None
    attack_progress_bps: float | None
    defender_progress_bps: float | None

    attacked_chain_node_count: int | None
    crossed_chain_node_count: int | None
    surviving_relevant_wall_count: int | None
    highest_or_lowest_reached_chain_node: float | None
    chain_advance_ticks: float | None

    cumulative_attributed_hits: float | None
    cumulative_residual_pulls: float | None
    cumulative_refills: float | None
    cumulative_attack_notional: float | None
    cumulative_counterflow_notional: float | None
    pull_share_raw: float | None  # raw only — no Episode-1 threshold

    coverage_ok: bool
    replay_epoch: int | None
    censor_reason: str | None
    look_ahead: bool

    # Cluster fields (structure only; gap not profit-optimized)
    attack_cluster_id: str | None = None
    touch_index_within_cluster: int | None = None
    first_touch_in_cluster: bool | None = None
    last_touch_in_cluster: bool | None = None
    cluster_start: str | None = None
    cluster_end: str | None = None

    # Past-only wall features carried for later percentile studies (not selecting Qxx here)
    rolling_size_percentile: float | None = None
    rolling_notional_percentile: float | None = None
    local_depth_share: float | None = None
    wall_rank: int | None = None
    lifetime_ms_to_decision: float | None = None
    distance_to_zone_ticks: float | None = None

    # Wall touched / attacked flags (facts, not labels)
    wall_was_attacked: bool | None = None
    joint_breach_observed: bool | None = None
    joint_reclaim_observed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OutcomeLabel:
    """Deterministic mechanical label derived only from OutcomeFacts."""

    outcome_contract_version: str
    episode_id: str
    anchor_type: str
    horizon_ms: int
    label: str
    label_is_trading_class: bool = False
    derivation_note: str = ""
    facts_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def empty_facts_template(**kwargs: Any) -> OutcomeFacts:
    base = dict(
        outcome_contract_version=CONTRACT_VERSION,
        episode_id="",
        chain_id=None,
        wall_generation_id=None,
        symbol="",
        zone_id="",
        wall_side="ask",
        anchor_type="",
        anchor_time="",
        horizon_ms=0,
        horizon_end="",
        max_input_available_at=None,
        start_bid=None,
        start_ask=None,
        start_midprice=None,
        start_microprice=None,
        end_bid=None,
        end_ask=None,
        end_midprice=None,
        end_microprice=None,
        end_price_side=None,
        end_microprice_side=None,
        first_price_breach_time=None,
        first_microprice_breach_time=None,
        first_joint_breach_time=None,
        first_price_reclaim_time=None,
        first_microprice_reclaim_time=None,
        first_joint_reclaim_time=None,
        attack_side_dwell_ms=None,
        defender_side_dwell_ms=None,
        longest_attack_side_dwell_ms=None,
        longest_defender_side_dwell_ms=None,
        recross_count=None,
        max_attack_excursion_ticks=None,
        max_defender_excursion_ticks=None,
        max_attack_excursion_bps=None,
        max_defender_excursion_bps=None,
        attack_progress_ticks=None,
        defender_progress_ticks=None,
        attack_progress_bps=None,
        defender_progress_bps=None,
        attacked_chain_node_count=None,
        crossed_chain_node_count=None,
        surviving_relevant_wall_count=None,
        highest_or_lowest_reached_chain_node=None,
        chain_advance_ticks=None,
        cumulative_attributed_hits=None,
        cumulative_residual_pulls=None,
        cumulative_refills=None,
        cumulative_attack_notional=None,
        cumulative_counterflow_notional=None,
        pull_share_raw=None,
        coverage_ok=False,
        replay_epoch=None,
        censor_reason=None,
        look_ahead=False,
    )
    base.update(kwargs)
    return OutcomeFacts(**base)

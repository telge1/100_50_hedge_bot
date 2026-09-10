"""Coverage funnel counters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FunnelCounters:
    n_closed_30m_zones: int = 0
    n_visits: int = 0
    n_attack_clusters: int = 0
    n_valid_clusters: int = 0
    n_excluded_clusters: int = 0
    n_enriched: int = 0
    n_ask_wall_flow: int = 0
    n_bid_wall_flow_skipped: int = 0
    n_price_response_ok: int = 0
    n_defense_chain_ok: int = 0
    n_feature_snapshots: int = 0
    n_outcome_complete: int = 0
    n_outcome_censored: int = 0
    exclusion_counts: dict[str, int] = field(default_factory=dict)

    def bump_exclusion(self, reason: str) -> None:
        self.exclusion_counts[reason] = int(self.exclusion_counts.get(reason, 0)) + 1
        self.n_excluded_clusters += 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

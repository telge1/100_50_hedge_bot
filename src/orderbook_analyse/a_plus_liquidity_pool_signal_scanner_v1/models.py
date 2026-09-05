"""Domain models for A+ pool signal scanner (research-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd


def _utc_naive(ts: datetime | None) -> datetime:
    if ts is None:
        raise ValueError("timestamp required")
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    else:
        t = t.tz_localize(None)
    return t.to_pydatetime()


class CandidateState(str, Enum):
    SCAN = "SCAN"
    CANDIDATE = "CANDIDATE"
    CONTEXT_VALID = "CONTEXT_VALID"
    LIMIT_INTENT_ARMED = "LIMIT_INTENT_ARMED"
    LIMIT_ARMED = "LIMIT_INTENT_ARMED"  # alias
    ARMED_AT_POOL = "LIMIT_INTENT_ARMED"  # alias for legacy tests
    WAITING_FOR_1M_CONFIRMATION = "WAITING_FOR_1M_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    HYPOTHETICAL_FILLED = "HYPOTHETICAL_FILLED"
    INVALIDATED = "INVALIDATED"
    INVALIDATED_UNFILLED = "INVALIDATED_UNFILLED"
    EXPIRED = "EXPIRED"
    EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
    NO_TRADE = "NO_TRADE"
    AMBIGUOUS_INTRABAR = "AMBIGUOUS_INTRABAR"
    TERMINAL_CANDIDATE_SUPERSEDED = "TERMINAL_CANDIDATE_SUPERSEDED"


@dataclass
class PoolRecord:
    pool_id: str
    symbol: str
    timeframe: str
    side: str  # BID | ASK
    lower_edge: float
    upper_edge: float
    midpoint: float
    component_count: int
    strength: float | None
    known_at: datetime
    invalidated_at: datetime | None
    source_timestamp: datetime
    available_at: datetime | None = None
    source_bar_start: datetime | None = None
    source_bar_end: datetime | None = None
    confirmation_bar_start: datetime | None = None
    confirmation_bar_end: datetime | None = None
    max_feature_timestamp: datetime | None = None
    lifecycle_status: str | None = None

    def __post_init__(self) -> None:
        if self.available_at is None:
            object.__setattr__(self, "available_at", self.known_at)
        # v2 contract: known_at == available_at
        object.__setattr__(self, "known_at", _utc_naive(self.available_at))
        object.__setattr__(self, "available_at", _utc_naive(self.available_at))

    @property
    def near_edge(self) -> float:
        return self.upper_edge if self.side == "BID" else self.lower_edge

    @property
    def far_edge(self) -> float:
        return self.lower_edge if self.side == "BID" else self.upper_edge

    def is_available_at(self, ts: datetime) -> bool:
        return _utc_naive(self.available_at) <= _utc_naive(ts)

    def is_known_before(self, ts: datetime) -> bool:
        """True when pool was already available strictly before ts."""
        return _utc_naive(self.available_at) < _utc_naive(ts)

    def is_active_at(self, ts: datetime) -> bool:
        if not self.is_available_at(ts):
            return False
        if self.invalidated_at is not None and _utc_naive(self.invalidated_at) <= _utc_naive(ts):
            return False
        return True

    def status_at(self, ts: datetime) -> str:
        if not self.is_available_at(ts):
            return "NOT_YET_KNOWN"
        if self.invalidated_at is not None and _utc_naive(self.invalidated_at) <= _utc_naive(ts):
            return "INVALIDATED"
        return "ACTIVE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "lower_edge": self.lower_edge,
            "upper_edge": self.upper_edge,
            "midpoint": self.midpoint,
            "component_count": self.component_count,
            "strength": self.strength,
            "known_at": self.known_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "invalidated_at": None if self.invalidated_at is None else self.invalidated_at.isoformat(),
            "source_timestamp": self.source_timestamp.isoformat(),
            "source_bar_start": None if self.source_bar_start is None else self.source_bar_start.isoformat(),
            "source_bar_end": None if self.source_bar_end is None else self.source_bar_end.isoformat(),
            "confirmation_bar_start": None
            if self.confirmation_bar_start is None
            else self.confirmation_bar_start.isoformat(),
            "confirmation_bar_end": None
            if self.confirmation_bar_end is None
            else self.confirmation_bar_end.isoformat(),
            "max_feature_timestamp": None
            if self.max_feature_timestamp is None
            else self.max_feature_timestamp.isoformat(),
            "lifecycle_status": self.lifecycle_status,
        }


@dataclass
class GateResult:
    gate: str
    passed: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "passed": self.passed, "reason": self.reason}


@dataclass
class ScannerCandidate:
    setup_id: str
    setup_type: str
    symbol: str
    direction: str  # LONG | SHORT
    state: CandidateState
    entry_pool: PoolRecord
    target_pool: PoolRecord | None
    approach_at: datetime | None = None
    armed_at: datetime | None = None
    signal_at: datetime | None = None
    decision_at: datetime | None = None
    confirmation_at: datetime | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reaction_high: float | None = None
    reaction_low: float | None = None
    reclaim_level: float | None = None
    sweep_high: float | None = None
    sweep_low: float | None = None
    gates: list[GateResult] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    htf_context: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    invalidation_reason: str | None = None
    limit_entry_price: float | None = None
    filled_at: datetime | None = None
    pool_selection_reason: str | None = None
    episode_id: str | None = None
    terminal_ladder_state: str | None = None
    filled_once: bool = False
    signal_id: str | None = None
    first_tradeable_touch_at_entry: datetime | None = None
    hypothetical_filled_at: datetime | None = None
    confirmed_at: datetime | None = None
    invalidated_at: datetime | None = None
    expired_at: datetime | None = None
    max_feature_timestamp: datetime | None = None
    entry_policy: str | None = None
    entry_order_type: str | None = None
    research_only: bool = True
    plan_frozen_at: datetime | None = None

    def all_gates_pass(self) -> bool:
        return bool(self.gates) and all(g.passed for g in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "setup_type": self.setup_type,
            "symbol": self.symbol,
            "direction": self.direction,
            "state": self.state.value,
            "entry_pool": self.entry_pool.to_dict(),
            "target_pool": None if self.target_pool is None else self.target_pool.to_dict(),
            "approach_at": None if self.approach_at is None else self.approach_at.isoformat(),
            "armed_at": None if self.armed_at is None else self.armed_at.isoformat(),
            "signal_at": None if self.signal_at is None else self.signal_at.isoformat(),
            "decision_at": None if self.decision_at is None else self.decision_at.isoformat(),
            "confirmation_at": None if self.confirmation_at is None else self.confirmation_at.isoformat(),
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "reaction_high": self.reaction_high,
            "reaction_low": self.reaction_low,
            "reclaim_level": self.reclaim_level,
            "sweep_high": self.sweep_high,
            "sweep_low": self.sweep_low,
            "gates": [g.to_dict() for g in self.gates],
            "reason_codes": list(self.reason_codes),
            "htf_context": dict(self.htf_context),
            "data_quality": dict(self.data_quality),
            "invalidation_reason": self.invalidation_reason,
            "limit_entry_price": self.limit_entry_price,
            "filled_at": None if self.filled_at is None else self.filled_at.isoformat(),
            "pool_selection_reason": self.pool_selection_reason,
            "episode_id": self.episode_id,
            "terminal_ladder_state": self.terminal_ladder_state,
            "filled_once": self.filled_once,
            "signal_id": self.signal_id or self.setup_id,
            "first_tradeable_touch_at_entry": None
            if self.first_tradeable_touch_at_entry is None
            else self.first_tradeable_touch_at_entry.isoformat(),
            "hypothetical_filled_at": None
            if self.hypothetical_filled_at is None
            else self.hypothetical_filled_at.isoformat(),
            "confirmed_at": None if self.confirmed_at is None else self.confirmed_at.isoformat(),
            "invalidated_at": None if self.invalidated_at is None else self.invalidated_at.isoformat(),
            "expired_at": None if self.expired_at is None else self.expired_at.isoformat(),
            "max_feature_timestamp": None
            if self.max_feature_timestamp is None
            else self.max_feature_timestamp.isoformat(),
            "entry_policy": self.entry_policy,
            "entry_order_type": self.entry_order_type,
            "research_only": self.research_only,
            "plan_frozen_at": None if self.plan_frozen_at is None else self.plan_frozen_at.isoformat(),
            "research_note": "Research Signal – keine ausgeführte Order",
        }

    def to_intent_dict(self) -> dict[str, Any]:
        """Export frozen plan at LIMIT_INTENT_ARMED (research-only)."""
        gross = self.data_quality.get("gross_rr")
        net = self.data_quality.get("estimated_net_rr")
        return {
            "signal_id": self.signal_id or self.setup_id,
            "episode_id": self.episode_id,
            "setup_type": self.setup_type,
            "direction": self.direction,
            "state": self.state.value,
            "armed_at": None if self.armed_at is None else self.armed_at.isoformat(),
            "decision_at": None if self.decision_at is None else self.decision_at.isoformat(),
            "pool_id": self.entry_pool.pool_id,
            "pool_known_at": self.entry_pool.known_at.isoformat(),
            "approach_at": None if self.approach_at is None else self.approach_at.isoformat(),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_price,
            "take_profit": self.target_price,
            "gross_rr": gross,
            "net_rr": net,
            "entry_policy": self.entry_policy,
            "entry_order_type": self.entry_order_type,
            "filled_once": self.filled_once,
            "research_only": self.research_only,
            "max_feature_timestamp": None
            if self.max_feature_timestamp is None
            else self.max_feature_timestamp.isoformat(),
            "plan_frozen_at": None if self.plan_frozen_at is None else self.plan_frozen_at.isoformat(),
        }

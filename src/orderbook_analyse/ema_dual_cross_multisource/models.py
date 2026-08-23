"""Event schema for EMA dual-cross multi-source research."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class CandidateType(str, Enum):
    SYNCHRONOUS_DUAL_EMA_CROSS = "SYNCHRONOUS_DUAL_EMA_CROSS"
    COMPRESSED_EMA59_REBOUND = "COMPRESSED_EMA59_REBOUND"
    REJECTED_EMA_CROSS = "REJECTED_EMA_CROSS"


class FinalVerdict(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    INCONCLUSIVE_DATA = "INCONCLUSIVE_DATA"
    REJECTED = "REJECTED"  # invalid EMA candidate (pre-gate)


class EpisodeState(str, Enum):
    NEUTRAL = "NEUTRAL"
    COMPRESSION_ACTIVE = "COMPRESSION_ACTIVE"
    CROSS_CANDIDATE = "CROSS_CANDIDATE"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    CANDIDATE_ALLOWED = "CANDIDATE_ALLOWED"
    CANDIDATE_BLOCKED = "CANDIDATE_BLOCKED"
    CANDIDATE_INCONCLUSIVE = "CANDIDATE_INCONCLUSIVE"
    TREND_EPISODE_ACTIVE = "TREND_EPISODE_ACTIVE"
    RESET_PENDING = "RESET_PENDING"
    RESET_COMPLETE = "RESET_COMPLETE"


class SourceVerdict(str, Enum):
    CONFIRMING = "CONFIRMING"
    SUPPORTING = "SUPPORTING"
    NEUTRAL = "NEUTRAL"
    CONTRADICTING = "CONTRADICTING"
    STRONGLY_CONTRADICTING = "STRONGLY_CONTRADICTING"
    INCONCLUSIVE_DATA = "INCONCLUSIVE_DATA"


@dataclass
class EmaCandidate:
    candidate_id: str
    episode_id: str
    symbol: str
    timeframe: str
    direction: Direction
    candidate_type: CandidateType
    candidate_at: datetime
    decision_at: datetime | None = None
    entry_at: datetime | None = None
    entry_price: float | None = None
    hypothetical_entry_at: datetime | None = None
    hypothetical_entry_price: float | None = None
    final_verdict: FinalVerdict = FinalVerdict.REJECTED
    reason_codes: list[str] = field(default_factory=list)
    policy_version: str = ""
    bar_index: int = 0
    ema_before: dict[str, Any] = field(default_factory=dict)
    ema_after: dict[str, Any] = field(default_factory=dict)
    ema_metrics: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    source_verdicts: dict[str, str] = field(default_factory=dict)
    outcomes_1h_4h: dict[str, Any] = field(default_factory=dict)
    overlap_flags: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "episode_id": self.episode_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "candidate_type": self.candidate_type.value,
            "candidate_at": self.candidate_at.isoformat(),
            "decision_at": self.decision_at.isoformat() if self.decision_at else None,
            "entry_at": self.entry_at.isoformat() if self.entry_at else None,
            "entry_price": self.entry_price,
            "hypothetical_entry_at": self.hypothetical_entry_at.isoformat() if self.hypothetical_entry_at else None,
            "hypothetical_entry_price": self.hypothetical_entry_price,
            "final_verdict": self.final_verdict.value,
            "reason_codes": list(self.reason_codes),
            "policy_version": self.policy_version,
            "bar_index": self.bar_index,
            "ema_before": dict(self.ema_before),
            "ema_after": dict(self.ema_after),
            "ema_metrics": dict(self.ema_metrics),
            "coverage": dict(self.coverage),
            "features": dict(self.features),
            "source_verdicts": dict(self.source_verdicts),
            "trades_verdict": self.source_verdicts.get("trades"),
            "ob_verdict": self.source_verdicts.get("ob"),
            "oi_verdict": self.source_verdicts.get("oi"),
            "liquidation_verdict": self.source_verdicts.get("liquidations"),
            "liquidity_verdict": self.source_verdicts.get("liquidity"),
            "volatility_verdict": self.source_verdicts.get("volatility"),
            "fake_impulse_verdict": self.source_verdicts.get("fake_impulse"),
            "outcomes_1h_4h": dict(self.outcomes_1h_4h),
            "overlap_flags": dict(self.overlap_flags),
        }

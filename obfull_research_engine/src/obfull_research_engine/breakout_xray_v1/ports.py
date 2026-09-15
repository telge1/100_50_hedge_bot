"""Typed domain objects and repository Protocols for Breakout X-Ray V1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .models import BaselineBookState, ReferenceLevel
from .trades import DedupStats, XRayTrade


@dataclass(frozen=True)
class MidState:
    bucket_start_ns: int
    mid: float
    spread: float | None
    best_bid: float | None
    best_ask: float | None
    book_hash: str
    bid_level_count: int = 0
    ask_level_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket_start_ns": self.bucket_start_ns,
            "mid": self.mid,
            "spread": self.spread,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "book_hash": self.book_hash,
            "bid_level_count": self.bid_level_count,
            "ask_level_count": self.ask_level_count,
        }


@dataclass(frozen=True)
class LevelChangeEvent:
    event_time_ns: int
    side: str  # bid | ask
    price: float
    change_type: str  # ADD | UPDATE | REMOVE
    new_size: float
    old_size: float | None = None
    notional_usdt: float | None = None
    chunk_key: str | None = None
    apply_order: int | None = None
    source_record_ordinal: int | None = None
    record_provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def event_time(self) -> datetime:
        from .time_windows import ns_to_dt

        return ns_to_dt(self.event_time_ns)

    @property
    def size_notional(self) -> float:
        if self.notional_usdt is not None:
            return float(self.notional_usdt)
        return float(self.price) * float(self.new_size)


@dataclass(frozen=True)
class ReadinessResult:
    status: str  # READY | NOT_READY
    reason: str = ""
    epoch_id: str = ""
    chunk_keys: tuple[str, ...] = ()
    level_change_count: int = 0
    state_count: int = 0
    chain_version: str = ""
    chain_hash: str = ""
    safe_start_ns: int | None = None
    safe_end_ns: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "epoch_id": self.epoch_id,
            "chunk_keys": list(self.chunk_keys),
            "level_change_count": self.level_change_count,
            "state_count": self.state_count,
            "chain_version": self.chain_version,
            "chain_hash": self.chain_hash,
            "safe_start_ns": self.safe_start_ns,
            "safe_end_ns": self.safe_end_ns,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BookLevel:
    side: str
    price: float
    size: float

    @property
    def notional_usdt(self) -> float:
        return float(self.price) * float(self.size)


@runtime_checkable
class ReadinessRepository(Protocol):
    def assess_window(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> ReadinessResult: ...


@runtime_checkable
class SilverMetricsRepository(Protocol):
    def load_mid_series(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[MidState]: ...

    def load_book_hash_at_bucket(
        self,
        *,
        symbol: str,
        bucket_start_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> str | None:
        """Return book_hash for exact bucket_start_ns, or None if missing."""
        ...


@runtime_checkable
class SilverLevelChangesRepository(Protocol):
    def load_level_changes(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
        price_min: float,
        price_max: float,
    ) -> list[LevelChangeEvent]: ...


@runtime_checkable
class PublicTradesRepository(Protocol):
    def load_trades(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> list[XRayTrade]:
        """Return raw trades; core performs public_trade_index dedup."""
        ...


@runtime_checkable
class BaselineBookRepository(Protocol):
    def load_baseline(
        self,
        *,
        symbol: str,
        start_ns: int,
        epoch_id: str,
        expected_book_hash: str | None,
    ) -> BaselineBookState: ...


@runtime_checkable
class MarketProfileRepository(Protocol):
    def load_previous_closed_30m_tpo(
        self, *, symbol: str, decision_time: datetime
    ) -> dict[str, Any]: ...


@runtime_checkable
class AvrRepository(Protocol):
    def load_avr(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]: ...


@runtime_checkable
class OpenInterestRepository(Protocol):
    def load_oi(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]: ...


@dataclass
class AnalysisDependencies:
    readiness: ReadinessRepository
    metrics: SilverMetricsRepository
    level_changes: SilverLevelChangesRepository
    trades: PublicTradesRepository
    baseline: BaselineBookRepository
    market_profile: MarketProfileRepository
    avr: AvrRepository
    open_interest: OpenInterestRepository

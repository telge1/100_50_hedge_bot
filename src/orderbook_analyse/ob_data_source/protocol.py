"""Shared contract for orderbook event sources (ClickHouse or OB200 files)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterator, Protocol, runtime_checkable

from orderbook_analyse.orderbook_replay import BookLevelEvent


@dataclass(frozen=True)
class BootstrapRef:
    """Bootstrap snapshot identity (matches find_bootstrap_snapshot return)."""

    exchange_ts: datetime
    update_id: int
    cross_sequence: int


@dataclass
class CoverageReport:
    symbol: str
    requested_start: datetime
    requested_end: datetime
    actual_first_ts: datetime | None = None
    actual_last_ts: datetime | None = None
    files_used: list[str] = field(default_factory=list)
    messages_read: int = 0
    events_emitted: int = 0
    snapshots: int = 0
    deltas: int = 0
    boundary_dedupes: int = 0
    invalid_json: int = 0
    update_gaps: int = 0
    sequence_backwards: int = 0
    timestamp_backwards: int = 0
    crossed_book_samples: int = 0
    valid: bool = False
    reason: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("requested_start", "requested_end", "actual_first_ts", "actual_last_ts"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d


@runtime_checkable
class OrderBookEventSource(Protocol):
    """Produces BookLevelEvent streams for OrderBookReplayer."""

    def find_bootstrap(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> BootstrapRef:
        """Return bootstrap snapshot ref usable at or before ``start``."""

    def iter_events(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[BookLevelEvent]:
        """Yield BookLevelEvents from bootstrap (warmup) through ``end``.

        Events with exchange_ts < start are warmup for state only; callers must
        not emit analysis samples before ``start``.
        """

    def coverage(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> CoverageReport:
        """Scan window and return coverage / integrity stats."""

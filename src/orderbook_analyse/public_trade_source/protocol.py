"""Shared contract for public trade sources (ClickHouse or CSV.GZ files)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class NormalizedPublicTrade:
    """Canonical trade row aligned with ClickHouse ``public_trades`` semantics."""

    trade_ts: datetime
    symbol: str
    side: str  # Buy | Sell (taker aggression; same as Bybit WS / CH ingest)
    size: Decimal  # CH: quantity
    price: Decimal
    notional: Decimal
    trade_id: str
    tick_direction: str
    source: str = ""
    source_file: str = ""
    source_line: int = 0
    notional_source: str = ""  # foreignNotional | price_times_size
    notional_mismatch: bool = False


@dataclass
class TradeCoverageReport:
    symbol: str
    requested_start: datetime
    requested_end: datetime
    actual_first_ts: datetime | None = None
    actual_last_ts: datetime | None = None
    files_expected: list[str] = field(default_factory=list)
    files_found: list[str] = field(default_factory=list)
    missing_dates: list[str] = field(default_factory=list)
    rows_read: int = 0
    trades_emitted: int = 0
    duplicate_trades: int = 0
    invalid_rows: int = 0
    notional_mismatches: int = 0
    buy_count: int = 0
    sell_count: int = 0
    valid: bool = False
    partial: bool = False
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
class PublicTradeSource(Protocol):
    def iter_trades(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[NormalizedPublicTrade]:
        """Yield trades with start <= trade_ts < end (half-open), chronological."""

    def coverage(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> TradeCoverageReport:
        ...

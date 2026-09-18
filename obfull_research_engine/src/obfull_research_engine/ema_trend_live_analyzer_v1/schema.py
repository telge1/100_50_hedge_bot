"""Shared schemas for ema_trend_live_analyzer_v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


ThresholdSide = Literal["ABOVE_EMA_THRESHOLD", "BELOW_EMA_THRESHOLD", "UNKNOWN"]

CandidateState = Literal[
    "UNRESOLVED",
    "EARLY_EVIDENCE",
    "CONTINUATION_EVIDENCE",
    "MEAN_REVERSION_EVIDENCE",
    "CONTINUATION_CANDIDATE",
    "MEAN_REVERSION_CANDIDATE",
    "CONFLICTING",
    "BLOCKED_COVERAGE",
    "BLOCKED_RAW_ARCHIVE",
    "EXPIRED",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    d = as_utc(dt)
    assert d is not None
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    s = str(value).replace("Z", "+00:00")
    return as_utc(datetime.fromisoformat(s))


@dataclass
class EmaSignalEvent:
    signal_id: str
    symbol: str
    candle_time: datetime | None
    created_on: datetime
    ema_value: float | None
    current_price: float | None
    distance: float | None
    expected_distance: float | None
    source_trade_direction: str | None  # metadata only
    batch_id: int | None = None
    signal_received_at: datetime | None = None
    threshold_side: ThresholdSide = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "candle_time": iso_z(self.candle_time),
            "created_on": iso_z(self.created_on),
            "ema_value": self.ema_value,
            "current_price": self.current_price,
            "distance": self.distance,
            "expected_distance": self.expected_distance,
            "source_trade_direction": self.source_trade_direction,
            "batch_id": self.batch_id,
            "signal_received_at": iso_z(self.signal_received_at),
            "threshold_side": self.threshold_side,
        }


@dataclass
class CoverageFlags:
    queue_coverage: bool = True
    sequence_coverage: bool = True
    trade_coverage: bool = True
    archive_coverage: bool = True
    exact_features_valid: bool = True

    def all_ok(self) -> bool:
        return (
            self.queue_coverage
            and self.sequence_coverage
            and self.trade_coverage
            and self.archive_coverage
            and self.exact_features_valid
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_coverage": self.queue_coverage,
            "sequence_coverage": self.sequence_coverage,
            "trade_coverage": self.trade_coverage,
            "archive_coverage": self.archive_coverage,
            "exact_features_valid": self.exact_features_valid,
        }


@dataclass
class WindowState:
    window_start: datetime
    window_end: datetime
    actual_elapsed: float
    book_event_count: int = 0
    trade_count: int = 0
    first_event_ts: str | None = None
    last_event_ts: str | None = None
    coverage: CoverageFlags = field(default_factory=CoverageFlags)
    mass_balance_ok: bool | None = None
    is_complete: bool = False
    horizon_s: float | None = None
    candidate_state: CandidateState = "UNRESOLVED"
    features: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_start": iso_z(self.window_start),
            "window_end": iso_z(self.window_end),
            "actual_elapsed": self.actual_elapsed,
            "book_event_count": self.book_event_count,
            "trade_count": self.trade_count,
            "first_event_ts": self.first_event_ts,
            "last_event_ts": self.last_event_ts,
            "coverage": self.coverage.to_dict(),
            "mass_balance_ok": self.mass_balance_ok,
            "is_complete": self.is_complete,
            "horizon_s": self.horizon_s,
            "candidate_state": self.candidate_state,
            "features": self.features,
        }

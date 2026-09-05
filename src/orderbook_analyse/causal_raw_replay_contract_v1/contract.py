"""Time contract and bucket semantics for CAUSAL_RAW_REPLAY_CONTRACT_V1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.orderbook_v2_live.clock import floor_second_ms

from . import CONTRACT_VERSION, FP_TOL


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bucket_start_ms(ts_ms: int) -> int:
    return floor_second_ms(ts_ms)


def bucket_end_ms(bucket_start: int) -> int:
    """Half-open interval [bucket_start, bucket_end)."""
    return bucket_start + 1000


def is_bucket_final(bucket_start: int, as_of_exclusive_ms: int) -> bool:
    """Final iff bucket_end <= as_of_exclusive."""
    return bucket_end_ms(bucket_start) <= as_of_exclusive_ms


def row_bucket_ms(row: dict[str, Any]) -> int:
    bs = row.get("bucket_start")
    if bs is not None and hasattr(bs, "timestamp"):
        if bs.tzinfo is None:
            bs = bs.replace(tzinfo=timezone.utc)
        return int(bs.timestamp() * 1000)
    return int(row.get("bucket_start_ms", 0))


def row_carried_forward(row: dict[str, Any]) -> bool:
    flags = row.get("quality_flags") or row.get("quality_flag") or ""
    if isinstance(flags, list):
        return "carried_forward" in flags
    return "carried_forward" in str(flags).split(",")


COMPARE_FIELDS = (
    "mid_price",
    "spread_bps",
    "spread_abs",
    "best_bid_price",
    "best_ask_price",
    "imbalance_l50",
    "bid_qty_l50",
    "ask_qty_l50",
)


@dataclass
class ContractBucket:
    """One 1s feature bucket with causal contract metadata."""

    bucket_start_ms: int
    bucket_end_ms: int
    as_of_exclusive_ms: int
    is_final: bool
    is_valid: bool
    carried_forward: bool
    event_time_ms: int
    information_time_ms: int
    max_event_time_used_ms: int
    seed_checkpoint_ts_ms: int | None
    segment_path: str | None
    row: dict[str, Any] = field(repr=False)

    @property
    def is_provisional(self) -> bool:
        return not self.is_final

    def compare_key(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for f in COMPARE_FIELDS:
            v = self.row.get(f)
            if v is not None:
                out[f] = float(v)
        return out


@dataclass
class ReplayInstrumentation:
    requested_as_of_exclusive_ms: int
    max_raw_event_ts_read_ms: int | None = None
    max_raw_event_ts_applied_ms: int | None = None
    max_information_time_final_ms: int | None = None
    first_final_bucket_ms: int | None = None
    last_final_bucket_ms: int | None = None
    provisional_bucket_count: int = 0
    final_bucket_count: int = 0
    seed_checkpoint_ts_ms: int | None = None
    segments_used: list[str] = field(default_factory=list)
    future_event_violation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_as_of_exclusive_ms": self.requested_as_of_exclusive_ms,
            "requested_as_of_exclusive": iso_z(
                datetime.fromtimestamp(self.requested_as_of_exclusive_ms / 1000, tz=timezone.utc)
            ),
            "max_raw_event_ts_read_ms": self.max_raw_event_ts_read_ms,
            "max_raw_event_ts_applied_ms": self.max_raw_event_ts_applied_ms,
            "max_information_time_final_ms": self.max_information_time_final_ms,
            "first_final_bucket_ms": self.first_final_bucket_ms,
            "last_final_bucket_ms": self.last_final_bucket_ms,
            "provisional_bucket_count": self.provisional_bucket_count,
            "final_bucket_count": self.final_bucket_count,
            "seed_checkpoint_ts_ms": self.seed_checkpoint_ts_ms,
            "segments_used": self.segments_used,
            "future_event_violation": self.future_event_violation,
        }


@dataclass
class ReplayResult:
    symbol: str
    as_of_exclusive_ms: int
    finalized: list[ContractBucket]
    provisional: list[ContractBucket]
    instrumentation: ReplayInstrumentation
    contract_version: str = CONTRACT_VERSION

    def finalized_by_start(self) -> dict[int, ContractBucket]:
        return {b.bucket_start_ms: b for b in self.finalized}

    def finalized_dict(self) -> dict[int, dict[str, float]]:
        return {b.bucket_start_ms: b.compare_key() for b in self.finalized}


def buckets_equal(a: dict[str, float], b: dict[str, float], tol: float = FP_TOL) -> bool:
    if set(a) != set(b):
        return False
    for k in a:
        if abs(a[k] - b[k]) > tol:
            return False
    return True


def finalized_prefix(result: ReplayResult, cutoff_ms: int) -> dict[int, ContractBucket]:
    """Final buckets with bucket_end <= cutoff_ms."""
    return {
        b.bucket_start_ms: b
        for b in result.finalized
        if b.bucket_end_ms <= cutoff_ms
    }

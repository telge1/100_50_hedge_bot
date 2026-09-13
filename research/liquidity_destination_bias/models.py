"""Immutable value objects for frozen liquidity-destination episodes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime forbidden")
    return value.astimezone(timezone.utc)


def iso_z(value: datetime | None) -> str:
    if value is None:
        return ""
    return utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class FrozenTarget:
    target_id: str
    side: str
    lower_price: float
    upper_price: float
    touch_price: float
    available_at: datetime
    source: str
    contract_version: str

    def __post_init__(self) -> None:
        if self.side not in {"ASK", "BID"}:
            raise ValueError(f"invalid side: {self.side}")
        if not (self.lower_price <= self.upper_price):
            raise ValueError("target lower_price > upper_price")
        utc(self.available_at)


@dataclass(frozen=True)
class PriceBucket:
    bucket_start: datetime
    low: float
    high: float
    close: float
    trade_count: int

    def __post_init__(self) -> None:
        utc(self.bucket_start)
        if self.low > self.high:
            raise ValueError("price bucket low > high")


@dataclass(frozen=True)
class FrozenEpisode:
    values: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


def target_dict(target: FrozenTarget) -> dict[str, Any]:
    out = asdict(target)
    out["available_at"] = iso_z(target.available_at)
    return out

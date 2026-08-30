"""Strict first-seen wall semantics (V2)."""

from __future__ import annotations

from enum import Enum


class FirstSeenClass(str, Enum):
    PRE_EXISTING_BEFORE_ARRIVAL = "PRE_EXISTING_BEFORE_ARRIVAL"
    FIRST_SEEN_AT_ARRIVAL = "FIRST_SEEN_AT_ARRIVAL"
    APPEARED_STRICTLY_AFTER_ARRIVAL = "APPEARED_STRICTLY_AFTER_ARRIVAL"
    TIMESTAMP_UNRESOLVED = "TIMESTAMP_UNRESOLVED"


def classify_first_seen(
    *,
    first_seen_ts_ms: int | None,
    arrival_ts_ms: int,
    present_in_pre: bool,
    present_at_exact_arrival: bool,
    present_strictly_after: bool,
) -> FirstSeenClass:
    """Hard rules: ts==arrival is never AFTER; snapshot<=arrival is never AFTER."""
    if first_seen_ts_ms is None and not (
        present_in_pre or present_at_exact_arrival or present_strictly_after
    ):
        return FirstSeenClass.TIMESTAMP_UNRESOLVED

    if present_in_pre or (
        first_seen_ts_ms is not None and first_seen_ts_ms < arrival_ts_ms
    ):
        return FirstSeenClass.PRE_EXISTING_BEFORE_ARRIVAL

    if present_at_exact_arrival or (
        first_seen_ts_ms is not None and first_seen_ts_ms <= arrival_ts_ms
    ):
        # includes equality with arrival — NEVER AFTER
        return FirstSeenClass.FIRST_SEEN_AT_ARRIVAL

    if present_strictly_after or (
        first_seen_ts_ms is not None and first_seen_ts_ms > arrival_ts_ms
    ):
        return FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL

    return FirstSeenClass.TIMESTAMP_UNRESOLVED


def wall_identity(*, symbol: str, pool_id: str, side: str, tick_price: float) -> str:
    return f"{symbol}|{pool_id}|{side}|{tick_price:.10g}"


def cluster_wall_identity(*, symbol: str, side: str, tick_price: float) -> str:
    return f"{symbol}|{side}|{tick_price:.10g}"


def normalize_tick_price(price: float, tick: float) -> float:
    return round(price / tick) * tick

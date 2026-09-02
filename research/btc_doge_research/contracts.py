"""Frozen and pilot-level contracts for the research database."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

TARGET_DATABASE = "btc_doge_research"
ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "DOGEUSDT"})
RESEARCH_CONTRACT_VERSION = "btc_doge_research_phase_1_v1"
OB200_CONTRACT_VERSION = "ob200_full_book_event_v1"
OB200_KEY_VERSION = "ob200_event_key_v1"
TRADE_CONTRACT_VERSION = "public_trade_taker_aggressor_v1"
LIQUIDATION_CONTRACT_VERSION = "liquidation_flow_facts_v1"
PROCESSOR_VERSION = "btc_doge_research_processor_v1"
FUNDING_STATUS = "NOT_AVAILABLE"
MAX_WINDOW_SECONDS = 3600


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime forbidden")
    return value.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc(parsed)


def validate_symbol(symbol: str) -> str:
    normalized = symbol.upper()
    if normalized not in ALLOWED_SYMBOLS:
        raise ValueError(f"unsupported symbol: {symbol}")
    return normalized


def validate_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = utc(start), utc(end)
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds > MAX_WINDOW_SECONDS:
        raise ValueError(f"window must be in (0,{MAX_WINDOW_SECONDS}] seconds")
    return start, end


def assert_target_database(database: str) -> None:
    if database != TARGET_DATABASE:
        raise PermissionError(
            f"write target must be {TARGET_DATABASE!r}, got {database!r}"
        )


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def finite_number(value: Any, *, field: str) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must not be NaN/Inf")
    return value


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value).rstrip(b"\x00")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, datetime):
        # clickhouse-connect returns UTC DateTime64 values as naive datetime
        # objects even when the column timezone is explicitly UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return utc(value).isoformat().replace("+00:00", "Z")
    return value

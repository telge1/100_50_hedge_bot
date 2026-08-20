"""Per-symbol skip_before derivation. Never copy one symbol's cutoff onto another."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.orderbook_v2 import PARSER_VERSION
from orderbook_analyse.orderbook_v2.ch_writer import FEATURES_TABLE


def skip_before_from_last_db(
    last_bucket: datetime | None,
    *,
    now: datetime,
    future_grace_sec: float = 2.0,
) -> dict[str, Any]:
    if last_bucket is None:
        return {
            "last_db_bucket": None,
            "skip_before_ms": None,
            "detected_gap_seconds": None,
            "catchup_required": False,
            "error": None,
        }
    if getattr(last_bucket, "tzinfo", None) is None:
        last_bucket = last_bucket.replace(tzinfo=timezone.utc)
    else:
        last_bucket = last_bucket.astimezone(timezone.utc)
    if last_bucket > now + timedelta(seconds=future_grace_sec):
        return {
            "last_db_bucket": last_bucket,
            "skip_before_ms": None,
            "detected_gap_seconds": None,
            "catchup_required": False,
            "error": "future_last_db_bucket",
        }
    skip_ms = int(last_bucket.timestamp() * 1000) + 1000
    gap = int(now.timestamp() - last_bucket.timestamp())
    return {
        "last_db_bucket": last_bucket,
        "skip_before_ms": skip_ms,
        "detected_gap_seconds": max(0, gap),
        "catchup_required": gap > 1,
        "error": None,
    }


def query_last_db_bucket(client: Any, symbol: str, depth: int = 200) -> datetime | None:
    result = client.query(
        f"SELECT max(bucket_start) FROM {FEATURES_TABLE} FINAL "
        "WHERE exchange = %(ex)s AND market = %(mkt)s AND symbol = %(sym)s "
        "AND depth = %(depth)s AND parser_version = %(pv)s",
        parameters={
            "ex": "bybit",
            "mkt": "linear",
            "sym": symbol,
            "depth": depth,
            "pv": PARSER_VERSION,
        },
    )
    value = result.result_rows[0][0] if result.result_rows else None
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_skip_map(
    client: Any,
    symbols: tuple[str, ...],
    *,
    now: datetime,
    depth: int = 200,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        try:
            last = query_last_db_bucket(client, symbol, depth=depth)
            row = skip_before_from_last_db(last, now=now)
        except Exception as exc:
            row = {
                "last_db_bucket": None,
                "skip_before_ms": None,
                "detected_gap_seconds": None,
                "catchup_required": False,
                "error": f"db_read_failed:{type(exc).__name__}",
            }
        out[symbol] = row
    return out

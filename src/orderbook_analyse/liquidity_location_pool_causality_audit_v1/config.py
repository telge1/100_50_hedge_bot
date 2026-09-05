"""Audit window, reference pools, and path constants."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

SYMBOL = "DOGEUSDT"

# Candle warm-up for rolling percentile / HTF history
WARMUP_START = datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)
AUDIT_START = datetime(2026, 8, 27, 0, 0, 0, tzinfo=timezone.utc)
AUDIT_END = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h")

# Named prefix checkpoints (UTC naive wall times as strings for CSV)
PREFIX_CHECKPOINTS = [
    ("T1", "2026-08-28 03:00:00"),
    ("T2", "2026-08-28 03:15:00"),
    ("T3", "2026-08-28 03:30:00"),
    ("T4", "2026-08-28 04:15:00"),
    ("T5", "2026-08-28 06:30:00"),
    ("T6", "2026-08-28 06:35:00"),
    ("T7", "2026-08-28 08:45:00"),
    ("T8", "2026-08-28 10:00:00"),
    ("T9", "2026-08-28 10:27:00"),
    ("T10", "2026-08-28 12:00:00"),
]

DENSE_START = "2026-08-28 03:00:00"
DENSE_END = "2026-08-28 11:00:00"

REFERENCE_POOLS = {
    "short_entry": {
        "pool_id": "lld:DOGEUSDT:15m:upper:1787886900",
        "expected_known_at": "2026-08-28 03:30:00",
        "armed_at": "2026-08-28 04:15:00",
        "fill_at": "2026-08-28 06:35:00",
        "role": "short_entry",
    },
    "short_target": {
        "pool_id": "lld:DOGEUSDT:15m:lower:1787825700",
        "expected_known_at": "2026-08-27 10:30:00",
        "armed_at": "2026-08-28 04:15:00",
        "fill_at": "2026-08-28 06:35:00",
        "role": "short_target",
    },
    "long_target": {
        "pool_id": "lld:DOGEUSDT:15m:upper:1787905800",
        "expected_known_at": "2026-08-28 08:45:00",
        "reclaim_at": "2026-08-28 10:27:00",
        "role": "long_target",
    },
}

ENGINE_ROOT = Path("/home/telgenbuescher/projects/trading_research_platform")
DASHBOARD_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
DEFAULT_OUT_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "liquidity_location_pool_causality_audit_v1"
)

TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}

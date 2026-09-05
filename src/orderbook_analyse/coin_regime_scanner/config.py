"""Constants for COIN_REGIME_SCANNER_V1."""

from __future__ import annotations

SCANNER_VERSION = "COIN_REGIME_SCANNER_V1"
DEFAULT_WARMUP_HOURS = 48
MIN_WARMUP_HOURS = 24
MARKET_ANCHOR = "BTCUSDT"

# Return thresholds (fraction) for direction votes
RET_THR_15M = 0.0015
RET_THR_1H = 0.0030
RET_THR_4H = 0.0060
BTC_ALIGN_THR_1H = 0.0015

# Near range edge (bps of mid)
NEAR_EDGE_BPS = 12.0
TOUCH_SEP_MIN = 5
TOUCH_TOL_BPS = 8.0

# ClickHouse query settings (SELECT only)
QSET = {
    "max_execution_time": 180,
    "receive_timeout": 200,
    "max_memory_usage": 4_000_000_000,
}


def default_warmup_hours() -> int:
    return DEFAULT_WARMUP_HOURS

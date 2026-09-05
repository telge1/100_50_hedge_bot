"""Causal public-trade bubble aggregation (research-only, no writes)."""

from __future__ import annotations

FORMAT_VERSION = "public_trade_bubbles/v1"
MISSING = "MISSING"

# Fixed research defaults — not DOGE-outcome tuned
DEFAULT_TIME_BUCKET_S = 1
DEFAULT_PRICE_TICKS_PER_BUCKET = 5
SIZE_LOOKBACK_CLOSED_BUCKETS = 300
SIZE_WARMUP_MIN = 40
# Causal quantiles of prior closed-bucket total_notional
Q_MEDIUM = 0.70
Q_LARGE = 0.90
Q_EXTREME = 0.97

SIZE_CLASSES = ("UNCALIBRATED", "SMALL", "MEDIUM", "LARGE", "EXTREME")
DISPLAY_MODES = ("off", "large", "large_medium", "all", "delta_debug")

# Bybit / CH convention: side = taker aggressor
# Buy = buyer is taker (hits ask) → aggressive BUY
# Sell = seller is taker (hits bid) → aggressive SELL

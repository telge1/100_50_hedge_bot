"""Central constants and response shapes for footprint candles."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

# --- MVP lock ---
SUPPORTED_SYMBOL = "BTCUSDT"
SUPPORTED_TIMEFRAME = "5m"
SUPPORTED_MODE = "DISPLAY"
BUCKET_STEP = Decimal("5")
TICK_SIZE = Decimal("0.1")
CANDLE_SECONDS = 300

# --- Imbalance (single source of truth) ---
IMBALANCE_RATIO = 3.0
MIN_COMPARE_SIZE = 0.01  # BTC (size units)
STACKED_MIN_LEVELS = 3

# --- API limits (5m candles) ---
# Hard request span: exactly 6 hours.
# 6h / 5m = 72 closed intervals in a half-open [from, to) window aligned to 5m.
# Allow +1 for a forming candle that may straddle the exclusive end → 73.
MAX_RANGE_SECONDS = 6 * 3600  # 21600
MAX_CLOSED_CANDLES = MAX_RANGE_SECONDS // CANDLE_SECONDS  # 72
MAX_CANDLES = MAX_CLOSED_CANDLES + 1  # 73 including forming
# Client may pad the *visible* range by this much on each side, then clamp to MAX_RANGE.
DEFAULT_BUFFER_SECONDS = 900  # 3 × 5m (not part of the 72 closed-candle count)
QUERY_TIMEOUT_S = 25.0

TRADES_FQN = "orderbook_analysis.public_trades_canonical"
CANDLES_FQN = "signal_generator.candles_1m"


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FootprintLevel:
    bucket_index: int
    price_low: float
    price_high: float
    bid_size: float
    ask_size: float
    bid_notional: float
    ask_notional: float
    total_size: float
    total_notional: float
    delta_size: float
    ask_imbalance: bool
    bid_imbalance: bool
    stacked_ask: bool
    stacked_bid: bool
    is_vpoc: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FootprintCandle:
    time: int  # candle open unix UTC seconds
    open: float
    high: float
    low: float
    close: float
    candle_delta_size: float
    candle_delta_notional: float
    vpoc_price: float | None
    vpoc_bucket_index: int | None
    coverage: str
    incomplete: bool
    levels: list[FootprintLevel] = field(default_factory=list)
    trade_count: int = 0
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "candle_delta_size": self.candle_delta_size,
            "candle_delta_notional": self.candle_delta_notional,
            "vpoc_price": self.vpoc_price,
            "vpoc_bucket_index": self.vpoc_bucket_index,
            "coverage": self.coverage,
            "incomplete": self.incomplete,
            "trade_count": self.trade_count,
            "sources": list(self.sources),
            "levels": [lv.to_dict() for lv in self.levels],
        }


def bucket_index_for_price(price: Decimal | float | str) -> int:
    """Global bucket index: floor(price / bucket_step), Decimal-stable."""
    p = price if isinstance(price, Decimal) else Decimal(str(price))
    step = BUCKET_STEP
    # Integer division toward -inf for positive crypto prices.
    return int(p // step)


def bucket_bounds(bucket_index: int) -> tuple[float, float]:
    lo = float(Decimal(bucket_index) * BUCKET_STEP)
    hi = float(Decimal(bucket_index + 1) * BUCKET_STEP)
    return lo, hi


def candle_start_unix(ts_unix: int, candle_seconds: int = CANDLE_SECONDS) -> int:
    return (int(ts_unix) // candle_seconds) * candle_seconds

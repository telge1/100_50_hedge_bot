from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BreakoutTier(str, Enum):
    FAKEOUT = "tier0_fakeout"
    VALID = "tier1_valid"
    STRONG = "tier2_strong"


class MarketState(str, Enum):
    SETUP = "setup"
    ACCUMULATION = "accumulation"
    BREAKOUT_CONFIRMED = "breakout_confirmed"
    FAKEOUT = "fakeout"
    CHOP = "chop"
    EXIT_WARNING = "exit_warning"
    EXIT_CONFIRMED = "exit_confirmed"
    HOLD = "hold"


class TouchDirection(str, Enum):
    FROM_ABOVE = "from_above"
    FROM_BELOW = "from_below"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ObBandSnapshot:
    """Order-book notional inside a bps band around mid."""

    bid_5bps: float
    ask_5bps: float
    bid_10bps: float
    ask_10bps: float

    @property
    def bid_ask_ratio_5bps(self) -> Optional[float]:
        if self.ask_5bps <= 0:
            return None
        return self.bid_5bps / self.ask_5bps

    @property
    def bid_dominant_5bps(self) -> bool:
        return self.bid_5bps > self.ask_5bps

    @property
    def ask_dominant_5bps(self) -> bool:
        return self.ask_5bps > self.bid_5bps


@dataclass(frozen=True)
class TradeWindowStats:
    """Aggregated public-trade aggression over a window."""

    buy_notional: float
    sell_notional: float
    trade_count: int = 0
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None

    @property
    def delta_notional(self) -> float:
        return self.buy_notional - self.sell_notional


@dataclass(frozen=True)
class EmaSnapshot:
    ema9: float
    ema20: float
    ema59: float
    price: float
    ema200: float | None = None

    @property
    def bullish_stack(self) -> bool:
        return self.ema9 > self.ema20 > self.ema59

    @property
    def ema9_below_ema59(self) -> bool:
        return self.ema9 < self.ema59

    @property
    def ema20_below_ema59(self) -> bool:
        return self.ema20 < self.ema59

    @property
    def price_below_ema59(self) -> bool:
        return self.price < self.ema59

    @property
    def ema20_above_ema200(self) -> bool:
        return self.ema200 is not None and self.ema20 > self.ema200

    @property
    def ema59_above_ema200(self) -> bool:
        return self.ema200 is not None and self.ema59 > self.ema200

    @property
    def ema9_below_ema200(self) -> bool:
        return self.ema200 is not None and self.ema9 < self.ema200

    @property
    def price_below_ema200(self) -> bool:
        return self.ema200 is not None and self.price < self.ema200

    @property
    def macro_long_bias(self) -> bool:
        """EMA20 still above EMA200 and not crossed under EMA59."""
        if self.ema200 is None:
            return self.bullish_stack
        return self.ema20 > self.ema200 and self.ema20 > self.ema59


@dataclass(frozen=True)
class CoinThresholds:
    symbol: str
    # Public-trade delta (USDT notional) over confirm window
    fakeout_max_confirm_delta: float
    tier1_min_confirm_delta: float
    tier2_min_confirm_delta: float
    # OB 5bps bid/ask ratio
    tier1_min_bid_ask_ratio_5bps: float
    tier2_min_bid_ask_ratio_5bps: float
    # Follow-through: next window delta must not flip hard against
    fakeout_followthrough_flip_delta: float
    # Context windows (minutes)
    context_lookback_minutes: int = 30
    confirm_window_minutes: int = 5
    followthrough_candles: int = 2


@dataclass
class ClassificationResult:
    state: MarketState
    tier: Optional[BreakoutTier] = None
    side: Optional[str] = None  # "long" | "short" | None
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "tier": self.tier.value if self.tier else None,
            "side": self.side,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }

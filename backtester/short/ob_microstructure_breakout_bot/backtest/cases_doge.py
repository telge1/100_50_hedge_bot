"""Known DOGE calibration cases (offline metrics for rule-engine smoke)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ob_microstructure_breakout_bot.models import (
    BreakoutTier,
    EmaSnapshot,
    MarketState,
    ObBandSnapshot,
    TradeWindowStats,
)


@dataclass(frozen=True)
class KnownCase:
    case_id: str
    label: str
    expected_state: MarketState
    expected_tier: BreakoutTier | None
    confirm: TradeWindowStats
    followthrough: TradeWindowStats | None = None
    ob: ObBandSnapshot | None = None
    ema: EmaSnapshot | None = None
    kind: str = "long_breakout"  # long_breakout | accumulation | ema_exit
    # accumulation extras
    box: TradeWindowStats | None = None
    price_above_box_high: bool = False
    # ema exit extras
    sell_followthrough: bool = False
    start: datetime | None = None
    end: datetime | None = None


def _dt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


DOGE_CASES: list[KnownCase] = [
    KnownCase(
        case_id="doge_20260918_1315_breakout",
        label="Tier2 real breakout 13:15-13:20",
        expected_state=MarketState.BREAKOUT_CONFIRMED,
        expected_tier=BreakoutTier.STRONG,
        confirm=TradeWindowStats(buy_notional=389_400, sell_notional=155_400),
        ob=ObBandSnapshot(
            bid_5bps=114_000, ask_5bps=40_600, bid_10bps=0, ask_10bps=0
        ),
        ema=EmaSnapshot(ema9=0.0857, ema20=0.0855, ema59=0.0852, price=0.0858),
        start=_dt(2026, 9, 18, 13, 15),
        end=_dt(2026, 9, 18, 13, 20),
    ),
    KnownCase(
        case_id="doge_20260918_1425_fakeout",
        label="Tier0 failed long 14:25-14:30",
        expected_state=MarketState.FAKEOUT,
        expected_tier=BreakoutTier.FAKEOUT,
        confirm=TradeWindowStats(buy_notional=50_000, sell_notional=40_000),
        followthrough=TradeWindowStats(buy_notional=100_000, sell_notional=480_600),
        ob=ObBandSnapshot(
            bid_5bps=93_600, ask_5bps=52_400, bid_10bps=0, ask_10bps=0
        ),
        start=_dt(2026, 9, 18, 14, 25),
        end=_dt(2026, 9, 18, 14, 30),
    ),
    KnownCase(
        case_id="doge_20260919_0935_tier1",
        label="Tier1 valid breakout 09:35-10:25",
        expected_state=MarketState.BREAKOUT_CONFIRMED,
        expected_tier=BreakoutTier.VALID,
        confirm=TradeWindowStats(buy_notional=385_100, sell_notional=272_400),
        ob=ObBandSnapshot(
            bid_5bps=126_055, ask_5bps=105_557, bid_10bps=0, ask_10bps=0
        ),
        ema=EmaSnapshot(ema9=0.0874, ema20=0.0873, ema59=0.0871, price=0.08735),
        start=_dt(2026, 9, 19, 9, 35),
        end=_dt(2026, 9, 19, 10, 25),
    ),
    KnownCase(
        case_id="doge_20260918_1105_accumulation",
        label="Box accumulation before 13:35 breakout",
        expected_state=MarketState.ACCUMULATION,
        expected_tier=None,
        kind="accumulation",
        confirm=TradeWindowStats(0, 0),
        box=TradeWindowStats(buy_notional=800_000, sell_notional=750_000),
        ob=ObBandSnapshot(
            bid_5bps=177_851, ask_5bps=89_748, bid_10bps=0, ask_10bps=0
        ),
        ema=EmaSnapshot(ema9=0.0853, ema20=0.0852, ema59=0.0850, price=0.0852),
        price_above_box_high=False,
        start=_dt(2026, 9, 18, 11, 5),
        end=_dt(2026, 9, 18, 13, 35),
    ),
    KnownCase(
        case_id="doge_20260918_2320_exit",
        label="EMA9 below EMA59 exit warning / confirm",
        expected_state=MarketState.EXIT_CONFIRMED,
        expected_tier=None,
        kind="ema_exit",
        confirm=TradeWindowStats(0, 0),
        # 23:20: EMA9 already below EMA59; EMA20 still above (crosses later ~23:40)
        ema=EmaSnapshot(ema9=0.08785, ema20=0.08810, ema59=0.08805, price=0.08787),
        sell_followthrough=True,
        start=_dt(2026, 9, 18, 23, 20),
        end=_dt(2026, 9, 18, 23, 30),
    ),
]


def get_case(case_id: str) -> KnownCase:
    for c in DOGE_CASES:
        if c.case_id == case_id:
            return c
    raise KeyError(f"Unknown case: {case_id}")


def list_cases() -> list[KnownCase]:
    return list(DOGE_CASES)

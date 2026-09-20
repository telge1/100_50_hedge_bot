"""Hydrate known cases from ClickHouse + Full-OB archives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ob_microstructure_breakout_bot.backtest.cases_doge import KnownCase, get_case, list_cases
from ob_microstructure_breakout_bot.data.ema_candles import ema_snapshot_at
from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands
from ob_microstructure_breakout_bot.data.trades import load_trade_window
from ob_microstructure_breakout_bot.models import TradeWindowStats


@dataclass(frozen=True)
class LiveWindows:
    """Where to pull live metrics for a case."""

    confirm_start: datetime
    confirm_end: datetime
    followthrough_start: datetime | None = None
    followthrough_end: datetime | None = None
    box_start: datetime | None = None
    box_end: datetime | None = None
    ob_at: datetime | None = None
    ema_at: datetime | None = None
    price_above_box_high: bool | None = None


def _dt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# Explicit live windows for calibrated DOGE cases.
LIVE_WINDOWS: dict[str, LiveWindows] = {
    "doge_20260918_1315_breakout": LiveWindows(
        confirm_start=_dt(2026, 9, 18, 13, 15),
        confirm_end=_dt(2026, 9, 18, 13, 20),
        # Live OB at 13:10 is ask-heavy; confirm-time book turns bid-supportive.
        ob_at=_dt(2026, 9, 18, 13, 15),
        ema_at=_dt(2026, 9, 18, 13, 20),
    ),
    "doge_20260918_1425_fakeout": LiveWindows(
        # First push still looked long; next window flipped hard sell.
        confirm_start=_dt(2026, 9, 18, 14, 20),
        confirm_end=_dt(2026, 9, 18, 14, 25),
        followthrough_start=_dt(2026, 9, 18, 14, 25),
        followthrough_end=_dt(2026, 9, 18, 14, 30),
        ob_at=_dt(2026, 9, 18, 14, 25),
    ),
    "doge_20260919_0935_tier1": LiveWindows(
        # Setup from 09:35; confirm expansion 10:00-10:15.
        confirm_start=_dt(2026, 9, 19, 10, 0),
        confirm_end=_dt(2026, 9, 19, 10, 15),
        ob_at=_dt(2026, 9, 19, 10, 15),
        ema_at=_dt(2026, 9, 19, 10, 15),
    ),
    "doge_20260918_1105_accumulation": LiveWindows(
        confirm_start=_dt(2026, 9, 18, 11, 5),
        confirm_end=_dt(2026, 9, 18, 11, 10),
        box_start=_dt(2026, 9, 18, 11, 5),
        box_end=_dt(2026, 9, 18, 13, 30),
        ob_at=_dt(2026, 9, 18, 11, 5),
        ema_at=_dt(2026, 9, 18, 13, 0),
        price_above_box_high=False,
    ),
    "doge_20260918_2320_exit": LiveWindows(
        # Chart EMA9 cross ~23:20; 5m computed EMA confirms on the next bar.
        confirm_start=_dt(2026, 9, 18, 23, 20),
        confirm_end=_dt(2026, 9, 18, 23, 25),
        followthrough_start=_dt(2026, 9, 18, 23, 20),
        followthrough_end=_dt(2026, 9, 18, 23, 25),
        ema_at=_dt(2026, 9, 18, 23, 25),
    ),
}


def hydrate_case(
    case: KnownCase,
    *,
    symbol: str = "DOGEUSDT",
    client: Any | None = None,
    fetch_ob: bool = True,
    fetch_ema: bool = True,
) -> KnownCase:
    """Replace seeded metrics with live ClickHouse / OB values where available."""
    windows = LIVE_WINDOWS.get(case.case_id)
    if windows is None:
        raise KeyError(f"No live windows for case {case.case_id}")

    confirm = load_trade_window(
        symbol, windows.confirm_start, windows.confirm_end, client=client
    )

    followthrough: TradeWindowStats | None = None
    if windows.followthrough_start is not None and windows.followthrough_end is not None:
        followthrough = load_trade_window(
            symbol,
            windows.followthrough_start,
            windows.followthrough_end,
            client=client,
        )

    box = case.box
    if windows.box_start is not None and windows.box_end is not None:
        box = load_trade_window(
            symbol, windows.box_start, windows.box_end, client=client
        )

    ob = case.ob
    if fetch_ob and windows.ob_at is not None:
        ob = sample_ob_bands(symbol, windows.ob_at)

    ema = case.ema
    if fetch_ema and windows.ema_at is not None:
        ema = ema_snapshot_at(symbol, windows.ema_at, client=client)

    sell_followthrough = case.sell_followthrough
    if followthrough is not None:
        sell_followthrough = followthrough.delta_notional < 0

    price_above = (
        case.price_above_box_high
        if windows.price_above_box_high is None
        else windows.price_above_box_high
    )

    return replace(
        case,
        confirm=confirm,
        followthrough=followthrough,
        box=box,
        ob=ob,
        ema=ema,
        sell_followthrough=sell_followthrough,
        price_above_box_high=price_above,
        start=windows.confirm_start,
        end=windows.confirm_end,
    )


def hydrate_all(
    *,
    symbol: str = "DOGEUSDT",
    case_ids: list[str] | None = None,
    client: Any | None = None,
) -> list[KnownCase]:
    cases = list_cases() if case_ids is None else [get_case(i) for i in case_ids]
    return [
        hydrate_case(c, symbol=symbol, client=client)
        for c in cases
        if c.case_id in LIVE_WINDOWS
    ]


def default_env_pythonpath() -> list[str]:
    """Paths needed for signal_generator + orderbook_analyse imports."""
    return [
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
        "/home/telgenbuescher/projects/orderbook_analyse/src",
    ]

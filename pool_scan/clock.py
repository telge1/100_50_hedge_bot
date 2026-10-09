"""Bar-open, bar-close, and the moment a pool or a break is known.

Candle timestamps, ``created_timestamp``, and ``invalidated_timestamp`` are
bucket opens. ``pool_available_at`` is the close of the confirmation bar.
A break stored on the open of the breaking bar is known only at that bar's close.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

TRP_ROOT = Path("/home/telgenbuescher/projects/trading_research_platform")


def ensure_trp() -> None:
    text = str(TRP_ROOT)
    if TRP_ROOT.exists() and text not in sys.path:
        sys.path.insert(0, text)


def bar_close(open_time: datetime, timeframe: str) -> datetime:
    ensure_trp()
    from indicators.liquidity_location.availability import bar_end

    return bar_end(open_time, timeframe)


def available_at(pool) -> datetime:
    ensure_trp()
    from indicators.liquidity_location.availability import pool_available_at

    return pool_available_at(pool)


def break_known_at(pool) -> datetime | None:
    """Close of the bar whose open is ``invalidated_timestamp``.

    The engine stores the breaking candle's open, not its close.
    """
    stamp = getattr(pool, "invalidated_timestamp", None)
    if stamp is None:
        return None
    timeframe = getattr(pool, "timeframe", None)
    if not timeframe:
        raise ValueError(f"pool {pool.pool_id} has an invalidation time but no timeframe")
    return bar_close(stamp, timeframe)

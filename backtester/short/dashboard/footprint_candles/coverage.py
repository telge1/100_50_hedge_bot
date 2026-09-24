"""Coverage classification for footprint windows (no density→COMPLETE)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .contracts import CoverageStatus


@dataclass(frozen=True)
class CandleTradePresence:
    time: int
    has_ohlc: bool
    trade_count: int
    sources: tuple[str, ...] = ()


def allows_imbalance_highlight(status: str | CoverageStatus) -> bool:
    return CoverageStatus(status) == CoverageStatus.COMPLETE


def classify_candle(
    *,
    has_ohlc: bool,
    trade_count: int,
    sources: Sequence[str] = (),
    positive_complete: bool = False,
) -> CoverageStatus:
    """Per-candle coverage.

    COMPLETE only when ``positive_complete`` is True (external proof).
    Trade density alone never upgrades to COMPLETE.
    """
    if not has_ohlc and trade_count <= 0:
        return CoverageStatus.MISSING
    if has_ohlc and trade_count <= 0:
        return CoverageStatus.MISSING
    if positive_complete and trade_count > 0:
        return CoverageStatus.COMPLETE
    src = {str(s).lower() for s in sources if s}
    if "archive" in src:
        # Backfilled / mixed archive presence → not confirmed live continuity.
        return CoverageStatus.PARTIAL
    # Trades present but no positive continuity proof.
    return CoverageStatus.UNKNOWN


def classify_window(
    candles: Iterable[CandleTradePresence],
    *,
    positive_complete: bool = False,
) -> CoverageStatus:
    """Roll up per-candle statuses for the response meta.

    - all MISSING → MISSING
    - any MISSING among otherwise populated → PARTIAL
    - any PARTIAL → PARTIAL
    - all COMPLETE → COMPLETE
    - else UNKNOWN
    """
    statuses = [
        classify_candle(
            has_ohlc=c.has_ohlc,
            trade_count=c.trade_count,
            sources=c.sources,
            positive_complete=positive_complete,
        )
        for c in candles
    ]
    if not statuses:
        return CoverageStatus.MISSING
    if all(s == CoverageStatus.MISSING for s in statuses):
        return CoverageStatus.MISSING
    if any(s == CoverageStatus.MISSING for s in statuses):
        return CoverageStatus.PARTIAL
    if any(s == CoverageStatus.PARTIAL for s in statuses):
        return CoverageStatus.PARTIAL
    if all(s == CoverageStatus.COMPLETE for s in statuses):
        return CoverageStatus.COMPLETE
    return CoverageStatus.UNKNOWN


# Documented outage used only in unit tests / fixtures (not consulted by live CH path).
KNOWN_PUBLIC_TRADE_GAP_UTC = (
    # Collector stopped ~2026-09-05 09:56 UTC, restarted ~2026-09-06 07:06 UTC.
    # CH showed empty trade hours 2026-09-06 00:00–06:59 UTC while candles existed.
    (1_757_116_800, 1_757_174_400),  # placeholders overridden in tests by ISO helpers
)


def gap_sep5_sep6_unix() -> tuple[int, int]:
    """Known public-trade collector gap (UTC) for regression fixtures."""
    from datetime import datetime, timezone

    start = int(datetime(2026, 9, 5, 9, 56, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 9, 6, 7, 6, tzinfo=timezone.utc).timestamp())
    return start, end


def overlaps_known_gap(candle_start: int, candle_end: int) -> bool:
    g0, g1 = gap_sep5_sep6_unix()
    return candle_start < g1 and candle_end > g0

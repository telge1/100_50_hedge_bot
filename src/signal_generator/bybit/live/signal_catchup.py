"""Shadow signal catch-up for the live collector (NO trading)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Sequence

from signal_generator.bybit.history import ensure_utc
from signal_generator.db.candles import CandleRepository
from signal_generator.db.processing_state import ProcessingStateRepository
from signal_generator.db.signals import SignalRepository
from signal_generator.pipeline.processor import (
    DEFAULT_LOOKBACK,
    PipelineMetrics,
    ShadowPipelineConfig,
    WaveFadeShadowPipeline,
)
from signal_generator.pipeline.versions import MODE_SHADOW
from signal_generator.timeframes import STRATEGY_TIMEFRAMES, TF_MINUTES, bucket_close, bucket_start

logger = logging.getLogger(__name__)

# Hard guard: this module must never enable trading.
SHADOW_MODE = True
TRADING_ENABLED = False


def assert_shadow_only() -> None:
    if not SHADOW_MODE or TRADING_ENABLED:
        raise RuntimeError("Live collector signal path must remain SHADOW_MODE only")


def htf_boundaries_at_close(close_time: datetime) -> tuple[str, ...]:
    """Return strategy HTFs whose bucket closes exactly at ``close_time`` (UTC).

    Uses ``STRATEGY_TIMEFRAMES`` only (15m / 30m / 1h / 4h). ``5m`` is never
    included — it is not a signal timeframe.
    """
    close_time = ensure_utc(close_time).replace(second=0, microsecond=0)
    hit: list[str] = []
    for tf in STRATEGY_TIMEFRAMES:
        mins = TF_MINUTES[tf]
        open_candidate = close_time - timedelta(minutes=mins)
        if bucket_start(open_candidate, tf) == open_candidate:
            if bucket_close(open_candidate, tf) == close_time:
                hit.append(tf)
    return tuple(hit)


def htf_boundary_at_close(close_time: datetime) -> bool:
    """True if ``close_time`` closes at least one strategy HTF bucket."""
    return bool(htf_boundaries_at_close(close_time))


def signal_catchup_end_exclusive(closed_1m_close_time: datetime) -> datetime:
    """Exclusive ``end`` for ``run_signal_catchup`` that includes HTFs available at close.

    Pipeline window is half-open on availability: ``available_at < end``.
    A 1m candle with ``close_time=T`` makes HTFs with ``available_at=T`` eligible,
    so exclusive end must be strictly after ``T`` (next UTC minute).
    """
    return ensure_utc(closed_1m_close_time).replace(second=0, microsecond=0) + timedelta(
        minutes=1
    )


def run_signal_catchup(
    *,
    symbols: Sequence[str],
    candles: CandleRepository,
    signals: SignalRepository,
    state: ProcessingStateRepository,
    end: datetime,
    start: datetime | None = None,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> PipelineMetrics:
    """Run watermark-driven shadow catch-up up to exclusive ``end`` (UTC)."""
    assert_shadow_only()
    end = ensure_utc(end)
    if start is None:
        # Window large enough that watermarks dominate resume; lookback fills warm-up.
        start = end - lookback
    start = ensure_utc(start)
    if end <= start:
        return PipelineMetrics(symbols_configured=list(symbols))

    cfg = ShadowPipelineConfig(
        symbols=list(symbols),
        start=start,
        end=end,
        mode=MODE_SHADOW,
        shadow=True,
        lookback=lookback,
    )
    pipe = WaveFadeShadowPipeline(
        candles=candles,
        signals=signals,
        state=state,
        config=cfg,
    )
    metrics = pipe.run()
    logger.info(
        "SIGNAL_CATCHUP symbols=%s end=%s candidates=%s tier_a=%s errors=%s",
        list(symbols),
        end.isoformat(),
        metrics.candidate_signals,
        metrics.tier_a_signals,
        metrics.errors,
    )
    return metrics

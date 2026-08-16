"""Shadow Wave-Fade signal processor (no trading).

Loads closed 1m → HTF aggregate → frozen strategy → persist candidates + watermarks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pandas as pd

from signal_generator.db.candles import CandleRepository
from signal_generator.db.processing_state import ProcessingState, ProcessingStateRepository
from signal_generator.db.signals import Signal, SignalRepository
from signal_generator.pipeline.mapper import wave_event_to_signal
from signal_generator.pipeline.trade_plan import (
    attach_resolved_entries,
    merge_trade_plan_into_metadata,
    reconstruct_trade_plan,
)
from signal_generator.pipeline.versions import (
    EDGES_VERSION,
    GENERATOR_VERSION,
    MODE_SHADOW,
    STRATEGY_VERSION,
)
from signal_generator.strategy.wave_fade.adapter import bars_to_ohlcv_df, one_minute_books
from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges
from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS
from signal_generator.strategy.wave_fade.signals import build_symbol_signals, build_waves_from_ohlcv
from signal_generator.timeframes import (
    STRATEGY_TIMEFRAMES,
    TF_MINUTES,
    BucketStatus,
    aggregate_1m_to_timeframe,
    bars_from_mappings,
    bucket_close,
    bucket_start,
    ensure_utc,
    inspect_bucket,
    iter_bucket_opens,
)

logger = logging.getLogger(__name__)

# Warm-up for Stoch/EMA on HTF (EMA400 worst-case). Prefer calendar lookback.
DEFAULT_LOOKBACK = timedelta(days=14)


@dataclass
class PipelineMetrics:
    symbols_configured: list[str] = field(default_factory=list)
    symbols_processed: int = 0
    candles_1m_loaded: int = 0
    buckets_processed: int = 0
    candidate_signals: int = 0
    tier_a_signals: int = 0
    long_signals: int = 0
    short_signals: int = 0
    errors: int = 0
    last_processed: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols_configured": self.symbols_configured,
            "symbols_processed": self.symbols_processed,
            "candles_1m_loaded": self.candles_1m_loaded,
            "buckets_processed": self.buckets_processed,
            "candidate_signals": self.candidate_signals,
            "tier_a_signals": self.tier_a_signals,
            "long_signals": self.long_signals,
            "short_signals": self.short_signals,
            "errors": self.errors,
            "last_processed": self.last_processed,
        }


@dataclass
class ShadowPipelineConfig:
    symbols: Sequence[str]
    start: datetime
    end: datetime
    exchange: str = "bybit"
    mode: str = MODE_SHADOW
    strategy_version: str = STRATEGY_VERSION
    generator_version: str = GENERATOR_VERSION
    edges_version: str = EDGES_VERSION
    timeframes: Sequence[str] = STRATEGY_TIMEFRAMES
    lookback: timedelta = DEFAULT_LOOKBACK
    # If True, never place orders (always enforced for shadow)
    shadow: bool = True


class WaveFadeShadowPipeline:
    """Deterministic shadow signal generator with persistent watermarks."""

    def __init__(
        self,
        *,
        candles: CandleRepository,
        signals: SignalRepository,
        state: ProcessingStateRepository,
        config: ShadowPipelineConfig,
        edges: dict[tuple[str, str, str], dict[float, float]] | None = None,
    ) -> None:
        if not config.shadow or config.mode != MODE_SHADOW:
            raise ValueError("WaveFadeShadowPipeline only supports shadow mode")
        self.candles = candles
        self.signals = signals
        self.state = state
        self.config = config
        self.edges = edges if edges is not None else load_frozen_eff_edges()
        self.metrics = PipelineMetrics(symbols_configured=list(config.symbols))

    def run(self) -> PipelineMetrics:
        for symbol in self.config.symbols:
            try:
                self._run_symbol(symbol)
                self.metrics.symbols_processed += 1
            except Exception:
                self.metrics.errors += 1
                logger.exception("pipeline failed for %s", symbol)
                raise
        return self.metrics

    def _run_symbol(self, symbol: str) -> None:
        cfg = self.config
        start = ensure_utc(cfg.start)
        end = ensure_utc(cfg.end)
        load_start = start - cfg.lookback

        rows = self.candles.get_candles(
            symbol, load_start, end, exchange=cfg.exchange, interval="1m"
        )
        self.metrics.candles_1m_loaded += len(rows)
        bars_1m = bars_from_mappings(rows)
        if not bars_1m:
            logger.warning("[%s] no 1m candles in [%s, %s)", symbol, load_start, end)
            return

        # as_of = last closed 1m close in window (causal end of replay)
        as_of = max(ensure_utc(b.close_time) for b in bars_1m)
        # Cap as_of to end (exclusive window means last usable is end)
        if as_of > end:
            as_of = end

        ohlcv_1m = bars_to_ohlcv_df(bars_1m)
        open_times, opens = one_minute_books(ohlcv_1m)

        htf_ohlcv: dict[str, pd.DataFrame] = {}
        waves_by_tf: dict[str, pd.DataFrame] = {}
        for tf in cfg.timeframes:
            htf_bars = aggregate_1m_to_timeframe(
                bars_1m, tf, as_of=as_of, require_complete=True
            )
            ohlcv = bars_to_ohlcv_df(htf_bars)
            htf_ohlcv[tf] = ohlcv
            waves_by_tf[tf] = build_waves_from_ohlcv(ohlcv, symbol=symbol, timeframe=tf)

        # Full annotated signal set (same edges for every symbol — GLOBAL_FROZEN_TIER_A)
        all_sig = build_symbol_signals(symbol, self.edges, waves_by_tf)
        if all_sig.empty:
            all_sig = pd.DataFrame()

        for tf in cfg.timeframes:
            self._process_timeframe(
                symbol=symbol,
                timeframe=tf,
                bars_1m=bars_1m,
                all_sig=all_sig,
                as_of=as_of,
                window_start=start,
                window_end=end,
                open_times=open_times,
                opens=opens,
            )

        # T0 1m open is often only available after confirmation; finalize pending prices.
        self._finalize_pending_entries(
            symbol=symbol,
            open_times=open_times,
            opens=opens,
            window_start=start,
            window_end=end,
        )

    def _process_timeframe(
        self,
        *,
        symbol: str,
        timeframe: str,
        bars_1m: list,
        all_sig: pd.DataFrame,
        as_of: datetime,
        window_start: datetime,
        window_end: datetime,
        open_times,
        opens,
    ) -> None:
        cfg = self.config
        st = self.state.get(symbol, timeframe, cfg.strategy_version, exchange=cfg.exchange)

        # Resume from next bucket after watermark (by open time)
        if st is not None:
            resume_from = ensure_utc(st.last_processed_candle_open_time) + timedelta(
                minutes=TF_MINUTES[timeframe]
            )
        else:
            # Include HTF bars whose available_at (=close) falls in [start, end).
            # A bar closing exactly at window_start opened one TF earlier.
            resume_from = ensure_utc(window_start) - timedelta(minutes=TF_MINUTES[timeframe])

        resume_from = bucket_start(resume_from, timeframe)

        tf_sig = (
            all_sig[all_sig["signal_tf"].astype(str) == timeframe].copy()
            if not all_sig.empty
            else pd.DataFrame()
        )

        for bucket_open in iter_bucket_opens(resume_from, window_end, timeframe):
            close_t = bucket_close(bucket_open, timeframe)
            # Half-open availability window: [window_start, window_end)
            if close_t < window_start:
                continue
            if close_t >= window_end:
                break
            if close_t > as_of:
                break

            insp = inspect_bucket(
                bars_1m,
                timeframe=timeframe,
                bucket_open=bucket_open,
                as_of=as_of,
            )
            if insp.status == BucketStatus.NOT_CLOSED:
                break
            if insp.status == BucketStatus.INCOMPLETE:
                # Gap: do not advance watermark — recovery can fill 1m later
                logger.warning(
                    "[%s][%s] incomplete bucket open=%s missing=%s — watermark held",
                    symbol,
                    timeframe,
                    bucket_open.isoformat(),
                    len(insp.missing_open_times),
                )
                break

            # Signals whose wave ends exactly when this HTF bar becomes available
            batch: list[Signal] = []
            if not tf_sig.empty:
                conf = pd.to_datetime(tf_sig["confirmation_available_at"], utc=True)
                close_ts = pd.Timestamp(close_t)
                if close_ts.tzinfo is None:
                    close_ts = close_ts.tz_localize("UTC")
                else:
                    close_ts = close_ts.tz_convert("UTC")
                # Floor to minute — CH/numpy may carry ms noise
                mask = conf.dt.floor("min") == close_ts.floor("min")
                matched = tf_sig.loc[mask]
                if not matched.empty:
                    matched = attach_resolved_entries(matched, open_times, opens)
                for _, row in matched.iterrows():
                    sig = wave_event_to_signal(
                        row,
                        symbol=symbol,
                        timeframe=timeframe,
                        mode=cfg.mode,
                        generator_version=cfg.generator_version,
                        strategy_version=cfg.strategy_version,
                        edges_version=cfg.edges_version,
                        selected=False,
                        selection_reason="CANDIDATE_RAW",
                    )
                    assert sig.traded is False
                    batch.append(sig)

            # At-least-once: insert signals first, then watermark
            if batch:
                self.signals.insert_signals(batch)
                self.metrics.candidate_signals += len(batch)
                for s in batch:
                    if s.tier_a:
                        self.metrics.tier_a_signals += 1
                    if s.direction == "LONG":
                        self.metrics.long_signals += 1
                    else:
                        self.metrics.short_signals += 1
                for s in batch:
                    logger.info(
                        "[%s][%s] processed=%s signal=%s tier_a=%s",
                        symbol,
                        timeframe,
                        close_t.strftime("%Y-%m-%dT%H:%MZ"),
                        s.direction,
                        str(s.tier_a).lower(),
                    )
            else:
                logger.debug(
                    "[%s][%s] processed=%s signal=none",
                    symbol,
                    timeframe,
                    close_t.strftime("%Y-%m-%dT%H:%MZ"),
                )

            self.state.upsert(
                ProcessingState(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_version=cfg.strategy_version,
                    last_processed_candle_open_time=bucket_open,
                    last_processed_available_at=close_t,
                    exchange=cfg.exchange,
                    metadata="{}",
                )
            )
            self.metrics.buckets_processed += 1
            self.metrics.last_processed[f"{symbol}:{timeframe}"] = close_t.isoformat()
    def _finalize_pending_entries(
        self,
        *,
        symbol: str,
        open_times,
        opens,
        window_start: datetime,
        window_end: datetime,
    ) -> None:
        """Re-insert signals whose T0 entry was not yet available (signal_price=0).

        Idempotent via ReplacingMergeTree(signal_id) + same deterministic id.
        Candidates remain stored; only price/metadata trade-plan fields are filled.
        """
        from decimal import Decimal

        from signal_generator.pipeline.trade_plan import parse_trade_plan_from_metadata

        query = getattr(self.signals, "query_signals", None)
        if not callable(query):
            return

        # Look slightly before window so delayed T0 after HTF close can fill.
        start = ensure_utc(window_start) - timedelta(hours=6)
        end = ensure_utc(window_end) + timedelta(minutes=5)
        rows, _total = query(
            start=start,
            end=end,
            symbols=[symbol],
            limit=2000,
            offset=0,
        )
        pending: list[Signal] = []
        for r in rows:
            try:
                px = float(r.get("signal_price") or 0)
            except (TypeError, ValueError):
                px = 0.0
            plan = parse_trade_plan_from_metadata(r.get("metadata"))
            if px > 0 and plan.get("entry_valid") is True:
                continue
            conf = ensure_utc(r["candle_close_time"])
            new_plan = reconstruct_trade_plan(
                confirmation_available_at=conf,
                side=str(r["direction"]),
                timeframe=str(r["timeframe"]),
                open_times=open_times,
                opens=opens,
            )
            if not new_plan.get("entry_valid") or not new_plan.get("entry_price"):
                continue
            entry = float(new_plan["entry_price"])
            sig = Signal(
                symbol=str(r["symbol"]),
                timeframe=str(r["timeframe"]),
                direction=str(r["direction"]),
                signal_type=str(r["signal_type"]),
                signal_price=Decimal(str(entry)),
                candle_open_time=ensure_utc(r["candle_open_time"]),
                candle_close_time=conf,
                generator_version=str(r["generator_version"]),
                strategy_version=str(r["strategy_version"]),
                signal_id=r["signal_id"],
                generated_at=ensure_utc(r["generated_at"]),
                stoch_k=r.get("stoch_k"),
                stoch_d=r.get("stoch_d"),
                wave_state=r.get("wave_state"),
                tier_a=bool(r.get("tier_a")),
                tier_a_context=str(r.get("tier_a_context") or ""),
                rank_score=r.get("rank_score"),
                selected=bool(r.get("selected")),
                selection_reason=str(r.get("selection_reason") or ""),
                trend_15m=r.get("trend_15m"),
                trend_30m=r.get("trend_30m"),
                trend_1h=r.get("trend_1h"),
                trend_4h=r.get("trend_4h"),
                signal_bias=r.get("signal_bias"),
                traded=bool(r.get("traded")),
                trade_id=r.get("trade_id"),
                metadata=merge_trade_plan_into_metadata(r.get("metadata") or "{}", new_plan),
            )
            pending.append(sig)
        if pending:
            self.signals.insert_signals(pending)
            logger.info(
                "[%s] finalized entry/tp/sl for %s pending signal(s)",
                symbol,
                len(pending),
            )

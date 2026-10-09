"""
Process-local canonical 1m buffer + TRP 5m aggregation for LMV2 research backfills.

Does not modify dashboard service.load_candles cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from data.models import Candle

from ...clickhouse_source import ClickHouseResearchCandleSource
from ...service import SOURCE_TF, _timeframe_seconds
from ...trp_import import ensure_trp_on_path, load_trp
from ..identity import build_live_micro_lld_v2_config


class CandleDataGapError(Exception):
    """Missing 1m bars in required range — fail closed."""


class CandleRevisionConflictError(Exception):
    """Same open_time with different OHLCV — fail closed."""


@dataclass
class SessionStats:
    ch_fetch_calls: int = 0
    slice_calls: int = 0
    slices_without_ch_fetch: int = 0
    full_5m_reaggregations: int = 0
    full_5m_rebuilds: int = 0
    incremental_bucket_recomputations: int = 0
    ch_fetch_seconds: float = 0.0
    full_aggregation_seconds: float = 0.0
    incremental_aggregation_seconds: float = 0.0
    window_slice_seconds: float = 0.0

    @property
    def cache_hit_rate(self) -> float:
        if self.slice_calls == 0:
            return 0.0
        return self.slices_without_ch_fetch / self.slice_calls


@dataclass
class CanonicalCandleSession:
    """
    Holds closed 1m candles for one symbol; builds 5m via trp.aggregate(strict).
    """

    symbol: str
    timeframe: str = "5m"
    max_1m_bars: int = 220_000
    use_incremental_5m: bool = False
    stats: SessionStats = field(default_factory=SessionStats)

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol).strip().upper()
        self.timeframe = str(self.timeframe).strip()
        if self.timeframe == SOURCE_TF:
            raise ValueError("CanonicalCandleSession targets HTF from 1m; use 5m+")
        self._source = ClickHouseResearchCandleSource()
        self._1m_by_unix: dict[int, Candle] = {}
        self._5m_by_open_unix: dict[int, Candle] = {}
        self._min_unix: int | None = None
        self._max_unix: int | None = None
        self._last_src_start_unix: int | None = None
        self._last_to_unix: int | None = None
        self._need_1m_per_5m = int(
            load_trp()["expected_source_bars"](SOURCE_TF, self.timeframe)
        )
        self._tf_sec = _timeframe_seconds(self.timeframe)

    def clear(self) -> None:
        self._1m_by_unix.clear()
        self._5m_by_open_unix.clear()
        self._min_unix = None
        self._max_unix = None
        self._last_src_start_unix = None
        self._last_to_unix = None
        self.stats = SessionStats()

    def ingest_1m_closed(self, rows: Sequence[Candle]) -> None:
        """Test/POC hook: merge closed 1m without ClickHouse (updates incremental 5m if enabled)."""
        new_u = self._merge_1m(rows)
        if self._1m_by_unix:
            self._min_unix = min(self._1m_by_unix)
            self._max_unix = max(self._1m_by_unix)
        if self.use_incremental_5m:
            self._incremental_update_5m(new_u)

    def _bucket_open_unix(self, one_m_unix: int) -> int:
        trp = load_trp()
        floor = trp["floor_utc"](
            datetime.fromtimestamp(int(one_m_unix), tz=timezone.utc), self.timeframe
        )
        return int(floor.timestamp())

    def _merge_1m(self, rows: Sequence[Candle]) -> list[int]:
        new_unix: list[int] = []
        for c in rows:
            u = int(c.unix_seconds)
            existing = self._1m_by_unix.get(u)
            if existing is None:
                self._1m_by_unix[u] = c
                new_unix.append(u)
                continue
            if (
                float(existing.open) != float(c.open)
                or float(existing.high) != float(c.high)
                or float(existing.low) != float(c.low)
                or float(existing.close) != float(c.close)
                or float(existing.volume) != float(c.volume)
            ):
                raise CandleRevisionConflictError(
                    f"1m revision conflict at {u} for {self.symbol}"
                )
        return new_unix

    def _sorted_1m(self) -> list[Candle]:
        return [self._1m_by_unix[u] for u in sorted(self._1m_by_unix)]

    def _aggregate_trp(self, one_m: list[Candle]) -> list[Candle]:
        ensure_trp_on_path()
        trp = load_trp()
        return trp["aggregate"](one_m, self.timeframe, strict_complete_buckets=True)

    def _recompute_bucket(self, bucket_open: int) -> None:
        need = self._need_1m_per_5m
        step = 60
        keys = [bucket_open + i * step for i in range(need)]
        group = [self._1m_by_unix[u] for u in keys if u in self._1m_by_unix]
        if len(group) != need:
            self._5m_by_open_unix.pop(bucket_open, None)
            return
        t0 = time.perf_counter()
        out = self._aggregate_trp(group)
        self.stats.incremental_aggregation_seconds += time.perf_counter() - t0
        self.stats.incremental_bucket_recomputations += 1
        if out:
            self._5m_by_open_unix[bucket_open] = out[0]
        else:
            self._5m_by_open_unix.pop(bucket_open, None)

    def _affected_bucket_opens(self, one_m_unix_list: Sequence[int]) -> set[int]:
        affected: set[int] = set()
        for u in one_m_unix_list:
            bo = self._bucket_open_unix(u)
            affected.add(bo)
            affected.add(bo - self._tf_sec)
        return affected

    def _incremental_update_5m(self, new_1m_unix: Sequence[int]) -> None:
        if not new_1m_unix:
            return
        if not self._5m_by_open_unix and self._1m_by_unix:
            self._rebuild_all_5m()
            return
        for bo in sorted(self._affected_bucket_opens(new_1m_unix)):
            self._recompute_bucket(bo)

    def _maybe_rebuild_incremental_5m(self, src_start_unix: int, to_unix: int) -> None:
        """Out-of-order closes or expanded left edge require a full 5m rebuild from the 1m buffer."""
        if not self.use_incremental_5m:
            return
        if self._last_to_unix is not None and int(to_unix) < int(self._last_to_unix):
            self._rebuild_all_5m()
        elif self._last_src_start_unix is not None and int(src_start_unix) < int(
            self._last_src_start_unix
        ) - 60:
            self._rebuild_all_5m()
        self._last_to_unix = int(to_unix)
        self._last_src_start_unix = int(src_start_unix)

    def _rebuild_all_5m(self) -> None:
        one_m = self._sorted_1m()
        if not one_m:
            self._5m_by_open_unix.clear()
            return
        t0 = time.perf_counter()
        raw = self._aggregate_trp(one_m)
        self.stats.full_aggregation_seconds += time.perf_counter() - t0
        self.stats.full_5m_rebuilds += 1
        self._5m_by_open_unix = {int(c.unix_seconds): c for c in raw}

    def _fetch_1m_range(self, fetch_lo: int, fetch_hi: int) -> None:
        if fetch_lo > fetch_hi:
            return
        self.stats.ch_fetch_calls += 1
        start_dt = datetime.fromtimestamp(fetch_lo, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(fetch_hi, tz=timezone.utc)
        t0 = time.perf_counter()
        rows = self._source.get_1m_candles(
            self.symbol,
            start=start_dt,
            end=end_dt,
            newest_first_limit=False,
        )
        self.stats.ch_fetch_seconds += time.perf_counter() - t0
        if not rows:
            raise CandleDataGapError(
                f"no 1m rows from CH for {self.symbol} [{fetch_lo}, {fetch_hi}]"
            )
        new_u = self._merge_1m(rows)
        if self._1m_by_unix:
            self._min_unix = min(self._1m_by_unix)
            self._max_unix = max(self._1m_by_unix)
        if self.use_incremental_5m:
            self._incremental_update_5m(new_u)

    def _ensure_1m(self, src_start_unix: int, end_unix: int) -> None:
        need_lo = int(src_start_unix)
        need_hi = int(end_unix)
        before = self.stats.ch_fetch_calls
        if self._min_unix is None or self._max_unix is None:
            self._fetch_1m_range(need_lo, need_hi)
        else:
            if need_lo < self._min_unix:
                self._fetch_1m_range(need_lo, self._min_unix - 60)
            if need_hi > self._max_unix:
                self._fetch_1m_range(self._max_unix + 60, need_hi)
        if self.stats.ch_fetch_calls == before:
            self.stats.slices_without_ch_fetch += 1
        if self._min_unix is None or need_lo < self._min_unix or need_hi > self._max_unix:
            raise CandleDataGapError(
                f"1m buffer does not cover [{need_lo}, {need_hi}] after fetch"
            )
        self._trim_left(keep_from_unix=need_lo)

    def _trim_left(self, *, keep_from_unix: int) -> None:
        removed: list[int] = []
        while len(self._1m_by_unix) > self.max_1m_bars:
            oldest = min(self._1m_by_unix)
            if oldest >= keep_from_unix:
                break
            removed.append(oldest)
            del self._1m_by_unix[oldest]
        pad = self._need_1m_per_5m * 60
        cutoff_5m = int(keep_from_unix) - pad - self._tf_sec
        for k in list(self._5m_by_open_unix):
            if k < cutoff_5m:
                del self._5m_by_open_unix[k]
        if self.use_incremental_5m and removed:
            self._incremental_update_5m(removed)
        if self._1m_by_unix:
            self._min_unix = min(self._1m_by_unix)
            self._max_unix = max(self._1m_by_unix)
        else:
            self._min_unix = self._max_unix = None
            self._5m_by_open_unix.clear()

    def _slice_1m(self, src_start_unix: int, end_unix: int) -> list[Candle]:
        lo, hi = int(src_start_unix), int(end_unix)
        out = [
            self._1m_by_unix[u]
            for u in sorted(self._1m_by_unix)
            if lo <= u <= hi
        ]
        if not out:
            raise CandleDataGapError(f"empty 1m slice [{lo}, {hi}]")
        return out

    def _aggregate_5m_full_path(
        self, one_m: list[Candle], agg_lo: int, agg_hi: int
    ) -> list[Candle]:
        t0 = time.perf_counter()
        raw = self._aggregate_trp(one_m)
        self.stats.full_aggregation_seconds += time.perf_counter() - t0
        self.stats.full_5m_reaggregations += 1
        filtered = [c for c in raw if int(c.unix_seconds) >= agg_lo]
        filtered = [c for c in filtered if int(c.unix_seconds) <= agg_hi]
        return filtered

    def _slice_5m_materialized(self, agg_lo: int, agg_hi: int) -> list[Candle]:
        t0 = time.perf_counter()
        out = [
            self._5m_by_open_unix[u]
            for u in sorted(self._5m_by_open_unix)
            if agg_lo <= u <= agg_hi
        ]
        self.stats.window_slice_seconds += time.perf_counter() - t0
        return out

    def check_1m_gap_free(self, src_start_unix: int, end_unix: int) -> None:
        """Fail closed if expected consecutive 1m opens are missing in buffer."""
        lo, hi = int(src_start_unix), int(end_unix)
        expected = (hi - lo) // 60 + 1
        present = sum(1 for u in range(lo, hi + 1, 60) if u in self._1m_by_unix)
        if present != expected:
            raise CandleDataGapError(
                f"1m gap in [{lo},{hi}]: expected {expected} minutes, have {present}"
            )

    def verify_materialized_5m_matches_full(
        self,
        src_start_unix: int,
        load_start: int,
        to_unix: int,
        decision_close: datetime,
    ) -> bool:
        """Debug/parity: incremental buffer vs full TRP on the same 1m slice."""
        from indicators.live_micro_lld_v2.as_of import filter_candles_closed_at_or_before

        to_dt = decision_close.astimezone(timezone.utc)
        one_m = self._slice_1m(src_start_unix, to_unix)
        ref = filter_candles_closed_at_or_before(
            self._aggregate_5m_full_path(one_m, load_start, to_unix),
            to_dt,
            self.timeframe,
        )
        got = filter_candles_closed_at_or_before(
            self._slice_5m_materialized(load_start, to_unix),
            to_dt,
            self.timeframe,
        )
        return candles_equal(got, ref)

    def load_for_window(
        self,
        history_from_close: datetime,
        decision_close: datetime,
        pool_detail: int,
        *,
        candle_limit: int = 50_000,
        verify_incremental_5m: bool = False,
    ) -> tuple[list[Candle], dict[str, Any]]:
        """
        Same contract as batch._load_candles_for_window (without service packed cache).
        """
        from indicators.live_micro_lld_v2.as_of import filter_candles_closed_at_or_before

        self.stats.slice_calls += 1
        pd = int(pool_detail)
        cfg = build_live_micro_lld_v2_config(pd)
        warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
        tf_sec = _timeframe_seconds(self.timeframe)

        from_dt = history_from_close.astimezone(timezone.utc)
        to_dt = decision_close.astimezone(timezone.utc)
        if from_dt > to_dt:
            raise ValueError("from_close must be <= to_close")

        from_unix = int(from_dt.timestamp())
        to_unix = int(to_dt.timestamp())
        load_start = max(0, from_unix - warmup_bars * tf_sec)

        ensure_trp_on_path()
        trp = load_trp()
        need = int(trp["expected_source_bars"](SOURCE_TF, self.timeframe))
        src_start_unix = int(load_start) - need * 60

        self._ensure_1m(src_start_unix, to_unix)
        self._maybe_rebuild_incremental_5m(src_start_unix, to_unix)
        if self.use_incremental_5m:
            raw = self._slice_5m_materialized(load_start, to_unix)
            if verify_incremental_5m and not self.verify_materialized_5m_matches_full(
                src_start_unix, load_start, to_unix, to_dt
            ):
                raise CandleDataGapError(
                    "incremental 5m materialization diverged from full TRP aggregate"
                )
        else:
            one_m = self._slice_1m(src_start_unix, to_unix)
            raw = self._aggregate_5m_full_path(one_m, load_start, to_unix)

        candles = filter_candles_closed_at_or_before(raw, to_dt, self.timeframe)
        if len(candles) > candle_limit:
            raise ValueError("candle_limit exceeded")

        meta = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pool_detail": pd,
            "config": cfg,
            "candle_load_start": datetime.fromtimestamp(load_start, tz=timezone.utc),
            "from_close": from_dt,
            "to_close": to_dt,
            "warmup_bars": warmup_bars,
            "packed": {
                "cache": "canonical_session_incremental"
                if self.use_incremental_5m
                else "canonical_session",
            },
        }
        return candles, meta


def candles_equal(a: Sequence[Candle], b: Sequence[Candle]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if int(x.unix_seconds) != int(y.unix_seconds):
            return False
        if str(x.symbol).upper() != str(y.symbol).upper():
            return False
        if x.timeframe != y.timeframe:
            return False
        for attr in ("open", "high", "low", "close", "volume"):
            if float(getattr(x, attr)) != float(getattr(y, attr)):
                return False
    return True

"""Aggregate 15m/30m candles from stored 5m using scanner semantics.

Default mode ``fill-missing`` never overwrites ``freqtrade_direct`` history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash
from research.regime_scanner.mysql_candle_store.schema import SOURCE_AGGREGATED_FROM_5M
from research.regime_scanner.mysql_candle_store.store_memory import CandleStore, UpsertStats
from research.regime_scanner.timeframes import (
    BARS_PER_AGGREGATE,
    aggregate_candles,
    ensure_utc_timestamp,
    timeframe_timedelta,
)

AggregateMode = Literal["fill-missing", "validate-only"]


@dataclass
class AggregateReport:
    exchange: str
    symbol: str
    timeframes: list[str]
    mode: str = "fill-missing"
    dry_run: bool = False
    source_5m_rows: int = 0
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_5m_for_aggregation(
    store: CandleStore,
    *,
    exchange: str,
    symbol: str,
) -> pd.DataFrame:
    frame = store.fetch_candles(
        exchange=exchange,
        symbol=symbol,
        timeframe="5m",
        closed_only=True,
    )
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    out = frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out.reset_index(drop=True)


def aggregate_htf_from_store(
    store: CandleStore,
    *,
    exchange: str,
    symbol: str,
    timeframes: list[str] | tuple[str, ...] = ("15m", "30m"),
    mode: AggregateMode = "fill-missing",
    dry_run: bool = False,
    batch_size: int = 2000,
    source_hash: str | None = None,
) -> AggregateReport:
    """Build HTF candles exclusively via ``timeframes.aggregate_candles``."""
    report = AggregateReport(
        exchange=exchange,
        symbol=symbol,
        timeframes=[str(t) for t in timeframes],
        mode=mode,
        dry_run=dry_run,
    )
    if mode not in ("fill-missing", "validate-only"):
        report.errors.append(f"unsupported aggregate mode: {mode}")
        return report
    forbidden = [t for t in report.timeframes if t not in ("15m", "30m")]
    if forbidden:
        report.errors.append(f"unsupported aggregate timeframes: {forbidden}")
        return report

    base = _load_5m_for_aggregation(store, exchange=exchange, symbol=symbol)
    report.source_5m_rows = int(len(base))
    if base.empty:
        report.errors.append("no closed 5m candles available for aggregation")
        return report

    last_open = ensure_utc_timestamp(base["timestamp"].iloc[-1])
    decision_time = last_open + timeframe_timedelta("5m")
    batch_hash = source_hash or f"agg_from_5m:{exchange}:{symbol}:{last_open.isoformat()}"

    for tf in report.timeframes:
        expected = int(BARS_PER_AGGREGATE[tf])
        agg = aggregate_candles(base, tf, decision_time)
        duration = timeframe_timedelta(tf)
        existing = store.fetch_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe=tf,
            closed_only=True,
        )
        if existing.empty:
            existing_opens: set[pd.Timestamp] = set()
        else:
            existing_opens = {
                ensure_utc_timestamp(ts) for ts in pd.to_datetime(existing["timestamp"], utc=True)
            }

        tf_report: dict[str, Any] = {
            "rows_computed": int(len(agg)),
            "already_present": 0,
            "missing_candidates": 0,
            "expected_5m_per_bucket": expected,
            "start": None,
            "end": None,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped_protected": 0,
            "conflicts": 0,
            "export_hash": None,
        }
        if agg.empty:
            report.results[tf] = tf_report
            continue
        tf_report["start"] = ensure_utc_timestamp(agg["timestamp"].iloc[0]).isoformat()
        tf_report["end"] = ensure_utc_timestamp(agg["timestamp"].iloc[-1]).isoformat()
        tf_report["export_hash"] = candles_export_hash(agg)

        rows: list[dict[str, Any]] = []
        already = 0
        for _, row in agg.iterrows():
            open_time = ensure_utc_timestamp(row["timestamp"])
            if open_time in existing_opens:
                already += 1
                if mode == "fill-missing":
                    continue
            rows.append(
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": tf,
                    "open_time": open_time,
                    "close_time": open_time + duration,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "is_closed": True,
                    "source": SOURCE_AGGREGATED_FROM_5M,
                    "source_timeframe": "5m",
                    "source_hash": batch_hash,
                }
            )
        tf_report["already_present"] = already
        tf_report["missing_candidates"] = len(rows)

        if mode == "validate-only" or dry_run:
            # No writes; policy-protected existing directs are counted as already_present.
            report.results[tf] = tf_report
            continue

        stats = UpsertStats()
        for i in range(0, len(rows), max(1, int(batch_size))):
            stats.merge(store.upsert_candles(rows[i : i + batch_size]))
        tf_report["inserted"] = stats.inserted
        tf_report["updated"] = stats.updated
        tf_report["unchanged"] = stats.unchanged
        tf_report["skipped_protected"] = stats.skipped_protected
        tf_report["conflicts"] = stats.conflicts
        if stats.conflicts:
            report.errors.append(f"{tf}: aggregation conflicts={stats.conflicts}")
        report.results[tf] = tf_report
    return report

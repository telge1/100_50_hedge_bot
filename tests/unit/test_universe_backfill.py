"""Unit tests for universe selection, checkpoints, chunks, gap classification."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from signal_generator.bybit.checkpoint import (
    CheckpointStore,
    SymbolCheckpoint,
    load_checkpoint,
    resume_start_for,
    save_checkpoint,
    should_skip_symbol,
)
from signal_generator.bybit.chunks import iter_time_chunks
from signal_generator.bybit.history import chunk_batches, expected_candle_count
from signal_generator.bybit.quality import (
    aggregate_quality_counts,
    classify_coverage_window,
    classify_final_status,
    find_gaps,
)
from signal_generator.bybit.universe import (
    Universe,
    UniverseSymbol,
    filter_active_linear_usdt_perpetuals,
    load_universe,
    rank_by_turnover24h,
    save_universe,
    select_universe,
)


def test_filter_only_active_linear_usdt_perpetuals():
    instruments = [
        {
            "symbol": "BTCUSDT",
            "settleCoin": "USDT",
            "status": "Trading",
            "contractType": "LinearPerpetual",
            "deliveryTime": "0",
        },
        {
            "symbol": "ETHUSDC",
            "settleCoin": "USDC",
            "status": "Trading",
            "contractType": "LinearPerpetual",
            "deliveryTime": "0",
        },
        {
            "symbol": "BTC-26JUN26",
            "settleCoin": "USDT",
            "status": "Trading",
            "contractType": "LinearFutures",
            "deliveryTime": "1780000000000",
        },
        {
            "symbol": "OLDUSDT",
            "settleCoin": "USDT",
            "status": "Closed",
            "contractType": "LinearPerpetual",
            "deliveryTime": "0",
        },
    ]
    out = filter_active_linear_usdt_perpetuals(instruments)
    assert [x["symbol"] for x in out] == ["BTCUSDT"]


def test_ranking_deterministic_turnover_then_symbol():
    instruments = [
        {"symbol": "AAAUSDT", "contractType": "LinearPerpetual", "status": "Trading", "settleCoin": "USDT", "launchTime": "0"},
        {"symbol": "BBBUSDT", "contractType": "LinearPerpetual", "status": "Trading", "settleCoin": "USDT", "launchTime": "0"},
        {"symbol": "CCCUSDT", "contractType": "LinearPerpetual", "status": "Trading", "settleCoin": "USDT", "launchTime": "0"},
    ]
    tickers = [
        {"symbol": "AAAUSDT", "turnover24h": "100", "volume24h": "1"},
        {"symbol": "BBBUSDT", "turnover24h": "100", "volume24h": "2"},
        {"symbol": "CCCUSDT", "turnover24h": "200", "volume24h": "3"},
    ]
    ranked = rank_by_turnover24h(instruments, tickers)
    assert [r.symbol for r in ranked] == ["CCCUSDT", "AAAUSDT", "BBBUSDT"]
    assert ranked[0].rank == 1


def test_select_universe_forces_must_include():
    ranked = [
        UniverseSymbol(f"S{i}USDT", i, str(1000 - i), "1", None, "LinearPerpetual", "Trading", "USDT")
        for i in range(1, 6)
    ]
    # Insert low-turnover must symbol
    ranked.append(
        UniverseSymbol("DOGEUSDT", 99, "1", "1", None, "LinearPerpetual", "Trading", "USDT")
    )
    ranked.sort(key=lambda d: d.rank)
    selected = select_universe(ranked, target_size=3, must_include=("DOGEUSDT",))
    assert "DOGEUSDT" in {d.symbol for d in selected}
    assert len(selected) == 3


def test_universe_json_roundtrip(tmp_path: Path):
    uni = Universe(
        generated_at="2026-08-10T00:00:00+00:00",
        source="test",
        selection_method="test_method",
        target_size=2,
        symbols=["BTCUSDT", "ETHUSDT"],
        details=[
            UniverseSymbol("BTCUSDT", 1, "10", "1", None, "LinearPerpetual", "Trading", "USDT"),
            UniverseSymbol("ETHUSDT", 2, "9", "1", None, "LinearPerpetual", "Trading", "USDT"),
        ],
    )
    path = tmp_path / "universe.json"
    save_universe(uni, path)
    loaded = load_universe(path)
    assert loaded.symbols == uni.symbols
    assert loaded.selection_method == uni.selection_method
    assert loaded.details[0].turnover24h == "10"


def test_chunk_boundaries_half_open():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 10, tzinfo=timezone.utc)
    chunks = iter_time_chunks(start, end, chunk_days=7)
    assert chunks == [
        (start, datetime(2026, 1, 8, tzinfo=timezone.utc)),
        (datetime(2026, 1, 8, tzinfo=timezone.utc), end),
    ]
    # Contiguous, no overlap
    assert chunks[0][1] == chunks[1][0]


def test_resume_skips_complete():
    cp = SymbolCheckpoint(
        symbol="BTCUSDT",
        requested_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        requested_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        status="COMPLETE",
    )
    assert should_skip_symbol(cp, resume=True, retry_failed=False) is True
    assert should_skip_symbol(cp, resume=False, retry_failed=False) is False


def test_resume_partial_continues_from_cursor():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mid = datetime(2026, 3, 1, tzinfo=timezone.utc)
    cp = SymbolCheckpoint(
        symbol="ETHUSDT",
        requested_start=start,
        requested_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        last_completed_timestamp=mid,
        status="PARTIAL",
    )
    assert should_skip_symbol(cp, resume=True, retry_failed=False) is False
    assert resume_start_for(cp) == mid


def test_checkpoint_recovery_roundtrip(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    store = CheckpointStore(
        version=1,
        requested_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 10, tzinfo=timezone.utc),
        chunk_days=7,
        symbols={
            "BTCUSDT": SymbolCheckpoint(
                symbol="BTCUSDT",
                requested_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                requested_end=datetime(2026, 8, 10, tzinfo=timezone.utc),
                last_completed_timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
                status="PARTIAL",
                rows_inserted=1000,
            )
        },
    )
    save_checkpoint(store, path)
    loaded = load_checkpoint(path)
    assert loaded is not None
    assert loaded.symbols["BTCUSDT"].status == "PARTIAL"
    assert loaded.symbols["BTCUSDT"].rows_inserted == 1000
    assert resume_start_for(loaded.symbols["BTCUSDT"]) == datetime(
        2026, 2, 1, tzinfo=timezone.utc
    )


def test_pre_listing_vs_internal_gap():
    requested_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    requested_end = datetime(2026, 4, 1, tzinfo=timezone.utc)
    effective = datetime(2026, 3, 15, tzinfo=timezone.utc)
    max_ot = datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc)
    pre, trailing = classify_coverage_window(
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=effective,
        max_open_time=max_ot,
    )
    assert pre == int((effective - requested_start).total_seconds() // 60)
    assert trailing == []  # continuous through end-1m

    # Internal gap between candles
    t0 = effective
    t2 = effective + timedelta(minutes=2)
    candles = [
        {"open_time": t0},
        {"open_time": t2},
    ]
    gaps = find_gaps(candles, symbol="X")
    assert len(gaps) == 1
    assert gaps[0].missing_candle_count == 1


def test_failed_symbol_retry_flag():
    cp = SymbolCheckpoint(
        symbol="X",
        requested_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        requested_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
        status="FAILED",
        last_error="boom",
    )
    assert should_skip_symbol(cp, resume=True, retry_failed=False) is True
    assert should_skip_symbol(cp, resume=True, retry_failed=True) is False


def test_idempotent_rerun_chunk_batches():
    # Re-chunking identical input yields stable batches (idempotent insert partitioning)
    items = list(range(5))
    assert chunk_batches(items, 2) == [[0, 1], [2, 3], [4]]  # type: ignore[arg-type]
    assert chunk_batches(items, 2) == chunk_batches(items, 2)  # type: ignore[arg-type]


def test_quality_aggregation_and_status():
    assert classify_final_status(unique_final=0, gap_count=0, ohlc_error_count=0, close_time_errors=0) == "NO_HISTORY"
    assert classify_final_status(unique_final=10, gap_count=0, ohlc_error_count=0, close_time_errors=0) == "COMPLETE_CLEAN"
    assert classify_final_status(unique_final=10, gap_count=2, ohlc_error_count=0, close_time_errors=0) == "COMPLETE_WITH_INTERNAL_GAPS"
    assert classify_final_status(unique_final=10, gap_count=0, ohlc_error_count=0, close_time_errors=0, checkpoint_status="FAILED") == "FAILED"

    from signal_generator.bybit.quality import SymbolQualityReport

    reports = [
        SymbolQualityReport("A", 1, 1, 1, None, None, 0, 0, 0, final_status="COMPLETE_CLEAN"),
        SymbolQualityReport("B", 1, 1, 1, None, None, 0, 2, 120, final_status="COMPLETE_WITH_INTERNAL_GAPS"),
        SymbolQualityReport("C", 1, 0, 0, None, None, 0, 0, 0, final_status="NO_HISTORY"),
    ]
    counts = aggregate_quality_counts(reports)
    assert counts["COMPLETE_CLEAN"] == 1
    assert counts["COMPLETE_WITH_INTERNAL_GAPS"] == 1
    assert counts["NO_HISTORY"] == 1


def test_expected_count_window():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert expected_candle_count(start, end) == 1440

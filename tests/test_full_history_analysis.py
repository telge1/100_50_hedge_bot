"""Tests for full-history Phase 0/1 orchestration helpers (mostly offline)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.full_history_analysis import (
    OUTPUT_FILES,
    PHASE2_OUTPUT_FILES,
    clip_range,
    decide_phase01,
    default_output_dir,
    parse_args,
    render_report,
    write_csv_headered,
)
from orderbook_analyse.market_bars import MarketContextError, parse_bar_timeframes, phase3_output_files
from orderbook_analyse.replay_segmentation import (
    OrderbookMessage,
    discover_replay_segments,
)
from orderbook_analyse.segment_replay import decide_phase2

TS0 = datetime(2026, 7, 26, 22, 0, 0, tzinfo=timezone.utc)


def test_clip_range() -> None:
    avail_s = TS0
    avail_e = TS0 + timedelta(hours=5)
    lo, hi = clip_range(avail_s, avail_e, start=TS0 + timedelta(hours=1), end=None)
    assert lo == TS0 + timedelta(hours=1)
    assert hi == avail_e
    lo2, hi2 = clip_range(avail_s, avail_e, start=TS0 - timedelta(hours=1), end=avail_e)
    assert lo2 == avail_s
    empty_lo, empty_hi = clip_range(
        avail_s, avail_e, start=avail_e + timedelta(hours=1), end=avail_e + timedelta(hours=2)
    )
    assert empty_lo is None and empty_hi is None


def test_decide_phase01() -> None:
    assert (
        decide_phase01(
            has_any_data=False,
            has_orderbook=False,
            segment_count=0,
            replayable_count=0,
            gap_count=0,
            integrity_ok=True,
        )
        == "FULL_HISTORY_ANALYSIS_FAILED"
    )
    assert (
        decide_phase01(
            has_any_data=True,
            has_orderbook=False,
            segment_count=0,
            replayable_count=0,
            gap_count=0,
            integrity_ok=True,
        )
        == "FULL_HISTORY_ANALYSIS_PARTIAL"
    )
    assert (
        decide_phase01(
            has_any_data=True,
            has_orderbook=True,
            segment_count=2,
            replayable_count=2,
            gap_count=0,
            integrity_ok=True,
        )
        == "FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE"
    )
    assert (
        decide_phase01(
            has_any_data=True,
            has_orderbook=True,
            segment_count=3,
            replayable_count=2,
            gap_count=1,
            integrity_ok=True,
        )
        == "FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE_WITH_GAPS"
    )


def test_default_output_path_contains_symbol() -> None:
    p = default_output_dir("VANRYUSDT")
    assert "VANRYUSDT" in str(p)
    assert "full_history_" in str(p)


def test_write_csv_headered_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    write_csv_headered(path, [], headers=["a", "b"])
    text = path.read_text(encoding="utf-8")
    assert text.startswith("a,b")


def test_offline_segmentation_outputs_schema(tmp_path: Path) -> None:
    msgs = []
    for i in range(0, 10):
        msgs.append(
            OrderbookMessage(
                exchange_ts=TS0 + timedelta(seconds=60 * i),
                update_id=1000 + i,
                cross_sequence=i + 1,
                message_type="snapshot" if i == 0 else "delta",
                bid_level_count=200,
                ask_level_count=200,
                total_level_count=400,
            )
        )
    # second segment after gap
    for i in range(0, 10):
        msgs.append(
            OrderbookMessage(
                exchange_ts=TS0 + timedelta(seconds=60 * (30 + i)),
                update_id=2000 + i,
                cross_sequence=100 + i,
                message_type="snapshot" if i == 0 else "delta",
                bid_level_count=200,
                ask_level_count=200,
                total_level_count=400,
            )
        )
    seg = discover_replay_segments(
        msgs, symbol="TESTUSDT", segment_minutes_min=5, min_snapshot_levels_per_side=150
    )
    assert seg.complete_snapshot_count >= 2
    assert len(seg.segments) >= 2
    assert len(seg.gaps) >= 1
    report = render_report(
        decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE_WITH_GAPS",
        symbol="TESTUSDT",
        analysis_start=TS0,
        analysis_end=TS0 + timedelta(hours=1),
        inventory=[
            {
                "table_name": "orderbook_deltas",
                "timestamp_column": "exchange_ts",
                "row_count": 1,
                "first_ts": TS0.isoformat(),
                "last_ts": TS0.isoformat(),
                "distinct_message_count": 20,
                "snapshot_message_count": 2,
                "delta_message_count": 18,
            }
        ],
        seg=seg,
        quality=[{"metric": "coverage_pct", "value": 50.0, "status": "ok", "details": ""}],
        health={"RECONNECT": 1},
        coverage_pct=50.0,
        limitations=["phase01"],
    )
    assert "Decision" in report or "Entscheidung" in report
    assert "TESTUSDT" in report


def test_required_output_names() -> None:
    assert "replay_segments.csv" in OUTPUT_FILES
    assert "replay_gaps.csv" in OUTPUT_FILES
    assert "integrity.json" in OUTPUT_FILES
    assert "segment_replay_results.csv" in PHASE2_OUTPUT_FILES
    assert "segment_replay_errors.csv" in PHASE2_OUTPUT_FILES


def test_parse_args_phase01_default_no_replay() -> None:
    args = parse_args(["--symbol", "VANRYUSDT"])
    assert args.run_segment_replay is False
    assert args.run_market_context is False
    assert args.run_wall_history is False
    assert getattr(args, "run_pattern_candidates", False) is False
    assert args.warmup_seconds == 300
    assert args.replay_sample_interval == 60
    assert args.bar_timeframes == "1m,5m"
    assert args.wall_sample_interval == 60
    assert args.wall_resolutions == "5,10,20,50"


def test_parse_args_phase5_flags() -> None:
    args = parse_args(
        [
            "--symbol",
            "APTUSDT",
            "--run-pattern-candidates",
            "--pattern-timeframe",
            "1m",
            "--pattern-lookback-bars",
            "5",
            "--pattern-delta-ratio-threshold",
            "0.25",
            "--pattern-wall-imbalance-threshold",
            "0.4",
        ]
    )
    assert args.run_pattern_candidates is True
    assert args.pattern_timeframe == "1m"
    assert args.pattern_lookback_bars == 5
    assert args.pattern_delta_ratio_threshold == 0.25
    assert args.pattern_wall_imbalance_threshold == 0.4


def test_pattern_candidates_auto_enable_deps_semantics() -> None:
    """Mirror CLI dependency auto-enable used inside run_full_history_phase01."""
    from orderbook_analyse.full_history_analysis import FullHistoryParams

    params = FullHistoryParams(symbol="APTUSDT", run_pattern_candidates=True)
    assert params.run_wall_history is False
    assert params.run_market_context is False
    # same mutation order as pipeline
    if params.run_pattern_candidates:
        if not params.run_wall_history:
            params.run_wall_history = True
        if not params.run_market_context:
            params.run_market_context = True
    if params.run_wall_history and not params.run_segment_replay:
        params.run_segment_replay = True
    assert params.run_wall_history is True
    assert params.run_market_context is True
    assert params.run_segment_replay is True



def test_parse_args_phase2_flags() -> None:
    args = parse_args(
        [
            "--symbol",
            "VANRYUSDT",
            "--run-segment-replay",
            "--warmup-seconds",
            "120",
            "--replay-sample-interval",
            "30",
        ]
    )
    assert args.run_segment_replay is True
    assert args.warmup_seconds == 120
    assert args.replay_sample_interval == 30


def test_decide_phase2_wired() -> None:
    assert (
        decide_phase2(
            phase01_decision="FULL_HISTORY_SEGMENT_DISCOVERY_COMPLETE_WITH_GAPS",
            gap_count=1,
            stats={"segments_replayable": 1, "segments_replay_ok": 1, "segments_replay_failed": 0},
        )
        == "FULL_HISTORY_SEGMENT_REPLAY_COMPLETE_WITH_GAPS"
    )


def test_parse_args_phase3_flags() -> None:
    args = parse_args(
        [
            "--symbol",
            "VANRYUSDT",
            "--run-market-context",
            "--bar-timeframes",
            "1m,5m",
            "--tiny-liquidation-notional",
            "2.5",
        ]
    )
    assert args.run_market_context is True
    assert args.bar_timeframes == "1m,5m"
    assert args.tiny_liquidation_notional == 2.5


def test_parse_bar_timeframes_invalid_raises() -> None:
    with pytest.raises(MarketContextError):
        parse_bar_timeframes("15m")


def test_parse_args_phase4_flags() -> None:
    args = parse_args(
        [
            "--symbol",
            "VANRYUSDT",
            "--run-wall-history",
            "--wall-sample-interval",
            "30",
            "--wall-warmup-seconds",
            "120",
            "--wall-resolutions",
            "10,20",
            "--wall-output-mode",
            "candidates",
        ]
    )
    assert args.run_wall_history is True
    assert args.wall_sample_interval == 30
    assert args.wall_warmup_seconds == 120
    assert args.wall_resolutions == "10,20"


def test_phase3_output_files_list() -> None:
    files = phase3_output_files(["1m", "5m"])
    assert "price_summary.csv" in files
    assert "price_bars_1m.csv" in files
    assert "analysis_timeline_5m.csv" in files


def test_render_report_includes_phase3_section() -> None:
    report = render_report(
        decision="FULL_HISTORY_MARKET_CONTEXT_COMPLETE",
        symbol="TEST",
        analysis_start=TS0,
        analysis_end=TS0 + timedelta(hours=1),
        inventory=[],
        seg=discover_replay_segments([], symbol="TEST"),
        quality=[],
        health={},
        coverage_pct=0.0,
        limitations=["phase3"],
        market_stats={
            "market_context_ok": True,
            "bar_timeframes": ["1m", "5m"],
            "trade_total_notional": "100",
            "trade_buy_notional": "60",
            "trade_sell_notional": "40",
            "trade_delta_notional": "20",
            "oi_start": "1000",
            "oi_end": "1100",
            "oi_change_pct": 10.0,
            "liquidation_event_count": 0,
            "liquidation_total_notional": "0",
            "tiny_liquidation_count": 0,
            "tiny_liquidation_notional": "1.0",
        },
        market_coverage={
            "ticker_samples": {"first_ts": TS0.isoformat(), "last_ts": TS0.isoformat(), "row_count": 1},
        },
        price_summary={"start_price": "1", "end_price": "1.1", "net_change_pct": 10.0, "vwap": 1.05},
        quadrant_summary={"PRICE_UP_OI_UP": 3},
    )
    assert "Phase 3" in report
    assert "No walls" in report or "no walls" in report.lower()

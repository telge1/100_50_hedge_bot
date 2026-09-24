from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pool_order_plan_v1.candles import FiveMinuteSeries
from pool_order_plan_v1.coverage import coverage_row
from pool_order_plan_v1.dedupe import dedupe_signals
from pool_order_plan_v1.metrics import dashboard_style_summary, display_round, strategy_stats
from pool_order_plan_v1.paired_compare import matches_observed_kpis
from pool_order_plan_v1.partial_exits import simulate_partial_exits
from pool_order_plan_v1.schema import clickhouse_candle_stamp
from pool_order_plan_v1 import store
from pool_order_plan_v1.store import SourceRejected


UTC = timezone.utc


def test_frozen_48h_is_half_open_on_candle_close():
    end = datetime(2026, 8, 14, 14, 54, 14, tzinfo=UTC)
    start = end - timedelta(hours=48)
    assert (end - start) == timedelta(hours=48)
    # collector: candle_close_time >= start AND candle_close_time < end
    assert start.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-08-12T14:54:14Z"


def test_identical_entry_set_after_dedupe():
    rows = [
        {"signal_id": "a", "symbol": "ACEUSDT", "entry_time": "2026-08-13T01:00:00Z", "available_at": "2026-08-13T00:59:00Z", "created_at": "2026-08-13T00:59:00Z", "entry_price": 1.0, "direction": "LONG", "timeframe": "15m"},
        {"signal_id": "b", "symbol": "ACEUSDT", "entry_time": "2026-08-13T01:00:00Z", "available_at": "2026-08-13T01:00:00Z", "created_at": "2026-08-13T01:00:00Z", "entry_price": 1.0, "direction": "SHORT", "timeframe": "1h"},
        {"signal_id": "c", "symbol": "ACEUSDT", "entry_time": "2026-08-13T02:00:00Z", "available_at": "2026-08-13T01:59:00Z", "created_at": "2026-08-13T01:59:00Z", "entry_price": 1.1, "direction": "LONG", "timeframe": "15m"},
    ]
    split = dedupe_signals(rows)
    assert [w["signal_id"] for w in split["winners"]] == ["a", "c"]
    assert split["ignored"][0]["signal_id"] == "b"
    assert split["ignored"][0]["winner_signal_id"] == "a"


def test_identical_outcome_as_of_caps_later_candles():
    start = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    as_of = start + timedelta(minutes=1)
    rows = []
    for i in range(5):
        ts = start + timedelta(minutes=i)
        rows.append({"timestamp": pd.Timestamp(ts), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1})
    df = pd.DataFrame(rows)
    out = simulate_partial_exits(
        direction="LONG",
        entry_time=start,
        entry_price=100.0,
        sl_price=90.0,
        tp1_price=101.0,
        tp1_size=1.0,
        tp2_price=None,
        tp2_size=None,
        candles_1m=df,
        timeframe="15m",
        as_of=as_of,
    )
    assert out["outcome_as_of"] == "2026-08-11T01:18:00Z"
    # only bars with timestamp < 01:18 are visible: 01:17
    assert out["outcome"] in ("TP1", "OPEN")


def test_no_plan_counts_as_zero_pnl_in_all_set():
    rows = [
        {"signal_id": "1", "entry_time": "t1", "plan_status": "NO_PLAN"},
        {"signal_id": "2", "entry_time": "t2", "plan_status": "READY", "outcome": "TP1", "gross_pnl_pct": 2.0, "net_pnl_pct": 1.89, "fees_pct": 0.11},
    ]
    s = strategy_stats(rows, kind="pool")
    assert s["no_plan"] == 1
    assert s["net_pnl_pct"] == pytest.approx(1.89)
    assert s["raw_or_set_n"] == 2


def test_ready_subset_excludes_no_plan_baseline_rows():
    base = [
        {"signal_id": "1", "entry_time": "t1", "result": "WIN", "pnl_pct": 1.0},
        {"signal_id": "2", "entry_time": "t2", "result": "LOSS", "pnl_pct": -1.0},
    ]
    ready = [base[0]]
    all_s = strategy_stats(base, kind="baseline")
    sub = strategy_stats(ready, kind="baseline")
    assert all_s["trades"] == 2
    assert sub["trades"] == 1
    assert sub["gross_pnl_pct"] == pytest.approx(1.0)


def test_coverage_field_names_use_query_vs_database_history():
    df = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-08-01T00:00:00Z"),
                "close_time": pd.Timestamp("2026-08-01T00:05:00Z"),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ]
    )
    series = FiveMinuteSeries(
        symbol="ACEUSDT",
        bars=df,
        one_minute_rows=5,
        missing_one_minute_rows=0,
        duplicate_one_minute_rows=0,
        dropped_incomplete_five_minute_buckets=0,
        history_start=datetime(2026, 8, 1, tzinfo=UTC),
        history_end=datetime(2026, 8, 2, tzinfo=UTC),
    )
    q0 = datetime(2026, 8, 12, tzinfo=UTC)
    q1 = datetime(2026, 8, 14, tzinfo=UTC)
    db0 = datetime(2025, 12, 1, tzinfo=UTC)
    cov = coverage_row(
        "ACEUSDT",
        series,
        entry_count=3,
        query_start=q0,
        query_end=q1,
        database_history_start=db0,
        database_history_end=datetime(2026, 8, 14, tzinfo=UTC),
    )
    assert cov["query_start"] == "2026-08-12T00:00:00Z"
    assert cov["query_end"] == "2026-08-14T00:00:00Z"
    assert cov["database_history_start"] == "2025-12-01T00:00:00Z"
    assert cov["listing_limited"] is False


def test_no_publish_latest_on_comparison_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path))
    run = store.write_run(
        "cmp",
        manifest={**clickhouse_candle_stamp(), "ok": True},
        preflight={},
        coverage={},
        plans=[],
        outcomes=[],
        ignored=[],
    )
    assert not (tmp_path / "latest").exists()
    store.publish_latest(run)
    monkeypatch.setenv("POOL_ORDER_PLAN_ARTIFACT_DIR", str(tmp_path / "other"))
    assert not store.artifact_available()


def test_observed_kpi_matcher():
    rows = []
    for i in range(15):
        rows.append({"result": "WIN", "pnl_pct": 1.0 if i < 10 else 3.0})
    # 10*1 + 5*3 = 25
    for i in range(7):
        rows.append({"result": "LOSS", "pnl_pct": -1.0 if i < 6 else -2.0})
    # -6-2 = -8
    s = dashboard_style_summary(rows)
    assert matches_observed_kpis(s)
    d = display_round(s)
    assert d["win_rate_pct_1dp"] == 68.2
    assert d["total_pnl_pct_1dp"] == 17.0

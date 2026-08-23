"""Tests for 1h/4h cluster-sweep outcome analysis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.cluster_sweep_research.outcome_analysis_1h_4h import (
    analyze_events_outcomes,
    compute_first_hit_matrix,
    compute_horizon_metrics,
    eligible_events,
    find_conservative_entry,
    flag_overlap_fields,
    group_episodes,
    slice_future_1m,
)
from orderbook_analyse.cluster_sweep_research.ema_features import attach_emas


def _mk_1m(start: datetime, n: int, *, high_step: float = 0.0, low_step: float = 0.0) -> pd.DataFrame:
    rows = []
    ep = 1.0
    for i in range(n):
        t = start + timedelta(minutes=i)
        hi = ep * (1 + high_step * (i + 1) / n)
        lo = ep * (1 - low_step * (i + 1) / n)
        rows.append({"open_time": t.replace(tzinfo=None), "open": ep, "high": hi, "low": lo, "close": ep, "volume": 1.0})
    return pd.DataFrame(rows)


def test_long_mfe_mae_formulas():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry, 60, high_step=0.02, low_step=0.01)
    chunk, cov = slice_future_1m(df, entry, 60)
    m = compute_horizon_metrics(chunk, entry_at=entry, entry_price=1.0, direction="BULLISH", coverage_meta=cov)
    assert m["mfe_pct"] == pytest.approx(2.0, rel=1e-3)
    assert m["mae_pct"] == pytest.approx(1.0, rel=1e-3)


def test_short_mfe_mae_mirrored():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry, 60, high_step=0.01, low_step=0.02)
    chunk, cov = slice_future_1m(df, entry, 60)
    m = compute_horizon_metrics(chunk, entry_at=entry, entry_price=1.0, direction="BEARISH", coverage_meta=cov)
    assert m["mfe_pct"] == pytest.approx(2.0, rel=1e-3)
    assert m["mae_pct"] == pytest.approx(1.0, rel=1e-3)


def test_1h_boundary_excludes_hour_mark():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry, 70)
    chunk, cov = slice_future_1m(df, entry, 60)
    assert len(chunk) == 60
    assert cov["coverage"] == "COMPLETE"


def test_4h_boundary():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry, 250)
    chunk, cov = slice_future_1m(df, entry, 240)
    assert len(chunk) == 240


def test_no_data_before_entry():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry - timedelta(minutes=10), 20)
    chunk, _ = slice_future_1m(df, entry, 60)
    assert chunk["open_time"].min() >= pd.Timestamp(entry.replace(tzinfo=None))


def test_incomplete_future_coverage():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry, 30)
    chunk, cov = slice_future_1m(df, entry, 60)
    assert cov["coverage"] == "INCOMPLETE_FUTURE_COVERAGE"
    assert len(chunk) == 30


def test_same_minute_first_hit_ambiguous():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": entry.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.02,
                "low": 0.98,
                "close": 1.0,
                "volume": 1.0,
            }
        ]
    )
    fh = compute_first_hit_matrix(df, entry_price=1.0, direction="BULLISH")
    assert fh["0.10"] == "SAME_MINUTE_AMBIGUOUS"


def test_first_hit_target_first_long():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            # 0.12% favorable only — sub-0.10% adverse avoids SAME_MINUTE_AMBIGUOUS at 0.10
            {"open_time": entry.replace(tzinfo=None), "open": 1.0, "high": 1.0012, "low": 0.9995, "close": 1.0, "volume": 1},
            {"open_time": (entry + timedelta(minutes=1)).replace(tzinfo=None), "open": 1.0, "high": 1.002, "low": 0.999, "close": 1.0, "volume": 1},
        ]
    )
    fh = compute_first_hit_matrix(df, entry_price=1.0, direction="BULLISH")
    assert fh["0.10"] == "TARGET_FIRST"


def test_eligible_events_filters():
    events = [
        {"final_status": "CONFIRMED", "entry_at": "2026-08-19T05:00:00+00:00", "entry_price": 1.0},
        {"final_status": "INVALIDATED", "entry_at": "2026-08-19T05:00:00+00:00", "entry_price": 1.0},
    ]
    assert len(eligible_events(events)) == 1


def test_overlap_flags():
    rows = [
        {"event_id": "a", "entry_at": "2026-08-19T05:00:00+00:00", "cluster_id": "c1", "cluster_low": 1.0, "cluster_high": 1.1},
        {"event_id": "b", "entry_at": "2026-08-19T05:30:00+00:00", "cluster_id": "c1", "cluster_low": 1.0, "cluster_high": 1.1},
    ]
    flag_overlap_fields(rows)
    assert rows[0]["overlapping_outcome"] is True
    assert rows[0]["same_cluster_family"] is True


def test_episode_grouping():
    rows = [
        {"event_id": "a", "entry_variant": "AGGRESSIVE", "direction": "BEARISH", "entry_at": "2026-08-18T21:15:00+00:00", "cluster_id": "u1", "cluster_low": 1.0, "cluster_high": 1.01, "confirmation_type": "A"},
        {"event_id": "b", "entry_variant": "AGGRESSIVE", "direction": "BEARISH", "entry_at": "2026-08-18T23:10:00+00:00", "cluster_id": "u1", "cluster_low": 1.0, "cluster_high": 1.01, "confirmation_type": "B"},
    ]
    eps = group_episodes(rows)
    assert len(eps) == 1
    assert eps[0]["number_of_events"] == 2


def test_conservative_entry_found():
    start = datetime(2026, 8, 18, 21, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        t = start + timedelta(minutes=5 * i)
        e9 = 1.0005 + i * 0.0002
        e59 = 1.0010
        rows.append({"open_time": t.replace(tzinfo=None), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1, "ema_9": e9, "ema_20": 1.0008, "ema_59": e59})
    df = pd.DataFrame(rows)
    ev = {"confirmation_at": "2026-08-18T21:10:00+00:00", "direction": "BEARISH", "strategy_timeframe": "5m", "expire_bars": 24}
    cons = find_conservative_entry(ev, df)
    assert cons["status"] == "FOUND"


def test_deterministic_repeat():
    entry = datetime(2026, 8, 19, 5, 0, tzinfo=timezone.utc)
    df = _mk_1m(entry, 300)
    events = [
        {
            "event_id": "e1",
            "final_status": "CONFIRMED",
            "direction": "BULLISH",
            "confirmation_at": "2026-08-19T04:55:00+00:00",
            "entry_at": "2026-08-19T05:00:00+00:00",
            "entry_price": 1.0,
            "cluster_id": "c1",
            "cluster_low": 0.99,
            "cluster_high": 1.0,
            "cluster_pool_count": 3,
            "confirmation_type": "CLOSE_BEYOND_CLUSTER_EDGE",
        }
    ]
    a = analyze_events_outcomes(events, df, symbol="XRPUSDT", strategy_timeframe="5m")
    b = analyze_events_outcomes(events, df, symbol="XRPUSDT", strategy_timeframe="5m")
    assert a["events_outcomes"][0]["mfe_1h_pct"] == b["events_outcomes"][0]["mfe_1h_pct"]

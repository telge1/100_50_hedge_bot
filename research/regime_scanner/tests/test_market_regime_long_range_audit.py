"""Tests for long-range market regime audit helpers (read-only)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research.regime_scanner.market_regime import (
    MarketRegimeClassifier,
    MarketRegimeFeatures,
    default_market_regime_config,
    h4_confirm_bars,
)
from research.regime_scanner.market_regime_long_range_audit import (
    AUDIT_START,
    BarRecord,
    assess_data,
    assert_htf_closed,
    build_segments,
    build_transitions,
    filter_audit_rows,
    run_classifier_timeline,
)
from research.regime_scanner.timeframes import timeframe_timedelta


def _feat(**kwargs) -> MarketRegimeFeatures:
    base = dict(
        ema9=100.0,
        ema20=101.0,
        ema9_slope_atr=-0.02,
        ema20_slope_atr=-0.02,
        ema_sep_atr=-0.1,
        ema_sep_change_atr=-0.05,
        share_above_both=0.1,
        share_below_both=0.8,
        ema_crosses=0,
        ema_flat=False,
        net_move_atr=-1.2,
        directional_efficiency=0.5,
        progress_vs_range=0.6,
        up_close_share=0.3,
        down_close_share=0.7,
        maximum_counter_move_atr=0.4,
        close=99.0,
        atr=1.0,
    )
    base.update(kwargs)
    return MarketRegimeFeatures(**base)


def _dt(h: int, day: int = 6) -> datetime:
    return datetime(2026, 1, day, h % 24, 0, tzinfo=timezone.utc)


def test_filter_audit_rows_excludes_warmup() -> None:
    rows = [
        {"decision_time": "2026-01-05T23:30:00+00:00", "regime": "x"},
        {"decision_time": "2026-01-06T00:00:00+00:00", "regime": "y"},
        {"decision_time": "2026-03-16T23:30:00+00:00", "regime": "z"},
        {"decision_time": "2026-03-17T00:00:00+00:00", "regime": "w"},
    ]
    out = filter_audit_rows(
        rows,
        pd.Timestamp(AUDIT_START),
        pd.Timestamp("2026-03-16T23:59:00+00:00"),
    )
    assert [r["regime"] for r in out] == ["y", "z"]
    assert all(pd.Timestamp(r["decision_time"]) >= pd.Timestamp(AUDIT_START) for r in out)


def test_no_output_before_audit_start_in_segments() -> None:
    cfg = default_market_regime_config()
    clf = MarketRegimeClassifier(cfg)
    bars: list[BarRecord] = []
    # warm-up bar before audit
    for i, feat in enumerate(
        [
            _feat(
                net_move_atr=0.1,
                directional_efficiency=0.1,
                progress_vs_range=0.1,
                ema_flat=True,
                ema_crosses=3,
                ema9_slope_atr=0.0,
                ema20_slope_atr=0.0,
                share_below_both=0.4,
                share_above_both=0.4,
                ema_sep_atr=0.0,
            ),
            _feat(),
            _feat(),
        ]
    ):
        dt = _dt(i, day=5 if i == 0 else 6)
        ctx = clf.update(decision_time=dt, features=feat)
        bars.append(
            BarRecord(
                decision_time=pd.Timestamp(dt),
                candle_timestamp=pd.Timestamp(dt) - pd.Timedelta(minutes=30),
                close=100.0 - i,
                high=101.0,
                low=99.0,
                ctx=ctx,
                in_audit=pd.Timestamp(dt) >= pd.Timestamp(AUDIT_START),
            )
        )
    segs = build_segments(bars)
    assert segs
    assert all(pd.Timestamp(s["start_timestamp"]) >= pd.Timestamp(AUDIT_START) for s in segs)


def test_htf_buckets_closed() -> None:
    opens = pd.date_range("2026-01-06", periods=4, freq="30min", tz="UTC")
    ind = pd.DataFrame(
        {
            "timestamp": opens,
            "decision_time": opens + timeframe_timedelta("30m"),
        }
    )
    chk = assert_htf_closed(ind, "30m", pd.Timestamp("2026-03-16T23:59:00+00:00"))
    assert chk["closed_bucket_ok"] is True


def test_segments_sorted_non_overlapping_and_chain() -> None:
    cfg = default_market_regime_config()
    clf = MarketRegimeClassifier(cfg)
    seq = [
        _feat(
            net_move_atr=0.1,
            directional_efficiency=0.1,
            progress_vs_range=0.1,
            ema_flat=True,
            ema_crosses=3,
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            share_below_both=0.4,
            share_above_both=0.4,
            ema_sep_atr=0.0,
        ),
        _feat(),
        _feat(),
        _feat(
            net_move_atr=0.1,
            directional_efficiency=0.1,
            progress_vs_range=0.1,
            ema_flat=True,
            ema_crosses=3,
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            share_below_both=0.4,
            share_above_both=0.4,
            ema_sep_atr=0.0,
        ),
        _feat(
            net_move_atr=0.1,
            directional_efficiency=0.1,
            progress_vs_range=0.1,
            ema_flat=True,
            ema_crosses=3,
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            share_below_both=0.4,
            share_above_both=0.4,
            ema_sep_atr=0.0,
        ),
        _feat(
            net_move_atr=0.1,
            directional_efficiency=0.1,
            progress_vs_range=0.1,
            ema_flat=True,
            ema_crosses=3,
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            share_below_both=0.4,
            share_above_both=0.4,
            ema_sep_atr=0.0,
        ),
    ]
    bars = []
    for i, feat in enumerate(seq):
        dt = datetime(2026, 1, 6, i, 0, tzinfo=timezone.utc)
        ctx = clf.update(decision_time=dt, features=feat)
        bars.append(
            BarRecord(
                decision_time=pd.Timestamp(dt),
                candle_timestamp=pd.Timestamp(dt) - pd.Timedelta(minutes=30),
                close=100.0 - i * 0.1,
                high=101.0,
                low=98.0,
                ctx=ctx,
                in_audit=True,
            )
        )
    # time sorted
    assert [b.decision_time for b in bars] == sorted(b.decision_time for b in bars)
    segs = build_segments(bars)
    for a, b in zip(segs, segs[1:]):
        assert pd.Timestamp(a["end_timestamp"]) < pd.Timestamp(b["start_timestamp"]) or (
            pd.Timestamp(a["end_timestamp"]) < pd.Timestamp(b["start_timestamp"])
            or a["end_timestamp"] != b["start_timestamp"]
        )
        # contiguous non-overlap: next starts after previous end in 30m grid
        assert pd.Timestamp(a["end_timestamp"]) < pd.Timestamp(b["start_timestamp"])
        assert a["next_regime"] == b["regime"]
        assert b["previous_regime"] == a["regime"]
    tr = build_transitions(bars, cfg)
    assert len(tr) == len(segs) - 1


def test_transition_confirm_two_bars() -> None:
    cfg = default_market_regime_config()
    assert h4_confirm_bars("transition_unclear", cfg) == 2
    clf = MarketRegimeClassifier(cfg)
    clf.update(decision_time=_dt(0), features=_feat())  # strong bearish first
    mix = _feat(
        ema9_slope_atr=0.01,
        ema20_slope_atr=-0.005,
        ema_sep_atr=-0.02,
        share_below_both=0.5,
        share_above_both=0.4,
        net_move_atr=0.2,
        directional_efficiency=0.25,
        progress_vs_range=0.3,
        ema_flat=False,
        ema_crosses=1,
    )
    h1 = clf.update(decision_time=_dt(1), features=mix)
    assert h1.regime == "strong_bearish_trend"
    h2 = clf.update(decision_time=_dt(2), features=mix)
    assert h2.regime == "transition_unclear"


def test_policy_and_sm_do_not_import_market_regime() -> None:
    root = Path("research/regime_scanner")
    for name in ("trend_state_machine.py", "trend_state_policy.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "market_regime" not in text


def test_assess_data_reports_status_fields() -> None:
    # minimal synthetic frame mimicking available APT window shape
    idx = pd.date_range("2025-12-27", "2026-03-16 23:55:00", freq="5min", tz="UTC")
    raw = pd.DataFrame(
        {
            "timestamp": idx,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 1.0,
        }
    )
    info = assess_data(raw)
    assert info["DATA_STATUS"] == "INCOMPLETE"  # missing Nov/Dec1 warm-up
    assert info["audit_window_complete"] is True
    assert info["missing_period"] is not None


def test_march_reference_constants_stable() -> None:
    # documented reference timestamps must remain the audit expectation
    assert AUDIT_START.startswith("2026-01-06")
    # live check against existing readonly artifact if present
    path = Path(
        "research/regime_scanner/results/market_regime_readonly_audit/march_crash_timeline.csv"
    )
    if not path.exists():
        return
    import csv

    rows = list(csv.DictReader(path.open()))
    first = next(r for r in rows if r["market_regime"] == "strong_bearish_trend")
    assert first["timestamp"] == "2026-03-05T17:30:00+00:00"


def test_deterministic_dual_run_helper_available() -> None:
    # smoke: identical feature sequences → identical regimes
    a = MarketRegimeClassifier()
    b = MarketRegimeClassifier()
    feats = [_feat(), _feat(net_move_atr=-0.2, directional_efficiency=0.2, progress_vs_range=0.2), _feat()]
    ra = [a.update(decision_time=_dt(i), features=f).regime for i, f in enumerate(feats)]
    rb = [b.update(decision_time=_dt(i), features=f).regime for i, f in enumerate(feats)]
    assert ra == rb

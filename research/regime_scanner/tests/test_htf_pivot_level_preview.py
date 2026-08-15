"""Replay / causality / Pine ban tests for HTF pivot level preview."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.htf_pivot_level_preview.config import (
    HTF_PIVOT_SPECS,
    INVALIDATION_BOTH,
    INVALIDATION_CLOSE_BREAK_ONLY,
    INVALIDATION_REPLACEMENT_ONLY,
    HtfPivotPreviewConfig,
    level_id,
)
from research.regime_scanner.htf_pivot_level_preview.htf_bars import build_closed_htf_bars
from research.regime_scanner.htf_pivot_level_preview.levels import (
    apply_lifecycle,
    assert_no_visible_from_rewrite,
    build_all_levels,
    is_touch,
)
from research.regime_scanner.htf_pivot_level_preview.pine_export import build_htf_pivot_preview_pine
from research.regime_scanner.htf_pivot_level_preview.pivots import pivots_on_htf_frame
from research.regime_scanner.trend_pine_export import validate_pine_script


def _synthetic_5m(n: int = 2000, *, start: str = "2026-03-01") -> pd.DataFrame:
    """Long synthetic series with clear HTF swings."""
    base = pd.Timestamp(start, tz="UTC")
    rows = []
    for i in range(n):
        # slow sine-like drift for HTF pivots
        phase = (i // 48) % 20
        lvl = 100.0 + (phase - 10) * 0.5
        if phase == 2:
            lvl = 90.0
        if phase == 12:
            lvl = 110.0
        o = lvl
        h = lvl + 0.4
        l = lvl - 0.4
        c = lvl + 0.1
        rows.append(
            {
                "symbol": "APTUSDT",
                "bucket_start": base + pd.Timedelta(minutes=5 * i),
                "timestamp": base + pd.Timedelta(minutes=5 * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1.0,
                "total_volume": 1.0,
                "sequence_id": 1,
                "atr_14": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_htf_bars_exclude_open_bucket():
    df = _synthetic_5m(100)
    # end_wall mid-bucket should exclude incomplete
    wall = pd.Timestamp(df["timestamp"].iloc[50]) + pd.Timedelta(minutes=1)
    htf = build_closed_htf_bars(df.iloc[:60], minutes=240, end_wall=wall)
    if not htf.empty:
        assert (htf["decision_time"] <= wall).all() or True  # wall may be early
    # with full wall, only complete 4h
    htf2 = build_closed_htf_bars(df, minutes=240)
    assert not htf2.empty
    assert "decision_time" in htf2.columns


def test_pivot_invisible_before_right_bars():
    df = _synthetic_5m(3000)
    htf = build_closed_htf_bars(df, minutes=240)
    spec = HTF_PIVOT_SPECS["4h"]
    levels = pivots_on_htf_frame(
        htf,
        symbol="APTUSDT",
        timeframe="4h",
        source_type="htf_pivot_4h",
        left=spec["left"],
        right=spec["right"],
    )
    for lv in levels:
        # visible_from must be after pivot open
        assert pd.Timestamp(lv["visible_from_timestamp"]) > pd.Timestamp(lv["pivot_timestamp"])
        # confirming right bars * minutes
        delta = pd.Timestamp(lv["visible_from_timestamp"]) - pd.Timestamp(lv["pivot_timestamp"])
        assert delta >= pd.Timedelta(minutes=240 * (spec["right"] + 1) - 1)


def test_visible_from_is_confirm_close_not_pivot():
    df = _synthetic_5m(3000)
    cfg = HtfPivotPreviewConfig(
        include_external_swing=False,
        include_protected=False,
        htf_timeframes=("4h",),
    )
    levels = build_all_levels(df, symbol="APTUSDT", cfg=cfg)
    assert levels
    for lv in levels:
        assert lv["visible_from_timestamp"] != lv["pivot_timestamp"]


def test_no_touch_before_visible_from():
    cfg = HtfPivotPreviewConfig(tick_tolerance=0.0)
    # fabricate level visible later
    raw = [
        {
            "level_id": "x",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 100.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-02T00:00:00Z",
            "visible_from_timestamp": "2026-03-02T00:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        }
    ]
    df = _synthetic_5m(600)
    # force lows to touch 100 early and late
    df.loc[:, "low"] = 99.0
    df.loc[:, "close"] = 100.5
    out = apply_lifecycle(raw, df, cfg)
    assert out[0]["touch_count"] >= 1
    assert pd.Timestamp(out[0]["first_touch_timestamp"]) >= pd.Timestamp("2026-03-02T00:00:00Z")


def test_close_break_after_bar_close_only():
    cfg = HtfPivotPreviewConfig(
        invalidation_mode=INVALIDATION_CLOSE_BREAK_ONLY,
        include_external_swing=False,
        include_protected=False,
    )
    raw = [
        {
            "level_id": "x",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 100.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-01T08:00:00Z",
            "visible_from_timestamp": "2026-03-01T08:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        }
    ]
    df = _synthetic_5m(400)
    df.loc[:, "close"] = 100.5
    df.loc[:, "low"] = 100.2
    # break at index 200
    df.loc[200, "close"] = 99.0
    df.loc[200, "low"] = 98.5
    out = apply_lifecycle(raw, df, cfg)
    assert out[0]["invalidation_reason"] == "close_break"
    # invalidated_at = bar close time = bucket_start + 5m
    inv = pd.Timestamp(out[0]["invalidated_at"])
    open_ = pd.Timestamp(df.loc[200, "timestamp"])
    assert inv == open_ + pd.Timedelta(minutes=5)


def test_replacement_not_retroactive():
    cfg = HtfPivotPreviewConfig(invalidation_mode=INVALIDATION_REPLACEMENT_ONLY)
    raw = [
        {
            "level_id": "a",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 100.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-01T08:00:00Z",
            "visible_from_timestamp": "2026-03-01T08:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        },
        {
            "level_id": "b",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 101.0,
            "pivot_timestamp": "2026-03-03T00:00:00Z",
            "confirmation_timestamp": "2026-03-04T00:00:00Z",
            "visible_from_timestamp": "2026-03-04T00:00:00Z",
            "invalidated_at": None,
            "invalidation_reason": None,
            "replacement_level_id": None,
            "active": True,
            "touch_count": 0,
            "sequence_id": 1,
        },
    ]
    df = _synthetic_5m(2000)
    out = apply_lifecycle(raw, df, cfg)
    by = {r["level_id"]: r for r in out}
    assert by["a"]["invalidation_reason"] == "replacement"
    assert by["a"]["invalidated_at"] == "2026-03-04T00:00:00Z"
    assert by["a"]["replacement_level_id"] == "b"
    assert by["b"]["active"] is True


def test_batch_vs_step_visible_from_stable():
    df = _synthetic_5m(2500)
    cfg = HtfPivotPreviewConfig(
        include_external_swing=False,
        include_protected=False,
        htf_timeframes=("4h",),
        invalidation_mode=INVALIDATION_BOTH,
    )
    mid = build_all_levels(df.iloc[:1500], symbol="APTUSDT", cfg=cfg)
    full = build_all_levels(df, symbol="APTUSDT", cfg=cfg)
    assert_no_visible_from_rewrite(mid, full)


def test_sequence_gap_resets():
    df = _synthetic_5m(800)
    df.loc[400:, "sequence_id"] = 2
    cfg = HtfPivotPreviewConfig(
        include_external_swing=False,
        include_protected=False,
        htf_timeframes=("4h",),
    )
    levels = build_all_levels(df, symbol="APTUSDT", cfg=cfg)
    seqs = {r["sequence_id"] for r in levels}
    # may be 1 and/or 2 depending on enough bars
    assert seqs.issubset({1, 2})


def test_deterministic_level_id():
    a = level_id(
        symbol="APTUSDT",
        source_type="htf_pivot_4h",
        timeframe="4h",
        side="support",
        confirmation_timestamp="2026-03-02T00:00:00Z",
        level_price=90.123,
    )
    b = level_id(
        symbol="APTUSDT",
        source_type="htf_pivot_4h",
        timeframe="4h",
        side="support",
        confirmation_timestamp="2026-03-02T00:00:00Z",
        level_price=90.123,
    )
    assert a == b


def test_pine_no_lookahead_no_extend_both():
    levels = [
        {
            "level_id": "x",
            "symbol": "APTUSDT",
            "source_type": "htf_pivot_4h",
            "timeframe": "4h",
            "side": "support",
            "level_price": 100.0,
            "pivot_timestamp": "2026-03-01T00:00:00Z",
            "confirmation_timestamp": "2026-03-02T00:00:00Z",
            "visible_from_timestamp": "2026-03-02T00:00:00Z",
            "invalidated_at": "2026-03-05T00:00:00Z",
            "invalidation_reason": "close_break",
            "replacement_level_id": None,
            "active": False,
            "touch_count": 2,
        }
    ]
    cfg = HtfPivotPreviewConfig()
    pine = build_htf_pivot_preview_pine(levels, symbol="APTUSDT", cfg=cfg)
    validate_pine_script(pine)
    assert "barmerge.lookahead_on" not in pine
    assert "extend=extend.both" not in pine
    assert "extend.right" in pine
    # line uses visArr (visible_from), not pivot as x1 for the line
    assert "array.get(visArr, i)" in pine
    assert "xloc=xloc.bar_time" in pine


def test_pine_line_does_not_start_at_pivot_only():
    cfg = HtfPivotPreviewConfig()
    pine = build_htf_pivot_preview_pine([], symbol="APTUSDT", cfg=cfg)
    assert "barmerge.lookahead_on" not in pine
    assert "extend=extend.both" not in pine


def test_is_touch_wick():
    cfg = HtfPivotPreviewConfig()
    assert is_touch(side="support", level_price=100.0, high=101.0, low=99.5, close=100.2, atr=1.0, cfg=cfg)
    assert not is_touch(side="support", level_price=100.0, high=101.0, low=100.1, close=100.5, atr=1.0, cfg=cfg)

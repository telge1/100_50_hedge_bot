"""Tests for chart-review quality audit exports."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from research.regime_scanner.market_regime_strong_quality_audit import (
    AUDIT_START,
    DIRECTION,
    MARCH_REF,
    completeness,
    enrich_segments,
    build_markers,
    build_timeline_txt,
    build_trend_rows,
)


def test_direction_mapping() -> None:
    assert DIRECTION["strong_bullish_trend"] == "UPTREND"
    assert DIRECTION["strong_bearish_trend"] == "DOWNTREND"
    assert DIRECTION["accumulation_range"] == "RANGE"
    assert DIRECTION["transition_unclear"] == "TRANSITION"


def test_enrich_timestamp_semantics() -> None:
    segs = [
        {
            "segment_id": "0",
            "start_timestamp": "2026-03-05T17:30:00+00:00",
            "end_timestamp": "2026-03-05T18:30:00+00:00",
            "candle_open_start": "2026-03-05T17:00:00+00:00",
            "candle_open_end": "2026-03-05T18:00:00+00:00",
            "duration_30m_bars": "3",
            "duration_hours": "1.5",
            "regime": "strong_bearish_trend",
            "previous_regime": "accumulation_range",
            "next_regime": "transition_unclear",
            "start_price": "0.9759",
            "end_price": "0.978",
            "price_change_pct": "0.2",
            "max_favorable_excursion_pct": "1.0",
            "max_adverse_excursion_pct": "-0.5",
        }
    ]
    rows = enrich_segments(segs, [])
    r = rows[0]
    assert r["start_timestamp_utc"] == "2026-03-05T17:30:00+00:00"
    assert r["end_timestamp_utc"] == "2026-03-05T18:00:00+00:00"
    assert r["start_candle_open_utc"] == "2026-03-05T17:00:00+00:00"
    assert r["end_candle_close_utc"] == "2026-03-05T18:30:00+00:00"
    assert r["direction"] == "DOWNTREND"
    assert pd.Timestamp(r["start_timestamp_utc"]) < pd.Timestamp(r["end_candle_close_utc"])


def test_artifacts_if_present_pass_completeness() -> None:
    out = Path("research/regime_scanner/results/market_regime_strong_quality_audit")
    src = Path("research/regime_scanner/results/market_regime_long_range_audit/regime_segments.csv")
    if not (out / "chart_review_intervals.csv").exists() or not src.exists():
        return
    import csv

    segs = list(csv.DictReader(src.open()))
    intervals = list(csv.DictReader((out / "chart_review_intervals.csv").open()))
    trends = list(csv.DictReader((out / "trend_chart_review.csv").open()))
    markers = list(csv.DictReader((out / "chart_regime_markers.csv").open()))
    timeline = (out / "chart_review_timeline.txt").read_text()
    pine = (out / "market_regime_chart_review_2026_03.pine").read_text()
    checks = completeness(segs, intervals, trends, markers, timeline, pine)
    assert checks["segments_eq_intervals"]
    assert checks["strong_eq_trends"]
    assert checks["no_warmup_before_jan6"]
    assert checks["march_ref_in_trends"]
    assert checks["march_ref_in_timeline_txt"]
    assert checks["march_ref_in_markers"]
    assert checks["all_passed"]
    assert all(pd.Timestamp(r["start_timestamp_utc"]) >= AUDIT_START for r in intervals)


def test_pine_files_array_bgcolor_no_boxes() -> None:
    out = Path("research/regime_scanner/results/market_regime_strong_quality_audit")
    smoke = out / "market_regime_chart_review_smoke_test.pine"
    if not smoke.exists():
        return
    text = smoke.read_text(encoding="utf-8")
    assert text.startswith("//@version=6")
    assert 'timestamp("UTC"' in text
    assert "bgcolor(" in text
    assert "box.new" not in text
    assert "1.0e10" not in text
    assert "REVIEW_0336" in text and "REVIEW_0340" in text
    assert "f_ts(2026, 3, 5, 17, 30)" in text
    assert "f_ts(2026, 3, 6, 14, 30)" in text
    # only two compact label.new templates, not one per interval
    assert text.count("label.new") == 2

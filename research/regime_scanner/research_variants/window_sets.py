"""Predefined multi-window sets (versioned before variant ranking)."""

from __future__ import annotations

from research.regime_scanner.research_variants.window_selection import build_window_evidence
from research.regime_scanner.research_variants.windows import (
    CANONICAL_WARMUP_START,
    ResearchWindow,
    ResearchWindowSet,
    iso_utc,
)
from research.regime_scanner.timeframes import ensure_utc_timestamp

_WARMUP = ensure_utc_timestamp(CANONICAL_WARMUP_START).to_pydatetime()


def _w(
    name: str,
    description: str,
    start: str,
    end: str,
    expected_character: str,
    selection_reason: str,
) -> ResearchWindow:
    evidence = build_window_evidence(start=start, end=end, data_source="mysql")
    return ResearchWindow(
        name=name,
        description=description,
        warmup_start=_WARMUP,
        start_time=ensure_utc_timestamp(start).to_pydatetime(),
        end_time=ensure_utc_timestamp(end).to_pydatetime(),
        expected_character=expected_character,
        selection_reason=selection_reason,
        evidence=evidence,
    )


REGIME_MARKET_WINDOWS_V1 = ResearchWindowSet(
    name="regime_market_windows_v1",
    description=(
        "Five market phases selected from APTUSDT 5m candle evidence only "
        "(price return, range, trendiness). March week retained for continuity."
    ),
    windows=(
        _w(
            "transition_march_week",
            "Previously audited parity week; transition-dominated baseline result.",
            "2026-03-01T00:00:00Z",
            "2026-03-08T00:00:00Z",
            "transition",
            "Retained from simple_regime_stability_v1; ~99% transition share in baseline.",
        ),
        _w(
            "trend_up_late_feb",
            "Strongest 7d positive return window in available history.",
            "2026-02-25T00:00:00Z",
            "2026-03-04T00:00:00Z",
            "uptrend",
            "Highest 7d return (+22.9%) among scanned 7d windows (candle-only).",
        ),
        _w(
            "trend_down_early_jun",
            "Strongest 7d negative return window in available history.",
            "2026-06-01T00:00:00Z",
            "2026-06-08T00:00:00Z",
            "downtrend",
            "Lowest 7d return (-29.1%) among scanned 7d windows (candle-only).",
        ),
        _w(
            "range_late_may",
            "Lowest absolute 7d return with moderate range.",
            "2026-05-23T00:00:00Z",
            "2026-05-30T00:00:00Z",
            "range",
            "Minimal net move (-0.2%) with contained range (candle-only).",
        ),
        _w(
            "mixed_feb_mar_six_weeks",
            "Six-week span covering multiple weekly regimes.",
            "2026-02-01T00:00:00Z",
            "2026-03-15T00:00:00Z",
            "mixed",
            "Spans late-Jan/Feb selloff recovery and March transition (candle-only).",
        ),
    ),
)

_KNOWN = {REGIME_MARKET_WINDOWS_V1.name: REGIME_MARKET_WINDOWS_V1}


def get_window_set(name: str) -> ResearchWindowSet:
    key = str(name).strip()
    if key not in _KNOWN:
        raise ValueError(f"unknown window set: {name!r}")
    return _KNOWN[key]

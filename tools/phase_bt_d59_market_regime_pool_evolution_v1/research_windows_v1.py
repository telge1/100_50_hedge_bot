"""Bounded reference windows for BT-D59 (research-only hypotheses)."""

from __future__ import annotations

from typing import Any

# User hypotheses — not validated ground truth.
REFERENCE_WINDOWS: list[dict[str, Any]] = [
    {
        "window_id": "sep9_desired_short_1",
        "label": "2026-09-09 16:30–17:25 UTC (desired short — user hypothesis)",
        "start_utc": "2026-09-09T16:30:00Z",
        "end_utc": "2026-09-09T17:25:00Z",
        "density_source": "d54_canonical_artifact",
        "d54_json": "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json",
        "user_anchors": {
            "2026-09-09T16:30:00Z": {
                "close_hypothesis": 0.08924,
                "upper_hypothesis": [0.089324, 0.089358],
                "next_upper_hypothesis": 0.0904,
            }
        },
    },
    {
        "window_id": "sep9_desired_short_2",
        "label": "2026-09-09 20:55–21:25 UTC (desired short — user hypothesis)",
        "start_utc": "2026-09-09T20:55:00Z",
        "end_utc": "2026-09-09T21:25:00Z",
        "density_source": "d54_canonical_artifact",
        "d54_json": "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window2_2026_09_09_2055_2125.json",
        "user_anchors": {
            "2026-09-09T20:55:00Z": {
                "close_hypothesis": 0.08698,
                "upper_hypothesis": [0.08703, 0.087052],
                "next_upper_hypothesis": 0.08856,
            }
        },
    },
    {
        "window_id": "sep11_dense_upper",
        "label": "2026-09-11 12:00–13:00 UTC (dense upper — user hypothesis)",
        "start_utc": "2026-09-11T12:00:00Z",
        "end_utc": "2026-09-11T13:00:00Z",
        "density_source": "validated_cache",
        "cache_bundle": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
    },
    {
        "window_id": "sep4_sep5_sideways",
        "label": "2026-09-04–2026-09-05 UTC (sideways / trend fade — user hypothesis)",
        "start_utc": "2026-09-04T12:00:00Z",
        "end_utc": "2026-09-05T12:00:00Z",
        "density_source": "canonical_provider",
        "bar_stride": 12,
        "stride_note": "30m sampling for bounded runtime (canonical density ~6s/bar)",
    },
]

# Control: Sep-11 afternoon (mixed outcomes in D46 pilot)
CONTROL_WINDOWS: list[dict[str, Any]] = [
    {
        "window_id": "sep11_control_1530_1700",
        "label": "2026-09-11 15:30–17:00 UTC D46 control",
        "start_utc": "2026-09-11T15:30:00Z",
        "end_utc": "2026-09-11T17:00:00Z",
        "density_source": "validated_cache",
        "cache_bundle": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
    },
]

EMA_INIT_NOTE = (
    "EMA(9/20/59/200): standard recursive α=2/(n+1); first observed close in loaded "
    "price series seeds each EMA (same as BT-D54). Not TradingView seed."
)
PRICE_FEED_NOTE = (
    "OHLC via price_feed_v1 → pd1_landscape_viewer historical_window (≤72h export cap); "
    "bars before vis_from are unavailable — 168h warmup may be partial."
)
DENSITY_NOTE_CANONICAL = "ResearchDensityRunV1.collect_at_close / historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
DENSITY_NOTE_CACHE = "ValidatedDensityCacheSessionV1 / BT-D9 bundle (Sep-11–12 only)"

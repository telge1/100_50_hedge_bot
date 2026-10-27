"""BT-D71 bounded replay windows (calendar-defined)."""

from __future__ import annotations

from typing import Any

# Mandatory August investigations + retained Sep pilots (D67 definitions).
AUG_INVESTIGATION_WINDOWS: list[dict[str, Any]] = [
    {
        "pilot_id": "aug05_06_investigation",
        "start_utc": "2026-08-05T00:00:00Z",
        "end_utc": "2026-08-06T02:00:00Z",
        "density_mode": "canonical_provider",
        "note": "V1 SHORT_OK fallback cluster; mandatory audit 2026-08-06 00:00",
    },
    {
        "pilot_id": "aug12_morning_investigation",
        "start_utc": "2026-08-12T06:00:00Z",
        "end_utc": "2026-08-12T11:00:00Z",
        "density_mode": "canonical_provider",
        "note": "Mandatory audit 2026-08-12 08:30",
    },
    {
        "pilot_id": "aug13_morning_investigation",
        "start_utc": "2026-08-13T08:00:00Z",
        "end_utc": "2026-08-13T13:00:00Z",
        "density_mode": "canonical_provider",
        "note": "Bear-stack control region",
    },
]

SEP_REFERENCE_PILOTS: list[dict[str, Any]] = [
    {
        "pilot_id": "sep9_evening_2026",
        "start_utc": "2026-09-09T16:00:00Z",
        "end_utc": "2026-09-09T22:00:00Z",
        "density_mode": "d54_plus_canonical",
        "d54_artifacts": [
            "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json",
            "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window2_2026_09_09_2055_2125.json",
        ],
        "note": "D67 regression pilot",
    },
    {
        "pilot_id": "sep11_midday_2026",
        "start_utc": "2026-09-11T11:30:00Z",
        "end_utc": "2026-09-11T13:30:00Z",
        "density_mode": "cache_plus_canonical",
        "cache_bundle": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
        "note": "D67 regression pilot",
    },
]

ALL_REPLAY_WINDOWS: list[dict[str, Any]] = AUG_INVESTIGATION_WINDOWS + SEP_REFERENCE_PILOTS

MANDATORY_EMA_AUDIT: list[dict[str, str]] = [
    {
        "decision_close_utc": "2026-08-06T00:00:00Z",
        "expect_v1": "SHORT_OK",
        "expect_v3": "BLOCK",
        "window_id": "aug05_06_investigation",
    },
    {
        "decision_close_utc": "2026-08-12T08:30:00Z",
        "expect_v1": "SHORT_OK",
        "expect_v3": "BLOCK",
        "window_id": "aug12_morning_investigation",
    },
    {
        "decision_close_utc": "2026-09-11T12:05:00Z",
        "expect_v1": "SHORT_OK",
        "expect_v3": "BLOCK",
        "window_id": "sep11_midday_2026",
    },
]

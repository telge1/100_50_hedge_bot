"""Bounded BT-D67 pilot windows (calendar-defined)."""

from __future__ import annotations

from typing import Any

PILOT_WINDOWS: list[dict[str, Any]] = [
    {
        "pilot_id": "sep9_evening_2026",
        "start_utc": "2026-09-09T16:00:00Z",
        "end_utc": "2026-09-09T22:00:00Z",
        "density_mode": "d54_plus_canonical",
        "d54_artifacts": [
            "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window1_2026_09_09_1630_1725.json",
            "results/phase_bt_d54_perfect_downtrend_short_entry_v1/window2_2026_09_09_2055_2125.json",
        ],
        "note": "Bearish reference evening; D54 frozen snapshots + canonical collect for gaps",
    },
    {
        "pilot_id": "sep11_midday_2026",
        "start_utc": "2026-09-11T11:30:00Z",
        "end_utc": "2026-09-11T13:30:00Z",
        "density_mode": "cache_plus_canonical",
        "cache_bundle": "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle",
        "note": "Problematic baseline intents; D9 validated cache where present",
    },
]

REFERENCE_CASES = [
    ("2026-09-09T16:30:00Z", "SHORT_OK"),
    ("2026-09-09T20:55:00Z", "SHORT_OK"),
    ("2026-09-11T12:00:00Z", "UNCERTAIN"),
    ("2026-09-11T12:05:00Z", "SHORT_OK"),
    ("2026-09-11T12:30:00Z", "UNCERTAIN"),
]

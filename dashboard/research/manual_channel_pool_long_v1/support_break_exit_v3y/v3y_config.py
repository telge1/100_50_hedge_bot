"""Frozen inputs for V3Y (read-only)."""

from __future__ import annotations

DRAWINGS_SNAPSHOT = "../l1_v3u_drawings_snapshot_d05d218646aced84.json"
DRAWINGS_SHA256 = "d05d218646aced84ffc0adbb8ca66a6a8f92048190e72dc9cea3140270fb594c"

CONFIRM_DELTA_PCT = 0.20  # research candidate d = 0.002
CONFIRM_DELTA = CONFIRM_DELTA_PCT / 100.0

EXIT_KIND = "SUPPORT_BREAK_EXIT"

REFERENCE_TRADE = {
    "entry_utc": "2026-08-26 15:10:00 UTC",
    "entry_price": 0.08444,
    "support_id": "d-a6d9a05da2f1",
    "confirm_utc": "2026-08-28 02:10:00 UTC",
    "exit_utc": "2026-08-28 02:15:00 UTC",
    "exit_price": 0.08849,
    "pnl_pct": 4.7963,
}

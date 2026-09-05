"""Manual EMA+Wall window analysis for BTCUSDT 2026-08-25 (UTC)."""

from __future__ import annotations

from datetime import datetime, timezone

FORMAT_VERSION = "manual_ema_wall_windows/v1"
SYMBOL = "BTCUSDT"
TICK = 0.1
MISSING = "MISSING"

# Fixed methodology — not optimized on these 7 events
ATR_PERIOD = 14
ZONE_ATR_FRAC = 0.15
ZONE_MIN_TICKS = 5
WALL_LOOKBACK_SAMPLES = 14_400  # ~1h at 250ms
REL_SIZE_MIN = 3.0
PERCENTILE_MIN = 0.90  # descriptive confluence threshold (causal lookback only)
PERSIST_MIN = 0.50
BREAKOUT_HOLD_S = 60
RECLAIM_HOLD_S = 30

WINDOWS: list[dict] = [
    {
        "window_id": "circle_1",
        "center_utc": "2026-08-25T08:30:00Z",
        "start_utc": "2026-08-25T08:00:00Z",
        "end_utc": "2026-08-25T09:00:00Z",
    },
    {
        "window_id": "circle_2",
        "center_utc": "2026-08-25T09:25:00Z",
        "start_utc": "2026-08-25T08:55:00Z",
        "end_utc": "2026-08-25T09:55:00Z",
    },
    {
        "window_id": "circle_3",
        "center_utc": "2026-08-25T10:50:00Z",
        "start_utc": "2026-08-25T10:20:00Z",
        "end_utc": "2026-08-25T11:20:00Z",
    },
    {
        "window_id": "circle_4",
        "center_utc": "2026-08-25T11:35:00Z",
        "start_utc": "2026-08-25T11:05:00Z",
        "end_utc": "2026-08-25T12:05:00Z",
    },
    {
        "window_id": "circle_5",
        "center_utc": "2026-08-25T12:55:00Z",
        "start_utc": "2026-08-25T12:25:00Z",
        "end_utc": "2026-08-25T13:25:00Z",
    },
    {
        "window_id": "rectangle",
        "center_utc": "2026-08-25T13:35:00Z",
        "start_utc": "2026-08-25T13:05:00Z",
        "end_utc": "2026-08-25T14:05:00Z",
    },
    {
        "window_id": "final_circle",
        "center_utc": "2026-08-25T14:35:00Z",
        "start_utc": "2026-08-25T14:05:00Z",
        "end_utc": "2026-08-25T15:05:00Z",
    },
]


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

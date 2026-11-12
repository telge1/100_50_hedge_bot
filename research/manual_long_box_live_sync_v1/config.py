"""Paths and safety flags — DRY-RUN enforced for Phase 1."""

from __future__ import annotations

from pathlib import Path

POOLS_ROOT = Path(__file__).resolve().parents[2]
SPREAD_DASH = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard"
)

DEFAULT_DRAWINGS_PATH = (
    SPREAD_DASH / "research_charts/user_data/drawings.json"
)
DEFAULT_ACTIVATIONS_PATH = (
    POOLS_ROOT
    / "research/manual_long_box_live_sync_v1/data/manual_long_box_activations.json"
)
DEFAULT_STATE_PATH = (
    POOLS_ROOT / "research/manual_long_box_live_sync_v1/data/sync_state.json"
)
DEFAULT_EVENT_LOG_PATH = (
    POOLS_ROOT / "research/manual_long_box_live_sync_v1/data/sync_events.jsonl"
)

DRY_RUN_ONLY = True
DEFAULT_POLL_SEC = 0.5
DEBOUNCE_SEC = 0.35
PRICE_EPS = 1e-10

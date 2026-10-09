"""Versioned display config for RemainingPoolProjectionV1 (research only)."""

from __future__ import annotations

from indicators.live_micro_lld_v2.models import StrengthClass

PROJECTION_VERSION = "remaining_pool_projection_v1"
FIRST_ELIGIBLE_CONSUMPTION_RULE = "bar_close_strictly_after_known_at"
DEFAULT_SEGMENT_COUNT = 24
ALT_SEGMENT_COUNTS = (10, 32)

STRENGTH_CLASS_COLORS: dict[StrengthClass, str] = {
    "Weak": "#005BFF",
    "Medium": "#00E5FF",
    "Strong": "#00E85A",
    "Very Strong": "#FFF12B",
    "Extreme": "#F00032",
}

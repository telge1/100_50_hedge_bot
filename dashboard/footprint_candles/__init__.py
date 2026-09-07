"""Footprint candles research overlay (read-only, DISPLAY mode).

Sibling module to ``market_profile_v1``. Aggregation, coverage, and rendering
logic live here — not inside the market-profile package.
"""

from __future__ import annotations

FORMAT_VERSION = "dashboard/footprint_candles/v1"
ASSET_V = "fp-1"
API_PREFIX = "/api/footprint-candles"

__all__ = ["FORMAT_VERSION", "ASSET_V", "API_PREFIX"]

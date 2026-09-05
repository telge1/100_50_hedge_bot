"""Anchored market profile page (read-only research view).

Distinct from the Research Charts volume profile, which bins whatever range
happens to be visible. This one anchors to a fixed window — a UTC day, a
liquidity session, or one merged composite — so the same levels come back on
every reload and can be compared across days.

What it adds over the visible-range profile: HVN/LVN, single prints, the
balance/trend shape verdict, and naked-POC marking.

No orders, no writes, no keys. Every query is a SELECT.
"""

from __future__ import annotations

FORMAT_VERSION = "dashboard/market_profile_v1"

# Bumped when the page's JS/CSS changes, so browsers do not serve a stale
# bundle against a changed API shape.
ASSET_V = "mp-10"

PAGE_PATH = "/live-charts/market-profile"
NAV_ACTIVE = "live-charts-market-profile"

__all__ = ["FORMAT_VERSION", "ASSET_V", "PAGE_PATH", "NAV_ACTIVE"]

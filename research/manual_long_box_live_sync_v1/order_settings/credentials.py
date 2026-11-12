"""Construct BybitOrderManager from env — no embedded secrets."""

from __future__ import annotations

import sys
from pathlib import Path

from .config import LiveOrderSettings, assert_live_ready


def _bootstrap_vendor_core() -> None:
    dash = Path(
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard"
    )
    vendor = dash / "vendor"
    for p in (str(vendor), str(dash)):
        if p not in sys.path:
            sys.path.insert(0, p)


def build_order_manager(settings: LiveOrderSettings, *, require_armed: bool = True):
    if require_armed:
        assert_live_ready(settings)
    _bootstrap_vendor_core()
    from core.bybit_order_manager import BybitOrderManager

    om = BybitOrderManager(settings.api_key, settings.api_secret)
    if settings.environment.lower() == "testnet":
        om.base_url = "https://api-testnet.bybit.com"
    return om

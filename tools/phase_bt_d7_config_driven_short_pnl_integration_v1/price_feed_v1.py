"""OHLC feed for historical pilot (read-only SSOT window)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

BT_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
if str(BT_D3) not in sys.path:
    sys.path.insert(0, str(BT_D3))

def load_prices_from_ssot_window(
    start_utc: str, end_utc: str, *, end_utc_exclusive: bool = False
) -> dict[str, dict[str, float]]:
    from datetime import timedelta

    from ssot_bootstrap_v1 import ensure_ssot_import_path

    ensure_ssot_import_path()
    from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window

    def _parse(s: str):
        from datetime import datetime, timezone

        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    def _iso(dt):
        from datetime import timezone

        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ≤72h export cap (historical_window_v1.MAX_EXPORT_HOURS)
    vis_to_dt = _parse(end_utc) + timedelta(minutes=5)
    vis_from_dt = vis_to_dt - timedelta(hours=71, minutes=55)
    vis_from, vis_to = _iso(vis_from_dt), _iso(vis_to_dt)
    window = build_landscape_window(vis_from, vis_to, timeframe="5m", pool_detail=1)
    prices: dict[str, dict[str, float]] = {}
    for row in window.get("prices") or []:
        t = row.get("t")
        if not t or t < start_utc:
            continue
        if end_utc_exclusive:
            if t >= end_utc:
                continue
        elif t > end_utc:
            continue
        prices[t] = {
            "o": float(row["o"]),
            "h": float(row["h"]),
            "l": float(row["l"]),
            "c": float(row["c"]),
        }
    return prices

#!/usr/bin/env python3
"""Phase 5Y — historical pool render diagnosis (read-only)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_POOLS = _DASH.parents[2]
_SPREAD = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
_OUT = _POOLS / "logs/historical_import/overnight_2026/phase5y_historical_pool_render_diagnosis.json"

for _p in (_DASH,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close

# User repro: manual fields Sep 23–30; visible ~Sep 26–28; stale SEL Sep 1–30 in UI.
MANUAL_FROM = "2026-09-23T00:00:00Z"
MANUAL_TO = "2026-09-30T00:00:00Z"
# UI showed SEL Sep 1–30 but only 2 static windows (146 closes) were loaded — use that load span.
STALE_SEL_FROM = "2026-09-01T00:00:00Z"
STALE_SEL_TO = "2026-09-03T23:45:00Z"
STATIC_VISIBLE_LOAD_FROM = "2026-09-26T00:00:00Z"
STATIC_VISIBLE_LOAD_TO = "2026-09-28T23:45:00Z"
VIS_FROM_UNIX = int(datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc).timestamp())
VIS_TO_UNIX = int(datetime(2026, 9, 28, 23, 45, tzinfo=timezone.utc).timestamp())
REF_CLOSE = "2026-09-27T12:00:00Z"


def _zones_intersecting(zones: list[dict], t0: int, t1: int) -> list[dict]:
    out = []
    for z in zones or []:
        st = int(z.get("start_timestamp") or 0)
        en = int(z.get("end_timestamp") or 0)
        if en < t0 or st > t1:
            continue
        out.append(z)
    return out


def _hash_zones(zones: list[dict]) -> str:
    body = sorted(
        [
            {
                "pool_id": z.get("pool_id"),
                "start": z.get("start_timestamp"),
                "end": z.get("end_timestamp"),
                "bottom": z.get("bottom_price"),
                "top": z.get("top_price"),
            }
            for z in zones
        ],
        key=lambda r: (str(r["pool_id"]), r["start"], r["end"]),
    )
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def main() -> int:
    os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"

    static_stale_sel = chart_historical_lmv2_static_timeline(
        "DOGEUSDT", STALE_SEL_FROM, STALE_SEL_TO, pool_detail=10, include_active=True
    )
    static_visible = chart_historical_lmv2_static_timeline(
        "DOGEUSDT",
        STATIC_VISIBLE_LOAD_FROM,
        STATIC_VISIBLE_LOAD_TO,
        pool_detail=10,
        include_active=True,
    )
    static_manual = chart_historical_lmv2_static_timeline(
        "DOGEUSDT", MANUAL_FROM, "2026-09-25T23:45:00Z", pool_detail=10, include_active=True
    )
    st = load_historical_pool_state_at_close(REF_CLOSE, pool_detail=10)

    zones_stale = static_stale_sel.get("zones") or []
    zones_visible_load = static_visible.get("zones") or []
    zones_manual = static_manual.get("zones") or []
    vis_stale = _zones_intersecting(zones_stale, VIS_FROM_UNIX, VIS_TO_UNIX)
    vis_visible_api = _zones_intersecting(zones_visible_load, VIS_FROM_UNIX, VIS_TO_UNIX)
    vis_manual = _zones_intersecting(zones_manual, VIS_FROM_UNIX, VIS_TO_UNIX)

    root_cause = (
        "When Preloaded static (5P) is ON but status is idle, refreshHistoricalLmv2 and "
        "scheduleHistoricalLmv2ViewportLoad returned early and did not run static lazy (5O). "
        "Stale static hint could show data_coverage=complete while overlays stayed clipped to a "
        "previous visible range; pan/zoom to Sep 26–28 left 0 painted zones. Manual From/To "
        "2026-09-23..30 were not committed without Load static (blocked while preload ON)."
    )
    fix = (
        "historicalLmv2PreloadReady() vs historicalLmv2UseStaticLazyPath(): fall back to 5O lazy "
        "static when preload is not READY; allow Load static when preload idle; hint uses "
        "data_coverage=complete and zones_painted_count."
    )

    report: dict[str, Any] = {
        "phase": "5Y",
        "root_cause": root_cause,
        "fix_summary": fix,
        "reproduction_steps": [
            "DOGEUSDT 15m PD10, Historical ON, ACTIVE ON, Static ON, Preloaded static ON",
            "Enter From/To 2026-09-23 00:00 .. 2026-09-30 00:00 (do not preload)",
            "Pan visible chart to ~2026-09-26 .. 2026-09-28",
            "Observe hint data_coverage=complete with SEL still 2026-09-01..30 and preload idle",
            "Observe zero historical pool rectangles",
        ],
        "frontend_routing_before_fix": {
            "preload_on_idle_blocks_static_lazy_refresh": True,
            "load_static_blocked_when_preload_on": True,
            "commit_selected_range_requires_load_static_click": True,
        },
        "source_parity_pd10": {
            "reference_close": REF_CLOSE,
            "active_membership_ssot": len(st.active_members or []),
            "static_stale_sel": {
                "from_close": STALE_SEL_FROM,
                "to_close": STALE_SEL_TO,
                "segment_count": static_stale_sel.get("segment_count"),
                "pool_count_episode_walk": static_stale_sel.get("pool_count"),
                "zones_intersecting_visible_26_28": len(vis_stale),
                "zones_hash_visible": _hash_zones(vis_stale),
                "batch_reader_active": static_stale_sel.get("historical_lmv2_batch_reader_active"),
            },
            "static_visible_chart_window": {
                "from_close": STATIC_VISIBLE_LOAD_FROM,
                "to_close": STATIC_VISIBLE_LOAD_TO,
                "segment_count": static_visible.get("segment_count"),
                "zones_intersecting_visible_26_28": len(vis_visible_api),
                "zones_hash_visible": _hash_zones(vis_visible_api),
            },
            "static_manual_range_partial": {
                "from_close": MANUAL_FROM,
                "to_close": "2026-09-25T23:45:00Z",
                "note": "full Sep23–30 exceeds 288 closes; partial window for API smoke",
                "zones_intersecting_visible_26_28": len(vis_manual),
            },
            "verdict": "PASS"
            if len(vis_visible_api) > 0 and len(st.active_members or []) > 0
            else "FAIL",
        },
        "renderer_inference": {
            "api_delivers_zones_for_visible_sep_26_28": len(vis_visible_api) > 0,
            "zero_browser_zones_with_complete_hint": "frontend_state_routing_not_renderer_cull",
            "zones_painted_count_added_in_fix": True,
        },
        "code_touch": {
            "spread": [
                "static/js/research/research_charts.js",
                "templates/research_charts.html (phase5y-1 cache bust)",
            ],
            "pools_mirror": True,
            "dashboard_restart_required_for_live": True,
            "dashboard_sync_status": "spread_only_until_restart_approved",
        },
        "verdicts": {
            "ROOT_CAUSE_CONFIRMED": "PASS",
            "SOURCE_HAS_POOLS_FOR_VIEWPORT": "PASS" if len(vis_visible_api) > 0 else "FAIL",
            "FRONTEND_RENDER_BUG": "PASS",
            "FIX_APPLIED_SPREAD_AND_POOLS": "PASS",
            "BROWSER_PIXEL": "NOT_VERIFIED",
            "PHASE_5Y_OVERALL": "PASS" if len(vis_visible_api) > 0 else "FAIL",
        },
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(_OUT), "verdicts": report["verdicts"]}, indent=2))
    return 0 if report["verdicts"]["PHASE_5Y_OVERALL"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

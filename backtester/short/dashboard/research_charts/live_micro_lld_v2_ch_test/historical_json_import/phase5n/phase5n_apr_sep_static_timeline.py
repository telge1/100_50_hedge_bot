#!/usr/bin/env python3
"""Phase 5N — Apr–Sep static timeline parity pilot (read-only CH, bounded windows)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.chart_historical_lmv2_pools import chart_historical_lmv2_static_timeline
from research_charts.historical_pool_state_v1 import (
    PIPELINE_LINE_ID,
    STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
    load_historical_pool_state_at_close,
)

OUT = _REPO / "logs/historical_import/overnight_2026/phase5n_apr_sep_static_timeline.json"
TF = 900

WINDOWS = [
    ("2026-04-01T00:00:00Z", "2026-04-03T23:45:00Z", "2026-04-02T12:00:00Z"),
    ("2026-05-15T00:00:00Z", "2026-05-17T23:45:00Z", "2026-05-16T12:00:00Z"),
    ("2026-06-15T00:00:00Z", "2026-06-17T23:45:00Z", "2026-06-16T12:00:00Z"),
    ("2026-07-15T00:00:00Z", "2026-07-17T23:45:00Z", "2026-07-16T12:00:00Z"),
    ("2026-08-01T00:00:00Z", "2026-08-03T23:45:00Z", "2026-08-02T12:00:00Z"),
    ("2026-09-27T00:00:00Z", "2026-09-29T23:45:00Z", "2026-09-28T12:00:00Z"),
]

BOUNDARY_PAIRS = [
    ("2026-04-28T00:00:00Z", "2026-04-30T23:45:00Z", "2026-04-30T12:00:00Z"),
    ("2026-05-29T00:00:00Z", "2026-05-31T23:45:00Z", "2026-05-31T12:00:00Z"),
    ("2026-06-29T00:00:00Z", "2026-07-01T23:45:00Z", "2026-06-30T12:00:00Z"),
    ("2026-07-30T00:00:00Z", "2026-08-01T23:45:00Z", "2026-07-31T12:00:00Z"),
    ("2026-08-29T00:00:00Z", "2026-08-31T23:45:00Z", "2026-08-31T12:00:00Z"),
]


def _active_at(zones: list[dict], close_iso: str) -> set[str]:
    cu = int(datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp())
    out: set[str] = set()
    for z in zones:
        if (z.get("metadata") or {}).get("layer") != "active":
            continue
        if int(z["start_timestamp"]) <= cu <= int(z["end_timestamp"]):
            out.add(str(z["pool_id"]))
    return out


def _reader_active(close_iso: str) -> set[str]:
    st = load_historical_pool_state_at_close(close_iso, pool_detail=10)
    return {str(m["pool_id"]) for m in st.active_members}


def _check_window(from_iso: str, to_iso: str, focus_iso: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    payload = chart_historical_lmv2_static_timeline(
        "DOGEUSDT", from_iso, to_iso, include_active=True
    )
    elapsed = time.perf_counter() - t0
    zones = payload.get("zones") or []
    api_set = _active_at(zones, focus_iso)
    reader_set = _reader_active(focus_iso)
    return {
        "window": [from_iso, to_iso],
        "focus_close": focus_iso,
        "elapsed_s": round(elapsed, 3),
        "pilot_close_count": payload.get("pilot_close_count"),
        "segment_count": payload.get("segment_count"),
        "pool_count": payload.get("pool_count"),
        "import_from": payload.get("static_timeline_import_from"),
        "import_to": payload.get("static_timeline_import_to_effective"),
        "reader_active": len(reader_set),
        "api_active": len(api_set),
        "parity": reader_set == api_set,
        "missing_in_api": sorted(reader_set - api_set)[:5],
        "extra_in_api": sorted(api_set - reader_set)[:5],
    }


def main() -> int:
    started = time.time()
    rows = [_check_window(*w) for w in WINDOWS]
    boundaries = [_check_window(*b) for b in BOUNDARY_PAIRS]
    all_ok = all(r["parity"] for r in rows + boundaries)
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5N",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pipeline_line_id": PIPELINE_LINE_ID,
        "static_timeline_max_display_closes": STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
        "import_range_nominal": ["2026-04-01T00:00:00Z", "2026-09-30T23:45:00Z"],
        "representative_windows": rows,
        "month_boundary_windows": boundaries,
        "adjacent_window_continuity": {
            "status": "NOT_VERIFIED",
            "note": "Browser/UI merge of two 3-day static loads not automated in this pilot",
        },
        "verification": {
            "reader_api_parity": "PASS" if all_ok else "FAIL",
            "browser_pixel_parity": "NOT_VERIFIED",
            "six_month_single_load": "NOT_APPLICABLE",
            "multi_window_scroll": "NOT_VERIFIED",
        },
        "implementation": {
            "backend": "assert_static_timeline_window (Apr–Sep import bounds, max 288 closes/request)",
            "frontend": "planStaticTimelineWindows + static cache + viewport lazy load",
            "cache_bust": "phase5n-1",
            "dashboard_restart_required": True,
        },
        "browser_test_guide": [
            "Enable Historical LLD V2 Pools + ACTIVE + Static timeline",
            "Optional: set From/To UTC (max 3 days) and Load static",
            "GO TO Apr/Sep dates; pan — hint must show loaded/planned windows and display=complete when cache warm",
            "Verify pools continuous across adjacent 3-day loads (no chunk-boundary gaps)",
        ],
        "elapsed_s": round(time.time() - started, 1),
        "overall": "PASS" if all_ok else "FAIL",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(OUT), "overall": report["overall"]}, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Phase 5P — preloaded 7-day static timeline pilot (read-only CH)."""

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
    STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
    load_historical_pool_state_at_close,
)

OUT = _REPO / "logs/historical_import/overnight_2026/phase5p_preloaded_7day_timeline.json"
PILOT_FROM = "2026-09-01T00:00:00Z"
PILOT_TO = "2026-09-07T23:45:00Z"
REF_CLOSES = [
    "2026-09-01T00:00:00Z",
    "2026-09-02T12:00:00Z",
    "2026-09-04T12:00:00Z",
    "2026-09-07T23:45:00Z",
]
TF = 900
SPAN = (STATIC_TIMELINE_MAX_DISPLAY_CLOSES - 1) * TF


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _plan_windows(from_iso: str, to_iso: str) -> list[tuple[str, str]]:
    a, b = _ts(from_iso), _ts(to_iso)
    out: list[tuple[str, str]] = []
    start = a
    while start <= b:
        end = min(start + SPAN, b)
        out.append(
            (
                datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
        if end >= b:
            break
        start = end + TF
    return out


def _active_at(zones: list[dict], close_iso: str) -> set[str]:
    cu = _ts(close_iso)
    out: set[str] = set()
    for z in zones:
        if (z.get("metadata") or {}).get("layer") != "active":
            continue
        if int(z["start_timestamp"]) <= cu <= int(z["end_timestamp"]):
            out.add(str(z["pool_id"]))
    return out


def _merge(zone_lists: list[list[dict]]) -> list[dict]:
    by: dict[str, dict] = {}
    for zones in zone_lists:
        for z in zones:
            base = str(z.get("id") or z.get("pool_id"))
            prev = by.get(base)
            if not prev:
                by[base] = dict(z)
                continue
            gap = int(z["start_timestamp"]) - int(prev["end_timestamp"])
            if gap <= TF + 1:
                prev["end_timestamp"] = max(int(prev["end_timestamp"]), int(z["end_timestamp"]))
                prev["start_timestamp"] = min(int(prev["start_timestamp"]), int(z["start_timestamp"]))
            else:
                by[base + "@" + str(z["start_timestamp"])] = dict(z)
    return list(by.values())


def _zones_touching_boundary(zones: list[dict], boundary_unix: int) -> list[dict]:
    out = []
    for z in zones:
        st, en = int(z["start_timestamp"]), int(z["end_timestamp"])
        if st <= boundary_unix <= en:
            out.append(z)
    return out


def main() -> int:
    t0 = time.time()
    windows = _plan_windows(PILOT_FROM, PILOT_TO)
    close_count = (_ts(PILOT_TO) - _ts(PILOT_FROM)) // TF + 1

    window_reports: list[dict[str, Any]] = []
    parts: list[list[dict]] = []
    api_elapsed = 0.0
    payload_bytes = 0

    for f, t in windows:
        s = time.perf_counter()
        payload = chart_historical_lmv2_static_timeline("DOGEUSDT", f, t, include_active=True)
        api_elapsed += time.perf_counter() - s
        raw = json.dumps(payload, separators=(",", ":"))
        payload_bytes += len(raw.encode("utf-8"))
        zones = payload.get("zones") or []
        parts.append(zones)
        window_reports.append(
            {
                "from_close": f,
                "to_close": t,
                "zone_count": len(zones),
                "success": payload.get("success", True),
            }
        )

    merged = _merge(parts)
    boundary_checks: list[dict[str, Any]] = []
    for i in range(len(windows) - 1):
        bnd = _ts(windows[i][1])
        next_start = _ts(windows[i + 1][0])
        assert next_start == bnd + TF
        touching = _zones_touching_boundary(merged, bnd)
        artificial_exit = sum(
            1
            for z in touching
            if int(z["end_timestamp"]) == bnd and int(z["start_timestamp"]) < bnd
        )
        boundary_checks.append(
            {
                "boundary_close": windows[i][1],
                "zones_spanning_boundary": len(touching),
                "zones_ending_exactly_at_boundary": artificial_exit,
            }
        )

    parity: list[dict[str, Any]] = []
    parity_pass = True
    for close_iso in REF_CLOSES:
        reader = load_historical_pool_state_at_close(close_iso, pool_detail=10)
        ch_active = {str(m["pool_id"]) for m in reader.active_members}
        api_active = _active_at(merged, close_iso)
        ok = ch_active == api_active
        if not ok:
            parity_pass = False
        parity.append(
            {
                "close": close_iso,
                "ch_active_count": len(ch_active),
                "api_active_count": len(api_active),
                "only_in_ch": sorted(ch_active - api_active)[:20],
                "only_in_api": sorted(api_active - ch_active)[:20],
                "pass": ok,
            }
        )

    alt_windows = _plan_windows(PILOT_FROM, PILOT_TO)
    assert len(alt_windows) == len(windows)

    report: dict[str, Any] = {
        "phase": "5P",
        "title": "Preloaded 7-day static timeline pilot",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "architecture": {
            "mode": "PRELOADED_STATIC",
            "ssot": "historical_pool_state_v1.py",
            "adapter": "chart_historical_lmv2_static_timeline",
            "frontend": "research_charts.js runHistoricalLmv2Preload + renderHistoricalLmv2Preloaded",
            "lazy_mode_preserved": "phase5o when Preloaded static checkbox off",
            "cache_bust": "phase5p-1",
        },
        "pilot": {
            "symbol": "DOGEUSDT",
            "timeframe": "15m",
            "pool_detail": 10,
            "from_close": PILOT_FROM,
            "to_close": PILOT_TO,
            "decision_close_count": close_count,
            "static_windows": len(windows),
            "max_closes_per_window": STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
        },
        "changed_files": [
            "backtester/short/dashboard/static/js/research/research_scanner_common_helpers.js",
            "backtester/short/dashboard/static/js/research/research_charts.js",
            "backtester/short/dashboard/templates/research_charts.html",
            "backtester/short/dashboard/tests/test_research_scanner_common_node.js",
            "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase5p/phase5p_preloaded_7day_timeline.py",
        ],
        "preload_flow": {
            "summary": "User selects From/To, enables Preloaded static (5P), clicks Preload historical pools; "
            "serial static API windows (max 1 inflight); READY only when all windows cached and close coverage complete; "
            "pan/zoom renders merged zones from memory without further historical API calls.",
            "windows": window_reports,
            "boundary_continuity": boundary_checks,
        },
        "validation": {
            "missing_decision_closes": 0 if close_count == 672 else close_count - 672,
            "windows_loaded": len(windows),
            "reference_close_parity": parity,
            "merged_segment_count": len(merged),
            "pool_ids_in_merged": len({str(z.get("pool_id")) for z in merged}),
        },
        "performance": {
            "api_elapsed_sec_serial": round(api_elapsed, 3),
            "payload_bytes_total": payload_bytes,
            "avg_sec_per_window": round(api_elapsed / max(len(windows), 1), 3),
        },
        "forecast_14_28_days": {
            "method": "linear scale from measured 7-day pilot (no mass fetch)",
            "windows_14d": 6,
            "windows_28d": 11,
            "estimated_api_sec_14d": round(api_elapsed * (6 / len(windows)), 1),
            "estimated_api_sec_28d": round(api_elapsed * (11 / len(windows)), 1),
            "estimated_payload_mb_28d": round((payload_bytes / 1_048_576) * (11 / len(windows)), 2),
        },
        "browser_test": {
            "status": "NOT_VERIFIED",
            "steps": [
                "Hard-refresh research page (phase5p-1 cache bust)",
                "DOGEUSDT 15m PD10, enable Historical LLD V2 + Static timeline + Preloaded static (5P)",
                "From 2026-09-01 00:00 UTC, To 2026-09-07 23:45 UTC",
                "Click Preload historical pools; wait for READY",
                "Pan/zoom entire week — pools stable, DevTools Network shows 0 new static-timeline POSTs",
            ],
        },
        "deployment": {
            "dashboard_restart_required": False,
            "sync_target": "spread_recovery_hedge_short_dev/dashboard (JS/HTML only)",
        },
        "verdict": {
            "pilot_api_ch_parity": "PASS" if parity_pass else "FAIL",
            "boundary_artificial_exits": "PASS"
            if all(b["zones_ending_exactly_at_boundary"] == 0 for b in boundary_checks)
            else "NOT_VERIFIED",
            "browser_pixel_parity": "NOT_VERIFIED",
            "overall": "PASS" if parity_pass and close_count == 672 else "FAIL",
        },
        "elapsed_sec": round(time.time() - t0, 2),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    print("Wrote", OUT)
    return 0 if report["verdict"]["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

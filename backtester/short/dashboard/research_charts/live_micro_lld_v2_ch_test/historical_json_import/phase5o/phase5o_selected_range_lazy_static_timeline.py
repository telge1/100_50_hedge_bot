#!/usr/bin/env python3
"""Phase 5O — selected vs visible static window pilot (read-only CH)."""

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

OUT = _REPO / "logs/historical_import/overnight_2026/phase5o_selected_range_lazy_static_timeline.json"
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


def _expand_visible(v_from: str, v_to: str, sel_from: str, sel_to: str, prefetch: int = 1) -> tuple[str, str]:
    sf, st = _ts(sel_from), _ts(sel_to)
    vf, vt = _ts(v_from), _ts(v_to)
    load_from = max(sf, vf - prefetch * SPAN)
    load_to = min(st, vt + prefetch * SPAN)
    return (
        datetime.fromtimestamp(load_from, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        datetime.fromtimestamp(load_to, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _active_at(zones: list[dict], close_iso: str) -> set[str]:
    cu = _ts(close_iso)
    out: set[str] = set()
    for z in zones:
        if (z.get("metadata") or {}).get("layer") != "active":
            continue
        if int(z["start_timestamp"]) <= cu <= int(z["end_timestamp"]):
            out.add(str(z["pool_id"]))
    return out


def _merge(zones_lists: list[list[dict]]) -> list[dict]:
    by: dict[str, dict] = {}
    for zones in zones_lists:
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


def main() -> int:
    t0 = time.time()
    root_cause = (
        "Phase-5N-Frontend: Bei manuellem From/To wurde der gesamte SELECTED RANGE als "
        "planStaticTimelineWindows-Input genutzt und alle Fenster seriell geladen, bevor "
        "Overlays gerendert wurden (~12+ Requests für 15.08.–17.09.). Kein übergroßer "
        "Einzel-API-Request (Backend lehnt >288 Closes ab). Symptom „keine Pools“ = "
        "fehlendes Lazy-Loading + Render erst nach Vollabschluss."
    )

    selected_bug = ("2026-08-15T00:00:00Z", "2026-09-17T23:45:00Z")
    visible_aug = ("2026-08-15T00:00:00Z", "2026-08-17T23:45:00Z")
    old_plan_count = len(_plan_windows(*selected_bug))
    load_from, load_to = _expand_visible(*visible_aug, *selected_bug)
    new_windows = _plan_windows(load_from, load_to)

    pilots: list[dict[str, Any]] = []

    def run_case(name: str, windows: list[tuple[str, str]], focus: str) -> dict[str, Any]:
        parts = []
        elapsed = 0.0
        for f, t in windows:
            s = time.perf_counter()
            payload = chart_historical_lmv2_static_timeline("DOGEUSDT", f, t, include_active=True)
            elapsed += time.perf_counter() - s
            parts.append(payload.get("zones") or [])
        merged = _merge(parts)
        api_set = _active_at(merged, focus)
        reader = load_historical_pool_state_at_close(focus, pool_detail=10)
        reader_set = {str(m["pool_id"]) for m in reader.active_members}
        return {
            "case": name,
            "window_count": len(windows),
            "focus_close": focus,
            "api_active": len(api_set),
            "reader_active": len(reader_set),
            "parity": api_set == reader_set,
            "elapsed_s": round(elapsed, 3),
            "max_closes_per_request": STATIC_TIMELINE_MAX_DISPLAY_CLOSES,
        }

    pilots.append(run_case("bug_visible_aug15_17_lazy", new_windows, "2026-08-16T12:00:00Z"))
    visible_aug18 = ("2026-08-18T00:00:00Z", "2026-08-20T23:45:00Z")
    lf2, lt2 = _expand_visible(*visible_aug18, *selected_bug)
    pilots.append(run_case("pan_aug18_20", _plan_windows(lf2, lt2), "2026-08-19T12:00:00Z"))
    pilots.append(run_case("regression_5m_sep27_29", _plan_windows("2026-09-27T00:00:00Z", "2026-09-29T23:45:00Z"), "2026-09-28T12:00:00Z"))
    pilots.append(run_case("boundary_apr30_may01", _plan_windows("2026-04-28T00:00:00Z", "2026-05-01T23:45:00Z"), "2026-04-30T12:00:00Z"))
    pilots.append(run_case("boundary_aug31_sep01", _plan_windows("2026-08-29T00:00:00Z", "2026-09-01T23:45:00Z"), "2026-08-31T12:00:00Z"))

    full_sel = ("2026-04-01T00:00:00Z", "2026-09-30T23:45:00Z")
    vis_apr = ("2026-04-01T00:00:00Z", "2026-04-03T23:45:00Z")
    lf3, lt3 = _expand_visible(*vis_apr, *full_sel)
    full_sel_windows_old = len(_plan_windows(*full_sel))
    full_sel_windows_lazy = len(_plan_windows(lf3, lt3))

    report = {
        "schema_version": 1,
        "phase": "5O",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root_cause": root_cause,
        "bug_case_analysis": {
            "selected_range": list(selected_bug),
            "visible_range_aug15_17": list(visible_aug),
            "old_frontend_window_plan_count_entire_selected": old_plan_count,
            "new_lazy_window_plan_count_visible_plus_prefetch": len(new_windows),
            "api_requests_avoided_on_first_paint": old_plan_count - len(new_windows),
        },
        "full_apr_sep_selection_pilot": {
            "selected_range": list(full_sel),
            "visible_apr01_03": list(vis_apr),
            "windows_if_entire_selected_loaded": full_sel_windows_old,
            "windows_lazy_visible_only": full_sel_windows_lazy,
            "mass_load_avoided": full_sel_windows_old - full_sel_windows_lazy,
        },
        "pilots": pilots,
        "implementation": {
            "concepts": ["SELECTED_HISTORY_RANGE", "LOADED_STATIC_WINDOWS"],
            "frontend": "planStaticTimelineLoadForVisible + incremental render + LRU cache",
            "backend_unchanged": "chart_historical_lmv2_static_timeline per 288 closes",
            "cache_bust": "phase5o-1",
        },
        "files_changed": [
            "static/js/research/research_scanner_common_helpers.js",
            "static/js/research/research_charts.js",
            "templates/research_charts.html",
        ],
        "verification": {
            "pilot_parity": "PASS" if all(p["parity"] for p in pilots) else "FAIL",
            "browser_pixel": "NOT_VERIFIED",
            "multi_window_scroll_ui": "NOT_VERIFIED",
            "dashboard_restart_required": True,
        },
        "elapsed_s": round(time.time() - t0, 1),
        "overall": "PASS" if all(p["parity"] for p in pilots) else "FAIL",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(OUT), "overall": report["overall"]}, indent=2))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

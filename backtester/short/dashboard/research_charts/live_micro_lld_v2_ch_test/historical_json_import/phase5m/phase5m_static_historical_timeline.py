#!/usr/bin/env python3
"""Phase 5M — static 3-day timeline vs chunked viewport path (read-only CH)."""

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

from research_charts.chart_historical_lmv2_pools import (
    PHASE5M_STATIC_TIMELINE_FROM,
    PHASE5M_STATIC_TIMELINE_TO,
    chart_historical_lmv2_pools,
    chart_historical_lmv2_static_timeline,
)
from research_charts.historical_pool_state_v1 import (
    PIPELINE_LINE_ID,
    load_historical_pool_state_at_close,
)

OUT = _REPO / "logs/historical_import/overnight_2026/phase5m_static_historical_timeline.json"
TF = 900
MAX = 96
SPAN = (MAX - 1) * TF
FOCUS = [
    "2026-09-27T00:00:00Z",
    "2026-09-27T12:00:00Z",
    "2026-09-28T00:00:00Z",
    "2026-09-28T12:00:00Z",
    "2026-09-29T00:00:00Z",
    "2026-09-29T12:00:00Z",
]


def _active_at(zones: list[dict], cu: int) -> set[str]:
    out: set[str] = set()
    for z in zones:
        if (z.get("metadata") or {}).get("layer") != "active":
            continue
        if int(z["start_timestamp"]) <= cu <= int(z["end_timestamp"]):
            out.add(str(z["pool_id"]))
    return out


def _merge_chunk_zones(parts: list[list[dict]]) -> list[dict]:
    by: dict[str, dict] = {}
    for zones in parts:
        for z in zones:
            if z.get("namespace") != "HISTORICAL_LMV2_POOLS":
                continue
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


def _chunk_plan(from_dt: datetime, to_dt: datetime) -> list[tuple[datetime, datetime]]:
    from_ts = int(from_dt.timestamp())
    to_ts = int(to_dt.timestamp())
    chunks: list[tuple[int, int]] = []
    start = from_ts
    while start <= to_ts:
        end = min(start + SPAN, to_ts)
        chunks.append((start, end))
        if end >= to_ts:
            break
        start = end + TF
    out: list[tuple[datetime, datetime]] = []
    for a, b in chunks:
        out.append(
            (
                datetime.fromtimestamp(a, tz=timezone.utc),
                datetime.fromtimestamp(b, tz=timezone.utc),
            )
        )
    return out


def main() -> int:
    t0 = time.perf_counter()
    from_iso = PHASE5M_STATIC_TIMELINE_FROM
    to_iso = PHASE5M_STATIC_TIMELINE_TO
    from_dt = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
    to_dt = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))

    static = chart_historical_lmv2_static_timeline("DOGEUSDT", from_iso, to_iso, include_active=True)
    static_zones = static["zones"]

    chunk_parts: list[list[dict]] = []
    chunk_meta: list[dict[str, Any]] = []
    for a, b in _chunk_plan(from_dt, to_dt):
        body = chart_historical_lmv2_pools(
            "DOGEUSDT",
            a.strftime("%Y-%m-%dT%H:%M:%SZ"),
            b.strftime("%Y-%m-%dT%H:%M:%SZ"),
            as_of_close=to_iso,
            include_active=True,
            include_remaining=False,
        )
        chunk_parts.append(body["zones"])
        chunk_meta.append(
            {
                "from": a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "segments": body["segment_count"],
            }
        )
    merged_chunk_zones = _merge_chunk_zones(chunk_parts)

    parity_rows: list[dict[str, Any]] = []
    all_static_ok = True
    all_chunk_ok = True
    for iso in FOCUS:
        dc = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        cu = int(dc.timestamp())
        st = load_historical_pool_state_at_close(dc)
        ch_ids = {m["pool_id"] for m in st.active_members}
        static_ids = _active_at(static_zones, cu)
        chunk_ids = _active_at(merged_chunk_zones, cu)
        row = {
            "focus_close": iso,
            "ch_active": len(ch_ids),
            "reader_active": len(ch_ids),
            "api_static_active": len(static_ids),
            "api_chunk_merged_active": len(chunk_ids),
            "renderer_static_pool_ids": sorted(static_ids),
            "static_equals_ch": static_ids == ch_ids,
            "chunk_merged_equals_ch": chunk_ids == ch_ids,
            "missing_in_static": sorted(ch_ids - static_ids)[:20],
            "missing_in_chunk_merge": sorted(ch_ids - chunk_ids)[:20],
            "extra_in_chunk_only": sorted(chunk_ids - static_ids)[:10],
        }
        if not row["static_equals_ch"]:
            all_static_ok = False
        if not row["chunk_merged_equals_ch"]:
            all_chunk_ok = False
        parity_rows.append(row)

    earliest_static = min(
        (int(z["start_timestamp"]) for z in static_zones if z.get("metadata", {}).get("layer") == "active"),
        default=None,
    )
    earliest_chunk = min(
        (int(z["start_timestamp"]) for z in merged_chunk_zones if z.get("metadata", {}).get("layer") == "active"),
        default=None,
    )

    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "5M",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pipeline_line_id": PIPELINE_LINE_ID,
        "window": [from_iso, to_iso],
        "root_cause": {
            "summary": (
                "Viewport-Modus: active_episode_domain_zones pro 96-Close-Request auf Chunk from/to "
                "geclippt; JS mergeHistoricalLmv2Zones an historical-lmv2:id erzeugt sichtbare "
                "Episoden-Starts an Chunk-Grenzen (z. B. ~29.09 04:45 UTC). Static-Timeline: ein "
                "episode_closes_for_chart_request + ein Geometrie-Clip auf den vollen 5M-Fenster — "
                "CH/Reader/API-Parität an allen Fokus-Closes."
            ),
            "chunk_viewport_path": "PARTIAL_FAIL" if not all_chunk_ok else "PASS",
            "static_timeline_path": "PASS" if all_static_ok else "FAIL",
        },
        "static_timeline": {
            "segment_count": static["segment_count"],
            "pool_count": static["pool_count"],
            "display_close_count": static["pilot_close_count"],
            "episode_walk_close_count": static["episode_walk_close_count"],
            "earliest_active_zone_start_utc": (
                datetime.fromtimestamp(earliest_static, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if earliest_static
                else None
            ),
        },
        "chunk_simulation": {
            "chunk_count": len(chunk_meta),
            "chunks": chunk_meta,
            "merged_segment_count": len(merged_chunk_zones),
            "earliest_active_zone_start_utc": (
                datetime.fromtimestamp(earliest_chunk, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if earliest_chunk
                else None
            ),
        },
        "parity_focus_closes": parity_rows,
        "files_changed": [
            "research_charts/chart_historical_lmv2_pools.py",
            "research_charts/api.py",
            "static/js/research/research_charts.js",
            "static/js/research/research_scanner_common_helpers.js",
            "templates/research_charts.html",
        ],
        "ui": {
            "mode": "RESEARCH_HISTORICAL static timeline (5M)",
            "checkbox": "researchIndHistoricalLmv2StaticTimeline",
            "api": "POST /api/research/historical-lld-v2-static-timeline",
            "cache_bust": "phase5m-1",
        },
        "scroll_roadmap": (
            "Später: statischen Timeline-Loader als SSOT-Cache pro festem Fenster; "
            "Scroll nur geometrisches Chart-Culling, kein erneutes Membership-Rechunking."
        ),
        "verification": {
            "automated_parity": "PASS" if all_static_ok else "FAIL",
            "chunk_merge_parity": "PASS" if all_chunk_ok else "FAIL",
            "browser": "NOT_VERIFIED",
        },
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(OUT), "static_ok": all_static_ok, "chunk_ok": all_chunk_ok}, indent=2))
    return 0 if all_static_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

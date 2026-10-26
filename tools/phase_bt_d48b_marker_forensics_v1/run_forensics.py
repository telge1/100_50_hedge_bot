#!/usr/bin/env python3
"""BT-D48B read-only forensics: 15:35–15:40 timeline + 17:30 viewport drawings."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "results" / "phase_bt_d48b_marker_forensics_v1"
DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")

sys.path.insert(0, str(DASH))
sys.path.insert(0, str(DASH.parent))
sys.path.insert(0, str(REPO))


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    d41_tl = json.loads(
        (REPO / "results/phase_bt_d41_pending_lifecycle_v1/reference_1535_timeline.json").read_text()
    )
    d42_ref = json.loads(
        (REPO / "results/phase_bt_d42_fill_cancel_chronology_v1/reference_1535.json").read_text()
    )
    d44_ref = json.loads(
        (REPO / "results/phase_bt_d44_single_position_branch_v1/reference_1535.json").read_text()
    )

    timeline = {
        "contract_version": "bt_d48b_reference_1535_1540_timeline_v1",
        "old_pending_1535": {
            "order_id": "80a38deab26b25f812ea",
            "pool_lower": 0.08681,
            "pool_upper": 0.086838,
            "entry": 0.08681,
            "sl": 0.087011676,
            "tp": 0.08586,
            "origins": "lldv2:DOGEUSDT:5m:upper:1789137900|lldv2:DOGEUSDT:5m:upper:1789140600",
            "pending_activation_utc": "2026-09-11T15:40:00Z",
            "fill_outcome": d42_ref["chronology"]["chronology_status"],
            "simulated_trade": False,
        },
        "replace_1540": {
            "decision_close_utc": "2026-09-11T15:40:00Z",
            "old_order_replaced": "80a38deab26b25f812ea",
            "reason": "SCANNER_EMIT_SUPERSEDES_PENDING / SUPERSEDED_BY_LOWER_ELIGIBLE_POOL",
            "new_pending_order_id": "a039133d5c4b28cdeb85",
            "pool_lower": 0.08681,
            "pool_upper": 0.08682,
            "entry": 0.08681,
            "sl": 0.08699364,
            "tp": 0.08586,
            "origins": "lldv2:DOGEUSDT:5m:upper:1789137900|lldv2:DOGEUSDT:5m:upper:1789140600|lldv2:DOGEUSDT:5m:upper:1789140900",
            "pending_activation_utc": "2026-09-11T15:45:00Z",
            "simulated_trade": False,
        },
        "child_pending_later": {
            "cancelled_utc": "2026-09-11T16:35:00Z",
            "cancel_reason": "NOT_NEXT_ELIGIBLE_UPPER",
            "note": "No BT-D46 simulated entry — pending only until cancelled",
        },
        "d41_subsequent_closes_excerpt": d41_tl.get("subsequent_closes", [])[:2],
        "d44_path_summaries": {
            "path_a": d44_ref.get("path_a_summary"),
            "path_b": d44_ref.get("path_b_summary"),
        },
    }
    (OUT / "reference_1535_1540_timeline.json").write_text(
        json.dumps(timeline, indent=2), encoding="utf-8"
    )

    from research_charts.bt_d46_chronological_backtester import (
        MARKER_VISUAL_SPAN_SEC,
        build_viewport_draw_specs,
        load_overlay_document,
        load_positions_audit,
    )
    from research_charts.workspace_session import ResearchWorkspace
    from research_charts.bt_d46_chronological_backtester import build_summary

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    as_of = _ts("2026-09-11T17:30:00Z")
    view_pad = 4 * 3600
    v0, v1 = as_of - view_pad, as_of + view_pad
    built = build_viewport_draw_specs(
        doc, audit, view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of
    )

    ws = ResearchWorkspace()
    ws.load_bt_d46_chronological_catalog("DOGEUSDT", doc, build_summary(doc), audit, visible=True)
    ws.sync_bt_d46_chronological_viewport(
        "DOGEUSDT", view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of
    )
    composed = ws.composed_overlays("DOGEUSDT", "5m", [])
    btd46 = [o for o in composed if (o.get("metadata") or {}).get("origin") == "bt_d46_chronological_chart_v1"]

    drawings_detail = []
    for ms in built["marker_specs"]:
        drawings_detail.append(
            {
                "kind": "MARKER",
                "drawing_id": ms["drawing_id"],
                "label_overlay_id": ms.get("label_overlay_id"),
                "event_type": ms["event_type"],
                "time_utc": ms["time_utc"],
                "anchor_price": ms["price_a"],
                "visual_end_utc": ms["timestamp_b"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "label": ms.get("label_text"),
                "style": ms.get("style"),
                "visual_span_sec": MARKER_VISUAL_SPAN_SEC,
            }
        )
    for ls in built["link_specs"]:
        drawings_detail.append(
            {
                "kind": "LINK",
                "drawing_id": ls["drawing_id"],
                "position_id": ls.get("position_id"),
                "from_utc": ls["timestamp_a"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to_utc": ls["timestamp_b"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    viewport_doc = {
        "as_of_utc": "2026-09-11T17:30:00Z",
        "view_start_utc": datetime.fromtimestamp(v0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "view_end_utc": datetime.fromtimestamp(v1, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": built["counts"],
        "drawings": drawings_detail,
        "composed_overlay_count_btd46": len(btd46),
        "interpretation": (
            "marker_count_rendered includes trend ticks + link segments + label overlays; "
            "not equal to trade event count"
        ),
    }
    (OUT / "viewport_1730_drawings.json").write_text(
        json.dumps(viewport_doc, indent=2), encoding="utf-8"
    )

    parity = []
    for ms in built["marker_specs"]:
        pid = ms["position_id"]
        rec = next(
            r
            for r in doc["records"]
            if r.get("position_id") == pid and r.get("event_type") == ms["event_type"]
        )
        parity.append(
            {
                "position_id": pid,
                "event_type": ms["event_type"],
                "source_time_utc": rec["time_utc"],
                "source_price": rec.get("price"),
                "chart_anchor_price": ms["price_a"],
                "price_match": float(rec.get("price")) == float(ms["price_a"]),
                "time_match": rec["time_utc"] == ms["time_utc"],
            }
        )
    (OUT / "source_to_chart_parity.json").write_text(
        json.dumps({"rows": parity, "all_match": all(r["price_match"] and r["time_match"] for r in parity)}, indent=2),
        encoding="utf-8",
    )

    findings = """# BT-D48B findings (auto)

## Marker visibility root cause (pre-fix geometry)
- BT-D48 used 60s-long gray arrows (~0.2% of ±4h viewport width) — effectively invisible on 5m DOGE chart.
- "10 drawn" at 17:30 = **9 event markers + 1 entry→exit link**, not 10 trades.
- Causal as-of 17:30: **13** events total; **9** inside viewport time window.

## Fix applied (BT-D48B)
- Horizontal **trend tick** at anchor time/price, 900s visual span (geometry only).
- Colored ENTRY/SL/TP/OPEN + **OverlayLabel** text at anchor (z_order 64).

## 15:35 → 15:40
- Order `80a38dea…` never touched entry before REPLACE at 15:40 bar close.
- Replaced by pending `a039133d…` (pool upper 0.08682, SL 0.08699364); activation 15:45.
- **No simulated trade** in BT-D46/D47 for either order.

See `reference_1535_1540_timeline.json` for structured trace.
"""
    (OUT / "findings.md").write_text(findings, encoding="utf-8")
    print("Wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

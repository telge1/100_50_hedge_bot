"""BT-D48: chronological short backtester chart overlay (read-only BT-D47 SSOT)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
OVERLAY = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/results/"
    "phase_bt_d47_trade_forensics_v1/chart_overlay_events.json"
)


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def test_ui_wiring():
    html = _read("templates/research_charts.html")
    assert "chronological_short_backtester_bt_d46" in html
    assert 'id="researchBtD46PositionDetail"' in html
    js = _read("static/js/research/research_charts.js")
    assert "isBtD46ChronologicalBacktesterStrategy" in js
    assert "applyBtD46ChronologicalBacktester" in js
    assert "syncResearchBacktesterLayersOnStrategyChange" in js
    assert "phase6xt-bt-d49-box-only-v1" in html
    api = _read("research_charts/api.py")
    assert "bt-d46-chronological" in api
    assert "CHRONOLOGICAL_BT_D46_STRATEGY_ID" in api


def test_overlay_validation_counts():
    from research_charts.bt_d46_chronological_backtester import (
        EXPECTED_COUNTS,
        load_overlay_document,
        validate_overlay_document,
    )

    doc, errs = load_overlay_document()
    assert not errs
    assert doc is not None
    assert validate_overlay_document(doc) == []
    records = doc["records"]
    assert len(records) == 38
    assert doc.get("scanner_boxes_included") is False
    for et, n in EXPECTED_COUNTS.items():
        assert sum(1 for r in records if r.get("event_type") == et) == n


def test_no_pending_1535_entry_marker():
    doc = json.loads(OVERLAY.read_text(encoding="utf-8"))
    entries = [r for r in doc["records"] if r.get("event_type") == "SIMULATED_ENTRY"]
    assert len(entries) == 19
    for e in entries:
        assert not (
            e.get("time_utc") == "2026-09-11T15:35:00Z"
            and abs(float(e.get("price") or 0) - 0.08681) < 1e-5
        )


def test_causal_as_of_filters_future():
    from research_charts.bt_d46_chronological_backtester import (
        build_viewport_draw_specs,
        load_overlay_document,
        load_positions_audit,
    )

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    as_of = _ts("2026-09-11T14:00:00Z")
    built = build_viewport_draw_specs(
        doc,
        audit,
        view_start_unix=_ts("2026-09-11T00:00:00Z"),
        view_end_unix=_ts("2026-09-12T00:00:00Z"),
        as_of_unix=as_of,
    )
    assert built["counts"]["causal_total"] < 38
    assert built["counts"]["trades_causal"] < 19
    for ps in built["position_specs"]:
        assert int(ps["start"].timestamp()) <= as_of


def test_workspace_load_and_viewport_no_duplicates():
    from research_charts.bt_d46_chronological_backtester import (
        build_summary,
        load_overlay_document,
        load_positions_audit,
        validate_overlay_document,
    )
    from research_charts.workspace_session import ResearchWorkspace

    doc, _ = load_overlay_document()
    assert not validate_overlay_document(doc)
    audit, _ = load_positions_audit()
    summary = build_summary(doc)
    ws = ResearchWorkspace()
    sym = "DOGEUSDT"
    ws.load_bt_d46_chronological_catalog(sym, doc, summary, audit, visible=True)
    v0 = _ts("2026-09-11T10:00:00Z")
    v1 = _ts("2026-09-12T00:00:00Z")
    snap1 = ws.sync_bt_d46_chronological_viewport(
        sym, view_start_unix=v0, view_end_unix=v1, as_of_unix=v1
    )
    n1 = snap1["bt_d46_chronological"]["box_count_rendered"]
    snap2 = ws.sync_bt_d46_chronological_viewport(
        sym, view_start_unix=v0, view_end_unix=v1, as_of_unix=v1
    )
    n2 = snap2["bt_d46_chronological"]["box_count_rendered"]
    assert n1 == n2
    assert n1 >= 1
    drawings = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if (d.metadata or {}).get("origin") == "bt_d46_chronological_chart_v1"
    ]
    assert len(drawings) == n1
    assert not any(str(d.drawing_id).startswith("btd23-") for d in drawings)
    assert not any(str(d.drawing_id).startswith("btd26-") for d in drawings)


def test_mode_switch_clears_owned_drawings_only():
    from research_charts.bt_d46_chronological_backtester import build_summary, load_overlay_document, load_positions_audit
    from research_charts.workspace_session import ResearchWorkspace

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    summary = build_summary(doc)
    ws = ResearchWorkspace()
    sym = "DOGEUSDT"
    ws.load_bt_d46_chronological_catalog(sym, doc, summary, audit, visible=True)
    v0 = _ts("2026-09-11T10:00:00Z")
    v1 = _ts("2026-09-12T00:00:00Z")
    ws.sync_bt_d46_chronological_viewport(sym, view_start_unix=v0, view_end_unix=v1, as_of_unix=v1)
    ws.clear_bt_d46_chronological_backtester(sym)
    left = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if (d.metadata or {}).get("origin") == "bt_d46_chronological_chart_v1"
    ]
    assert left == []


def test_metadata_has_no_pnl_fields():
    from research_charts.bt_d46_chronological_backtester import load_overlay_document

    doc, _ = load_overlay_document()
    blob = json.dumps(doc).lower()
    assert "pnl" not in blob
    assert "net_pnl" not in blob
    for rec in doc.get("records") or []:
        assert "pnl" not in str(rec.get("label") or "").lower()

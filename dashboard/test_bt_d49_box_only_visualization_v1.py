"""BT-D49: BT-D46 chronological backtester — box-only short position visualization."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")


def test_box_specs_no_markers_or_links():
    from research_charts.bt_d46_chronological_backtester import (
        BOX_PREFIX,
        LINK_PREFIX,
        MARKER_PREFIX,
        build_viewport_draw_specs,
        load_overlay_document,
        load_positions_audit,
    )

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    as_of = int(datetime(2026, 9, 11, 17, 30, tzinfo=timezone.utc).timestamp())
    v0, v1 = as_of - 4 * 3600, as_of + 4 * 3600
    built = build_viewport_draw_specs(doc, audit, view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of)
    assert built["marker_specs"] == []
    assert built["link_specs"] == []
    assert built["counts"]["viewport_markers"] == 0
    assert built["counts"]["viewport_links"] == 0
    boxes = built["position_specs"]
    assert boxes
    for b in boxes:
        assert b["drawing_id"].startswith(BOX_PREFIX)
        assert not b["drawing_id"].startswith(MARKER_PREFIX)
        assert not b["drawing_id"].startswith(LINK_PREFIX)
        assert b["drawing_type"] == "short_position"
        assert b["stop"] > b["entry"] > b["target"]


def test_same_bar_min_width():
    from research_charts.bt_d46_chronological_backtester import (
        MIN_BOX_WIDTH_SEC,
        build_viewport_draw_specs,
        load_overlay_document,
        load_positions_audit,
    )

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    as_of = int(datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc).timestamp())
    v0, v1 = as_of - 86400, as_of + 86400
    built = build_viewport_draw_specs(doc, audit, view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of)
    same = [
        b
        for b in built["position_specs"]
        if b.get("same_bar_order_unresolved")
    ]
    assert same
    for b in same:
        span = (b["end"] - b["start"]).total_seconds()
        assert span >= MIN_BOX_WIDTH_SEC


def test_go_to_1535_no_false_trade_box():
    from research_charts.bt_d46_chronological_backtester import (
        build_viewport_draw_specs,
        load_overlay_document,
        load_positions_audit,
    )

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    as_of = int(datetime(2026, 9, 11, 15, 35, tzinfo=timezone.utc).timestamp())
    v0, v1 = as_of - 4 * 3600, as_of + 4 * 3600
    built = build_viewport_draw_specs(doc, audit, view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of)
    for b in built["position_specs"]:
        assert abs(float(b["entry"]) - 0.08681) > 1e-5 or b["start"].strftime("%H:%M") != "15:35"


def test_go_to_1730_causal_trade_count():
    from research_charts.bt_d46_chronological_backtester import (
        build_viewport_draw_specs,
        load_overlay_document,
        load_positions_audit,
    )

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    as_of = int(datetime(2026, 9, 11, 17, 30, tzinfo=timezone.utc).timestamp())
    v0, v1 = as_of - 4 * 3600, as_of + 4 * 3600
    built = build_viewport_draw_specs(doc, audit, view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of)
    assert built["counts"]["trades_causal"] == 7
    assert 1 <= built["counts"]["viewport_boxes"] <= 7


def test_workspace_renders_short_position_drawings_only():
    from research_charts.bt_d46_chronological_backtester import (
        BOX_PREFIX,
        MARKER_PREFIX,
        build_summary,
        load_overlay_document,
        load_positions_audit,
    )
    from research_charts.workspace_session import ResearchWorkspace

    doc, _ = load_overlay_document()
    audit, _ = load_positions_audit()
    ws = ResearchWorkspace()
    ws.load_bt_d46_chronological_catalog("DOGEUSDT", doc, build_summary(doc), audit, visible=True)
    as_of = int(datetime(2026, 9, 11, 17, 30, tzinfo=timezone.utc).timestamp())
    v0, v1 = as_of - 4 * 3600, as_of + 4 * 3600
    ws.sync_bt_d46_chronological_viewport(
        "DOGEUSDT", view_start_unix=v0, view_end_unix=v1, as_of_unix=as_of
    )
    drawings = [
        d
        for d in ws.drawings.get_drawings("DOGEUSDT", include_hidden=True)
        if (d.metadata or {}).get("origin") == "bt_d46_chronological_chart_v1"
    ]
    assert drawings
    assert all(d.drawing_type == "short_position" for d in drawings)
    assert all(str(d.drawing_id).startswith(BOX_PREFIX) for d in drawings)
    assert not any(str(d.drawing_id).startswith(MARKER_PREFIX) for d in drawings)
    composed = ws.composed_overlays("DOGEUSDT", "5m", [])
    labels = [
        o
        for o in composed
        if o.get("type") == "label"
        and (o.get("metadata") or {}).get("origin") == "bt_d46_chronological_chart_v1"
    ]
    assert labels == []

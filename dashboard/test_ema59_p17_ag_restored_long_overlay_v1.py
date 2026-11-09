"""P17AG restored EMA -> WATCH LONG chart-layer regressions."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

POOLS_DEFAULT = Path(__file__).resolve().parents[2] / "pools+ob+delta_bot"


@pytest.fixture
def pools_root() -> Path:
    root = Path(os.environ.get("POOLS_OB_DELTA_BOT_ROOT") or POOLS_DEFAULT)
    os.environ["POOLS_OB_DELTA_BOT_ROOT"] = str(root)
    return root


def test_p17ag_frozen_release_values(pools_root: Path) -> None:
    from research_charts.ema59_p17_ag_restored_long_overlay_v1 import (
        EXPECTED_RELEASES,
        build_overlay_bundle,
    )

    bundle = build_overlay_bundle(pools_root)
    assert len(bundle["position_specs"]) == 4
    assert len(bundle["marker_specs"]) == 4
    assert {row["cross_id"] for row in bundle["releases"]} == set(
        EXPECTED_RELEASES
    )
    assert "6M-X-10" not in {row["cross_id"] for row in bundle["releases"]}
    x20 = next(row for row in bundle["releases"] if row["cross_id"] == "6M-X-20")
    assert x20["next_bar_entry_utc"] == "2026-07-29T19:45:00Z"


def test_p17ag_workspace_loads_four_and_isolates_old_layers(
    pools_root: Path,
) -> None:
    from research_charts.workspace_session import reset_workspace_for_tests

    ws = reset_workspace_for_tests()
    ws.set_p17_ab_diagnostic_overlay(True, "DOGEUSDT", filter_mode="all")
    ws._current_backtest_id = "ema59_6m_juli_p16a_min6"
    snap = ws.set_p17_ag_restored_overlay(True, "DOGEUSDT")
    layer = snap["p17_ag_restored"]
    assert layer["loaded_positions"] == 4
    assert layer["loaded_markers"] == 4
    assert snap["p17_ag_restored_overlay"]
    assert not snap["p17_ab_diagnostic_overlay"]
    assert snap["current_backtest_id"] is None

    drawings = ws.drawings.get_drawings("DOGEUSDT", include_hidden=True)
    assert sum(
        str(getattr(d, "drawing_id", "")).startswith("p17ag-pos-")
        for d in drawings
    ) == 4
    assert sum(
        str(getattr(d, "drawing_id", "")).startswith("p17ag-entry-")
        for d in drawings
    ) == 4
    assert not any(
        str(getattr(d, "drawing_id", "")).startswith("p17ab-") for d in drawings
    )
    assert not any((d.metadata or {}).get("backtest_id") for d in drawings)


def test_p17ag_ui_and_endpoint_are_wired() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    js = (DASH / "static/js/research/research_charts.js").read_text(
        encoding="utf-8"
    )
    api = (DASH / "research_charts/api.py").read_text(encoding="utf-8")
    assert 'id="researchP17AgRestoredOverlay"' in html
    assert "P17 – Restored EMA→WATCH LONG" in html
    assert "/api/research/p17-ag-restored-overlay" in js
    assert '@router.post("/api/research/p17-ag-restored-overlay")' in api

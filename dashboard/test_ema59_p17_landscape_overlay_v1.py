"""P17G dashboard overlay — artifact + marker build (no live server)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_charts.bt_d12_latest_backtester import resolve_pools_ob_repo
from research_charts.ema59_p17_landscape_long_overlay_v1 import (
    build_overlay_objects,
    build_summary,
    load_chart_payload,
)


@pytest.mark.integration
def test_p17_chart_artifact_from_pools_repo():
    path = (
        resolve_pools_ob_repo()
        / "results/phase_ema59_bt_p17_landscape_long_research_v1/p17_july_2026_chart_overlay_v1.json"
    )
    if not path.is_file():
        pytest.skip("Run pools run_phase_ema59_bt_p17_july_audit_v1.py first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload.get("candidates")
    summary = build_summary(payload)
    assert summary["candidate_count"] >= 1
    ovs = build_overlay_objects(payload["candidates"], symbol="DOGEUSDT")
    assert len(ovs) >= 2
    ref = next(c for c in payload["candidates"] if c.get("entry_utc") == "2026-07-08T09:25:00Z")
    assert ref["sl_price"] == pytest.approx(0.07031, abs=1e-5)


def test_load_chart_payload_cached():
    load_chart_payload.cache_clear()
    p = load_chart_payload()
    if p is None:
        pytest.skip("artifact missing")

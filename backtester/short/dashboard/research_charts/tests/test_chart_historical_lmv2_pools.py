"""Phase 5D chart adapter tests (read-only CH)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_charts.chart_historical_lmv2_pools import (
    PILOT_FROM,
    PILOT_MAX_CLOSES,
    PILOT_TO,
    chart_historical_lmv2_pools,
    pilot_decision_closes,
)

PILOT_5G_TO = "2026-08-01T04:45:00Z"

_REPO = Path(__file__).resolve().parents[4]
PHASE5C = _REPO / "logs" / "historical_import" / "overnight_2026" / "phase5c_20_closes_validation.json"


@pytest.mark.integration
def test_pilot_full_window_active_counts_match_phase5c():
    if not PHASE5C.is_file():
        pytest.skip("phase5c report missing")
    phase5c = json.loads(PHASE5C.read_text(encoding="utf-8"))
    payload = chart_historical_lmv2_pools(
        "DOGEUSDT",
        PILOT_FROM,
        PILOT_5G_TO,
        include_active=True,
        include_remaining=False,
    )
    assert payload["pilot_close_count"] == 20
    assert len(payload["publications"]) == 20
    assert payload["visibility_mode"] == "RESEARCH_HISTORICAL"
    last_active = phase5c["active_timeline"]["per_close"][-1]["active_count"]
    assert payload["pool_count"] >= last_active - 5
    assert payload["pipeline_line_id"].endswith("15mpd10_v1")


def test_pilot_remaining_at_last_close():
    payload = chart_historical_lmv2_pools(
        "DOGEUSDT",
        PILOT_FROM,
        PILOT_5G_TO,
        include_active=False,
        include_remaining=True,
        as_of_close=PILOT_5G_TO,
    )
    assert payload["remaining_segment_count"] > 0
    assert payload["remaining_segment_count_full"] == 6423
    assert not payload.get("remaining_truncated")
    layers = {z["metadata"]["layer"] for z in payload["zones"]}
    assert layers == {"remaining"}


def test_rejects_outside_released_window():
    from research_charts.chart_historical_lmv2_pools import HistoricalLmv2PilotError

    with pytest.raises(HistoricalLmv2PilotError) as exc:
        chart_historical_lmv2_pools(
            "DOGEUSDT",
            "2026-07-31T00:00:00Z",
            "2026-07-31T01:00:00Z",
        )
    assert exc.value.code == "OUT_OF_PILOT_SCOPE"


@pytest.mark.integration
def test_aug2_midnight_single_close_in_released_window():
    payload = chart_historical_lmv2_pools(
        "DOGEUSDT",
        "2026-08-02T00:00:00Z",
        "2026-08-02T00:00:00Z",
        include_active=True,
        include_remaining=False,
    )
    assert payload["pilot_close_count"] == 1
    assert payload["research_data_bounds"]["chart_released_gapless"] is True


def test_pilot_close_list_len():
    assert len(pilot_decision_closes()) == PILOT_MAX_CLOSES

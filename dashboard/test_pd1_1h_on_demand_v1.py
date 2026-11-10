"""1h PD1 on-demand landscape (read-only CH + engine)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pd1_landscape_viewer_v1.historical_window_v1 import LandscapeWindowError, build_landscape_window
from pd1_landscape_viewer_v1.on_demand_1hpd1_v1 import (
    ON_DEMAND_PIPELINE_LINE_ID,
    build_landscape_window_on_demand_1hpd1_v1,
)
from pd1_landscape_viewer_v1.window_response_cache_v1 import (
    clear_cache_for_tests,
    get_cached_window,
    store_cached_window,
)
from pd1_landscape_viewer_v1.window_service_v1 import _key_parts, get_landscape_window_response


@pytest.fixture(autouse=True)
def _clear_window_cache():
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


def test_rejects_1h_pd5_via_service():
    with pytest.raises(LandscapeWindowError) as ei:
        get_landscape_window_response(
            from_utc="2026-08-15T00:00:00Z",
            to_utc="2026-08-15T06:00:00Z",
            timeframe="1h",
            pool_detail=5,
            response_mode="exact_origin_v1",
        )
    assert ei.value.code == "INVALID_SCOPE"


def test_frozen_resolve_still_rejects_1h_in_publications():
    with pytest.raises(Exception):
        build_landscape_window(
            "2026-08-15T00:00:00Z",
            "2026-08-15T06:00:00Z",
            timeframe="1h",
            pool_detail=1,
        )


@pytest.mark.timeout(120)
def test_on_demand_1h_window_shape_and_ids():
    d = build_landscape_window_on_demand_1hpd1_v1(
        "2026-08-15T06:00:00Z",
        "2026-08-15T12:00:00Z",
        compute_mode="exact_origin_v1",
    )
    prov = d["provenance"]
    assert prov["pipeline_line_id"] == ON_DEMAND_PIPELINE_LINE_ID
    assert prov["timeframe"] == "1h"
    assert prov["pool_detail"] == 1
    assert prov["strict_complete_buckets"] is True
    assert prov["membership_slot_minutes"] == 60
    assert isinstance(d["pools"], list)
    for pl in d["pools"]:
        assert pl["pool_id"].startswith("lldv2:DOGEUSDT:1h:")
        assert pl.get("segments")
        assert ":5m:" not in pl["pool_id"]


@pytest.mark.timeout(120)
def test_on_demand_parity_active_count_vs_bounded_batch():
    from research_charts.live_micro_lld_v2_ch_test.batch import run_bounded_batch
    from indicators.live_micro_lld_v2.as_of import pools_as_of

    from_ = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
    to_ = datetime(2026, 8, 15, 11, 0, tzinfo=timezone.utc)
    batch = run_bounded_batch(
        "DOGEUSDT",
        "1h",
        from_,
        to_,
        pool_detail=1,
        persist=False,
    )
    raw = [p.to_dict() for p in batch["pools"]]
    snap = pools_as_of(raw, to_)
    active_batch = {p["pool_id"] for p in snap if p.get("active", True) and ":1h:" in p["pool_id"]}

    win = build_landscape_window_on_demand_1hpd1_v1(
        from_.strftime("%Y-%m-%dT%H:%M:%SZ"),
        to_.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    dc = to_.replace(minute=0, second=0, microsecond=0)
    active_win = set()
    for pl in win["pools"]:
        for seg in pl.get("segments") or []:
            if seg[0] <= dc.isoformat().replace("+00:00", "Z") <= seg[1]:
                active_win.add(pl["pool_id"])
    assert active_batch == active_win


@pytest.mark.timeout(120)
def test_window_cache_key_separates_5m_and_1h():
    k5 = _key_parts(
        from_utc="2026-08-15T00:00:00Z",
        to_utc="2026-08-15T06:00:00Z",
        timeframe="5m",
        pool_detail=1,
        compute_mode="exact_origin_v1",
    ).digest()
    k1 = _key_parts(
        from_utc="2026-08-15T00:00:00Z",
        to_utc="2026-08-15T06:00:00Z",
        timeframe="1h",
        pool_detail=1,
        compute_mode="exact_origin_v1",
    ).digest()
    assert k5 != k1


@pytest.mark.timeout(180)
def test_service_cache_hit_for_1h():
    clear_cache_for_tests()
    p1 = get_landscape_window_response(
        from_utc="2026-08-15T06:00:00Z",
        to_utc="2026-08-15T12:00:00Z",
        timeframe="1h",
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    assert p1.get("response_cache", {}).get("hit") is False
    p2 = get_landscape_window_response(
        from_utc="2026-08-15T06:00:00Z",
        to_utc="2026-08-15T12:00:00Z",
        timeframe="1h",
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    assert p2.get("response_cache", {}).get("hit") is True


def test_capabilities_includes_1h_scope():
    from pd1_landscape_viewer_v1.historical_window_v1 import landscape_capabilities

    caps = landscape_capabilities()
    scopes = caps.get("supported_viewer_scopes") or []
    one_h = [s for s in scopes if s.get("timeframe") == "1h" and s.get("pool_detail") == 1]
    assert one_h
    assert one_h[0].get("source") == "on_demand_ch1m"


def test_5m_regression_smoke():
    """Ensure 5m frozen path still resolves (may skip CH if unavailable)."""
    try:
        d = build_landscape_window(
            "2026-09-14T21:45:00Z",
            "2026-09-14T23:45:00Z",
            timeframe="5m",
            pool_detail=1,
            compute_mode="exact_origin_v1",
        )
    except LandscapeWindowError:
        pytest.skip("5m publications unavailable in this environment")
    assert d["provenance"]["timeframe"] == "5m"
    assert "5mpd1" in d["provenance"]["pipeline_line_id"]

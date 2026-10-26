"""BT-D34 — window response cache contract (no ClickHouse)."""

from __future__ import annotations

import copy

from pd1_landscape_viewer_v1.window_response_cache_v1 import (
    COMPUTE_MODE_EXACT_ORIGIN_V1,
    COMPUTE_MODE_FULL,
    WindowCacheKeyParts,
    attach_response_cache_meta,
    clear_cache_for_tests,
    get_cached_window,
    normalize_compute_mode,
    payload_cache_eligible,
    store_cached_window,
)


def _sample_payload() -> dict:
    return {
        "pools": [{"pool_id": "p1", "segments": [["2026-09-11T10:00:00Z", "2026-09-11T11:00:00Z"]]}],
        "provenance": {
            "pipeline_line_id": "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
            "config_hash": "abc",
            "timeframe": "5m",
            "pool_detail": 1,
            "remaining": False,
        },
        "window": {"from_utc": "2026-09-11T10:00:00Z", "to_utc": "2026-09-11T12:00:00Z"},
    }


def test_normalize_compute_mode():
    assert normalize_compute_mode(None) == COMPUTE_MODE_FULL
    assert normalize_compute_mode("exact_origin_v1") == COMPUTE_MODE_EXACT_ORIGIN_V1
    assert normalize_compute_mode("b-layer") == COMPUTE_MODE_EXACT_ORIGIN_V1


def test_cache_key_differs_by_compute_mode():
    base = {
        "symbol": "DOGEUSDT",
        "from_utc": "2026-09-11T10:00:00Z",
        "to_utc": "2026-09-11T12:00:00Z",
        "timeframe": "5m",
        "pool_detail": 1,
        "pipeline_line_id": "line",
        "config_hash": "hash",
    }
    k_full = WindowCacheKeyParts(**base, compute_mode=COMPUTE_MODE_FULL).digest()
    k_eo = WindowCacheKeyParts(**base, compute_mode=COMPUTE_MODE_EXACT_ORIGIN_V1).digest()
    assert k_full != k_eo


def test_cache_store_and_hit():
    clear_cache_for_tests()
    payload = _sample_payload()
    assert payload_cache_eligible(payload)
    parts = WindowCacheKeyParts(
        symbol="DOGEUSDT",
        from_utc="2026-09-11T10:00:00Z",
        to_utc="2026-09-11T12:00:00Z",
        timeframe="5m",
        pool_detail=1,
        pipeline_line_id="historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
        config_hash="abc",
        compute_mode=COMPUTE_MODE_EXACT_ORIGIN_V1,
    )
    digest = parts.digest()
    assert get_cached_window(digest) is None
    store_cached_window(digest, payload)
    hit = get_cached_window(digest)
    assert hit is not None
    assert hit["pools"][0]["pool_id"] == "p1"
    hit["pools"][0]["pool_id"] = "mutated"
    again = get_cached_window(digest)
    assert again["pools"][0]["pool_id"] == "p1"


def test_attach_response_cache_meta():
    out = attach_response_cache_meta(_sample_payload(), hit=True, key_digest="deadbeef", compute_mode="full")
    assert out["response_cache"]["hit"] is True
    assert out["response_cache"]["contract"].startswith("bt_d34")

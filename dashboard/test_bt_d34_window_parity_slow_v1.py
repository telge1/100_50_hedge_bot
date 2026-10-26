"""BT-D34 — pools parity full vs exact_origin_v1 (requires ClickHouse, slow)."""

from __future__ import annotations

import json
import os

import pytest

from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
from pd1_landscape_viewer_v1.window_response_cache_v1 import COMPUTE_MODE_EXACT_ORIGIN_V1, COMPUTE_MODE_FULL

pytestmark = pytest.mark.skipif(
    os.environ.get("BT_D34_SLOW_PARITY", "").strip() != "1",
    reason="set BT_D34_SLOW_PARITY=1 to run CH-backed parity",
)

REF_WINDOWS = [
    ("ref_12_00_utc", "2026-09-11T10:00:00Z", "2026-09-11T14:00:00Z"),
    ("ref_15_35_utc", "2026-09-11T13:35:00Z", "2026-09-11T17:35:00Z"),
    ("ref_17_00_utc", "2026-09-11T15:00:00Z", "2026-09-11T19:00:00Z"),
]


def _canonical_pools(pools: list) -> str:
    return json.dumps(pools, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("label,from_utc,to_utc", REF_WINDOWS)
def test_pools_parity_full_vs_exact_origin(label, from_utc, to_utc):
    full = build_landscape_window(
        from_utc,
        to_utc,
        timeframe="5m",
        pool_detail=1,
        compute_mode=COMPUTE_MODE_FULL,
    )
    slim = build_landscape_window(
        from_utc,
        to_utc,
        timeframe="5m",
        pool_detail=1,
        compute_mode=COMPUTE_MODE_EXACT_ORIGIN_V1,
    )
    assert _canonical_pools(full["pools"]) == _canonical_pools(slim["pools"])
    assert slim.get("clusters_by_dc") == {}
    assert (slim.get("timing_ms") or {}).get("build_clusters_by_dc") == 0.0

"""Cache miss falls through to canonical layer."""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))


class _CacheMiss:
    def collect_at_close(self, bar: str) -> dict:
        raise RuntimeError("no cache entry for decision close")


class _Canon:
    def collect_at_close(self, bar: str) -> dict:
        return {"upper_bands": [{"lower_price": "0.1", "upper_price": "0.11"}], "lower_bands": []}


def test_cache_miss_uses_canonical():
    from pilot_density_session_v1 import PilotDensitySessionV1

    sess = PilotDensitySessionV1(
        base_config={},
        pilot={"pilot_id": "t", "density_mode": "cache_plus_canonical"},
        canonical_session=_Canon(),
        cache_session=_CacheMiss(),
        d54_snapshots={},
    )
    snap = sess.collect_at_close("2026-09-11T11:30:00Z")
    assert snap["density_transport"] == "canonical_provider"

"""Phase 2J — lineage-aware lifecycle unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO_ROOT / "tools/phase_short_main_v2_combined_exit_v1"))

from long_lineage_lifecycle_v2j_v1 import origin_continuity_ok, resolve_region_by_lineage_at_close  # noqa: E402
from lower_pool_gap_tp_v1 import AtomicLowerBandV1, merge_atomic_lowers_into_regions  # noqa: E402
from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402


class _SnapSession:
    def __init__(self, snap: dict):
        self._snap = snap

    def collect_at_close(self, _utc: str) -> dict:
        return self._snap


def _region(lo, hi, oids="a|b"):
    a = AtomicLowerBandV1(f"{lo}|{hi}", lo, hi, tuple(oids.split("|")), 2)
    return merge_atomic_lowers_into_regions([a])[0]


def test_origin_continuity_allows_growth():
    old = _region(0.079758, 0.07997, "a|b")
    new = _region(0.079758, 0.08003, "a|b|c")
    assert origin_continuity_ok(frozen_origin_ids="a|b", region=new)
    assert not origin_continuity_ok(frozen_origin_ids="a|b|c", region=old)


def test_resolve_lineage_finds_grown_union():
    yaml = load_long_research_yaml(REPO_ROOT / "configs/long_v3_trend_continuation_research_v1.yaml").support
    tr = CausalLineageTrackerV2H(min_unique_origins=2, min_age_bars=5)
    r1 = _region(0.079758, 0.07997)
    tr.advance(bar_utc="t0", regions=[r1], market_close=0.081)
    r2 = _region(0.079758, 0.08003, "a|b|c")
    tr.advance(bar_utc="t1", regions=[r2], market_close=0.081)
    lid = tr.lineage_id_for_region(r2)
    snap = {"lower_bands": []}
    session = _SnapSession(snap)
    # session stacked uses collect - need lower_bands in snap for _stacked
    # use real regions via monkeypatch - simpler: pass regions through tracker only
    # resolve uses session.collect - mock with bands from region
    bands = []
    for a in r2.atomics:
        bands.append(
            {
                "lower_price": a.lower_edge,
                "upper_price": a.upper_edge,
                "pool_status": "ACTIVE",
                "origin_ids": "|".join(a.origin_ids),
                "unique_origin_count": len(a.origin_ids),
            }
        )
    session = _SnapSession({"lower_bands": bands})
    tr.advance(bar_utc="t1", regions=[r2], market_close=0.081)
    res = resolve_region_by_lineage_at_close(
        session,
        decision_close_utc="t1",
        lineage_id=lid,
        lineage_tracker=tr,
        yaml=yaml,
    )
    assert res.region is not None
    assert abs(res.region.upper_edge - 0.08003) < 1e-9


def test_pending_fields_default_lineage_none():
    from long_pending_v1 import LongPendingOrderV1

    p = LongPendingOrderV1(
        order_id="x",
        decision_close_utc="t",
        earliest_eligible_execution_utc="t2",
        pool_lower=1.0,
        pool_upper=2.0,
        pool_key="1|2",
        origin_ids="a",
        planned_entry=2.0,
        planned_sl=0.5,
    )
    assert p.lineage_id is None
    assert p.frozen_support_key is None

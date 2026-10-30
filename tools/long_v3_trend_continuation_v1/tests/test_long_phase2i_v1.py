"""Phase 2I — below-price streak + segment formation tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO_ROOT / "tools/phase_short_main_v2_combined_exit_v1"))

from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H  # noqa: E402
from lower_pool_gap_tp_v1 import AtomicLowerBandV1, LowerRegionV1, merge_atomic_lowers_into_regions  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_support_selection_v2e_v1 import region_pool_height_pct, audit_region_qualification  # noqa: E402


def _region(lo, hi, oids="a|b"):
    a = AtomicLowerBandV1(f"{lo}|{hi}", lo, hi, tuple(oids.split("|")), 2)
    return merge_atomic_lowers_into_regions([a])[0]


def test_height_gte_no_max_cap():
    yaml = load_long_research_yaml(REPO_ROOT / "configs/long_v3_trend_continuation_research_v1.yaml").support
    for hi in (0.07997, 0.08003, 0.085):
        assert region_pool_height_pct(_region(0.079758, hi)) + 1e-12 >= yaml.min_pool_height_pct


def test_lineage_age_persists_on_upper_growth():
    tr = CausalLineageTrackerV2H(min_unique_origins=2, min_age_bars=5)
    r1 = _region(0.079758, 0.07997)
    tr.advance(bar_utc="t0", regions=[r1], market_close=0.081)
    r2 = LowerRegionV1(
        0.079758,
        0.08003,
        [
            AtomicLowerBandV1("a", 0.079758, 0.07997, ("a", "b"), 2),
            AtomicLowerBandV1("b", 0.07997, 0.08003, ("a", "c"), 2),
        ],
        ("a", "b", "c"),
    )
    tr.advance(bar_utc="t1", regions=[r2], market_close=0.081)
    assert tr.region_lineage_age_for_region(r2) >= 2


def test_below_price_streak_resets_when_not_below():
    tr = CausalLineageTrackerV2H(min_unique_origins=2, min_age_bars=5)
    r = _region(0.079758, 0.07997)
    for i in range(6):
        tr.advance(bar_utc=f"t{i}", regions=[r], market_close=0.081)
    assert tr.below_price_streak_for_region(r) >= 5
    tr.advance(bar_utc="t6", regions=[r], market_close=0.0799)
    assert tr.below_price_streak_for_region(r) == 0


def test_segment_formation_without_legacy_start():
    yaml = load_long_research_yaml(REPO_ROOT / "configs/long_v3_trend_continuation_research_v1.yaml").support
    tr = CausalLineageTrackerV2H(min_unique_origins=2, min_age_bars=5)
    r = _region(0.079758, 0.07997)
    closes = []
    prices = {}
    for i in range(6):
        b = f"2026-09-16T00:{i*5:02d}:00Z"
        closes.append(b)
        prices[b] = {"c": 0.081}
        tr.advance(bar_utc=b, regions=[r], market_close=0.081)
    from lower_atomic_pool_key_v1 import canonical_lower_atomic_pool_key

    live = {canonical_lower_atomic_pool_key(0.079758, 0.07997): 6}
    pa = audit_region_qualification(
        None,
        region=r,
        decision_close_utc=closes[-1],
        market_close=0.081,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live,
        pool_age_semantics="CAUSAL_REGION_LINEAGE_5",
        lineage_tracker=tr,
        formation_semantics="CAUSAL_BELOW_PRICE_SEGMENT",
    )
    assert pa.qualified

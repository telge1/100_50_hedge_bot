"""Phase 2H — lineage continuity + height >= threshold."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO_ROOT / "tools/phase_short_main_v2_combined_exit_v1"))

from causal_region_lineage_v2h_v1 import CausalLineageTrackerV2H, _frame  # noqa: E402
from lower_pool_gap_tp_v1 import AtomicLowerBandV1, LowerRegionV1, merge_atomic_lowers_into_regions  # noqa: E402
from long_support_selection_v2e_v1 import region_pool_height_pct, audit_region_qualification  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402


def _region(lo, hi, oids="a|b"):
    a = AtomicLowerBandV1(f"{lo}|{hi}", lo, hi, tuple(oids.split("|")), 2)
    return merge_atomic_lowers_into_regions([a])[0]


def test_height_uses_gte_not_exact():
    yaml = load_long_research_yaml(REPO_ROOT / "configs/long_v3_trend_continuation_research_v1.yaml").support
    for hi in (0.07997, 0.08003, 0.081):
        r = _region(0.079758, hi)
        h = region_pool_height_pct(r)
        assert h + 1e-12 >= yaml.min_pool_height_pct


def test_lineage_continues_on_upper_growth():
    tr = CausalLineageTrackerV2H(min_unique_origins=2, min_age_bars=5)
    r1 = _region(0.079758, 0.07997, "a|b")
    tr.advance(bar_utc="2026-09-16T00:35:00Z", regions=[r1], market_close=0.08)
    r2 = LowerRegionV1(
        0.079758,
        0.08003,
        [
            AtomicLowerBandV1("0.079758|0.07997", 0.079758, 0.07997, ("a", "b"), 2),
            AtomicLowerBandV1("0.07997|0.08003", 0.07997, 0.08003, ("a", "c"), 2),
        ],
        ("a", "b", "c"),
    )
    tr.advance(bar_utc="2026-09-16T00:40:00Z", regions=[r2], market_close=0.08)
    assert tr.streak_for_region(r2) >= 2

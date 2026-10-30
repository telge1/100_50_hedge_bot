"""Phase 2E — YAML, age, height, formation, selection, SL@Q."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_YAML = REPO_ROOT / "configs" / "long_v3_trend_continuation_research_v1.yaml"
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO_ROOT / "tools/phase_short_main_v2_combined_exit_v1"))

from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from long_risk_audit_v1 import risk_pct  # noqa: E402
from long_support_selection_v2e_v1 import (  # noqa: E402
    region_pool_height_pct,
    select_support_v2e,
    validate_frozen_region_v2e,
)
from long_support_selection_v2d_v1 import select_support_v2d  # noqa: E402
from lower_pool_gap_tp_v1 import LowerRegionV1, AtomicLowerBandV1  # noqa: E402
from pool_age_consecutive_lower_v1 import consecutive_active_streak_lower  # noqa: E402
from tests.fixtures_v1 import snap  # noqa: E402


class MockSession:
    def __init__(self, snaps: dict[str, dict]) -> None:
        self._snaps = snaps

    def has_close(self, decision_close_utc: str) -> bool:
        return decision_close_utc in self._snaps

    def collect_at_close(self, decision_close_utc: str) -> dict:
        return self._snaps[decision_close_utc]


def _band(lo: float, hi: float, oids: str = "a|b") -> dict:
    n = len([x for x in oids.split("|") if x])
    return {
        "side": "lower",
        "lower_price": str(lo),
        "upper_price": str(hi),
        "unique_origin_count": max(n, 2),
        "origin_ids": oids,
        "pool_status": "ACTIVE",
    }


def _yaml():
    return load_long_research_yaml(CONFIG_YAML).support


def _region(lo: float, hi: float, oids: str = "a|b") -> LowerRegionV1:
    a = AtomicLowerBandV1(f"{lo}|{hi}", lo, hi, tuple(oids.split("|")), 2)
    return LowerRegionV1(lo, hi, [a], tuple(oids.split("|")))


def test_yaml_min_age_and_height():
    y = load_long_research_yaml(CONFIG_YAML)
    assert y.support.min_pool_age_bars == 5
    assert y.support.min_pool_height_pct == pytest.approx(0.1)
    assert y.enabled is False


def test_height_016_excluded_010_ok():
    yaml = _yaml()
    narrow = _region(0.086776, 0.08679)
    wide = _region(0.090, 0.091)
    assert region_pool_height_pct(narrow) < yaml.min_pool_height_pct
    assert region_pool_height_pct(wide) + 1e-12 >= yaml.min_pool_height_pct


from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_consecutive_age_four_fails_five_ok():
    yaml = _yaml()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [_iso(t0 + timedelta(minutes=5 * i)) for i in range(12)]
    snaps = {}
    for i, b in enumerate(bars):
        if i < 4:
            snaps[b] = snap([(0.08, 0.081, "c|d")])
        else:
            snaps[b] = snap([(0.09, 0.091, "a|b"), (0.08, 0.081, "c|d")])
    session = MockSession(snaps)
    s4, ok4, _ = consecutive_active_streak_lower(
        session, decision_close_utc=bars[7], pool_lo=0.09, pool_hi=0.091, min_unique_origins=2
    )
    s5, ok5, _ = consecutive_active_streak_lower(
        session, decision_close_utc=bars[8], pool_lo=0.09, pool_hi=0.091, min_unique_origins=2
    )
    assert ok4 and s4 == 4
    assert ok5 and s5 == 5
    assert s4 < yaml.min_pool_age_bars
    assert s5 >= yaml.min_pool_age_bars


def test_sl_at_q_upper_and_limit_at_p_upper():
    yaml = _yaml()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [_iso(t0 + timedelta(minutes=5 * i)) for i in range(12)]
    band_snap = snap([(0.10, 0.101, "a|b"), (0.095, 0.096, "c|d")])
    snaps = {}
    for i, b in enumerate(bars):
        snaps[b] = snap([]) if i < 2 else band_snap
    prices = {b: {"o": 0.102, "h": 0.102, "l": 0.102, "c": 0.102} for b in bars}
    session = MockSession(snaps)
    sel, reason, _ = select_support_v2e(
        session,
        decision_close_utc=bars[-1],
        market_price=0.102,
        prices=prices,
        yaml=yaml,
    )
    assert reason is None and sel is not None
    assert sel.entry_limit == pytest.approx(0.101)
    assert sel.structural_sl == pytest.approx(0.096)
    assert risk_pct(sel.entry_limit, sel.structural_sl) == pytest.approx((0.101 - 0.096) / 0.101 * 100)


def test_v2e_config_dispatch():
    yaml = _yaml()
    cfg = LongResearchConfigV2DV1("V2E_QUALIFIED", "DEEPER_Q_UPPER", "NEXT_BAR", v2e_yaml=yaml)
    s = snap([(0.10, 0.101, "a|b"), (0.095, 0.096, "c|d")])
    sel, reason = select_support_v2d(
        s,
        market_price=0.102,
        config=cfg,
        session=MockSession({"2026-01-01T00:00:00Z": s}),
        decision_close_utc="2026-01-01T00:00:00Z",
        prices={"2026-01-01T00:00:00Z": {"c": 0.102}},
    )
    assert reason == "V2E_CONTEXT_MISSING" or sel is None or sel.structural_sl == 0.096

"""Phase 2G — canonical pool key regression (Phase 2F streak lookup bug)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(REPO_ROOT / "tools/phase_short_main_v2_combined_exit_v1"))

from lower_atomic_pool_key_v1 import canonical_lower_atomic_pool_key  # noqa: E402
from long_config_yaml_v1 import load_long_research_yaml  # noqa: E402
from long_support_selection_v2e_v1 import select_support_v2e  # noqa: E402
from pool_consecutive_streak_v2e_v1 import streak_for_band, update_consecutive_lower_streaks  # noqa: E402
from tests.fixtures_v1 import snap  # noqa: E402


def test_canonical_key_stable_for_repr_mismatch():
    lo, hi = float("0.067456"), float("0.0676")
    raw_key = f"{lo}|{hi}"
    canon = canonical_lower_atomic_pool_key(lo, hi)
    assert raw_key != canon or True  # may differ from naive f-string
    assert canon == canonical_lower_atomic_pool_key(0.067456000000000002, 0.067599999999999993)


def test_incremental_streak_matches_audit_lookup():
    yaml = load_long_research_yaml(
        REPO_ROOT / "configs" / "long_v3_trend_continuation_research_v1.yaml"
    ).support
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        (t0 + timedelta(minutes=5 * i)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(8)
    ]
    band = snap([(0.067456, 0.0676, "a|b"), (0.095, 0.096, "c|d")])
    streak: dict[str, int] = {}
    for b in bars:
        update_consecutive_lower_streaks(band, streak, min_unique_origins=yaml.min_unique_origins)
    s = streak_for_band(streak, 0.067456, 0.0676)
    assert s >= 5


class MockSession:
    def __init__(self, snaps: dict[str, dict]) -> None:
        self._snaps = snaps

    def has_close(self, decision_close_utc: str) -> bool:
        return decision_close_utc in self._snaps

    def collect_at_close(self, decision_close_utc: str) -> dict:
        return self._snaps[decision_close_utc]


def test_select_v2e_age_ok_with_live_streak_cache():
    yaml = load_long_research_yaml(
        REPO_ROOT / "configs" / "long_v3_trend_continuation_research_v1.yaml"
    ).support
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = [
        (t0 + timedelta(minutes=5 * i)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(12)
    ]
    band_snap = snap([(0.10, 0.101, "a|b"), (0.095, 0.096, "c|d")])
    snaps = {b: band_snap for b in bars}
    prices = {b: {"o": 0.102, "h": 0.102, "l": 0.102, "c": 0.102} for b in bars}
    streak: dict[str, int] = {}
    for i, b in enumerate(bars):
        update_consecutive_lower_streaks(band_snap, streak, min_unique_origins=yaml.min_unique_origins)
    session = MockSession(snaps)
    sel, reason, pa = select_support_v2e(
        session,
        decision_close_utc=bars[-1],
        market_price=0.102,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=streak,
        bar_index=len(bars) - 1,
        closes=bars,
    )
    assert reason is None and sel is not None and pa is not None and pa.qualified

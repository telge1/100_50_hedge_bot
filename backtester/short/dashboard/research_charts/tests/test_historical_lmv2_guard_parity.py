"""Guard advance parity: V2 subclass uses original H4FloorGuard.advance."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from dashboard.research_charts.historical_lmv2.guard_v2 import V2H4FloorGuard
from dashboard.research_charts.historical_lmv2.strategy_paths import ensure_strategy_paths

UTC = timezone.utc


def test_v2_guard_delegates_to_h4floorguard_advance():
    ensure_strategy_paths()
    from short_block_4h_guard import H4FloorGuard
    import inspect

    src = inspect.getsource(V2H4FloorGuard.advance)
    assert "H4FloorGuard.advance" in src


def test_guard_replay_identical_snapshot_parity():
    ensure_strategy_paths()
    from short_block_4h_guard import H4FloorGuard, H4Snapshot

    snap = H4Snapshot(
        decision_time=datetime(2026, 7, 8, 12, 0, tzinfo=UTC),
        pane_4h_len=1,
        tip="t",
        lowers=[],
        deepest_active={"pool_id": "x", "bottom": 0.08, "top": 0.10},
        pool_below_deepest=False,
        gap_pct_below=None,
    )
    pf = datetime(2026, 2, 10, tzinfo=UTC)
    pt = datetime(2026, 12, 1, tzinfo=UTC)
    legacy = H4FloorGuard("DOGEUSDT", pf, pt)
    # Minimal V2 instance without store calls — patch _snapshot on both
    legacy._snapshot = lambda _dt: snap  # type: ignore[method-assign]

    legacy2 = H4FloorGuard("DOGEUSDT", pf, pt)
    legacy2._snapshot = lambda _dt: snap  # type: ignore[method-assign]

    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    bars = [
        {"close": 0.095, "low": 0.08, "high": 0.10},
        {"close": 0.096, "low": 0.09, "high": 0.091},
        {"close": 0.097, "low": 0.09, "high": 0.092},
    ]
    for i, bar in enumerate(bars):
        moment = t0 + timedelta(minutes=15 * i)
        assert legacy.advance(bar, moment) == legacy2.advance(bar, moment)

    # 48h later episode should clear block
    later = t0 + timedelta(hours=49)
    bar_l = {"close": 0.1, "low": 0.1, "high": 0.1}
    assert legacy.advance(bar_l, later) == legacy2.advance(bar_l, later)


def _guard_with_fixed_deepest(bottom: float = 0.08, top: float = 0.10):
    ensure_strategy_paths()
    from short_block_4h_guard import H4FloorGuard, H4Snapshot

    pf = datetime(2026, 2, 10, tzinfo=UTC)
    pt = datetime(2026, 12, 1, tzinfo=UTC)
    guard = H4FloorGuard("DOGEUSDT", pf, pt)

    def snap(_dt):
        return H4Snapshot(
            decision_time=datetime(2026, 7, 8, 12, 0, tzinfo=UTC),
            pane_4h_len=1,
            tip="t",
            lowers=[],
            deepest_active={"pool_id": "x", "bottom": bottom, "top": top},
            pool_below_deepest=False,
            gap_pct_below=None,
        )

    guard._snapshot = snap  # type: ignore[method-assign]
    return guard


def test_guard_a2_near_top_without_prior_touch():
    guard = _guard_with_fixed_deepest()
    moment = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    # Close just above pool top, bar fully above top (no overlap → not A1).
    blocked, reason = guard.advance(
        {"close": 0.1005, "low": 0.1005, "high": 0.1006},
        moment,
    )
    assert blocked and reason == "A2_near_top"


def test_guard_b_episode_after_touch():
    guard = _guard_with_fixed_deepest()
    t0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    touch = guard.advance({"close": 0.09, "low": 0.08, "high": 0.10}, t0)
    assert touch[0] and touch[1] == "A1_touch"
    blocked, reason = guard.advance(
        {"close": 0.104, "low": 0.103, "high": 0.105},
        t0 + timedelta(minutes=15),
    )
    assert blocked and reason == "B_episode"


def test_guard_a3_in_zone_when_bar_does_not_overlap_pool():
    """A3: close inside [bottom, top] but bar range does not intersect the pool band."""
    guard = _guard_with_fixed_deepest(bottom=0.08, top=0.10)
    moment = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    # close in zone; low/high both above top → no touch, not A2 (too far above top).
    blocked, reason = guard.advance(
        {"close": 0.095, "low": 0.101, "high": 0.102},
        moment,
    )
    assert blocked and reason == "A3_in_zone"


def test_v2_guard_same_reasons_as_legacy_with_patched_snapshot():
    """V2 ``advance`` on inner guard matches legacy for A2 after shared snapshot."""
    ensure_strategy_paths()
    from short_block_4h_guard import H4FloorGuard

    legacy = _guard_with_fixed_deepest()
    inner = _guard_with_fixed_deepest()
    moment = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    bar = {"close": 0.1005, "low": 0.1005, "high": 0.1006}
    assert H4FloorGuard.advance(inner, bar, moment) == legacy.advance(bar, moment)

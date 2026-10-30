"""Phase 2B — runner, exits, preflight, tie-break tests."""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG.parents[1] / "phase_short_main_v2_combined_exit_v1"))

from bearish_ema_cross_exit_v1 import bearish_cross_confirmed_on_close  # noqa: E402
from contracts_v1 import BARS_PER_HOUR_5M, MIN_POOL_LOOKBACK_HOURS  # noqa: E402
from coverage_preflight_v1 import run_coverage_preflight  # noqa: E402
from fixtures_v1 import snap  # noqa: E402
from long_atomic_tp_v1 import select_long_a_tp_at_fill  # noqa: E402
from long_exit_lifecycle_v1 import LongExitAuditEventV1, LongExitStateV1, process_long_exit_bar  # noqa: E402
from long_intrabar_exit_v1 import long_intrabar_exit_decision  # noqa: E402
from long_support_selection_v1 import select_support_below_price  # noqa: E402
from upper_pool_gap_tp_v1 import select_upper_pool_gap_tp_candidate  # noqa: E402


def test_tie_break_prefers_wider_region_smaller_lower_edge():
    s = snap(
        [
            (0.0880, 0.0890, "a|b"),
            (0.0885, 0.0890, "c|d"),
            (0.0800, 0.0810, "e|f"),
        ]
    )
    sel, _ = select_support_below_price(s, market_price=0.095)
    assert sel is not None
    assert sel.region.lower_edge == 0.0880


def test_long_a_tp_min_half_percent():
    s = snap(
        [(0.08, 0.081, "a|b")],
        [(0.095, 0.096, "u1|u2"), (0.100, 0.101, "u3|u4")],
    )
    tp, _, reason = select_long_a_tp_at_fill(s, entry_fill=0.089)
    assert tp is not None
    assert (tp - 0.089) / 0.089 * 100 >= 0.5 - 1e-6


def test_upper_gap_tp_must_be_above_entry():
    s = snap([], [(0.090, 0.092, "a|b"), (0.100, 0.102, "c|d")])
    cand = select_upper_pool_gap_tp_candidate(s, market_price=0.095, entry_fill=0.089, gap_threshold_pct=3.5)
    assert cand is None or cand["tp_limit_price"] > 0.089


def test_bearish_ema_cross():
    sig = bearish_cross_confirmed_on_close(
        prev_bar_utc="t0",
        bar_utc="t1",
        prev_ema_fast=0.101,
        prev_ema_slow=0.100,
        ema_fast=0.099,
        ema_slow=0.100,
    )
    assert sig is not None


def test_intrabar_sl_before_tp_ambiguous():
    r, p, amb = long_intrabar_exit_decision(
        ohlc={"o": 0.09, "h": 0.10, "l": 0.08, "c": 0.095},
        sl_price=0.085,
        tp_price=0.095,
        policy="no_trade_ambiguous",
    )
    assert r == "AMBIGUOUS_INTRABAR"
    assert amb


def test_long_exit_sl_same_bar():
    st = LongExitStateV1(entry_price=0.09, sl_price=0.085, exit_mode="LONG_A", active_tp_limit=0.095)
    audit: list[LongExitAuditEventV1] = []
    process_long_exit_bar(
        st,
        prev_bar_utc="2026-01-01T00:00:00Z",
        bar_utc="2026-01-01T00:05:00Z",
        next_bar_utc="2026-01-01T00:10:00Z",
        ohlc={"o": 0.09, "h": 0.091, "l": 0.084, "c": 0.088},
        snapshot_at_close=snap([(0.08, 0.081, "a|b")]),
        prev_ema59=0.1,
        prev_ema200=0.09,
        ema59=0.1,
        ema200=0.09,
        audit=audit,
    )
    assert st.closed
    assert st.exit_reason == "STRUCTURAL_SL"


def test_preflight_blocks_month_start():
    pre = run_coverage_preflight(
        decision_closes=["2026-09-01T00:00:00Z", "2026-09-01T00:05:00Z"],
        transport="test",
        bundle_first_close="2026-09-01T00:00:00Z",
    )
    assert not pre.ok


def test_preflight_ok_after_168h():
    n = MIN_POOL_LOOKBACK_HOURS * BARS_PER_HOUR_5M + 5
    closes = [f"2026-06-01T00:{i:02d}:00Z" for i in range(n)]
    pre = run_coverage_preflight(decision_closes=closes, transport="test", bundle_first_close=None)
    assert pre.ok


def test_open_gap_sl_below_limit():
    st = LongExitStateV1(entry_price=0.09, sl_price=0.088, exit_mode="LONG_A", active_tp_limit=None)
    audit: list[LongExitAuditEventV1] = []
    process_long_exit_bar(
        st,
        prev_bar_utc="t0",
        bar_utc="t1",
        next_bar_utc="t2",
        ohlc={"o": 0.087, "h": 0.09, "l": 0.086, "c": 0.088},
        snapshot_at_close=snap([]),
        prev_ema59=1,
        prev_ema200=1,
        ema59=1,
        ema200=1,
        audit=audit,
    )
    assert st.exit_reason == "STRUCTURAL_SL"
    assert st.exit_price == 0.087

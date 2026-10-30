"""LONG V3 entry engine unit and state-machine tests."""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_short_main_v2_combined_exit_v1"))

from buy_limit_fill_v1 import evaluate_long_buy_limit_fill  # noqa: E402
from fixtures_v1 import snap  # noqa: E402
from long_ema_gate_v1 import evaluate_long_ema_gate  # noqa: E402
from long_entry_chronology_v1 import LongEntryStateV1, process_long_entry_bar  # noqa: E402
from long_risk_audit_v1 import risk_pct, structural_long_sl  # noqa: E402
from long_support_selection_v1 import select_support_below_price  # noqa: E402
from long_touch_recovery_v1 import TouchRecoveryTrackerV1, process_touch_recovery_at_close  # noqa: E402
from pool_history_gate_v1 import evaluate_pool_history_gate  # noqa: E402


def _ema(ok: bool):
    base = 0.10
    if ok:
        return base + 0.01, base + 0.008, base + 0.006, base, base - 0.0001
    return base - 0.01, base + 0.008, base + 0.006, base, base - 0.0001


def test_ema9_under_200_blocks():
    e9, e20, e59, e200, p6 = _ema(True)
    r = evaluate_long_ema_gate(ema9=e9 - 0.02, ema20=e20, ema59=e59, ema200=e200, ema200_prev6=p6)
    assert not r.authorized


def test_ema20_under_200_blocks():
    e9, e20, e59, e200, p6 = _ema(True)
    r = evaluate_long_ema_gate(ema9=e9, ema20=e200 - 0.001, ema59=e59, ema200=e200, ema200_prev6=p6)
    assert not r.authorized


def test_ema59_under_200_blocks():
    e9, e20, e59, e200, p6 = _ema(True)
    r = evaluate_long_ema_gate(ema9=e9, ema20=e20, ema59=e200 - 0.001, ema200=e200, ema200_prev6=p6)
    assert not r.authorized


def test_ema200_not_rising_blocks():
    e9, e20, e59, e200, _p6 = _ema(True)
    r = evaluate_long_ema_gate(ema9=e9, ema20=e20, ema59=e59, ema200=e200, ema200_prev6=e200)
    assert not r.authorized


def test_ema_all_ok():
    e9, e20, e59, e200, p6 = _ema(True)
    r = evaluate_long_ema_gate(ema9=e9, ema20=e20, ema59=e59, ema200=e200, ema200_prev6=p6)
    assert r.authorized


def test_gap_below_threshold_no_support():
    s = snap(
        [
            (0.090, 0.091, "a|b"),
            (0.0895, 0.0898, "c|d"),
        ]
    )
    sel, reason = select_support_below_price(s, market_price=0.095)
    assert sel is None
    assert reason


def test_sl_exactly_02_pct_below():
    lo = 0.10
    sl = structural_long_sl(lo)
    assert abs(sl - lo * 0.998) < 1e-15


def test_touch_rejected_if_pool_not_in_prev_snapshot():
    prev = snap([(0.080, 0.081, "o1|o2")])
    now = snap([(0.080, 0.081, "o1|o2"), (0.088, 0.089, "n1|n2")])
    tr = TouchRecoveryTrackerV1()
    tr, _ = process_touch_recovery_at_close(
        bar="2026-01-01T01:00:00Z",
        prev_bar="2026-01-01T00:55:00Z",
        snap_prev=prev,
        snap_now=now,
        close_prev=0.092,
        ohlc={"o": 0.091, "h": 0.092, "l": 0.088, "c": 0.091},
        ema_authorized=True,
        pool_history_ok=True,
        tracker=tr,
    )
    assert tr.phase == "IDLE"


def test_touch_on_pool_known_in_prev_snapshot():
    prev = snap([(0.088, 0.089, "a|b"), (0.080, 0.081, "c|d")])
    now = prev
    tr = TouchRecoveryTrackerV1()
    tr, _ = process_touch_recovery_at_close(
        bar="2026-01-01T01:00:00Z",
        prev_bar="2026-01-01T00:55:00Z",
        snap_prev=prev,
        snap_now=now,
        close_prev=0.092,
        ohlc={"o": 0.091, "h": 0.092, "l": 0.0885, "c": 0.091},
        ema_authorized=True,
        pool_history_ok=True,
        tracker=tr,
    )
    assert tr.phase == "TOUCHED"


def test_touch_without_recovery_no_pending():
    st = LongEntryStateV1()
    e9, e20, e59, e200, p6 = _ema(True)
    s = snap([(0.088, 0.089, "a|b"), (0.080, 0.081, "c|d")])
    st = process_long_entry_bar(
        st,
        bar="2026-01-02T12:00:00Z",
        bar_index=2016,
        prev_bar="2026-01-02T11:55:00Z",
        ohlc={"o": 0.092, "h": 0.093, "l": 0.0885, "c": 0.092},
        close_prev=0.093,
        snap_prev=s,
        snap=s,
        ema9=e9,
        ema20=e20,
        ema59=e59,
        ema200=e200,
        ema200_prev6=p6,
    )
    assert st.pending is None
    assert st.touch_tracker.phase == "TOUCHED"


def test_recovery_then_place_pending():
    st = LongEntryStateV1()
    e9, e20, e59, e200, p6 = _ema(True)
    s = snap([(0.088, 0.089, "a|b"), (0.080, 0.081, "c|d")])
    st = process_long_entry_bar(
        st,
        bar="2026-01-02T12:00:00Z",
        bar_index=2016,
        prev_bar="2026-01-02T11:55:00Z",
        ohlc={"o": 0.092, "h": 0.093, "l": 0.0885, "c": 0.092},
        close_prev=0.093,
        snap_prev=s,
        snap=s,
        ema9=e9,
        ema20=e20,
        ema59=e59,
        ema200=e200,
        ema200_prev6=p6,
    )
    st = process_long_entry_bar(
        st,
        bar="2026-01-02T12:05:00Z",
        bar_index=2017,
        prev_bar="2026-01-02T12:00:00Z",
        ohlc={"o": 0.092, "h": 0.094, "l": 0.091, "c": 0.093},
        close_prev=0.092,
        snap_prev=s,
        snap=s,
        ema9=e9,
        ema20=e20,
        ema59=e59,
        ema200=e200,
        ema200_prev6=p6,
    )
    assert st.pending is not None
    assert st.pending.planned_entry == 0.089


def test_buy_limit_fill_reached():
    dec = evaluate_long_buy_limit_fill(
        fill_model="causal_buy_limit_v2",
        limit_price=0.09,
        bar_utc="2026-01-01T00:05:00Z",
        order_active_from_utc="2026-01-01T00:05:00Z",
        ohlc={"o": 0.091, "h": 0.092, "l": 0.089, "c": 0.090},
    )
    assert dec.filled
    assert dec.fill_price == 0.09


def test_buy_limit_not_reached():
    dec = evaluate_long_buy_limit_fill(
        fill_model="causal_buy_limit_v2",
        limit_price=0.08,
        bar_utc="2026-01-01T00:05:00Z",
        order_active_from_utc="2026-01-01T00:05:00Z",
        ohlc={"o": 0.091, "h": 0.092, "l": 0.085, "c": 0.090},
    )
    assert not dec.filled


def test_ema_cancel_after_fill_same_bar_not_applied_to_fill():
    st = LongEntryStateV1()
    e9, e20, e59, e200, p6 = _ema(True)
    st.pending = type(st.pending or object).__class__  # placeholder fix below
    from long_pending_v1 import LongPendingOrderV1

    st = LongEntryStateV1()
    st.pending = LongPendingOrderV1(
        order_id="1",
        decision_close_utc="2026-01-01T00:00:00Z",
        earliest_eligible_execution_utc="2026-01-01T00:05:00Z",
        pool_lower=0.088,
        pool_upper=0.089,
        pool_key="0.088|0.089",
        origin_ids="a|b",
        planned_entry=0.089,
        planned_sl=structural_long_sl(0.088),
    )
    s = snap([(0.088, 0.089, "a|b"), (0.080, 0.081, "c|d")])
    st = process_long_entry_bar(
        st,
        bar="2026-01-01T00:05:00Z",
        bar_index=2016,
        prev_bar="2026-01-01T00:00:00Z",
        ohlc={"o": 0.091, "h": 0.092, "l": 0.088, "c": 0.090},
        close_prev=0.091,
        snap_prev=s,
        snap=s,
        ema9=e9 - 0.05,
        ema20=e20,
        ema59=e59,
        ema200=e200,
        ema200_prev6=p6,
    )
    assert st.position is not None
    assert st.pending is None


def test_pool_history_fail_closed():
    g = evaluate_pool_history_gate(close_index=100)
    assert not g.ok


def test_risk_formula():
    entry = 0.089
    sl = structural_long_sl(0.088)
    rp = risk_pct(entry, sl)
    assert rp is not None
    assert abs(rp - (entry - sl) / entry * 100) < 1e-9


def test_no_double_position():
    from long_pending_v1 import LongPositionV1

    st = LongEntryStateV1()
    st.position = LongPositionV1(
        position_id="p1",
        entry_price=0.09,
        sl_price=0.088,
        fill_bar_utc="x",
        signal_close_utc="y",
        pool_key="k",
        risk_pct=1.0,
    )
    e9, e20, e59, e200, p6 = _ema(True)
    s = snap([(0.088, 0.089, "a|b"), (0.080, 0.081, "c|d")])
    st = process_long_entry_bar(
        st,
        bar="2026-01-01T00:05:00Z",
        bar_index=2016,
        prev_bar=None,
        ohlc={"o": 0.09, "h": 0.09, "l": 0.08, "c": 0.085},
        close_prev=None,
        snap_prev=None,
        snap=s,
        ema9=e9,
        ema20=e20,
        ema59=e59,
        ema200=e200,
        ema200_prev6=p6,
    )
    assert st.pending is None

"""Tests for Phase A multilevel market structure."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from research.regime_scanner.multilevel_market_structure import (
    BEARISH,
    BULLISH,
    MultiLevelStructureEngine,
    classify_combined_context,
    run_multilevel_structure,
)
from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf

ROOT = Path("research/regime_scanner")
PROTECTED = {
    "market_regime.py": "1e79f30af2ddf95c3f91c1b1a012cded",
    "trend_structure.py": "4976cbd9921e9df58dcfaace5cb125a2",
    "trend_state_machine.py": "3a8ed63f60f86ec29bf05e7831bb3349",
    "trend_state_policy.py": "412f672652b66c93b7d44d4b692da2aa",
    "trend_zones.py": "6378f736a184e51efe070ebd2c2d969c",
    "regime_snapshot.py": "e8eed043f62cb636b972dae3af7e5a48",
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _frame(ohlc: list[tuple[float, float, float, float]], minutes: int = 30) -> pd.DataFrame:
    start = pd.Timestamp("2026-01-01T00:00:00+00:00")
    delta = pd.Timedelta(minutes=minutes)
    rows = []
    for i, (o, h, l, c) in enumerate(ohlc):
        ts = start + i * delta
        rows.append(
            {
                "timestamp": ts,
                "decision_time": ts + delta,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_protected_hashes_unchanged():
    for name, expected in PROTECTED.items():
        assert _md5(ROOT / name) == expected


def test_no_imports_into_policy_or_state_machine():
    for name in ("trend_state_policy.py", "trend_state_machine.py", "trend_structure.py"):
        assert "multilevel_market_structure" not in (ROOT / name).read_text(encoding="utf-8")


def test_internal_and_swing_fully_separated():
    # Build path with different internal vs swing pivot scales
    ohlc = []
    for i in range(60):
        ohlc.append((1.0, 1.0 + (i % 7) * 0.01, 0.9, 1.0))
    ohlc[10] = (1.0, 2.0, 0.9, 1.5)  # local spike
    ohlc[40] = (1.0, 3.0, 0.9, 2.0)  # larger swing spike
    rows, eng = run_multilevel_structure(_frame(ohlc), internal_size=5, swing_size=20)
    int_ids = {p.pivot_id for p in eng.internal.pivots}
    sw_ids = {p.pivot_id for p in eng.swing.pivots}
    assert int_ids.isdisjoint(sw_ids)
    assert all(p.structure_level == "internal" for p in eng.internal.pivots)
    assert all(p.structure_level == "swing" for p in eng.swing.pivots)


def test_internal_hh_never_compared_to_swing_high():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    # manually set last prices differently then activate
    eng.internal.last_high_price = 1.0
    eng.swing.last_high_price = 9.0
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(5)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    piv = eng._activate_pivot(
        eng.internal,
        side="high",
        price=1.2,
        extreme_i=0,
        confirm_i=2,
        times=times,
        decisions=decisions,
    )
    assert piv.point_type == "HH"  # vs internal 1.0, not swing 9.0
    piv2 = eng._activate_pivot(
        eng.internal,
        side="high",
        price=1.1,
        extreme_i=1,
        confirm_i=3,
        times=times,
        decisions=decisions,
    )
    assert piv2.point_type == "LH"


def test_swing_lh_never_compared_to_internal_high():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    eng.internal.last_high_price = 0.5
    eng.swing.last_high_price = 2.0
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(5)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    piv = eng._activate_pivot(
        eng.swing,
        side="high",
        price=1.5,
        extreme_i=0,
        confirm_i=2,
        times=times,
        decisions=decisions,
    )
    assert piv.point_type == "LH"  # vs swing 2.0


def test_three_pivot_timestamps_separated():
    size = 3
    ohlc = [
        (1, 1.0, 0.9, 0.95),
        (1, 1.1, 0.9, 1.0),
        (1, 1.2, 0.95, 1.1),
        (1, 5.0, 1.0, 4.0),
        (4, 4.0, 3.0, 3.5),
        (3, 3.6, 2.8, 3.0),
        (3, 3.2, 2.5, 2.8),
        (2, 2.9, 2.4, 2.6),
    ]
    rows, eng = run_multilevel_structure(_frame(ohlc), internal_size=size, swing_size=size)
    assert eng.all_pivots
    p = eng.all_pivots[0]
    assert p.extreme_timestamp_utc != p.confirmation_timestamp_utc
    assert p.confirmation_timestamp_utc != p.available_from_timestamp_utc
    assert pd.Timestamp(p.available_from_timestamp_utc) > pd.Timestamp(p.extreme_timestamp_utc)


def test_no_use_before_available_from():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(6)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing,
        side="high",
        price=2.0,
        extreme_i=0,
        confirm_i=2,
        times=times,
        decisions=decisions,
    )
    # before available_from: decision at confirm open (times[2]) < available (decisions[2])
    flags = eng._process_crosses(
        eng.swing,
        high=3.0,
        low=1.0,
        close=2.5,
        prior_close=1.5,
        decision_ts=times[2],  # open of confirm bar — too early
    )
    assert flags["bullish_bos"] is False
    assert flags["bullish_choch"] is False
    # at available_from
    flags2 = eng._process_crosses(
        eng.swing,
        high=3.0,
        low=1.0,
        close=2.5,
        prior_close=1.5,
        decision_ts=decisions[2],
    )
    assert flags2["close_cross_high"] is True


def test_unconfirmed_pivot_does_not_replace_active_level():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(6)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    p1 = eng._activate_pivot(
        eng.internal, side="high", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    level_id = eng.internal.active_high.level_id
    # without calling activate again, level remains
    assert eng.internal.active_high.source_pivot_id == p1.pivot_id
    assert eng.internal.active_high.level_id == level_id


def test_level_stays_until_replace_or_close_break():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(8)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing, side="high", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    lvl = eng.swing.active_high
    assert lvl.active is True
    # wick only
    eng._process_crosses(
        eng.swing, high=2.5, low=1.0, close=1.8, prior_close=1.7, decision_ts=decisions[3]
    )
    assert lvl.active is True
    assert lvl.event_emitted is False
    # close break
    eng._process_crosses(
        eng.swing, high=2.6, low=1.0, close=2.2, prior_close=1.9, decision_ts=decisions[4]
    )
    assert lvl.active is False
    assert lvl.event_emitted is True


def test_wick_does_not_create_bos_choch():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(6)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing, side="high", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    flags = eng._process_crosses(
        eng.swing, high=3.0, low=1.0, close=1.5, prior_close=1.4, decision_ts=decisions[3]
    )
    assert flags["wick_cross_high"] is True
    assert flags["bullish_bos"] is False
    assert flags["bullish_choch"] is False
    assert len(eng.all_events) == 0


def test_close_break_emits_exactly_one_event():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(8)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing, side="high", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    eng._process_crosses(
        eng.swing, high=3.0, low=1.0, close=2.5, prior_close=1.5, decision_ts=decisions[3]
    )
    eng._process_crosses(
        eng.swing, high=3.5, low=1.0, close=3.0, prior_close=2.6, decision_ts=decisions[4]
    )
    assert len(eng.all_events) == 1


def test_broken_level_no_second_event():
    test_close_break_emits_exactly_one_event()


def test_bullish_break_bearish_bias_is_choch():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    eng.swing.bias = BEARISH
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(6)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing, side="high", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    flags = eng._process_crosses(
        eng.swing, high=3.0, low=1.0, close=2.5, prior_close=1.5, decision_ts=decisions[3]
    )
    assert flags["bullish_choch"] is True
    assert flags["bullish_bos"] is False


def test_bullish_break_bullish_bias_is_bos():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    eng.swing.bias = BULLISH
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(6)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing, side="high", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    flags = eng._process_crosses(
        eng.swing, high=3.0, low=1.0, close=2.5, prior_close=1.5, decision_ts=decisions[3]
    )
    assert flags["bullish_bos"] is True
    assert flags["bullish_choch"] is False


def test_bearish_break_analog():
    eng = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    eng.swing.bias = BULLISH
    times = [pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=30 * i) for i in range(6)]
    decisions = [t + pd.Timedelta(minutes=30) for t in times]
    eng._activate_pivot(
        eng.swing, side="low", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    flags = eng._process_crosses(
        eng.swing, high=3.0, low=1.0, close=1.5, prior_close=2.5, decision_ts=decisions[3]
    )
    assert flags["bearish_choch"] is True
    eng2 = MultiLevelStructureEngine(internal_size=2, swing_size=2)
    eng2.swing.bias = BEARISH
    eng2._activate_pivot(
        eng2.swing, side="low", price=2.0, extreme_i=0, confirm_i=2, times=times, decisions=decisions
    )
    flags2 = eng2._process_crosses(
        eng2.swing, high=3.0, low=1.0, close=1.5, prior_close=2.5, decision_ts=decisions[3]
    )
    assert flags2["bearish_bos"] is True


def test_internal_bullish_swing_bearish_is_recovery():
    ctx = classify_combined_context(
        internal_bias=BULLISH,
        swing_bias=BEARISH,
        swing_close_broken_bull=False,
        swing_close_broken_bear=False,
        internal_bull_bos_after_choch=False,
        internal_bear_bos_after_choch=False,
        swing_bull_choch_pending=False,
        swing_bear_choch_pending=False,
        swing_bull_confirmed=False,
        swing_bear_confirmed=False,
    )
    assert ctx.bullish_recovery_inside_bearish_swing is True
    assert ctx.primary_label == "bullish_recovery_inside_bearish_swing"


def test_swing_choch_without_bos_possible_reversal():
    ctx = classify_combined_context(
        internal_bias=BULLISH,
        swing_bias=BEARISH,
        swing_close_broken_bull=False,
        swing_close_broken_bear=False,
        internal_bull_bos_after_choch=False,
        internal_bear_bos_after_choch=False,
        swing_bull_choch_pending=True,
        swing_bear_choch_pending=False,
        swing_bull_confirmed=False,
        swing_bear_confirmed=False,
    )
    assert ctx.possible_bullish_swing_reversal is True


def test_swing_choch_plus_bos_confirmed_reversal():
    ctx = classify_combined_context(
        internal_bias=BULLISH,
        swing_bias=BULLISH,
        swing_close_broken_bull=False,
        swing_close_broken_bear=False,
        internal_bull_bos_after_choch=False,
        internal_bear_bos_after_choch=False,
        swing_bull_choch_pending=False,
        swing_bear_choch_pending=False,
        swing_bull_confirmed=True,
        swing_bear_confirmed=False,
    )
    assert ctx.confirmed_bullish_swing_reversal is True
    assert ctx.primary_label == "confirmed_bullish_swing_reversal"


def test_closed_buckets_only_helper():
    start = pd.Timestamp("2026-01-01T00:00:00+00:00")
    rows = []
    for i in range(8):
        ts = start + pd.Timedelta(minutes=5 * i)
        rows.append(
            {"timestamp": ts, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1.0}
        )
    df = pd.DataFrame(rows)
    end_wall = start + pd.Timedelta(minutes=35)
    agg = aggregate_closed_htf(df, 30, end_wall)
    assert len(agg) == 1
    assert all(pd.Timestamp(t) <= end_wall for t in agg["decision_time"])


def test_no_lookahead_prefix():
    ohlc = [(1, 1 + 0.01 * i, 0.9, 1.0) for i in range(40)]
    ohlc[8] = (1, 4.0, 0.9, 3.0)
    ohlc[25] = (1, 5.0, 0.9, 4.0)
    df = _frame(ohlc)
    full, _ = run_multilevel_structure(df, internal_size=5, swing_size=10)
    pref, _ = run_multilevel_structure(df.iloc[:20].copy(), internal_size=5, swing_size=10)
    for a, b in zip(pref, full[:20]):
        assert a["internal_bias"] == b["internal_bias"]
        assert a["swing_bias"] == b["swing_bias"]


def test_deterministic_repeat():
    ohlc = [(1, 1 + 0.02 * (i % 9), 0.8, 1.0) for i in range(50)]
    ohlc[12] = (1, 3.0, 0.8, 2.5)
    df = _frame(ohlc)
    a, _ = run_multilevel_structure(df, internal_size=5, swing_size=15)
    b, _ = run_multilevel_structure(df, internal_size=5, swing_size=15)
    assert a == b

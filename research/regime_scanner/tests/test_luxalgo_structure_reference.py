"""Unit tests for LuxAlgo structure reference (CC BY-NC-SA 4.0 attribution)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.luxalgo_structure_reference import (
    BEARISH,
    BEARISH_LEG,
    BULLISH,
    BULLISH_LEG,
    LuxStructureEngine,
    new_leg_high,
    new_leg_low,
    pine_highest,
    pine_lowest,
    run_lux_structure,
)

ROOT = Path("research/regime_scanner")
PROTECTED = {
    "market_regime.py": "1e79f30af2ddf95c3f91c1b1a012cded",
    "trend_structure.py": "4976cbd9921e9df58dcfaace5cb125a2",
    "trend_state_machine.py": "3a8ed63f60f86ec29bf05e7831bb3349",
    "trend_state_policy.py": "412f672652b66c93b7d44d4b692da2aa",
    "trend_zones.py": "6378f736a184e51efe070ebd2c2d969c",
}


def _md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def _frame(ohlc: list[tuple[float, float, float, float]], minutes: int = 30) -> pd.DataFrame:
    """Build closed-bar frame from (o,h,l,c) tuples."""
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


def test_pine_highest_lowest_window_excludes_candidate():
    # Bars: indices 0..5. At i=5, size=3 → ta.highest(3)=max(high[3],high[4],high[5])
    # high[size]=high[2] is excluded from the window.
    highs = np.array([1.0, 2.0, 10.0, 3.0, 4.0, 5.0], dtype=float)
    lows = np.array([0.5, 1.0, 0.1, 2.0, 1.5, 1.0], dtype=float)
    assert pine_highest(highs, 5, 3) == 5.0  # max(3,4,5)
    assert pine_lowest(lows, 5, 3) == 1.0  # min(2,1.5,1)
    # Candidate high[3] at i=5 size=3 is highs[2]=10 > window max 5 → new leg high
    assert new_leg_high(highs, 5, 3) is True
    # Candidate low[3]=lows[2]=0.1 < window min 1 → new leg low
    assert new_leg_low(lows, 5, 3) is True


def test_leg_semantics_bearish_then_bullish():
    # Construct so a distant high is greater than recent highs → BEARISH_LEG
    # then a distant low is less than recent lows → BULLISH_LEG
    size = 2
    # i: 0 1 2 3 4 5 6
    # Need at i=4: high[2] > max(high[3],high[4])
    # Need at i=6: low[4] < min(low[5],low[6])
    ohlc = [
        (1, 1.0, 0.9, 0.95),
        (1, 1.1, 0.95, 1.0),
        (1, 3.0, 1.0, 2.5),  # pivot high candidate later
        (2, 2.2, 1.8, 2.0),
        (2, 2.1, 0.5, 0.8),  # low candidate; also confirmation for high
        (1, 1.5, 0.7, 1.2),
        (1, 1.4, 0.8, 1.0),
    ]
    df = _frame(ohlc)
    eng = LuxStructureEngine(timeframe="30m", internal_size=size, swing_size=size)
    bars = eng.run(df)
    # After enough bars, leg should have flipped at least once
    legs = [b.swing_leg for b in bars]
    assert BEARISH_LEG in legs
    assert BULLISH_LEG in legs


def test_pivot_confirmed_only_after_size_bars():
    size = 3
    # Isolated spike high at index 3; confirmation when i=6
    ohlc = [
        (1, 1.0, 0.9, 0.95),
        (1, 1.1, 0.9, 1.0),
        (1, 1.2, 0.95, 1.1),
        (1, 5.0, 1.0, 4.0),  # pivot high candle
        (4, 4.0, 3.0, 3.5),
        (3, 3.6, 2.8, 3.0),
        (3, 3.2, 2.5, 2.8),  # confirmation bar index 6
        (2, 2.9, 2.4, 2.6),
    ]
    df = _frame(ohlc)
    rows = run_lux_structure(df, timeframe="30m", internal_size=size, swing_size=size)
    confirms = [r for r in rows if r["swing_new_pivot_high"]]
    assert len(confirms) >= 1
    c0 = confirms[0]
    pivot_ts = pd.Timestamp(c0["pivot_candle_timestamp"])
    conf_ts = pd.Timestamp(c0["confirmation_timestamp"])
    decision_ts = pd.Timestamp(c0["event_decision_timestamp"])
    bar = pd.Timedelta(minutes=30)
    # pivot open → confirmation bar open is exactly size bars; decision = confirmation bar close
    confirm_open = conf_ts - bar
    assert confirm_open - pivot_ts == size * bar
    assert decision_ts == conf_ts
    # Not known on the pivot candle itself
    assert conf_ts > pivot_ts + bar * (size - 1)

def test_pivot_and_decision_timestamps_remain_separate():
    size = 2
    ohlc = [
        (1, 1.0, 0.9, 0.95),
        (1, 1.2, 0.9, 1.1),
        (1, 4.0, 1.0, 3.5),
        (3, 3.0, 2.0, 2.5),
        (2, 2.6, 1.8, 2.0),
    ]
    rows = run_lux_structure(_frame(ohlc), timeframe="30m", internal_size=size, swing_size=size)
    for r in rows:
        if r["swing_new_pivot_high"] or r["swing_new_pivot_low"]:
            assert r["pivot_candle_timestamp"] != r["event_decision_timestamp"]
            assert r["confirmation_timestamp"] == r["event_decision_timestamp"]


def test_no_backpainting_in_decision_timeline():
    size = 2
    ohlc = [
        (1, 1.0, 0.9, 0.95),
        (1, 1.2, 0.9, 1.1),
        (1, 4.0, 1.0, 3.5),
        (3, 3.0, 2.0, 2.5),
        (2, 2.6, 1.8, 2.0),
        (2, 2.4, 1.7, 2.1),
    ]
    rows = run_lux_structure(_frame(ohlc), timeframe="30m", internal_size=size, swing_size=size)
    for r in rows:
        # Decision outputs never use pivot candle as event_decision_timestamp for pivots
        if r["swing_new_pivot_high"] or r["swing_new_pivot_low"]:
            assert pd.Timestamp(r["event_decision_timestamp"]) > pd.Timestamp(r["pivot_candle_timestamp"])


def _bos_choch_fixture(*, prior_bias: int, bullish: bool) -> list[dict]:
    """Build a short path that produces BOS or CHoCH with controlled prior bias."""
    # Manually drive engine state is hard; use crafted prices.
    # Sequence: establish swing high/low pivots, set bias via first break, then second break.
    size = 2
    if bullish:
        # pivot high at bar2=3.0, confirm at bar4; then close cross above 3.0
        ohlc = [
            (1.0, 1.2, 0.9, 1.0),
            (1.0, 1.3, 0.95, 1.1),
            (1.1, 3.0, 1.0, 2.8),  # high pivot
            (2.5, 2.7, 2.0, 2.2),
            (2.2, 2.5, 1.8, 2.0),  # confirm high (leg change)
            (2.0, 2.1, 1.5, 1.6),  # make a low pivot path
            (1.6, 1.8, 1.4, 1.5),
            (1.5, 1.7, 1.0, 1.2),  # low pivot candidate
            (1.2, 1.4, 1.05, 1.3),
            (1.3, 1.5, 1.1, 1.25),  # confirm low
            (1.3, 3.2, 1.2, 3.1),  # close cross above swing high → BOS/CHoCH
        ]
    else:
        ohlc = [
            (3.0, 3.2, 2.8, 3.0),
            (3.0, 3.3, 2.9, 3.1),
            (3.0, 3.1, 1.0, 1.2),  # low pivot
            (1.5, 2.0, 1.3, 1.8),
            (1.8, 2.1, 1.4, 1.9),  # confirm low
            (2.0, 4.0, 1.9, 3.8),  # high pivot
            (3.5, 3.7, 3.0, 3.2),
            (3.2, 3.4, 2.9, 3.0),  # confirm high
            (3.0, 3.1, 0.8, 0.9),  # close cross under swing low
        ]
    eng = LuxStructureEngine(timeframe="30m", internal_size=size, swing_size=size)
    # Seed bias before run by setting trend after construction — inject via attribute
    eng.swing_trend.bias = prior_bias
    eng.internal_trend.bias = prior_bias
    return [b.to_dict() for b in eng.run(_frame(ohlc))]


def test_bullish_cross_bos_when_prior_bullish_bias():
    rows = _bos_choch_fixture(prior_bias=BULLISH, bullish=True)
    assert any(r["swing_bullish_bos"] for r in rows)
    assert not any(r["swing_bullish_choch"] for r in rows)


def test_bullish_cross_choch_when_prior_bearish_bias():
    rows = _bos_choch_fixture(prior_bias=BEARISH, bullish=True)
    assert any(r["swing_bullish_choch"] for r in rows)
    assert not any(r["swing_bullish_bos"] for r in rows)


def test_bearish_cross_bos_when_prior_bearish_bias():
    rows = _bos_choch_fixture(prior_bias=BEARISH, bullish=False)
    assert any(r["swing_bearish_bos"] for r in rows)
    assert not any(r["swing_bearish_choch"] for r in rows)


def test_bearish_cross_choch_when_prior_bullish_bias():
    rows = _bos_choch_fixture(prior_bias=BULLISH, bullish=False)
    assert any(r["swing_bearish_choch"] for r in rows)
    assert not any(r["swing_bearish_bos"] for r in rows)


def test_pivot_level_broken_only_once():
    size = 2
    ohlc = [
        (1.0, 1.2, 0.9, 1.0),
        (1.0, 1.3, 0.95, 1.1),
        (1.1, 3.0, 1.0, 2.8),
        (2.5, 2.7, 2.0, 2.2),
        (2.2, 2.5, 1.8, 2.0),
        (2.0, 2.1, 1.5, 1.6),
        (1.6, 1.8, 1.4, 1.5),
        (1.5, 1.7, 1.0, 1.2),
        (1.2, 1.4, 1.05, 1.3),
        (1.3, 1.5, 1.1, 1.25),
        (1.3, 3.2, 1.2, 3.1),  # first break
        (3.1, 3.5, 3.0, 3.4),  # still above — must not re-fire
        (3.4, 3.6, 3.2, 3.5),
    ]
    eng = LuxStructureEngine(timeframe="30m", internal_size=size, swing_size=size)
    eng.swing_trend.bias = BEARISH
    rows = [b.to_dict() for b in eng.run(_frame(ohlc))]
    breaks = [r for r in rows if r["swing_bullish_bos"] or r["swing_bullish_choch"]]
    assert len(breaks) == 1


def test_closed_buckets_only_helper_contract():
    from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf

    # 5m bars for one incomplete + one complete 30m bucket
    start = pd.Timestamp("2026-01-01T00:00:00+00:00")
    rows = []
    for i in range(8):  # 00:00..00:35 → complete 00:00-00:30, incomplete 00:30-01:00
        ts = start + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    end_wall = start + pd.Timedelta(minutes=35)  # mid incomplete bucket
    agg = aggregate_closed_htf(df, 30, end_wall)
    assert len(agg) == 1
    assert agg.iloc[0]["decision_time"] == start + pd.Timedelta(minutes=30)
    assert all(pd.Timestamp(t) <= end_wall for t in agg["decision_time"])


def test_no_lookahead_decision_uses_only_past_and_current():
    size = 2
    ohlc = [
        (1, 1.0, 0.9, 0.95),
        (1, 1.2, 0.9, 1.1),
        (1, 4.0, 1.0, 3.5),
        (3, 3.0, 2.0, 2.5),
        (2, 2.6, 1.8, 2.0),
        (2, 9.0, 1.7, 8.0),  # future spike must not affect earlier bars
    ]
    df = _frame(ohlc)
    full = run_lux_structure(df, timeframe="30m", internal_size=size, swing_size=size)
    prefix = run_lux_structure(df.iloc[:5].copy(), timeframe="30m", internal_size=size, swing_size=size)
    for a, b in zip(prefix, full[:5]):
        assert a["swing_leg"] == b["swing_leg"]
        assert a["swing_bias"] == b["swing_bias"]
        assert a["swing_pivot_high"] == b["swing_pivot_high"]


def test_deterministic_repeat():
    ohlc = [(1, 1 + i * 0.1, 0.9, 1.0) for i in range(40)]
    # add a spike
    ohlc[10] = (1, 5.0, 0.9, 4.0)
    df = _frame(ohlc)
    a = run_lux_structure(df, timeframe="30m", internal_size=5, swing_size=10)
    b = run_lux_structure(df, timeframe="30m", internal_size=5, swing_size=10)
    assert a == b


def test_protected_module_hashes_unchanged():
    for name, expected in PROTECTED.items():
        got = _md5(ROOT / name)
        assert got == expected, f"{name}: {got} != {expected}"


def test_reference_not_imported_by_policy_or_state_machine():
    for name in ("trend_state_policy.py", "trend_state_machine.py", "trend_structure.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "luxalgo_structure" not in text


def test_attribution_and_license_present():
    text = Path("research/regime_scanner/luxalgo_structure_reference.py").read_text(encoding="utf-8")
    assert "CC BY-NC-SA 4.0" in text
    assert "LuxAlgo" in text

"""Mirror parity tests: Protected-High as mathematical reflection of Protected-Low."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orderbook_analyse.c3_protected_low_event_driven_decision import find_causal_decision
from orderbook_analyse.c3_protected_low_historical_catalog import build_candidates
from orderbook_analyse.c3_protected_structure_historical_catalog import build_candidates_high
from orderbook_analyse.c3_protected_structure_mirror import (
    MIRROR_PARITY_TABLE,
    find_causal_decision_high,
    flip_candidate_side,
    mirror_ticks,
)


def _tick(ts: datetime, price: float, side: str = "sell", notional: float = 500.0) -> SimpleNamespace:
    return SimpleNamespace(trade_ts=ts, price=price, side=side, notional=notional)


def _synth_breakdown_ticks(known: datetime, level: float = 1.0) -> list[SimpleNamespace]:
    """Sparse path that confirms BREAKDOWN via path_no_reclaim (+30m, lower low)."""
    ticks: list[SimpleNamespace] = []
    for m in range(0, 35):
        px = 0.997 if m < 5 else 0.994  # deeper after +5m; ~30–60 bps below
        for s in (0, 20, 40):
            ticks.append(_tick(known + timedelta(minutes=m, seconds=s), px, side="sell", notional=800.0))
    return ticks


def _synth_reclaim_ticks(known: datetime, level: float = 1.0) -> list[SimpleNamespace]:
    reclaim = known + timedelta(seconds=30)
    ticks = [_tick(known + timedelta(seconds=1), 0.99, side="sell", notional=500.0)]
    ticks.append(_tick(reclaim, 1.01, side="buy", notional=2000.0))
    for s in range(1, 70):
        ticks.append(
            _tick(reclaim + timedelta(seconds=s), 1.01 + s * 0.00001, side="buy", notional=100.0)
        )
    return ticks


def test_mirror_breakdown_to_breakout_same_ts() -> None:
    known = datetime(2026, 7, 26, 11, 50, tzinfo=timezone.utc)
    level = 1.0
    synth = _synth_breakdown_ticks(known, level)
    late = known + timedelta(hours=1)
    low_dec = find_causal_decision(
        synth, level=level, available_at=known, late_end=late, check_every_s=1
    )
    assert low_dec["outcome"] == "BREAKDOWN_CONFIRMED"
    assert low_dec["decision_ts"] is not None

    # High path = mirror of low-breakdown; high decision must be BREAKOUT at same ts
    high_ticks = mirror_ticks(synth, level)
    high_dec = find_causal_decision_high(
        high_ticks, level=level, available_at=known, late_end=late, check_every_s=1
    )
    assert high_dec["outcome"] == "BREAKOUT_CONFIRMED"
    assert high_dec["decision_ts"] == low_dec["decision_ts"]


def test_mirror_reclaim_to_reclaim_down_same_ts() -> None:
    known = datetime(2026, 7, 26, 11, 50, tzinfo=timezone.utc)
    level = 1.0
    synth = _synth_reclaim_ticks(known, level)
    late = known + timedelta(hours=1)
    low_dec = find_causal_decision(
        synth, level=level, available_at=known, late_end=late, check_every_s=1
    )
    assert low_dec["outcome"] == "RECLAIM_CONFIRMED"
    assert low_dec["decision_ts"] is not None

    high_ticks = mirror_ticks(synth, level)
    high_dec = find_causal_decision_high(
        high_ticks, level=level, available_at=known, late_end=late, check_every_s=1
    )
    assert high_dec["outcome"] == "RECLAIM_DOWN_CONFIRMED"
    assert high_dec["decision_ts"] == low_dec["decision_ts"]


def test_candidate_sides_flip() -> None:
    known = datetime(2026, 7, 26, 11, 50, tzinfo=timezone.utc)
    level = 1.0
    synth = _synth_breakdown_ticks(known, level)
    late = known + timedelta(hours=1)
    low_dec = find_causal_decision(
        synth, level=level, available_at=known, late_end=late, check_every_s=1
    )
    assert low_dec["outcome"] == "BREAKDOWN_CONFIRMED"
    low_long, low_short, _ = build_candidates(
        event_id="E_LOW",
        symbol="APTUSDT",
        level=level,
        available_at=known,
        outcome="BREAKDOWN_CONFIRMED",
        decision=low_dec,
        ticks=synth,
        late_end=late,
    )
    assert low_short and not low_long

    high_ticks = mirror_ticks(synth, level)
    high_dec = find_causal_decision_high(
        high_ticks, level=level, available_at=known, late_end=late, check_every_s=1
    )
    assert high_dec["outcome"] == "BREAKOUT_CONFIRMED"
    h_long, h_short, _ = build_candidates_high(
        event_id="E_HIGH",
        symbol="APTUSDT",
        level=level,
        available_at=known,
        outcome="BREAKOUT_CONFIRMED",
        decision=high_dec,
        ticks=high_ticks,
        late_end=late,
    )
    assert h_long and not h_short
    assert all(c["side"] == "LONG" for c in h_long)
    assert all(c.get("source") == "PROTECTED_HIGH_BREAKOUT" for c in h_long)
    assert flip_candidate_side("SHORT") == "LONG"
    assert flip_candidate_side("LONG") == "SHORT"


def test_mirror_parity_table_present() -> None:
    assert MIRROR_PARITY_TABLE
    assert MIRROR_PARITY_TABLE["BREAKDOWN_CONFIRMED"] == "BREAKOUT_CONFIRMED"
    assert MIRROR_PARITY_TABLE["RECLAIM_CONFIRMED"] == "RECLAIM_DOWN_CONFIRMED"
    assert MIRROR_PARITY_TABLE["protected_low"] == "protected_high"


def test_mirror_ticks_flips_price_and_side() -> None:
    known = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    level = 10.0
    ticks = [_tick(known, 9.0, side="Buy", notional=1.0)]
    mirrored = mirror_ticks(ticks, level)
    assert abs(mirrored[0].price - 11.0) < 1e-12
    assert mirrored[0].side == "Sell"
    assert mirrored[0].trade_ts == known
    assert mirrored[0].notional == 1.0

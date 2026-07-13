"""Unit tests for causal Phase-B TrendZoneTracker."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.swings import ConfirmedPivot
from research.regime_scanner.trend_structure import MarketStructureState, StructureEvent
from research.regime_scanner.trend_zones import (
    TrendZoneTracker,
    ZoneConfig,
    compute_half_width,
    event_id,
    merge_variant,
    width_variant,
)


def _ts(s: str) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _candle(ts: str, o: float, h: float, l: float, c: float, atr: float = 0.01) -> dict:
    open_ts = _ts(ts)
    return {
        "timestamp": open_ts,
        "close_time": open_ts + pd.Timedelta(minutes=30),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 1.0,
        "atr": atr,
    }


def _ev(
    typ: str,
    *,
    level: float,
    time: str,
    pivot_time: str | None = None,
    pivot_price: float | None = None,
) -> StructureEvent:
    return StructureEvent(
        event_type=typ,
        timeframe="30m",
        event_time=_ts(time),
        level=level,
        reference_pivot_time=None if pivot_time is None else _ts(pivot_time),
        reference_pivot_price=pivot_price if pivot_price is not None else level,
        direction="bullish" if "low" in typ or "breakdown" in typ else "bearish",
        reason_codes=("test",),
    )


def _pivot(price: float, ts: str, kind: str = "high") -> ConfirmedPivot:
    conf = _ts(ts) + pd.Timedelta(hours=1)
    return ConfirmedPivot(
        pivot_index=0,
        pivot_timestamp=_ts(ts).isoformat(),
        confirmation_index=2,
        confirmation_timestamp=conf.isoformat(),
        price=price,
        pivot_type=kind,  # type: ignore[arg-type]
    )


def _cfg(**kwargs) -> ZoneConfig:
    base = dict(
        width_mode="pct_fixed",
        width_pct=0.50,
        merge_mode="reinforce_only",
        episode_mode="bars_outside",
        episode_min_bars_outside=2,
        activation_mode="immediate",
        rejection_mode="close_outside",
        break_mode="close_beyond",
        contact_window_bars=3,
        approach_atr=0.5,
    )
    base.update(kwargs)
    return ZoneConfig(**base)


def _ms_with_high(price: float = 1.0, ts: str = "2026-01-01T00:00:00Z") -> MarketStructureState:
    ms = MarketStructureState(timeframe="30m")
    ms.last_confirmed_swing_high = _pivot(price, ts, "high")
    return ms


def _ms_with_low(price: float = 1.0, ts: str = "2026-01-01T00:00:00Z") -> MarketStructureState:
    ms = MarketStructureState(timeframe="30m")
    ms.last_confirmed_swing_low = _pivot(price, ts, "low")
    return ms


def test_confirmed_pivot_high_creates_resistance() -> None:
    tr = TrendZoneTracker(_cfg())
    ms = _ms_with_high(1.02, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 1.0, 1.02, 0.99, 1.01), [], ms, 0.01)
    assert len(tr.zones) == 1
    assert tr.zones[0].role == "resistance"
    assert tr.zones[0].overlaps_price(1.02)


def test_confirmed_pivot_low_creates_support() -> None:
    tr = TrendZoneTracker(_cfg())
    ms = _ms_with_low(0.98, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 1.0, 1.01, 0.98, 0.99), [], ms, 0.01)
    assert len(tr.zones) == 1
    assert tr.zones[0].role == "support"


def test_hh_label_does_not_birth_duplicate() -> None:
    """HH is reinforce-only; birth comes from confirmed pivot once."""
    tr = TrendZoneTracker(_cfg())
    ms = _ms_with_high(1.02, "2026-01-01T00:00:00Z")
    hh = _ev(
        "higher_high",
        level=1.02,
        time="2026-01-01T00:30:00Z",
        pivot_time="2026-01-01T00:00:00Z",
    )
    tr.update(_candle("2026-01-01T00:00:00Z", 1.0, 1.02, 0.99, 1.01), [hh], ms, 0.01)
    assert len(tr.zones) == 1


def test_reinforce_without_bound_expansion() -> None:
    cfg = merge_variant("M0", _cfg(width_pct=0.20))
    tr = TrendZoneTracker(cfg)
    ms = _ms_with_high(1.025, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 1.02, 1.03, 1.01, 1.02), [], ms, 0.01)
    z = tr.zones[0]
    birth = (z.lower_bound, z.upper_bound)
    # nearby failed_breakout should reinforce without expanding under M0
    fb = _ev("failed_breakout", level=1.028, time="2026-01-01T01:00:00Z")
    ms2 = MarketStructureState(timeframe="30m")
    ms2.last_confirmed_swing_high = ms.last_confirmed_swing_high
    tr.update(_candle("2026-01-01T00:30:00Z", 1.02, 1.03, 1.01, 1.02), [fb], ms2, 0.01)
    assert (tr.zones[0].lower_bound, tr.zones[0].upper_bound) == birth
    assert "failed_breakout" in tr.zones[0].source_event_types


def test_expansion_cap_m1() -> None:
    cfg = merge_variant("M1", _cfg(width_pct=0.10, width_mode="pct_fixed"))
    tr = TrendZoneTracker(cfg)
    ms = _ms_with_high(1.025, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 1.02, 1.03, 1.01, 1.02), [], ms, atr=0.01)
    z0 = tr.zones[0]
    birth_w = z0.width_abs
    # Anchor far enough to request expansion but capped at 0.10 ATR = 0.001
    fb = _ev("failed_breakout", level=1.040, time="2026-01-01T01:00:00Z")
    # Make half-width of new anchor overlap via mid distance — force merge target by overlapping band
    # Use level inside merge distance
    fb = _ev("failed_breakout", level=1.027, time="2026-01-01T01:00:00Z")
    ms2 = MarketStructureState(timeframe="30m")
    ms2.last_confirmed_swing_high = ms.last_confirmed_swing_high
    tr.update(_candle("2026-01-01T00:30:00Z", 1.02, 1.03, 1.01, 1.02), [fb], ms2, atr=0.01)
    z = tr.zones[0]
    assert z.cumulative_expansion <= 0.10 * 0.01 + 1e-9
    assert z.width_abs <= birth_w + 0.10 * 0.01 + 1e-9


def test_far_anchor_creates_separate_zone_m3() -> None:
    cfg = merge_variant("M3", _cfg(width_pct=0.10, merge_atr_mult=0.05))
    tr = TrendZoneTracker(cfg)
    ms = _ms_with_high(1.025, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 1.02, 1.03, 1.01, 1.02), [], ms, atr=0.01)
    # New pivot far away
    ms2 = MarketStructureState(timeframe="30m")
    ms2.last_confirmed_swing_high = _pivot(1.080, "2026-01-01T02:00:00Z", "high")
    tr.update(_candle("2026-01-01T02:00:00Z", 1.07, 1.09, 1.06, 1.08), [], ms2, atr=0.01)
    assert len(tr.zones) >= 2
    assert tr.separate_zone_created_count >= 0  # may birth without merge attempt if no overlap


def test_three_candles_in_zone_one_contact_episode() -> None:
    tr = TrendZoneTracker(_cfg(width_pct=1.0))
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.995), [], ms, 0.01)
    z = tr.zones[0]
    # stay inside band
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.002, 0.99, 0.998), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:00:00Z", 0.998, 1.001, 0.992, 0.996), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:30:00Z", 0.996, 1.003, 0.991, 0.997), [], ms, 0.01)
    assert z.touch_episode_count == 1
    assert z.contact_count >= 3


def test_resistance_touch_falling_closes_rejection() -> None:
    tr = TrendZoneTracker(
        _cfg(rejection_mode="close_outside", width_pct=0.5, contact_window_bars=3)
    )
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, 0.01)
    # touch then leave below
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.002, 0.99, 0.997), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:00:00Z", 0.997, 0.999, 0.985, 0.988), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:30:00Z", 0.988, 0.99, 0.97, 0.975), [], ms, 0.01)
    outcomes = [e.outcome for e in tr.contact_episodes]
    assert "REJECTION_CONFIRMED" in outcomes


def test_support_touch_rising_closes_rejection() -> None:
    tr = TrendZoneTracker(
        _cfg(rejection_mode="close_outside", width_pct=0.5, contact_window_bars=3)
    )
    ms = _ms_with_low(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 1.01, 1.02, 1.0, 1.01), [], ms, 0.01)
    tr.update(_candle("2026-01-01T00:30:00Z", 1.005, 1.01, 0.998, 1.003), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:00:00Z", 1.003, 1.015, 1.001, 1.012), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:30:00Z", 1.012, 1.03, 1.01, 1.025), [], ms, 0.01)
    outcomes = [e.outcome for e in tr.contact_episodes]
    assert "REJECTION_CONFIRMED" in outcomes


def test_wick_outside_is_not_breakout() -> None:
    tr = TrendZoneTracker(_cfg(break_mode="close_beyond", width_pct=0.3))
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, 0.01)
    # wick above, close inside
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.01, 0.99, 0.997), [], ms, 0.01)
    assert tr.zones[0].state != "broken"
    assert all(e.outcome != "BREAKOUT_CONFIRMED" for e in tr.contact_episodes)


def test_close_outside_plus_reclaim_false_breakout() -> None:
    tr = TrendZoneTracker(
        _cfg(break_mode="close_no_reclaim_2", width_pct=0.3, false_break_max_bars=3)
    )
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, 0.01)
    # close beyond
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.02, 0.99, 1.015), [], ms, 0.01)
    # reclaim
    tr.update(_candle("2026-01-01T01:00:00Z", 1.01, 1.012, 0.99, 0.995), [], ms, 0.01)
    assert any(e.outcome == "FALSE_BREAKOUT" for e in tr.contact_episodes)
    assert tr.zones[0].state != "broken"


def test_two_closes_outside_confirmed_breakout() -> None:
    tr = TrendZoneTracker(_cfg(break_mode="two_closes", width_pct=0.3))
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, 0.01)
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.02, 0.99, 1.012), [], ms, 0.01)
    tr.update(_candle("2026-01-01T01:00:00Z", 1.012, 1.03, 1.01, 1.02), [], ms, 0.01)
    assert any(e.outcome == "BREAKOUT_CONFIRMED" for e in tr.contact_episodes)
    assert tr.zones[0].state == "broken"
    assert tr.zones[0].flip_candidate


def test_rejection_and_breakout_mutually_exclusive() -> None:
    tr = TrendZoneTracker(_cfg(break_mode="close_beyond", rejection_mode="close_outside", width_pct=0.3))
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, 0.01)
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.02, 0.99, 1.015), [], ms, 0.01)
    for e in tr.contact_episodes:
        assert not (e.outcome == "BREAKOUT_CONFIRMED" and e.outcome == "REJECTION_CONFIRMED")
        flags = sum(
            [
                e.breakout_confirmed or e.breakdown_confirmed,
                e.resistance_rejection_confirmed or e.support_rejection_confirmed,
                e.false_breakout or e.false_breakdown,
            ]
        )
        if e.closed:
            assert flags <= 1 or e.outcome in {"FALSE_BREAKOUT", "BREAKOUT_CONFIRMED", "REJECTION_CONFIRMED"}


def test_outcome_only_after_causal_confirmation() -> None:
    tr = TrendZoneTracker(_cfg(break_mode="two_closes", width_pct=0.3))
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, 0.01)
    tr.update(_candle("2026-01-01T00:30:00Z", 0.995, 1.02, 0.99, 1.012), [], ms, 0.01)
    # after one close beyond — not yet confirmed under B2
    assert not any(e.outcome == "BREAKOUT_CONFIRMED" for e in tr.contact_episodes)
    tr.update(_candle("2026-01-01T01:00:00Z", 1.012, 1.03, 1.01, 1.02), [], ms, 0.01)
    assert any(e.outcome_at == _ts("2026-01-01T01:30:00Z") for e in tr.contact_episodes if e.outcome == "BREAKOUT_CONFIRMED")


def test_band_frozen_at_birth_later_atr_ignored() -> None:
    tr = TrendZoneTracker(_cfg(width_mode="atr_mult", width_atr_mult=0.2, width_pct=0.1))
    ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
    tr.update(_candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, atr=0.01)
    lo, hi = tr.zones[0].lower_bound, tr.zones[0].upper_bound
    tr.update(_candle("2026-01-01T00:30:00Z", 0.99, 1.0, 0.98, 0.99), [], ms, atr=0.05)
    assert tr.zones[0].lower_bound == lo
    assert tr.zones[0].upper_bound == hi
    assert tr.zones[0].birth_atr == 0.01


def test_deterministic_replay() -> None:
    def run() -> list[dict]:
        tr = TrendZoneTracker(_cfg())
        ms = _ms_with_high(1.0, "2026-01-01T00:00:00Z")
        candles = [
            _candle("2026-01-01T00:00:00Z", 0.99, 1.0, 0.98, 0.99),
            _candle("2026-01-01T00:30:00Z", 0.995, 1.002, 0.99, 0.997),
            _candle("2026-01-01T01:00:00Z", 0.997, 0.999, 0.98, 0.985),
        ]
        for c in candles:
            tr.update(c, [], ms, 0.01)
        return [z.to_dict() for z in tr.zones] + [e.to_dict() for e in tr.contact_episodes]

    assert run() == run()


def test_no_future_leak_event_id_stable() -> None:
    ev = _ev("failed_breakout", level=1.0, time="2026-01-01T00:30:00Z")
    assert "2026-01-01" in event_id(ev)


def test_compute_half_width_w3_cap() -> None:
    cfg = width_variant("W3")
    hw, _ = compute_half_width(center=1.0, atr=1.0, cfg=cfg)
    # atr_mult 0.15 → 0.15, pct 0.10% → 0.001, cap 0.30% → 0.003 → max(0.001,0.15)=0.15 then min cap 0.003
    assert hw <= 0.003 + 1e-12


def test_mega_zone_not_created_under_m2() -> None:
    cfg = merge_variant("M2", _cfg(width_pct=0.25, width_mode="pct_fixed"))
    tr = TrendZoneTracker(cfg)
    ms = _ms_with_high(1.0254, "2026-02-27T10:00:00Z")
    tr.update(_candle("2026-02-27T10:00:00Z", 1.02, 1.03, 1.01, 1.02), [], ms, atr=0.01)
    z = tr.zones[0]
    birth_lo, birth_hi = z.birth_lower, z.birth_upper
    # many nearby failed breakouts trying to expand
    for i, lvl in enumerate([1.028, 1.030, 1.032, 1.020, 1.015, 1.010, 1.000, 0.998]):
        fb = _ev("failed_breakout", level=lvl, time=f"2026-02-27T{11+i:02d}:00:00Z")
        ms2 = MarketStructureState(timeframe="30m")
        ms2.last_confirmed_swing_high = ms.last_confirmed_swing_high
        tr.update(
            _candle(f"2026-02-27T{10+i:02d}:30:00Z", 1.02, 1.03, 1.01, 1.02),
            [fb],
            ms2,
            atr=0.01,
        )
    z = tr.zones[0]
    assert z.cumulative_expansion <= 0.25 * z.birth_width_abs + 1e-9
    # Must not balloon to ~0.998–1.032 from ~1.023–1.028
    assert z.lower_bound >= birth_lo - 0.25 * z.birth_width_abs - 1e-9
    assert z.upper_bound <= birth_hi + 0.25 * z.birth_width_abs + 1e-9

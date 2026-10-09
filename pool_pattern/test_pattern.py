"""Pattern rules on synthetic pools. No live market feed."""

from datetime import datetime, timedelta, timezone

from pool_pattern.confirm_15m import annotate
from pool_pattern.excursion import measure
from pool_pattern.machine import Phase, PhaseStamp, replay
from pool_pattern.signal import Signal, SignalStamp, read_signals
from pool_pattern.market import Bar, Observation, ensure_paths
from pool_pattern.profile import PatternProfile
from pool_pattern.snapshot import next_relevant_above, zones_at


def _pool(side: str, low: float, high: float, born: datetime, end: datetime | None = None):
    ensure_paths()
    from indicators.liquidity_location.models import LiquidityPool

    return LiquidityPool(
        pool_id=f"lld:DOGEUSDT:4h:{side}:{int(born.timestamp())}:{low:.5f}",
        symbol="DOGEUSDT",
        timeframe="4h",
        side=side,
        created_index=0,
        created_timestamp=born,
        source_index=0,
        source_timestamp=born - timedelta(hours=4),
        top_price=high,
        bottom_price=low,
        strength=None,
        active=end is None,
        invalidated_timestamp=end,
        metadata={"available_at": born},
    )


def _moment(day: int, hour: int) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=timezone.utc)


def _obs(day: int, hour: int, close: float, *, high: float, low: float, up: bool) -> Observation:
    if up:
        e9, e20, e59, e200 = close * 0.99, close * 0.98, close * 0.97, close * 0.90
    else:
        e9, e20, e59, e200 = close * 1.01, close * 1.02, close * 1.03, close * 1.10
    return Observation(_moment(day, hour), close, high, low, close, e9, e20, e59, e200)


def _stack(side: str, low: float, born: datetime, end: datetime | None = None):
    return [
        _pool(side, low, low * 1.02, born, end),
        _pool(side, low * 1.004, low * 1.024, born + timedelta(minutes=1), end),
        _pool(side, low * 1.008, low * 1.028, born + timedelta(minutes=2), end),
    ]


def test_a_gap_under_three_percent_is_one_zone() -> None:
    born = _moment(1, 0)
    pools = [_pool("upper", 1.00, 1.01, born), _pool("upper", 1.012, 1.02, born)]
    zones = [zone for zone in zones_at(pools, _moment(1, 8), PatternProfile()) if zone.side == "upper"]
    assert len(zones) == 1


def test_a_standalone_pool_stays_relevant() -> None:
    born = _moment(1, 0)
    pools = [_pool("upper", 1.00, 1.01, born), _pool("upper", 1.08, 1.09, born)]
    zones = [zone for zone in zones_at(pools, _moment(1, 8), PatternProfile()) if zone.side == "upper"]
    assert len(zones) == 2
    assert all(zone.relevant for zone in zones)


def test_a_thin_band_inside_a_wide_gap_is_not_the_next_pool() -> None:
    born = _moment(1, 0)
    pools = [
        *_stack("upper", 1.00, born),
        _pool("upper", 1.070, 1.074, born),
        *_stack("upper", 1.12, born),
    ]
    profile = PatternProfile()
    zones = zones_at(pools, _moment(1, 8), profile)
    thin = [zone for zone in zones if zone.side == "upper" and abs(zone.low - 1.070) < 1e-6]
    assert thin and thin[0].thin and not thin[0].relevant
    nxt, gap = next_relevant_above(zones, 1.028)
    assert nxt is not None
    assert nxt.low > 1.10
    assert gap >= profile.wide_upper_gap_pct


def test_one_bar_under_the_upper_pool_does_not_start_the_stair() -> None:
    born = _moment(1, 0)
    up = _stack("upper", 1.00, born)
    old = [_pool("lower", 0.78, 0.82, born)]
    pools = up + old
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 0.990, high=0.995, low=0.985, up=False),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[-1].phase is Phase.UPPER_BUILD


def test_two_collected_lower_steps_start_the_stair() -> None:
    born = _moment(1, 0)
    leg = _moment(2, 4)
    step_a = _pool("lower", 0.94, 0.96, leg, _moment(2, 12))
    step_b = _pool("lower", 0.90, 0.92, leg, _moment(2, 12))
    pools = [*_stack("upper", 1.00, born), _pool("lower", 0.78, 0.82, born), step_a, step_b]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 0.990, high=0.995, low=0.985, up=False),
        _obs(2, 12, 0.970, high=0.975, low=0.960, up=False),
        _obs(2, 16, 0.960, high=0.965, low=0.950, up=False),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[1].phase is Phase.UPPER_BUILD
    assert stamps[-1].phase is Phase.STAIR_DOWN


def test_a_wick_into_the_top_of_the_old_lower_does_not_end_the_stair() -> None:
    born = _moment(1, 0)
    pools = [*_stack("upper", 1.00, born), _pool("lower", 0.74, 0.86, born)]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 0.990, high=0.995, low=0.985, up=False),
        _obs(2, 12, 0.980, high=0.985, low=0.970, up=False),
        _obs(2, 16, 0.880, high=0.900, low=0.850, up=False),
        _obs(2, 20, 0.755, high=0.770, low=0.745, up=False),
        _obs(3, 0, 0.755, high=0.770, low=0.745, up=False),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[3].phase is Phase.STAIR_DOWN
    assert stamps[4].phase is Phase.STAIR_DOWN
    assert stamps[5].phase is Phase.STAIR_DOWN
    assert stamps[6].phase is Phase.EXHAUSTED


def test_a_lower_pool_within_three_percent_keeps_the_stair() -> None:
    born = _moment(1, 0)
    pools = [
        *_stack("upper", 1.00, born),
        _pool("lower", 0.90, 0.94, born),
        _pool("lower", 0.86, 0.88, _moment(2, 16)),
    ]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 0.970, high=0.975, low=0.960, up=False),
        _obs(2, 12, 0.960, high=0.965, low=0.950, up=False),
        _obs(2, 16, 0.905, high=0.910, low=0.902, up=False),
        _obs(2, 20, 0.905, high=0.910, low=0.902, up=False),
        _obs(3, 0, 0.868, high=0.875, low=0.862, up=False),
        _obs(3, 4, 0.868, high=0.875, low=0.862, up=False),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[4].phase is Phase.STAIR_DOWN
    assert stamps[5].phase is Phase.STAIR_DOWN
    assert stamps[7].phase is Phase.EXHAUSTED


def test_the_rise_starts_after_the_last_lower_pool() -> None:
    born = _moment(1, 0)
    pools = [
        *_stack("upper", 1.00, born),
        *_stack("upper", 0.78, born),
        _pool("lower", 0.70, 0.74, born),
    ]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 0.900, high=0.910, low=0.890, up=False),
        _obs(2, 12, 0.880, high=0.890, low=0.870, up=False),
        _obs(2, 16, 0.720, high=0.730, low=0.705, up=False),
        _obs(2, 20, 0.800, high=0.810, low=0.760, up=True),
        _obs(3, 0, 0.800, high=0.810, low=0.760, up=True),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[4].phase is Phase.STAIR_DOWN
    assert stamps[6].phase is Phase.UPPER_BUILD


def test_a_later_born_deeper_lower_does_not_block_the_next_rise() -> None:
    born = _moment(1, 0)
    later = _moment(2, 16)
    pools = [
        *_stack("upper", 1.00, born),
        *_stack("upper", 0.78, born),
        _pool("lower", 0.70, 0.74, born),
        _pool("lower", 0.64, 0.66, later),
    ]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 0.900, high=0.910, low=0.890, up=False),
        _obs(2, 12, 0.720, high=0.730, low=0.705, up=False),
        _obs(2, 16, 0.730, high=0.735, low=0.725, up=False),
        _obs(2, 20, 0.800, high=0.810, low=0.790, up=True),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[3].phase is Phase.STAIR_DOWN
    assert stamps[4].phase is Phase.STAIR_DOWN
    assert stamps[5].phase is Phase.UPPER_BUILD


def test_a_new_upper_below_a_broken_stair_starts_again() -> None:
    born = _moment(1, 0)
    leg = _moment(2, 4)
    step_a = _pool("lower", 1.44, 1.46, leg, _moment(2, 12))
    step_b = _pool("lower", 1.40, 1.42, leg, _moment(2, 12))
    pools = [
        *_stack("upper", 1.50, born),
        _pool("lower", 1.20, 1.28, born),
        *_stack("upper", 1.00, born),
        step_a,
        step_b,
    ]
    samples = [
        _obs(2, 0, 1.52, high=1.53, low=1.50, up=True),
        _obs(2, 4, 1.52, high=1.53, low=1.50, up=True),
        _obs(2, 12, 1.40, high=1.41, low=1.39, up=False),
        _obs(2, 16, 1.40, high=1.41, low=1.39, up=False),
        _obs(3, 0, 1.01, high=1.02, low=1.00, up=True),
        _obs(3, 4, 1.01, high=1.02, low=1.00, up=True),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[1].phase is Phase.UPPER_BUILD
    assert stamps[3].phase is Phase.STAIR_DOWN
    assert stamps[4].phase is Phase.STAIR_DOWN
    assert stamps[5].phase is Phase.UPPER_BUILD


def test_a_far_upper_pool_is_not_taken_without_delta() -> None:
    born = _moment(1, 0)
    pools = [*_stack("upper", 1.00, born), *_stack("upper", 1.12, born)]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
        _obs(2, 8, 1.05, high=1.125, low=1.04, up=True),
        _obs(2, 12, 1.05, high=1.125, low=1.04, up=True),
    ]
    stamps = replay(samples, pools, PatternProfile(), delta_taken=False)
    assert stamps[-1].upper_low is not None
    assert stamps[-1].upper_low < 1.05


def test_15m_confirms_the_fall_after_the_upper_is_taken() -> None:
    moment = _moment(2, 16)
    earlier = PhaseStamp(moment - timedelta(hours=4), Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    taken = PhaseStamp(moment, Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    bars = [
        Bar(moment - timedelta(hours=2), 1.02, 1.05, 1.01, 1.02),
        Bar(moment - timedelta(minutes=15), 0.98, 0.99, 0.97, 0.98),
    ]
    for step in range(16):
        close = 0.96
        bars.append(Bar(moment + timedelta(minutes=15 * step), close, close + 0.01, close - 0.01, close))
    noted = annotate([earlier, taken], bars)
    assert noted[-1].stamp.phase is Phase.STAIR_DOWN
    assert noted[-1].confirmed is True
    assert noted[-1].note == "held_under_taken_upper"


def test_1h_confirms_the_same_fall_without_a_later_bar() -> None:
    moment = _moment(2, 16)
    earlier = PhaseStamp(moment - timedelta(hours=4), Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    taken = PhaseStamp(moment, Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    bars = [Bar(moment - timedelta(hours=1), 0.98, 1.05, 0.97, 0.98)]
    for step in range(4):
        bars.append(Bar(moment + timedelta(hours=step), 0.96, 0.97, 0.95, 0.96))
    bars.append(Bar(moment + timedelta(hours=4), 1.20, 1.22, 1.18, 1.20))
    noted = annotate([earlier, taken], bars)
    assert noted[-1].stamp.phase is Phase.STAIR_DOWN
    assert noted[-1].confirmed is True


def test_15m_rejects_a_fall_that_goes_back_into_the_upper() -> None:
    moment = _moment(2, 16)
    earlier = PhaseStamp(moment - timedelta(hours=4), Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    taken = PhaseStamp(moment, Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    bars = [
        Bar(moment - timedelta(hours=2), 1.02, 1.05, 1.01, 1.02),
        Bar(moment - timedelta(minutes=15), 0.98, 0.99, 0.97, 0.98),
        Bar(moment, 1.02, 1.03, 1.01, 1.02),
        Bar(moment + timedelta(minutes=15), 1.02, 1.03, 1.01, 1.02),
        Bar(moment + timedelta(minutes=30), 1.02, 1.03, 1.01, 1.02),
        Bar(moment + timedelta(minutes=45), 1.02, 1.03, 1.01, 1.02),
    ]
    noted = annotate([earlier, taken], bars)
    assert noted[-1].stamp.phase is Phase.STAIR_DOWN
    assert noted[-1].confirmed is False


def _hold_bars(moment, *, minutes: int, count: int, close: float) -> list[Bar]:
    step = timedelta(minutes=minutes)
    return [Bar(moment + step * index, close, close + 0.01, close - 0.01, close) for index in range(count)]


def test_a_confirmed_stair_is_a_short_and_a_confirmed_upper_is_a_long() -> None:
    moment = _moment(2, 16)
    before = PhaseStamp(moment - timedelta(hours=4), Phase.UPPER_BUILD, "working_upper_cluster", upper_low=1.00)
    stair = PhaseStamp(moment, Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00, lower_high=0.70)
    upper = PhaseStamp(
        moment + timedelta(hours=4),
        Phase.UPPER_BUILD,
        "left_lower_zone",
        upper_low=1.00,
        lower_high=0.70,
    )
    still = PhaseStamp(
        moment + timedelta(hours=12),
        Phase.UPPER_BUILD,
        "working_upper_cluster",
        upper_low=1.00,
        lower_high=0.70,
    )
    bars_15m = [
        Bar(moment - timedelta(hours=2), 1.02, 1.05, 1.01, 1.02),
        Bar(moment - timedelta(minutes=15), 0.98, 0.99, 0.97, 0.98),
        *_hold_bars(moment, minutes=15, count=32, close=0.80),
    ]
    bars_1h = [
        Bar(moment - timedelta(hours=1), 0.98, 1.05, 0.97, 0.98),
        *_hold_bars(moment, minutes=60, count=8, close=0.80),
    ]
    noted = read_signals([before, stair, upper, still], bars_15m, bars_1h)
    assert noted[1].signal is Signal.SHORT
    assert noted[2].signal is Signal.LONG


def test_a_later_touch_in_the_same_stair_is_no_trade() -> None:
    moment = _moment(2, 16)
    again = PhaseStamp(moment + timedelta(hours=4), Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    before = PhaseStamp(moment - timedelta(hours=4), Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    bars_15m = [
        Bar(moment - timedelta(hours=2), 1.02, 1.05, 1.01, 1.02),
        Bar(moment - timedelta(minutes=15), 0.98, 0.99, 0.97, 0.98),
        *_hold_bars(moment, minutes=15, count=16, close=0.96),
    ]
    bars_1h = [
        Bar(moment - timedelta(hours=1), 0.98, 1.05, 0.97, 0.98),
        *_hold_bars(moment, minutes=60, count=4, close=0.96),
    ]
    assert read_signals([before, again], bars_15m, bars_1h)[-1].signal is Signal.NONE


def test_one_timeframe_or_an_exhausted_phase_is_no_trade() -> None:
    moment = _moment(2, 16)
    stair = PhaseStamp(moment, Phase.STAIR_DOWN, "collecting_lower_pools", upper_low=1.00)
    exhausted = PhaseStamp(moment, Phase.EXHAUSTED, "old_lower_reached", upper_low=1.00, lower_high=0.70)
    held_15m = [
        Bar(moment - timedelta(hours=2), 1.02, 1.05, 1.01, 1.02),
        Bar(moment - timedelta(minutes=15), 0.98, 0.99, 0.97, 0.98),
        *_hold_bars(moment, minutes=15, count=16, close=0.96),
    ]
    reclaimed_1h = [
        Bar(moment - timedelta(hours=1), 0.98, 1.05, 0.97, 0.98),
        *_hold_bars(moment, minutes=60, count=4, close=1.02),
    ]
    assert read_signals([stair], held_15m, reclaimed_1h)[0].signal is Signal.NONE
    both = [
        Bar(moment - timedelta(hours=1), 0.98, 1.05, 0.97, 0.98),
        *_hold_bars(moment, minutes=60, count=4, close=0.96),
    ]
    assert read_signals([exhausted], held_15m, both)[0].signal is Signal.NONE


def test_a_failed_retest_under_a_wide_gap_starts_the_stair() -> None:
    born = _moment(1, 0)
    gone = _moment(2, 16)
    base = [
        _pool("upper", 1.00, 1.02, born, gone),
        _pool("upper", 1.01, 1.03, born, gone),
        _pool("upper", 1.02, 1.04, born, gone),
    ]
    top_born = _moment(3, 4)
    top = [
        _pool("upper", 1.095, 1.105, top_born),
        _pool("upper", 1.098, 1.110, top_born),
        _pool("upper", 1.100, 1.112, top_born),
    ]
    pools = [*base, *top, *_stack("upper", 1.17, born), _pool("lower", 0.80, 0.84, born)]
    samples = [
        _obs(2, 0, 1.025, high=1.030, low=1.015, up=True),
        _obs(2, 4, 1.030, high=1.035, low=1.020, up=True),
        _obs(2, 8, 1.040, high=1.050, low=1.030, up=True),
        _obs(2, 12, 1.055, high=1.070, low=1.045, up=True),
        _obs(2, 16, 1.065, high=1.080, low=1.055, up=True),
        _obs(2, 20, 1.075, high=1.085, low=1.065, up=True),
        _obs(3, 0, 1.080, high=1.095, low=1.070, up=True),
        _obs(3, 4, 1.050, high=1.070, low=1.040, up=False),
        _obs(3, 8, 1.055, high=1.065, low=1.045, up=False),
        _obs(3, 12, 1.070, high=1.085, low=1.055, up=True),
        _obs(3, 16, 1.050, high=1.060, low=1.045, up=False),
        _obs(3, 20, 1.045, high=1.055, low=1.040, up=False),
    ]
    stamps = replay(samples, pools, PatternProfile())
    assert stamps[6].phase is Phase.UPPER_BUILD
    assert stamps[9].phase is Phase.UPPER_BUILD
    assert stamps[-1].phase is Phase.STAIR_DOWN
    assert stamps[-1].reason == "failed_high_under_gap"
    assert stamps[-1].upper_low is not None and stamps[-1].upper_low > 1.05


def test_mfe_and_mae_follow_the_path_until_the_next_signal() -> None:
    moment = _moment(2, 16)
    long = SignalStamp(moment, Signal.LONG, Phase.UPPER_BUILD, "confirmed_upper")
    short = SignalStamp(moment + timedelta(hours=4), Signal.SHORT, Phase.STAIR_DOWN, "confirmed_stair")
    bars = [
        Bar(moment, 1.00, 1.00, 0.98, 0.99),
        Bar(moment + timedelta(minutes=15), 0.99, 1.05, 0.99, 1.02),
        Bar(moment + timedelta(minutes=30), 1.02, 1.08, 0.90, 1.04),
        Bar(moment + timedelta(hours=4), 1.00, 1.03, 0.97, 0.99),
        Bar(moment + timedelta(hours=4, minutes=15), 0.99, 0.99, 0.95, 0.96),
    ]
    noted = measure([long, short], bars, {long.as_of: 1.0, short.as_of: 1.0})
    assert round(noted[0].mae_before_profit_pct or 0, 2) == 2.0
    assert round(noted[0].mfe_pct, 2) == 8.0
    assert round(noted[0].mae_pct, 2) == 10.0
    assert noted[1].mae_before_profit_pct is not None
    assert noted[1].until is None


def test_15m_does_not_change_the_4h_phase() -> None:
    born = _moment(1, 0)
    pools = [*_stack("upper", 1.00, born), _pool("lower", 0.78, 0.82, born)]
    samples = [
        _obs(2, 0, 1.015, high=1.02, low=1.01, up=True),
        _obs(2, 4, 1.016, high=1.02, low=1.01, up=True),
    ]
    stamps = replay(samples, pools, PatternProfile())
    switch = stamps[-1]
    bars = []
    moment = switch.as_of
    for step in range(8):
        close = 0.90 if step < 7 else 0.70
        bars.append(Bar(moment + timedelta(minutes=15 * step), close, close, close, close))
    noted = annotate(stamps, bars)
    assert noted[-1].stamp.phase is Phase.UPPER_BUILD
    assert noted[-1].confirmed is False


if __name__ == "__main__":
    test_a_gap_under_three_percent_is_one_zone()
    test_a_standalone_pool_stays_relevant()
    test_a_thin_band_inside_a_wide_gap_is_not_the_next_pool()
    test_one_bar_under_the_upper_pool_does_not_start_the_stair()
    test_two_collected_lower_steps_start_the_stair()
    test_a_wick_into_the_top_of_the_old_lower_does_not_end_the_stair()
    test_a_lower_pool_within_three_percent_keeps_the_stair()
    test_the_rise_starts_after_the_last_lower_pool()
    test_a_new_upper_below_a_broken_stair_starts_again()
    test_a_far_upper_pool_is_not_taken_without_delta()
    test_15m_confirms_the_fall_after_the_upper_is_taken()
    test_1h_confirms_the_same_fall_without_a_later_bar()
    test_15m_rejects_a_fall_that_goes_back_into_the_upper()
    test_a_confirmed_stair_is_a_short_and_a_confirmed_upper_is_a_long()
    test_one_timeframe_or_an_exhausted_phase_is_no_trade()
    test_a_later_touch_in_the_same_stair_is_no_trade()
    test_a_failed_retest_under_a_wide_gap_starts_the_stair()
    test_mfe_and_mae_follow_the_path_until_the_next_signal()
    test_15m_does_not_change_the_4h_phase()
    print("ok")

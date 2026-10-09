"""Phase rules without a live market feed, plus the DOGE checkpoints."""

from datetime import datetime, timedelta, timezone

from pool_state_maschine.coin_profile import CoinProfile
from pool_state_maschine.features import (
    Observation,
    Pool,
    band_cleared,
    cluster_count,
    bounce_pool_still_open,
    lower_destination_open,
    stair_step_count,
)
from pool_state_maschine.rotate_exam import judge_segment
from pool_state_maschine.scanner import request_for, walk
from pool_state_maschine.state_machine import MarketPhase, MarketState, PoolStateMachine


def _obs(hour: int, price: float, *, high: float | None = None, low: float | None = None, e9: float, e20: float, e59: float, e200: float) -> Observation:
    moment = datetime(2026, 8, 19, hour, tzinfo=timezone.utc)
    return Observation(moment, price, high if high is not None else price, low if low is not None else price, e9, e20, e59, e200)


def test_a_nick_of_ema200_is_not_a_band_clear() -> None:
    obs = _obs(0, 0.07218, e9=0.07094, e20=0.07050, e59=0.07025, e200=0.07209)
    assert band_cleared(obs, armed=True) is False


def test_the_august_impulse_clears_the_band() -> None:
    obs = _obs(16, 0.07248, high=0.07418, low=0.07010, e9=0.07058, e20=0.07032, e59=0.07020, e200=0.07139)
    assert band_cleared(obs, armed=True) is True


def test_one_dip_does_not_leave_an_upper_build() -> None:
    profile = CoinProfile("DOGEUSDT")
    start = datetime(2026, 8, 19, tzinfo=timezone.utc)
    samples = []
    for step in range(6):
        price = 0.080 + step * 0.001
        samples.append(
            Observation(start + timedelta(hours=4 * step), price, price, price, price - 0.001, price - 0.002, price - 0.003, price - 0.004)
        )
    samples.append(Observation(start + timedelta(hours=24), 0.079, 0.0795, 0.078, 0.082, 0.081, 0.080, 0.070))
    machine = PoolStateMachine("DOGEUSDT")
    states = machine.replay(samples, [], profile)
    assert states[-1].phase is MarketPhase.UPPER_BUILD


def test_stairs_that_share_a_price_count_once() -> None:
    born = datetime(2026, 5, 14, tzinfo=timezone.utc)
    after = datetime(2026, 5, 16, tzinfo=timezone.utc)
    pools = [
        Pool("lower", 0.108, 0.110, born, datetime(2026, 5, 17, tzinfo=timezone.utc)),
        Pool("lower", 0.109, 0.1102, born, datetime(2026, 5, 17, tzinfo=timezone.utc)),
        Pool("lower", 0.100, 0.103, born, datetime(2026, 5, 20, tzinfo=timezone.utc)),
    ]
    assert stair_step_count(pools, born_after=born, after=after, as_of=datetime(2026, 5, 21, tzinfo=timezone.utc), separation_pct=1.2) == 2


def test_a_lower_pool_three_percent_under_price_keeps_the_stair_open() -> None:
    moment = datetime(2026, 5, 28, tzinfo=timezone.utc)
    obs = Observation(moment, 0.10000, 0.10100, 0.09950, 0.10200, 0.10300, 0.10400, 0.10500)
    pools = [Pool("lower", 0.09300, 0.09500, datetime(2026, 4, 27, tzinfo=timezone.utc), None)]
    assert lower_destination_open(pools, obs, CoinProfile("DOGEUSDT")) is True


def test_a_relevant_pool_built_after_the_low_keeps_the_stair_open() -> None:
    moment = datetime(2026, 6, 12, tzinfo=timezone.utc)
    floor_at = datetime(2026, 6, 6, 8, tzinfo=timezone.utc)
    obs = Observation(moment, 0.17060, 0.17100, 0.16740, 0.16600, 0.16500, 0.16000, 0.20000)
    wide = Pool("lower", 0.15630, 0.16050, datetime(2026, 6, 9, 20, tzinfo=timezone.utc), None)
    thin = Pool("lower", 0.08347, 0.08430, datetime(2026, 6, 8, 8, tzinfo=timezone.utc), None)
    profile = CoinProfile("DOGEUSDT")
    assert bounce_pool_still_open([wide], obs, profile, 0.14840, floor_at) is True
    assert bounce_pool_still_open([thin], obs, profile, 0.07757, floor_at) is False


def test_doge_windows_keep_their_phases() -> None:
    """The four chart windows. A later change has to keep these phases."""
    as_of = datetime(2026, 9, 27, tzinfo=timezone.utc)
    states = walk(request_for("DOGEUSDT", lookback_days=180, as_of=as_of))
    phases = {state.as_of: state.phase for state in states}
    expected = {
        datetime(2026, 5, 23, 8, tzinfo=timezone.utc): MarketPhase.STAIR_DOWN,
        datetime(2026, 6, 5, 20, tzinfo=timezone.utc): MarketPhase.STAIR_DOWN,
        datetime(2026, 6, 30, 16, tzinfo=timezone.utc): MarketPhase.STAIR_DOWN,
        datetime(2026, 7, 2, 20, tzinfo=timezone.utc): MarketPhase.EXHAUSTED,
        datetime(2026, 7, 5, 12, tzinfo=timezone.utc): MarketPhase.EXHAUSTED,
        datetime(2026, 8, 12, 4, tzinfo=timezone.utc): MarketPhase.EXHAUSTED,
        datetime(2026, 8, 19, 16, tzinfo=timezone.utc): MarketPhase.UPPER_BUILD,
        datetime(2026, 9, 18, 4, tzinfo=timezone.utc): MarketPhase.UPPER_BUILD,
    }
    for moment, phase in expected.items():
        assert phases[moment] is phase, (moment.isoformat(), phases.get(moment), phase)


def _stamp(moment: datetime, phase: MarketPhase, reason: str) -> MarketState:
    return MarketState("DOGEUSDT", phase, moment, reason)


def test_a_stair_that_starts_on_its_low_is_flagged() -> None:
    first = datetime(2026, 5, 8, 4, tzinfo=timezone.utc)
    second = first + timedelta(hours=4)
    segment = [
        _stamp(first, MarketPhase.STAIR_DOWN, "lower_stairs_being_collected"),
        _stamp(second, MarketPhase.STAIR_DOWN, "lower_stairs_being_collected"),
    ]
    by_time = {
        first: Observation(first, 0.106, 0.108, 0.105, 0.11, 0.11, 0.11, 0.10),
        second: Observation(second, 0.107, 0.108, 0.106, 0.11, 0.11, 0.11, 0.10),
    }
    stamp = judge_segment("DOGEUSDT", segment, by_time, second + timedelta(hours=4))
    assert "am_tief" in stamp.flags
    assert "zu_kurz" in stamp.flags


def test_an_upper_start_before_the_high_passes() -> None:
    first = datetime(2026, 5, 8, 16, tzinfo=timezone.utc)
    later = first + timedelta(hours=12)
    segment = [
        _stamp(first, MarketPhase.UPPER_BUILD, "price_working_through_upper_pools"),
        _stamp(first + timedelta(hours=4), MarketPhase.UPPER_BUILD, "price_working_through_upper_pools"),
        _stamp(later, MarketPhase.UPPER_BUILD, "price_working_through_upper_pools"),
    ]
    by_time = {
        first: Observation(first, 0.108, 0.1082, 0.107, 0.11, 0.11, 0.11, 0.10),
        first + timedelta(hours=4): Observation(first + timedelta(hours=4), 0.109, 0.1096, 0.108, 0.11, 0.11, 0.11, 0.10),
        later: Observation(later, 0.110, 0.1111, 0.109, 0.11, 0.11, 0.11, 0.10),
    }
    stamp = judge_segment("DOGEUSDT", segment, by_time, later + timedelta(hours=4))
    assert stamp.flags == ()


def test_overlapping_pools_count_as_one_cluster() -> None:
    born = datetime(2026, 4, 29, tzinfo=timezone.utc)
    pools = [
        Pool("upper", 0.10080, 0.10336, born, None),
        Pool("upper", 0.10349, 0.10956, born, None),
    ]
    assert cluster_count(pools, gap_pct=CoinProfile("DOGEUSDT").cluster_gap_pct) == 2


if __name__ == "__main__":
    test_a_nick_of_ema200_is_not_a_band_clear()
    test_the_august_impulse_clears_the_band()
    test_one_dip_does_not_leave_an_upper_build()
    test_stairs_that_share_a_price_count_once()
    test_a_lower_pool_three_percent_under_price_keeps_the_stair_open()
    test_a_relevant_pool_built_after_the_low_keeps_the_stair_open()
    test_a_stair_that_starts_on_its_low_is_flagged()
    test_an_upper_start_before_the_high_passes()
    test_doge_windows_keep_their_phases()
    test_overlapping_pools_count_as_one_cluster()
    print("ok")

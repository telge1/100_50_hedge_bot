"""Phase tracker for the pool staircase.

A phase is a sequence. One candle against it does not flip the phase.
The machine does not place trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pool_state_maschine.coin_profile import CoinProfile
from pool_state_maschine.features import (
    Observation,
    above_fast,
    band_cleared,
    bands_rolling_down,
    lost_fast,
    bounce_pool_still_open,
    lower_destination_open,
    stair_step_count,
)
from pool_state_maschine.market_data import Pool


class MarketPhase(str, Enum):
    UPPER_BUILD = "upper_build"
    STAIR_DOWN = "stair_down"
    EXHAUSTED = "exhausted"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class MarketState:
    symbol: str
    phase: MarketPhase
    as_of: datetime | None
    reason: str


class PoolStateMachine:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.state = MarketState(symbol, MarketPhase.NEUTRAL, None, "not_scanned")

    def replay(self, samples: list[Observation], pools: list[Pool], profile: CoinProfile) -> list[MarketState]:
        phase = MarketPhase.NEUTRAL
        reason = "not_scanned"
        armed = False
        peak_high = 0.0
        peak_at: datetime | None = None
        trough_at: datetime | None = None
        floor_low = 0.0
        floor_at: datetime | None = None
        leg_low = 0.0
        quiet_bars = 0
        clear_bars = 0
        turn_bars = 0
        recent_lows: list[float] = []
        states: list[MarketState] = []

        for obs in samples:
            if obs.price < obs.e200:
                armed = True
            recent_lows.append(obs.low)
            if len(recent_lows) > 18:
                del recent_lows[0]

            if phase is MarketPhase.UPPER_BUILD:
                drop = (peak_high - obs.low) / peak_high * 100.0 if peak_high else 0.0
                steps = 0
                if peak_at is not None and trough_at is not None:
                    steps = stair_step_count(
                        pools,
                        born_after=trough_at,
                        after=peak_at,
                        as_of=obs.as_of,
                        separation_pct=profile.stair_separation_pct,
                    )
                broke_leg = leg_low > 0 and obs.low < leg_low
                if (
                    lost_fast(obs)
                    and bands_rolling_down(obs)
                    and drop >= profile.stair_drop_pct
                    and steps >= profile.min_stair_steps
                    and broke_leg
                ):
                    phase = MarketPhase.STAIR_DOWN
                    reason = "lower_stairs_being_collected"
                    floor_low = obs.low
                    floor_at = obs.as_of
                    quiet_bars = 0
                    clear_bars = 0
                    turn_bars = 0
                elif obs.high > peak_high:
                    peak_high = obs.high
                    peak_at = obs.as_of
                    leg_low = min(recent_lows)
            elif phase is MarketPhase.STAIR_DOWN:
                if obs.low < floor_low:
                    floor_low = obs.low
                    floor_at = obs.as_of
                    quiet_bars = 0
                    clear_bars = 0
                    turn_bars = 0
                else:
                    quiet_bars += 1
                # Thin bands born on the bounce do not keep the stair open.
                # A relevant lower pool still sitting between the low and price does.
                pools_still_below = lower_destination_open(
                    pools, obs, profile, floor_low
                ) or bounce_pool_still_open(pools, obs, profile, floor_low, floor_at)
                if pools_still_below:
                    clear_bars = 0
                else:
                    clear_bars += 1
                bands_turned = obs.e9 > obs.e20 and above_fast(obs)
                turn_bars = turn_bars + 1 if bands_turned else 0
                turned_up = turn_bars >= 2 and clear_bars >= 1 and quiet_bars >= 1
                if band_cleared(obs, armed=armed):
                    phase, reason, peak_high, peak_at, trough_at, armed, leg_low = _enter_upper(
                        obs, "band_cleared_from_below"
                    )
                elif turned_up:
                    phase = MarketPhase.EXHAUSTED
                    reason = "old_lower_pool_reached"
            elif phase is MarketPhase.EXHAUSTED:
                if band_cleared(obs, armed=armed):
                    phase, reason, peak_high, peak_at, trough_at, armed, leg_low = _enter_upper(
                        obs, "band_cleared_from_below"
                    )
                elif _broke_floor(obs, profile, floor_low) and bands_rolling_down(obs):
                    phase = MarketPhase.STAIR_DOWN
                    reason = "lower_stairs_being_collected"
                    if peak_high <= 0.0:
                        peak_high = obs.high
                        peak_at = obs.as_of
                    floor_low = obs.low
                    floor_at = obs.as_of
                    quiet_bars = 0
                    clear_bars = 0
                    turn_bars = 0
            else:
                if band_cleared(obs, armed=armed) or (obs.price > obs.e9 and obs.price > obs.e20 and obs.price > obs.e200 and obs.e9 > obs.e200):
                    phase, reason, peak_high, peak_at, trough_at, armed, leg_low = _enter_upper(
                        obs, "price_working_through_upper_pools"
                    )

            if phase is MarketPhase.UPPER_BUILD:
                quiet_bars = 0
            self.state = MarketState(self.symbol, phase, obs.as_of, reason)
            states.append(self.state)
        return states


def _enter_upper(
    obs: Observation, reason: str
) -> tuple[MarketPhase, str, float, datetime, datetime, bool, float]:
    return (MarketPhase.UPPER_BUILD, reason, obs.high, obs.as_of, obs.as_of, False, 0.0)


def _broke_floor(obs: Observation, profile: CoinProfile, floor_low: float) -> bool:
    if floor_low <= 0:
        return False
    return obs.low < floor_low * (1.0 - profile.floor_break_pct / 100.0)

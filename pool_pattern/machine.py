"""4h phase from the pool sequence in docs/4h-pool-cluster.md.

Small pools on the way up stay inside upper_build.
A wide gap to the next upper zone is not taken unless delta_taken is true.
This module does not read the order book. One closed bar against the phase does not flip it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pool_pattern.market import Observation
from pool_pattern.profile import PatternProfile
from pool_pattern.snapshot import Zone, next_relevant_above, relevant, zones_at, zone_touched


class Phase(str, Enum):
    UPPER_BUILD = "upper_build"
    STAIR_DOWN = "stair_down"
    EXHAUSTED = "exhausted"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class PhaseStamp:
    as_of: datetime
    phase: Phase
    reason: str
    upper_low: float | None = None
    lower_high: float | None = None
    lower_low: float | None = None


def replay(
    samples: list[Observation],
    pools,
    profile: PatternProfile,
    *,
    delta_taken: bool = False,
) -> list[PhaseStamp]:
    phase = Phase.NEUTRAL
    reason = "not_scanned"
    pending: Phase | None = None
    pending_bars = 0
    pending_reason = ""
    tagged: Zone | None = None
    leg_at: datetime | None = None
    old_lower: Zone | None = None
    tail: Zone | None = None
    floor_seen = False
    stamps: list[PhaseStamp] = []

    for obs in samples:
        picture = zones_at(pools, obs.as_of, profile)
        desired, desired_reason, seen_tagged, seen_leg, seen_lower, seen_tail, seen_floor = _desired(
            phase,
            obs,
            picture,
            pools,
            profile,
            tagged,
            leg_at,
            old_lower,
            tail=tail,
            floor_seen=floor_seen,
            delta_taken=delta_taken,
        )
        if desired in (Phase.STAIR_DOWN, Phase.EXHAUSTED):
            if seen_tail is not None:
                tail = seen_tail
            floor_seen = seen_floor
        if desired is phase:
            reason = desired_reason
            pending = None
            pending_bars = 0
            tagged, leg_at, old_lower = seen_tagged, seen_leg, seen_lower
            if phase is Phase.NEUTRAL:
                tagged, leg_at, old_lower, tail, floor_seen = None, None, None, None, False
                reason = "not_scanned"
        else:
            if pending is desired:
                pending_bars += 1
            else:
                pending = desired
                pending_bars = 1
                pending_reason = desired_reason
            if pending_bars >= profile.hold_bars:
                phase = desired
                reason = pending_reason
                pending = None
                pending_bars = 0
                tagged, leg_at, old_lower, tail, floor_seen = (
                    seen_tagged,
                    seen_leg,
                    seen_lower,
                    seen_tail,
                    seen_floor,
                )
        stamps.append(
            PhaseStamp(
                obs.as_of,
                phase,
                reason,
                upper_low=None if tagged is None else tagged.low,
                lower_high=None if old_lower is None else old_lower.high,
                lower_low=None if old_lower is None else old_lower.low,
            )
        )
    return stamps


def _desired(
    phase: Phase,
    obs: Observation,
    zones: list[Zone],
    pools,
    profile: PatternProfile,
    tagged: Zone | None,
    leg_at: datetime | None,
    old_lower: Zone | None,
    *,
    tail: Zone | None,
    floor_seen: bool,
    delta_taken: bool,
) -> tuple[Phase, str, Zone | None, datetime | None, Zone | None, Zone | None, bool]:
    uppers = relevant(zones, "upper")
    entered = [
        zone
        for zone in uppers
        if zone_touched(zone, obs.high, obs.low, obs.close) and _allowed(zone, tagged, profile, delta_taken)
    ]
    if entered:
        chosen = min(entered, key=lambda zone: zone.low)
        if tagged is None or chosen.low > tagged.low:
            tagged = chosen
        if leg_at is None:
            leg_at = obs.as_of
        if old_lower is None:
            old_lower = _old_lower(zones, obs.close, leg_at)

    if phase is Phase.NEUTRAL:
        if tagged is not None and zone_touched(tagged, obs.high, obs.low, obs.close):
            return Phase.UPPER_BUILD, "working_upper_cluster", tagged, leg_at, old_lower, None, False
        return Phase.NEUTRAL, "not_scanned", tagged, leg_at, old_lower, None, False

    if phase is Phase.UPPER_BUILD:
        if tagged is not None and _stair_ready(obs, zones, pools, profile, tagged, leg_at):
            return Phase.STAIR_DOWN, "collecting_lower_pools", tagged, leg_at, old_lower, None, False
        staffel = tagged is not None and obs.close < tagged.low
        reason = "staffel_toward_next_stack" if staffel else "working_upper_cluster"
        return Phase.UPPER_BUILD, reason, tagged, leg_at, old_lower, None, False

    if phase is Phase.STAIR_DOWN:
        fresh = _fresh_upper(obs, zones, old_lower)
        if fresh is not None:
            leg = obs.as_of
            return (
                Phase.UPPER_BUILD,
                "working_upper_cluster",
                fresh,
                leg,
                _old_lower(zones, obs.close, leg),
                None,
                False,
            )
        dest = _next_lower(zones, old_lower, tail, profile)
        settled = _floor_settled(obs, zones, dest, profile, floor_seen, tail)
        turned = _turned_up(obs, zones, dest, settled)
        if turned is not None:
            leg = obs.as_of
            return (
                Phase.UPPER_BUILD,
                "left_lower_zone",
                turned,
                leg,
                _old_lower(zones, obs.close, leg),
                None,
                False,
            )
        if dest is not None and _reached_old_lower(obs, zones, dest, profile):
            return Phase.EXHAUSTED, "old_lower_reached", tagged, leg_at, old_lower, dest, settled
        return Phase.STAIR_DOWN, "collecting_lower_pools", tagged, leg_at, old_lower, dest, settled

    dest = _next_lower(zones, old_lower, tail, profile)
    settled = _floor_settled(obs, zones, dest, profile, floor_seen, tail)
    turned = _turned_up(obs, zones, dest, settled)
    if turned is not None:
        leg = obs.as_of
        return (
            Phase.UPPER_BUILD,
            "left_lower_zone",
            turned,
            leg,
            _old_lower(zones, obs.close, leg),
            None,
            False,
        )
    if _reclaimed_upper(obs, zones, old_lower):
        return Phase.UPPER_BUILD, "left_lower_zone", None, None, None, None, False
    if dest is not None and obs.close < dest.low and _rolling_down(obs):
        return Phase.STAIR_DOWN, "lower_zone_failed", tagged, leg_at, old_lower, dest, settled
    return Phase.EXHAUSTED, "old_lower_reached", tagged, leg_at, old_lower, dest, settled


def _allowed(zone: Zone, tagged: Zone | None, profile: PatternProfile, delta_taken: bool) -> bool:
    if tagged is None or zone.low <= tagged.high:
        return True
    gap = (zone.low - tagged.high) / tagged.high * 100.0
    return gap < profile.wide_upper_gap_pct or delta_taken


def _rolling_down(obs: Observation) -> bool:
    return obs.close < obs.e9 and obs.e9 < obs.e20 and obs.e9 < obs.e59


def _stair_ready(
    obs: Observation,
    zones: list[Zone],
    pools,
    profile: PatternProfile,
    tagged: Zone,
    leg_at: datetime | None,
) -> bool:
    """Stair starts after the upper was taken, once the close is back under it and lowers still wait."""
    if obs.close >= tagged.low or not _lowers_waiting(pools, obs.as_of, obs.close):
        return False
    nxt, gap = next_relevant_above(zones, tagged.high)
    if nxt is not None and gap < profile.cluster_gap_pct:
        return False
    new_stack = [
        zone
        for zone in relevant(zones, "upper")
        if leg_at is not None
        and (zone.pool_count >= 3 or zone.width_pct >= profile.relevant_pool_width_pct)
        and zone.born >= leg_at
        and obs.close < zone.low < tagged.low
    ]
    return not new_stack


def _lowers_waiting(pools, as_of: datetime, price: float) -> bool:
    for pool in pools:
        if getattr(pool, "side", None) != "lower":
            continue
        if pool.created_timestamp > as_of:
            continue
        invalidated = pool.invalidated_timestamp
        if invalidated is not None and invalidated <= as_of:
            continue
        if float(pool.top_price) < price:
            return True
    return False


def _next_lower(
    zones: list[Zone],
    anchor: Zone | None,
    tail: Zone | None,
    profile: PatternProfile,
) -> Zone | None:
    """The next pool within 3 % under this stair's own lower target, including that same pool as it grows.

    The target itself does not walk down through every later pool.
    """
    if anchor is None:
        return None
    best = None
    for zone in relevant(zones, "lower"):
        if zone.low >= anchor.low:
            continue
        same_pool = tail is not None and zone.born == tail.born
        gap = 0.0 if zone.high >= anchor.low else (anchor.low - zone.high) / zone.high * 100.0
        if same_pool or gap < profile.cluster_gap_pct:
            if best is None or zone.low < best.low:
                best = zone
    return anchor if best is None else best


def _deeper_lower(zones: list[Zone], followed: Zone, profile: PatternProfile) -> bool:
    limit = followed.low * (1.0 - profile.standalone_gap_pct / 100.0)
    return any(zone.high <= limit for zone in relevant(zones, "lower"))


def _floor_settled(
    obs: Observation,
    zones: list[Zone],
    followed: Zone | None,
    profile: PatternProfile,
    floor_seen: bool,
    prior: Zone | None,
) -> bool:
    if followed is None or _deeper_lower(zones, followed, profile):
        return False
    tagged = obs.low <= followed.low * (1.0 + profile.standalone_gap_pct / 100.0)
    if prior is not None and followed.low < prior.low:
        return tagged
    return tagged or floor_seen


def _turned_up(obs: Observation, zones: list[Zone], followed: Zone | None, settled: bool) -> Zone | None:
    """After the last lower is traded, a close above it into an upper pool starts the next rise."""
    if not settled or followed is None or obs.close <= followed.high:
        return None
    touched = [
        zone
        for zone in relevant(zones, "upper")
        if zone.low > followed.low and zone_touched(zone, obs.high, obs.low, obs.close)
    ]
    if not touched:
        return None
    return min(touched, key=lambda zone: zone.low)


def _old_lower(zones: list[Zone], price: float, leg_at: datetime) -> Zone | None:
    candidates = [zone for zone in relevant(zones, "lower") if zone.high < price and zone.born <= leg_at]
    if not candidates:
        return None
    return max(candidates, key=lambda zone: zone.high)


def _reached_old_lower(obs: Observation, zones: list[Zone], old_lower: Zone, profile: PatternProfile) -> bool:
    """The stair ends at the bottom of the last old lower, held as support.

    A wick into the top of a wide lower zone is still a step of the fall.
    """
    if obs.close < old_lower.low or obs.close > old_lower.high:
        return False
    floor = old_lower.low * (1.0 + profile.standalone_gap_pct / 100.0)
    if obs.close > floor:
        return False
    limit = old_lower.low * (1.0 - profile.standalone_gap_pct / 100.0)
    return not any(zone.high <= limit for zone in relevant(zones, "lower"))


def _fresh_upper(obs: Observation, zones: list[Zone], old_lower: Zone | None) -> Zone | None:
    """A close inside a relevant upper below a broken stair target starts a new leg."""
    if old_lower is None or obs.close >= old_lower.low:
        return None
    inside = [
        zone
        for zone in relevant(zones, "upper")
        if zone.high < old_lower.low and zone.low <= obs.close <= zone.high
    ]
    if not inside:
        return None
    return min(inside, key=lambda zone: zone.low)


def _reclaimed_upper(obs: Observation, zones: list[Zone], old_lower: Zone | None) -> bool:
    if old_lower is None or obs.close <= old_lower.high:
        return False
    return any(zone_touched(zone, obs.high, obs.low, obs.close) for zone in relevant(zones, "upper"))

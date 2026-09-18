"""Touch FSM, penetration / reclaim / acceptance, price labels.

ABSORB definition (V1, documented):
  After first touch, without ever reaching min_penetration_bps beyond the
  outer edge of the zone, mid moves at least min_penetration_bps in the fade
  direction past the inner edge. Trigger = first such confirmation tick.

FAILED_BREAK:
  Relevant penetration, reclaim (full return past inner edge) within
  max_reclaim_delay_s from penetration_start, then continuous hold on the
  fade side for reclaim_hold_s with reclaim_tolerance_bps (default 0 = strict:
  any 100ms tick on the wrong side aborts the hold and may restart reclaim).

TRUE_BREAK:
  Relevant penetration and continuous time beyond the full outer edge for
  true_break_acceptance_s without a confirmed reclaim.

UNRESOLVED / censored:
  Window end or invalid coverage before a label can be confirmed.

Reset (new event on same logical zone_id) requires one of:
  - min_event_separation_s since last event touch
  - mid at least reset_distance_bps from zone
  - active MP zone identity changed (zone_id / profile set)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .params import PilotParams
from .schema import ActiveLevel, ConfluenceZone, MidTick, TouchEvent
from .util import (
    NS,
    bps_distance,
    bps_signed,
    make_event_id,
    param_fingerprint,
    price_offset_bps,
)


STATE_ARMED = "ARMED"
STATE_TOUCHED = "TOUCHED"
STATE_BEYOND = "BEYOND"
STATE_RECLAIMED = "RECLAIMED"
STATE_ACCEPTED = "ACCEPTED"
STATE_RESET = "RESET"


def fade_side_for(role: str) -> str:
    if role == "UPPER":
        return "SHORT"
    if role == "LOWER":
        return "LONG"
    raise ValueError(role)


def distance_to_zone_bps(mid: float, lo: float, hi: float) -> float:
    """0 if inside [lo,hi]; else bps to nearest edge."""
    if lo <= mid <= hi:
        return 0.0
    if mid < lo:
        return bps_distance(mid, lo)
    return bps_distance(mid, hi)


def in_touch_band(mid: float, lo: float, hi: float, touch_tol_bps: float) -> bool:
    return distance_to_zone_bps(mid, lo, hi) <= float(touch_tol_bps)


def is_beyond(mid: float, role: str, lo: float, hi: float) -> bool:
    if role == "UPPER":
        return mid > hi
    return mid < lo


def is_fully_reclaimed(mid: float, role: str, lo: float, hi: float, reclaim_tol_bps: float) -> bool:
    """Price fully on fade side of the complete zone (strict or with small tol)."""
    if role == "UPPER":
        # below low (optionally allow slight overshoot up to tol)
        limit = lo + price_offset_bps(lo, reclaim_tol_bps)
        return mid < limit if reclaim_tol_bps > 0 else mid < lo
    limit = hi - price_offset_bps(hi, reclaim_tol_bps)
    return mid > limit if reclaim_tol_bps > 0 else mid > hi


def penetration_bps(mid: float, role: str, lo: float, hi: float) -> float:
    if role == "UPPER":
        if mid <= hi:
            return 0.0
        return bps_signed(mid - hi, hi)
    if mid >= lo:
        return 0.0
    return bps_signed(lo - mid, lo)


def fade_distance_bps(mid: float, role: str, lo: float, hi: float) -> float:
    """Favorable distance past inner edge (fade side)."""
    if role == "UPPER":
        if mid >= lo:
            return 0.0
        return bps_signed(lo - mid, lo)
    if mid <= hi:
        return 0.0
    return bps_signed(mid - hi, hi)


def break_distance_bps(mid: float, role: str, lo: float, hi: float) -> float:
    return penetration_bps(mid, role, lo, hi)


@dataclass
class _ZoneTracker:
    zone_id: str
    state: str = STATE_ARMED
    last_event_touch_ns: int | None = None
    last_profiles: tuple[str, ...] = ()
    # open event fields
    event: TouchEvent | None = None
    beyond_start_ns: int | None = None
    beyond_accum_ms: int = 0
    last_beyond_ns: int | None = None
    reclaim_candidate_ns: int | None = None
    reclaim_hold_start_ns: int | None = None
    acceptance_start_ns: int | None = None
    relevant_pen: bool = False
    reset_armed_reason: str = ""


def _zone_signature(z: ConfluenceZone) -> tuple[str, ...]:
    return tuple(sorted(z.profile_ids))


def detect_events(
    mids: Sequence[MidTick],
    *,
    zones_by_ns: dict[int, list[ConfluenceZone]],
    zone_timeline: Sequence[tuple[int, list[ConfluenceZone]]],
    params: PilotParams,
    epoch_id: str,
    end_ns: int,
    levels_by_zone: dict[str, list[ActiveLevel]] | None = None,
) -> list[TouchEvent]:
    """Run FSM over mid series.

    ``zone_timeline`` is a sorted list of (effective_from_ns, zones) change points.
    Between changes, active zones are constant.
    """
    if not zone_timeline:
        return []
    fp = param_fingerprint(params.to_manifest_dict())
    trackers: dict[str, _ZoneTracker] = {}
    events: list[TouchEvent] = []

    tl_idx = 0
    active_zones = zone_timeline[0][1]
    active_from = zone_timeline[0][0]

    def zones_at(ts_ns: int) -> list[ConfluenceZone]:
        nonlocal tl_idx, active_zones, active_from
        while tl_idx + 1 < len(zone_timeline) and zone_timeline[tl_idx + 1][0] <= ts_ns:
            tl_idx += 1
            active_from = zone_timeline[tl_idx][0]
            active_zones = zone_timeline[tl_idx][1]
        return active_zones

    for tick in mids:
        if not tick.valid:
            continue
        ts = tick.ts_ns
        mid = tick.mid
        zones = zones_at(ts)
        active_ids = {z.zone_id for z in zones}

        # Profile-change reset for trackers whose zone disappeared / changed
        for zid, tr in list(trackers.items()):
            if zid not in active_ids and tr.state not in (STATE_ARMED, STATE_RESET):
                # finalize open as censored if needed
                if tr.event is not None and tr.event.label == "UNRESOLVED":
                    _censor(tr.event, end_ns, "PROFILE_CHANGED_BEFORE_RESOLVE")
                    events.append(tr.event)
                    tr.event = None
                tr.state = STATE_RESET
                tr.reset_armed_reason = "PROFILE_CHANGED"
                tr.last_profiles = ()

        for z in zones:
            tr = trackers.get(z.zone_id)
            if tr is None:
                tr = _ZoneTracker(zone_id=z.zone_id, last_profiles=_zone_signature(z))
                trackers[z.zone_id] = tr
            sig = _zone_signature(z)
            if tr.last_profiles and sig != tr.last_profiles:
                if tr.event is not None and tr.event.label == "UNRESOLVED":
                    _censor(tr.event, end_ns, "PROFILE_CHANGED_BEFORE_RESOLVE")
                    events.append(tr.event)
                    tr.event = None
                tr.state = STATE_RESET
                tr.reset_armed_reason = "PROFILE_CHANGED"
            tr.last_profiles = sig

            role = z.role
            lo, hi = z.confluence_low, z.confluence_high
            touched = in_touch_band(mid, lo, hi, params.touch_tolerance_bps)
            beyond = is_beyond(mid, role, lo, hi)
            pen = penetration_bps(mid, role, lo, hi)
            fade_d = fade_distance_bps(mid, role, lo, hi)
            break_d = break_distance_bps(mid, role, lo, hi)
            dist = distance_to_zone_bps(mid, lo, hi)

            # RESET → ARMED checks
            if tr.state == STATE_RESET:
                sep_ok = (
                    tr.last_event_touch_ns is None
                    or (ts - tr.last_event_touch_ns) / NS >= params.min_event_separation_s
                )
                dist_ok = dist >= params.reset_distance_bps
                prof_ok = tr.reset_armed_reason == "PROFILE_CHANGED"
                if sep_ok or dist_ok or prof_ok:
                    tr.state = STATE_ARMED
                    tr.reset_armed_reason = ""
                    tr.beyond_start_ns = None
                    tr.beyond_accum_ms = 0
                    tr.reclaim_candidate_ns = None
                    tr.reclaim_hold_start_ns = None
                    tr.acceptance_start_ns = None
                    tr.relevant_pen = False

            if tr.state == STATE_ARMED:
                if touched:
                    # open new event
                    ev = TouchEvent(
                        event_id="",  # fill below
                        symbol=params.symbol,
                        first_touch_ts_ns=ts,
                        touch_price=mid,
                        event_role=role,
                        fade_side=fade_side_for(role),
                        zone_id=z.zone_id,
                        confluence_class=z.confluence_class,
                        confluence_low=lo,
                        confluence_high=hi,
                        confluence_center=z.confluence_center,
                        confluence_width_bps=z.confluence_width_bps,
                        timeframes=list(z.timeframes),
                        active_profile_ids=list(z.profile_ids),
                        level_ids=list(z.level_ids),
                        profile_start_ts_ns=None,
                        profile_end_ts_ns=None,
                        profile_available_ts_ns=None,
                        profile_source=params.profile_source,
                        timeframe="+".join(z.timeframes),
                        epoch_id=epoch_id or tick.epoch_id,
                        reset_reason="",
                    )
                    # causality: all profiles available
                    # profile timestamps from zone members via levels_by_zone optional
                    if levels_by_zone and z.zone_id in levels_by_zone:
                        lvs = levels_by_zone[z.zone_id]
                        for lv in lvs:
                            if lv.profile_available_ts_ns > ts:
                                raise RuntimeError(
                                    f"CAUSALITY_VIOLATION: {lv.profile_id} available "
                                    f"{lv.profile_available_ts_ns} > touch {ts}"
                                )
                        if lvs:
                            ev.profile_available_ts_ns = max(
                                lv.profile_available_ts_ns for lv in lvs
                            )
                            ev.profile_start_ts_ns = min(
                                lv.profile_start_ts_ns for lv in lvs
                            )
                            ev.profile_end_ts_ns = max(lv.profile_end_ts_ns for lv in lvs)
                    else:
                        # still assert using asof — zone built only from available levels
                        pass
                    ev.event_id = make_event_id(
                        symbol=params.symbol,
                        zone_id=z.zone_id,
                        first_touch_ns=ts,
                        event_role=role,
                        confluence_class=z.confluence_class,
                        param_fingerprint=fp,
                    )
                    if ev.profile_available_ts_ns is not None:
                        if ev.profile_available_ts_ns > ev.first_touch_ts_ns:
                            raise RuntimeError("CAUSALITY_VIOLATION: profile_available_ts > event_ts")
                    tr.event = ev
                    tr.state = STATE_TOUCHED
                    tr.last_event_touch_ns = ts
                    tr.beyond_start_ns = None
                    tr.beyond_accum_ms = 0
                    tr.relevant_pen = False
                    tr.reclaim_candidate_ns = None
                    tr.reclaim_hold_start_ns = None
                    tr.acceptance_start_ns = None
                continue

            ev = tr.event
            if ev is None:
                continue
            if ev.label != "UNRESOLVED":
                continue

            ev.max_distance_fade_bps = max(ev.max_distance_fade_bps, fade_d)
            ev.max_distance_break_bps = max(ev.max_distance_break_bps, break_d)
            ev.available_forward_s = max(0.0, (end_ns - ev.first_touch_ts_ns) / NS)

            if beyond:
                if tr.beyond_start_ns is None:
                    tr.beyond_start_ns = ts
                    ev.penetration_start_ts_ns = ts
                if tr.last_beyond_ns is not None and ts >= tr.last_beyond_ns:
                    # accumulate contiguous beyond using bucket step approx
                    step_ms = max(0, (ts - tr.last_beyond_ns) // 1_000_000)
                    tr.beyond_accum_ms += step_ms
                tr.last_beyond_ns = ts
                ev.time_beyond_ms = tr.beyond_accum_ms
                ev.max_penetration_bps = max(ev.max_penetration_bps, pen)
                if pen >= params.min_penetration_bps:
                    tr.relevant_pen = True
                tr.state = STATE_BEYOND
                # acceptance clock
                if tr.relevant_pen:
                    if tr.acceptance_start_ns is None:
                        tr.acceptance_start_ns = tr.beyond_start_ns or ts
                    held = (ts - tr.acceptance_start_ns) / NS
                    ev.acceptance_time_s = held
                    if held >= params.true_break_acceptance_s and tr.reclaim_hold_start_ns is None:
                        # no confirmed reclaim
                        if ev.reclaim_ts_ns is None:
                            ev.label = "TRUE_BREAK"
                            ev.trigger_ts_ns = tr.acceptance_start_ns + int(
                                params.true_break_acceptance_s * NS
                            )
                            ev.trigger_price = mid
                            ev.trigger_reason = "ACCEPTANCE"
                            ev.fsm_end_state = STATE_ACCEPTED
                            tr.state = STATE_ACCEPTED
                            events.append(ev)
                            tr.event = None
                            tr.state = STATE_RESET
                            tr.reset_armed_reason = ""
                            continue
                # reclaim interrupt if back
            else:
                tr.last_beyond_ns = None
                tr.acceptance_start_ns = None  # must be continuous beyond

            # ABSORB path (no relevant pen)
            if tr.state == STATE_TOUCHED and not tr.relevant_pen:
                if fade_d >= params.min_penetration_bps and ev.max_penetration_bps < params.min_penetration_bps:
                    ev.label = "ABSORB"
                    ev.trigger_ts_ns = ts
                    ev.trigger_price = mid
                    ev.trigger_reason = "FADE_CONFIRM_NO_RELEVANT_PENETRATION"
                    ev.fsm_end_state = STATE_TOUCHED
                    events.append(ev)
                    tr.event = None
                    tr.state = STATE_RESET
                    continue
                if beyond and pen > 0:
                    tr.state = STATE_BEYOND

            # Reclaim after beyond
            if tr.relevant_pen and tr.beyond_start_ns is not None:
                if is_fully_reclaimed(mid, role, lo, hi, params.reclaim_tolerance_bps):
                    delay_s = (ts - tr.beyond_start_ns) / NS
                    if delay_s <= params.max_reclaim_delay_s:
                        if tr.reclaim_hold_start_ns is None:
                            tr.reclaim_candidate_ns = ts
                            tr.reclaim_hold_start_ns = ts
                            tr.state = STATE_RECLAIMED
                        # hold continuity — strict: must stay reclaimed
                        hold_s = (ts - tr.reclaim_hold_start_ns) / NS
                        ev.reclaim_hold_s_observed = hold_s
                        if hold_s >= params.reclaim_hold_s:
                            ev.label = "FAILED_BREAK"
                            ev.reclaim_ts_ns = tr.reclaim_candidate_ns
                            ev.reclaim_delay_ms = int(
                                (tr.reclaim_candidate_ns - ev.first_touch_ts_ns) / 1_000_000
                            )
                            ev.trigger_ts_ns = tr.reclaim_hold_start_ns + int(
                                params.reclaim_hold_s * NS
                            )
                            ev.trigger_price = mid
                            ev.trigger_reason = "RECLAIM_CONFIRMED"
                            ev.fsm_end_state = STATE_RECLAIMED
                            events.append(ev)
                            tr.event = None
                            tr.state = STATE_RESET
                            continue
                    else:
                        # reclaim too late — keep waiting for TRUE_BREAK acceptance
                        tr.reclaim_hold_start_ns = None
                        tr.reclaim_candidate_ns = None
                else:
                    # broke reclaim hold with wrong-side tick
                    if tr.reclaim_hold_start_ns is not None:
                        tr.reclaim_hold_start_ns = None
                        tr.reclaim_candidate_ns = None
                        if beyond:
                            tr.state = STATE_BEYOND

    # End of window: censor open events
    for tr in trackers.values():
        if tr.event is not None and tr.event.label == "UNRESOLVED":
            _censor(tr.event, end_ns, "WINDOW_END")
            events.append(tr.event)
            tr.event = None

    events.sort(key=lambda e: (e.first_touch_ts_ns, e.event_id))
    return events


def _censor(ev: TouchEvent, end_ns: int, reason: str) -> None:
    ev.is_censored = True
    ev.censor_reason = reason
    ev.label = "UNRESOLVED"
    ev.available_forward_s = max(0.0, (end_ns - ev.first_touch_ts_ns) / NS)
    ev.fsm_end_state = STATE_RESET

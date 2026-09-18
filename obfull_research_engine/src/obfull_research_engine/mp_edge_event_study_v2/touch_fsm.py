"""V2 touch FSM: arming + approach filter + delayed TRUE_BREAK classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from obfull_research_engine.mp_edge_event_study_v1.schema import ActiveLevel, ConfluenceZone, MidTick
from obfull_research_engine.mp_edge_event_study_v1.util import NS, make_event_id, param_fingerprint

from .geometry import (
    approach_origin_ok,
    approach_side_label,
    distance_to_zone_bps,
    fade_distance_bps,
    in_touch_band,
    is_beyond,
    is_fully_reclaimed,
    on_break_side,
    penetration_bps,
)
from .params import PilotParamsV2
from .schema import EventV2, RejectedTouch, RejectionStats
from .sides import break_side_for, fade_side_for, trade_side_for


STATE_DISARMED = "DISARMED"
STATE_ARMED = "ARMED"
STATE_TOUCHED = "TOUCHED"
STATE_BEYOND = "BEYOND"
STATE_RECLAIMING = "RECLAIMING"
STATE_RESET = "RESET"


@dataclass
class _Tracker:
    zone_id: str
    state: str = STATE_DISARMED
    armed_ts_ns: int | None = None
    approach_start_price: float | None = None
    last_event_touch_ns: int | None = None
    last_profiles: tuple[str, ...] = ()
    event: EventV2 | None = None
    penetration_ts: int | None = None
    acceptance_run_start: int | None = None
    first_acceptance_ts: int | None = None
    max_acceptance_hold_s: float = 0.0
    last_beyond_ns: int | None = None
    beyond_accum_ms: int = 0
    relevant_pen: bool = False
    reclaim_candidate_ns: int | None = None
    reclaim_hold_start_ns: int | None = None
    saw_acceptance: bool = False
    profile_just_activated_inside: bool = False


def _zone_sig(z: ConfluenceZone) -> tuple[str, ...]:
    return tuple(sorted(z.profile_ids))


def _finalize_sides(ev: EventV2) -> None:
    side, reason = trade_side_for(ev.event_role, ev.label_price_only)
    ev.trade_side = side
    ev.trade_side_reason = reason


def _censor(ev: EventV2, end_ns: int, reason: str) -> None:
    ev.is_censored = True
    ev.censor_reason = reason
    ev.label_price_only = "UNRESOLVED"
    ev.available_forward_s = max(0.0, (end_ns - ev.first_touch_ts_ns) / NS)
    if not ev.transition_pattern:
        ev.transition_pattern = "PENETRATE_UNRESOLVED" if ev.penetration_ts_ns else "TOUCH_REJECT"
    _finalize_sides(ev)


def detect_events_v2(
    mids: Sequence[MidTick],
    *,
    zone_timeline: Sequence[tuple[int, list[ConfluenceZone]]],
    params: PilotParamsV2,
    epoch_id: str,
    end_ns: int,
    levels_by_zone: dict[str, list[ActiveLevel]] | None = None,
) -> tuple[list[EventV2], list[RejectedTouch], RejectionStats]:
    if not zone_timeline:
        return [], [], RejectionStats()

    fp = param_fingerprint(params.to_manifest_dict())
    trackers: dict[str, _Tracker] = {}
    events: list[EventV2] = []
    rejected: list[RejectedTouch] = []
    stats = RejectionStats()

    tl_idx = 0
    active_zones = zone_timeline[0][1]

    def zones_at(ts_ns: int) -> list[ConfluenceZone]:
        nonlocal tl_idx, active_zones
        while tl_idx + 1 < len(zone_timeline) and zone_timeline[tl_idx + 1][0] <= ts_ns:
            tl_idx += 1
            # profile change: mark new zones that appear while price already inside/beyond
            new_zones = zone_timeline[tl_idx][1]
            old_ids = {z.zone_id for z in active_zones}
            active_zones = new_zones
            for z in active_zones:
                if z.zone_id not in old_ids:
                    tr = trackers.get(z.zone_id)
                    if tr is None:
                        tr = _Tracker(zone_id=z.zone_id)
                        trackers[z.zone_id] = tr
                    tr.state = STATE_DISARMED
                    tr.armed_ts_ns = None
                    tr.approach_start_price = None
                    tr.profile_just_activated_inside = True
                    tr.last_profiles = _zone_sig(z)
        return active_zones

    for tick in mids:
        if not tick.valid:
            continue
        ts = tick.ts_ns
        mid = tick.mid
        zones = zones_at(ts)
        active_ids = {z.zone_id for z in zones}

        for zid, tr in list(trackers.items()):
            if zid not in active_ids and tr.event is not None and tr.event.label_price_only == "UNRESOLVED":
                _censor(tr.event, end_ns, "PROFILE_CHANGED_BEFORE_RESOLVE")
                events.append(tr.event)
                tr.event = None
                tr.state = STATE_DISARMED
                tr.armed_ts_ns = None

        for z in zones:
            tr = trackers.get(z.zone_id)
            if tr is None:
                tr = _Tracker(zone_id=z.zone_id, last_profiles=_zone_sig(z))
                # First sighting while already in/beyond zone → must arm from approach side first.
                lo0, hi0 = z.confluence_low, z.confluence_high
                if (lo0 <= mid <= hi0) or is_beyond(mid, z.role, lo0, hi0):
                    tr.profile_just_activated_inside = True
                    tr.state = STATE_DISARMED
                trackers[z.zone_id] = tr
            sig = _zone_sig(z)
            if tr.last_profiles and sig != tr.last_profiles:
                if tr.event is not None and tr.event.label_price_only == "UNRESOLVED":
                    _censor(tr.event, end_ns, "PROFILE_CHANGED_BEFORE_RESOLVE")
                    events.append(tr.event)
                    tr.event = None
                tr.state = STATE_DISARMED
                tr.armed_ts_ns = None
                tr.approach_start_price = None
                tr.profile_just_activated_inside = True
            tr.last_profiles = sig

            role = z.role
            lo, hi = z.confluence_low, z.confluence_high
            touched = in_touch_band(mid, lo, hi, params.touch_tolerance_bps)
            beyond = is_beyond(mid, role, lo, hi)
            pen = penetration_bps(mid, role, lo, hi)
            fade_d = fade_distance_bps(mid, role, lo, hi)
            break_d = pen
            dist = distance_to_zone_bps(mid, lo, hi)
            origin_ok = approach_origin_ok(
                mid, role, lo, hi, params.approach_origin_distance_bps
            )

            # --- DISARMED: require approach origin before arming ---
            if tr.state == STATE_DISARMED and tr.event is None:
                inside_or_beyond = (lo <= mid <= hi) or beyond
                if tr.profile_just_activated_inside and inside_or_beyond:
                    # stay disarmed until origin_ok
                    if touched or beyond:
                        stats.raw_touch_candidates += 1
                        stats.rejected_profile_activation_inside_zone += 1
                        rejected.append(
                            RejectedTouch(
                                ts_ns=ts,
                                zone_id=z.zone_id,
                                event_role=role,
                                confluence_class=z.confluence_class,
                                mid=mid,
                                confluence_low=lo,
                                confluence_high=hi,
                                reason="PROFILE_ACTIVATION_INSIDE_ZONE",
                                approach_side=approach_side_label(role),
                                armed=False,
                            )
                        )
                    if origin_ok:
                        tr.profile_just_activated_inside = False
                        tr.state = STATE_ARMED
                        tr.armed_ts_ns = ts
                        tr.approach_start_price = mid
                    continue
                if origin_ok:
                    tr.state = STATE_ARMED
                    tr.armed_ts_ns = ts
                    tr.approach_start_price = mid
                    tr.profile_just_activated_inside = False
                elif touched and params.require_correct_approach:
                    stats.raw_touch_candidates += 1
                    stats.rejected_not_armed += 1
                    rejected.append(
                        RejectedTouch(
                            ts_ns=ts,
                            zone_id=z.zone_id,
                            event_role=role,
                            confluence_class=z.confluence_class,
                            mid=mid,
                            confluence_low=lo,
                            confluence_high=hi,
                            reason="NOT_ARMED",
                            approach_side=approach_side_label(role),
                            armed=False,
                        )
                    )
                continue

            # --- ARMED: wait for touch from approach side ---
            if tr.state == STATE_ARMED and tr.event is None:
                # lose arming if we wander to break side without touch (optional)
                if on_break_side(mid, role, lo, hi) and not params.allow_retest_from_break_side:
                    # still armed but any touch from break side rejected below
                    pass
                if not touched:
                    # can re-record approach origin price while staying armed
                    if origin_ok:
                        if tr.approach_start_price is None:
                            tr.approach_start_price = mid
                    continue

                stats.raw_touch_candidates += 1
                # duplicate / separation
                if (
                    tr.last_event_touch_ns is not None
                    and (ts - tr.last_event_touch_ns) / NS < params.min_event_separation_s
                    and dist < params.reset_distance_bps
                ):
                    stats.rejected_duplicate += 1
                    rejected.append(
                        RejectedTouch(
                            ts_ns=ts,
                            zone_id=z.zone_id,
                            event_role=role,
                            confluence_class=z.confluence_class,
                            mid=mid,
                            confluence_low=lo,
                            confluence_high=hi,
                            reason="DUPLICATE_NO_RESET",
                            approach_side=approach_side_label(role),
                            armed=True,
                        )
                    )
                    continue

                # wrong approach: currently on break side of zone (retest from beyond)
                if beyond and not params.allow_retest_from_break_side:
                    stats.rejected_wrong_approach += 1
                    rejected.append(
                        RejectedTouch(
                            ts_ns=ts,
                            zone_id=z.zone_id,
                            event_role=role,
                            confluence_class=z.confluence_class,
                            mid=mid,
                            confluence_low=lo,
                            confluence_high=hi,
                            reason="WRONG_APPROACH_FROM_BREAK_SIDE",
                            approach_side=approach_side_label(role),
                            armed=True,
                        )
                    )
                    continue

                # require that we were armed from origin (already true) and lookback evidence
                if params.require_correct_approach:
                    # mid at touch must not be approaching from break side:
                    # for UPPER, mid should be <= hi (touching from below/inside from below)
                    if role == "UPPER" and mid > hi:
                        stats.rejected_wrong_approach += 1
                        rejected.append(
                            RejectedTouch(
                                ts_ns=ts,
                                zone_id=z.zone_id,
                                event_role=role,
                                confluence_class=z.confluence_class,
                                mid=mid,
                                confluence_low=lo,
                                confluence_high=hi,
                                reason="WRONG_APPROACH_UPPER_FROM_ABOVE",
                                approach_side=approach_side_label(role),
                                armed=True,
                            )
                        )
                        continue
                    if role == "LOWER" and mid < lo:
                        stats.rejected_wrong_approach += 1
                        rejected.append(
                            RejectedTouch(
                                ts_ns=ts,
                                zone_id=z.zone_id,
                                event_role=role,
                                confluence_class=z.confluence_class,
                                mid=mid,
                                confluence_low=lo,
                                confluence_high=hi,
                                reason="WRONG_APPROACH_LOWER_FROM_BELOW",
                                approach_side=approach_side_label(role),
                                armed=True,
                            )
                        )
                        continue
                    if tr.armed_ts_ns is None:
                        stats.rejected_not_armed += 1
                        rejected.append(
                            RejectedTouch(
                                ts_ns=ts,
                                zone_id=z.zone_id,
                                event_role=role,
                                confluence_class=z.confluence_class,
                                mid=mid,
                                confluence_low=lo,
                                confluence_high=hi,
                                reason="NOT_ARMED",
                                approach_side=approach_side_label(role),
                                armed=False,
                            )
                        )
                        continue

                # open event
                approach_px = tr.approach_start_price
                approach_dist = None
                if approach_px is not None:
                    if role == "UPPER":
                        approach_dist = (lo - approach_px) / lo * 10_000.0 if approach_px < lo else 0.0
                    else:
                        approach_dist = (approach_px - hi) / hi * 10_000.0 if approach_px > hi else 0.0

                ev = EventV2(
                    event_id="",
                    symbol=params.symbol,
                    first_touch_ts_ns=ts,
                    touch_price=mid,
                    event_role=role,
                    label_price_only="UNRESOLVED",
                    fade_side=fade_side_for(role),
                    break_side=break_side_for(role),
                    trade_side="",
                    trade_side_reason="",
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
                    approach_side=approach_side_label(role),
                    approach_start_price=approach_px,
                    approach_distance_bps=approach_dist,
                    armed_ts_ns=tr.armed_ts_ns,
                )
                if levels_by_zone and z.zone_id in levels_by_zone:
                    lvs = levels_by_zone[z.zone_id]
                    for lv in lvs:
                        if lv.profile_available_ts_ns > ts:
                            raise RuntimeError(
                                f"CAUSALITY_VIOLATION: {lv.profile_id} > touch {ts}"
                            )
                    if lvs:
                        ev.profile_available_ts_ns = max(lv.profile_available_ts_ns for lv in lvs)
                        ev.profile_start_ts_ns = min(lv.profile_start_ts_ns for lv in lvs)
                        ev.profile_end_ts_ns = max(lv.profile_end_ts_ns for lv in lvs)
                        if ev.profile_available_ts_ns > ts:
                            raise RuntimeError("CAUSALITY_VIOLATION: profile_available_ts > event_ts")
                ev.event_id = make_event_id(
                    symbol=params.symbol,
                    zone_id=z.zone_id,
                    first_touch_ns=ts,
                    event_role=role,
                    confluence_class=z.confluence_class,
                    param_fingerprint=fp,
                )
                tr.event = ev
                tr.state = STATE_TOUCHED
                tr.last_event_touch_ns = ts
                tr.penetration_ts = None
                tr.acceptance_run_start = None
                tr.first_acceptance_ts = None
                tr.max_acceptance_hold_s = 0.0
                tr.relevant_pen = False
                tr.reclaim_candidate_ns = None
                tr.reclaim_hold_start_ns = None
                tr.saw_acceptance = False
                tr.beyond_accum_ms = 0
                tr.last_beyond_ns = None
                stats.valid_directional_touches += 1
                continue

            # --- open event tracking ---
            ev = tr.event
            if ev is None or ev.label_price_only != "UNRESOLVED":
                # after resolve → wait for re-arm via DISARMED path
                if tr.state == STATE_RESET:
                    tr.state = STATE_DISARMED
                    tr.armed_ts_ns = None
                    tr.approach_start_price = None
                continue

            ev.max_distance_fade_bps = max(ev.max_distance_fade_bps, fade_d)
            ev.max_distance_break_bps = max(ev.max_distance_break_bps, break_d)
            ev.available_forward_s = max(0.0, (end_ns - ev.first_touch_ts_ns) / NS)

            # ABSORB path
            if not tr.relevant_pen and ev.max_penetration_bps < params.min_penetration_bps:
                age_s = (ts - ev.first_touch_ts_ns) / NS
                if fade_d >= params.absorb_confirmation_bps:
                    ev.label_price_only = "ABSORB"
                    ev.transition_pattern = "TOUCH_REJECT"
                    ev.trigger_ts_ns = ts
                    ev.trigger_price = mid
                    ev.trigger_reason = "ABSORB_CONFIRMATION"
                    _finalize_sides(ev)
                    events.append(ev)
                    tr.event = None
                    tr.state = STATE_DISARMED
                    tr.armed_ts_ns = None
                    tr.approach_start_price = None
                    continue
                if age_s > params.absorb_confirmation_max_s and not beyond:
                    # leave open until window / other resolution — do not force ABSORB
                    pass

            if beyond:
                if tr.penetration_ts is None and pen >= params.min_penetration_bps:
                    tr.penetration_ts = ts
                    ev.penetration_ts_ns = ts
                    tr.relevant_pen = True
                elif tr.penetration_ts is None and pen > 0:
                    # wait until relevant
                    ev.max_penetration_bps = max(ev.max_penetration_bps, pen)
                    if pen >= params.min_penetration_bps:
                        tr.penetration_ts = ts
                        ev.penetration_ts_ns = ts
                        tr.relevant_pen = True
                ev.max_penetration_bps = max(ev.max_penetration_bps, pen)
                if pen >= params.min_penetration_bps:
                    tr.relevant_pen = True
                    if ev.penetration_ts_ns is None:
                        ev.penetration_ts_ns = ts
                        tr.penetration_ts = ts

                if tr.last_beyond_ns is not None and ts >= tr.last_beyond_ns:
                    tr.beyond_accum_ms += max(0, (ts - tr.last_beyond_ns) // 1_000_000)
                tr.last_beyond_ns = ts
                ev.time_beyond_ms = tr.beyond_accum_ms
                tr.state = STATE_BEYOND

                if tr.relevant_pen:
                    if tr.acceptance_run_start is None:
                        tr.acceptance_run_start = ts
                    held = (ts - tr.acceptance_run_start) / NS
                    ev.acceptance_time_s = held
                    tr.max_acceptance_hold_s = max(tr.max_acceptance_hold_s, held)
                    if held >= params.true_break_acceptance_s and tr.first_acceptance_ts is None:
                        tr.first_acceptance_ts = tr.acceptance_run_start + int(
                            params.true_break_acceptance_s * NS
                        )
                        ev.first_acceptance_ts_ns = tr.first_acceptance_ts
                        tr.saw_acceptance = True
            else:
                tr.last_beyond_ns = None
                tr.acceptance_run_start = None  # continuous acceptance required

            # Reclaim within failed_break_horizon
            if tr.relevant_pen and tr.penetration_ts is not None:
                since_pen = (ts - tr.penetration_ts) / NS
                if is_fully_reclaimed(mid, role, lo, hi, params.reclaim_tolerance_bps):
                    if since_pen <= params.failed_break_horizon_s:
                        if tr.reclaim_hold_start_ns is None:
                            tr.reclaim_candidate_ns = ts
                            tr.reclaim_hold_start_ns = ts
                            if ev.reclaim_ts_ns is None:
                                ev.reclaim_ts_ns = ts
                            tr.state = STATE_RECLAIMING
                        hold_s = (ts - tr.reclaim_hold_start_ns) / NS
                        ev.reclaim_hold_s_observed = hold_s
                        if hold_s >= params.reclaim_hold_s:
                            ev.confirmed_reclaim_ts_ns = tr.reclaim_hold_start_ns + int(
                                params.reclaim_hold_s * NS
                            )
                            ev.label_price_only = "FAILED_BREAK"
                            if tr.saw_acceptance:
                                ev.transition_pattern = "PENETRATE_ACCEPT_RECLAIM"
                            else:
                                ev.transition_pattern = "PENETRATE_RECLAIM"
                            ev.trigger_ts_ns = ev.confirmed_reclaim_ts_ns
                            ev.trigger_price = mid
                            ev.trigger_reason = "RECLAIM_CONFIRMED"
                            _finalize_sides(ev)
                            events.append(ev)
                            tr.event = None
                            tr.state = STATE_DISARMED
                            tr.armed_ts_ns = None
                            tr.approach_start_price = None
                            continue
                    # reclaim after horizon: do not convert to FAILED_BREAK
                else:
                    if tr.reclaim_hold_start_ns is not None:
                        tr.reclaim_hold_start_ns = None
                        tr.reclaim_candidate_ns = None
                        if beyond:
                            tr.state = STATE_BEYOND

                # TRUE_BREAK: horizon elapsed without confirmed reclaim + acceptance + continuation
                if (
                    since_pen >= params.failed_break_horizon_s
                    and tr.saw_acceptance
                    and ev.confirmed_reclaim_ts_ns is None
                    and break_d >= params.true_break_continuation_bps
                ):
                    # still need to be beyond (or recently accepted with continuation observed)
                    if beyond or break_d >= params.true_break_continuation_bps:
                        confirm_ts = tr.penetration_ts + int(params.failed_break_horizon_s * NS)
                        # also require first_acceptance observed before confirm
                        if (
                            ev.first_acceptance_ts_ns is not None
                            and ev.first_acceptance_ts_ns <= confirm_ts
                        ):
                            # ensure enough forward data to observe horizon
                            if confirm_ts <= end_ns:
                                ev.true_break_confirmation_ts_ns = max(
                                    confirm_ts,
                                    ev.first_acceptance_ts_ns,
                                )
                                ev.label_price_only = "TRUE_BREAK"
                                ev.transition_pattern = "PENETRATE_ACCEPT_CONTINUE"
                                ev.trigger_ts_ns = ev.true_break_confirmation_ts_ns
                                ev.trigger_price = mid
                                ev.trigger_reason = "TRUE_BREAK_HORIZON_CONFIRMED"
                                _finalize_sides(ev)
                                events.append(ev)
                                tr.event = None
                                tr.state = STATE_DISARMED
                                tr.armed_ts_ns = None
                                tr.approach_start_price = None
                                continue

                # Censor if not enough forward for horizon after pen
                if tr.penetration_ts is not None:
                    need = tr.penetration_ts + int(params.failed_break_horizon_s * NS)
                    # handled at window end

    # window end
    for tr in trackers.values():
        if tr.event is not None and tr.event.label_price_only == "UNRESOLVED":
            ev = tr.event
            # if penetration exists but horizon not fully observable → censored, not TRUE_BREAK
            if tr.penetration_ts is not None:
                need = tr.penetration_ts + int(params.failed_break_horizon_s * NS)
                if need > end_ns:
                    _censor(ev, end_ns, "FAILED_BREAK_HORIZON_CENSORED")
                else:
                    _censor(ev, end_ns, "WINDOW_END")
            else:
                _censor(ev, end_ns, "WINDOW_END")
            events.append(ev)
            tr.event = None

    events.sort(key=lambda e: (e.first_touch_ts_ns, e.event_id))
    return events, rejected, stats

"""Window classification rules (causal timestamps)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    BREAKOUT_HOLD_S,
    MISSING,
    RECLAIM_HOLD_S,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import EmaZone


@dataclass
class Timeline:
    zone_touch_at: str | None = None
    attack_start_at: str | None = None
    wall_defended_at: str | None = None
    wall_absorbed_at: str | None = None
    breakout_at: str | None = None
    breakout_confirmed_at: str | None = None
    retest_at: str | None = None
    reclaim_at: str | None = None
    classification_at: str | None = None
    primary_class: str = "UNDETERMINED"
    mechanism: str = "UNDETERMINED"
    notes: str = ""


def _iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def classify_window(
    *,
    data_incomplete: bool,
    incomplete_reason: str,
    samples: list[Any],  # AnalysisSample
    zone: EmaZone | None,
    zone_role: str,  # resistance / support / none
    contact_ts_ms: int | None,
    mechanism: str,
    wall_present_before_contact: bool,
    wall_present_after_60s: bool,
    wall_moved: bool,
) -> Timeline:
    tl = Timeline(mechanism=mechanism)
    if data_incomplete:
        tl.primary_class = "DATA_INCOMPLETE"
        tl.classification_at = MISSING
        tl.notes = incomplete_reason
        return tl

    if zone is None or contact_ts_ms is None:
        tl.primary_class = "NO_RELEVANT_ZONE_CONTACT"
        if samples:
            tl.classification_at = _iso(samples[-1].ts_ms)
        tl.notes = "no_ema_zone_touch_in_window"
        return tl

    tl.zone_touch_at = _iso(contact_ts_ms)
    # attack_start: first sample within 30s before touch where mid moves toward zone
    attack = None
    for s in samples:
        if contact_ts_ms - 30_000 <= s.ts_ms < contact_ts_ms:
            if zone_role == "resistance" and s.mid >= zone.low - zone.half_width:
                attack = s.ts_ms
                break
            if zone_role == "support" and s.mid <= zone.high + zone.half_width:
                attack = s.ts_ms
                break
    tl.attack_start_at = _iso(attack or contact_ts_ms)

    after = [s for s in samples if s.ts_ms >= contact_ts_ms]
    if not after:
        tl.primary_class = "UNDETERMINED"
        tl.classification_at = _iso(contact_ts_ms)
        tl.notes = "no_samples_after_touch"
        return tl

    # breakout: mid fully beyond zone band (not wick-only — need sustained)
    breakout_ms = None
    if zone_role == "resistance":
        for s in after:
            if s.mid > zone.high:
                breakout_ms = s.ts_ms
                break
    elif zone_role == "support":
        for s in after:
            if s.mid < zone.low:
                breakout_ms = s.ts_ms
                break

    confirmed_ms = None
    if breakout_ms is not None:
        tl.breakout_at = _iso(breakout_ms)
        hold_until = breakout_ms + BREAKOUT_HOLD_S * 1000
        held = [
            s
            for s in after
            if breakout_ms <= s.ts_ms <= hold_until
        ]
        if held:
            if zone_role == "resistance" and all(s.mid > zone.high for s in held) and held[-1].ts_ms >= hold_until - 1000:
                confirmed_ms = held[-1].ts_ms
            if zone_role == "support" and all(s.mid < zone.low for s in held) and held[-1].ts_ms >= hold_until - 1000:
                confirmed_ms = held[-1].ts_ms
        # looser confirm: last sample in hold window still outside
        if confirmed_ms is None and held:
            last = held[-1]
            ok = (zone_role == "resistance" and last.mid > zone.high) or (
                zone_role == "support" and last.mid < zone.low
            )
            span = last.ts_ms - breakout_ms
            if ok and span >= BREAKOUT_HOLD_S * 1000:
                confirmed_ms = last.ts_ms
        tl.breakout_confirmed_at = _iso(confirmed_ms)

    # reclaim: after confirmed (or long) breakout, back through full band and hold.
    # Brief pierces that never confirmed do not count as false breakout.
    reclaim_ms = None
    if breakout_ms is not None and (confirmed_ms is not None or True):
        # require either confirmed breakout OR mid stayed outside >= 20s before reclaim
        post_bo = [s for s in after if s.ts_ms > breakout_ms]
        outside_span_ok = confirmed_ms is not None
        if not outside_span_ok and post_bo:
            # measure contiguous outside duration before first full reclaim cross
            for s in post_bo:
                back = (zone_role == "resistance" and s.mid < zone.low) or (
                    zone_role == "support" and s.mid > zone.high
                )
                if back:
                    outside_span_ok = (s.ts_ms - breakout_ms) >= 20_000
                    break
                still_out = (zone_role == "resistance" and s.mid > zone.high) or (
                    zone_role == "support" and s.mid < zone.low
                )
                if not still_out and (zone.low <= s.mid <= zone.high):
                    # returned into band without full cross — not reclaim yet
                    continue
        if outside_span_ok:
            for s in post_bo:
                back = (zone_role == "resistance" and s.mid < zone.low) or (
                    zone_role == "support" and s.mid > zone.high
                )
                if back:
                    end = s.ts_ms + RECLAIM_HOLD_S * 1000
                    win = [x for x in post_bo if s.ts_ms <= x.ts_ms <= end]
                    if win and (
                        (zone_role == "resistance" and all(x.mid <= zone.high for x in win))
                        or (zone_role == "support" and all(x.mid >= zone.low for x in win))
                    ):
                        # only if breakout had meaningful hold (>=20s outside)
                        if (s.ts_ms - breakout_ms) >= 20_000 or confirmed_ms is not None:
                            reclaim_ms = win[-1].ts_ms
                        break
        tl.reclaim_at = _iso(reclaim_ms)

    # retest: return to zone after confirmed breakout without full reclaim
    if confirmed_ms is not None and reclaim_ms is None:
        for s in after:
            if s.ts_ms <= confirmed_ms:
                continue
            if zone.low <= s.mid <= zone.high:
                tl.retest_at = _iso(s.ts_ms)
                break

    # defense timestamps
    if mechanism in ("ASK_DEFENSE", "BID_DEFENSE"):
        # defended when post-contact aggression fails to break for 60s
        def_ms = contact_ts_ms + 60_000
        tl.wall_defended_at = _iso(def_ms)
    if mechanism in ("ASK_ABSORPTION", "BID_ABSORPTION"):
        tl.wall_absorbed_at = _iso(confirmed_ms or breakout_ms or contact_ts_ms)

    # primary class
    if reclaim_ms is not None and (confirmed_ms is not None or breakout_ms is not None):
        tl.primary_class = "FALSE_BREAKOUT_RECLAIM"
        tl.classification_at = _iso(reclaim_ms)
    elif mechanism == "LIQUIDITY_PULL" and confirmed_ms is not None:
        tl.primary_class = "LIQUIDITY_PULL_BREAKOUT"
        tl.classification_at = _iso(confirmed_ms)
    elif mechanism in ("ASK_ABSORPTION", "BID_ABSORPTION") and confirmed_ms is not None:
        # Absorption+confirmed breakout wins over later reclaim only if reclaim unset
        tl.primary_class = "ABSORPTION_THEN_BREAKOUT"
        tl.classification_at = _iso(confirmed_ms)
    elif confirmed_ms is not None:
        tl.primary_class = "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION"
        tl.classification_at = _iso(confirmed_ms)
    elif mechanism in ("ASK_DEFENSE", "BID_DEFENSE") and (breakout_ms is None or confirmed_ms is None):
        tl.primary_class = "DEFENSE_REJECTION"
        tl.classification_at = tl.wall_defended_at or _iso(contact_ts_ms + 60_000)
    elif breakout_ms is not None and confirmed_ms is None and reclaim_ms is None:
        # wick / brief pierce
        tl.primary_class = "RANGE_AROUND_ZONE"
        tl.classification_at = _iso(after[-1].ts_ms)
        tl.notes = "pierce_without_hold"
    else:
        # stayed around zone
        in_zone_frac = sum(1 for s in after[:240] if zone.low <= s.mid <= zone.high) / max(
            1, min(240, len(after))
        )
        if mechanism in ("ASK_DEFENSE", "BID_DEFENSE"):
            tl.primary_class = "DEFENSE_REJECTION"
            tl.classification_at = tl.wall_defended_at or _iso(contact_ts_ms + 60_000)
        elif in_zone_frac > 0.3 or any(zone.low <= s.mid <= zone.high for s in after[:120]):
            tl.primary_class = "RANGE_AROUND_ZONE"
            tl.classification_at = _iso(min(after[-1].ts_ms, contact_ts_ms + 120_000))
        else:
            tl.primary_class = "DEFENSE_REJECTION" if wall_present_after_60s else "UNDETERMINED"
            tl.classification_at = _iso(min(after[-1].ts_ms, contact_ts_ms + 120_000))

    # Special: absorption confirmed then later reclaim → still false breakout (failed hold)
    if (
        mechanism in ("ASK_ABSORPTION", "BID_ABSORPTION")
        and confirmed_ms is not None
        and reclaim_ms is not None
    ):
        tl.primary_class = "FALSE_BREAKOUT_RECLAIM"
        tl.classification_at = _iso(reclaim_ms)
        tl.notes = (tl.notes + "|" if tl.notes else "") + "absorption_breakout_then_reclaim"

    extras = []
    if wall_present_before_contact:
        extras.append("wall_preexisting")
    else:
        extras.append("wall_appeared_on_contact")
    if wall_moved:
        extras.append("wall_price_migrated")
    if wall_present_after_60s:
        extras.append("wall_still_present_60s")
    tl.notes = (tl.notes + "|" if tl.notes else "") + "|".join(extras)
    return tl

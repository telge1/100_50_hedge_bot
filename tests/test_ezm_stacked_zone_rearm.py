"""STACKED_EMA_ZONE rearm must not suppress all follow-up exact-touch exports."""

from __future__ import annotations

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_defaults import COOLDOWN_S
from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
    DetectorBuffers,
    _apply_rearm_tracking,
    _dist_outside,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import make_setup_id
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import EmaZone


def _stacked_zone(*, low: float, high: float, atr: float = 1.0) -> EmaZone:
    center = (low + high) / 2.0
    hw = (high - low) / 2.0
    return EmaZone(name="STACKED", center=center, low=low, high=high, half_width=hw, atr=atr)


def _simulate_exact_touch_cycle(
    *,
    zkey: str,
    zone: EmaZone,
    mids: list[tuple[int, float]],
) -> list[int]:
    """Minimal Stage-A exact-touch gate (cooldown + rearm) for regression tests."""
    buf = DetectorBuffers()
    events: list[int] = []
    for ts_ms, mid in mids:
        inside = zone.low <= mid <= zone.high
        dist = _dist_outside(zone, mid)
        _apply_rearm_tracking(buf, zkey=zkey, zone=zone, inside=inside, dist=dist)
        exact_touch = inside  # stacked synthetic: close inside merged band
        if not exact_touch or zkey in buf.active:
            continue
        if buf.cooldown_until.get(zkey, 0) > ts_ms:
            continue
        if not buf.last_outside.get(zkey, True) and zkey not in buf.watches:
            continue
        events.append(ts_ms)
        buf.cooldown_until[zkey] = ts_ms + COOLDOWN_S * 1000
        buf.last_outside[zkey] = False
    return events


def test_stacked_rearm_on_close_leave_without_half_width_distance():
    zkey = "STACKED_EMA_ZONE:EMA20+EMA59+EMA200"
    zone = _stacked_zone(low=100.0, high=110.0)
    buf = DetectorBuffers()
    buf.last_outside[zkey] = False

    # Brief exit: outside merged band but well inside half_width (5.0) of center.
    _apply_rearm_tracking(buf, zkey=zkey, zone=zone, inside=False, dist=0.5)
    assert buf.last_outside[zkey] is True


def test_single_ema_still_requires_half_width_leave():
    zkey = "EMA20"
    zone = _stacked_zone(low=100.0, high=102.0)  # half_width = 1.0
    buf = DetectorBuffers()
    buf.last_outside[zkey] = False

    _apply_rearm_tracking(buf, zkey=zkey, zone=zone, inside=False, dist=0.5)
    assert buf.last_outside[zkey] is False

    _apply_rearm_tracking(buf, zkey=zkey, zone=zone, inside=False, dist=1.0)
    assert buf.last_outside[zkey] is True


def test_two_touch_cycles_after_leave_and_re_entry():
    zkey = "STACKED_EMA_ZONE:EMA20+EMA59"
    zone = _stacked_zone(low=100.0, high=110.0)
    events = _simulate_exact_touch_cycle(
        zkey=zkey,
        zone=zone,
        mids=[
            (1_000, 105.0),  # first touch
            (2_000, 105.0),  # suppressed — still inside, not rearmed
            (3_000, 111.0),  # leave merged band → rearm
            (3_600_000, 105.0),  # second touch after cooldown
        ],
    )
    assert events == [1_000, 3_600_000]


def test_setup_ids_differ_per_touch_anchor():
    zkey = "STACKED_EMA_ZONE:EMA20+EMA59"
    a = make_setup_id(symbol="XRPUSDT", zone_key=zkey, anchor_ms=1_000)
    b = make_setup_id(symbol="XRPUSDT", zone_key=zkey, anchor_ms=3_600_000)
    assert a != b
    assert "XRPUSDT" in a and zkey in a

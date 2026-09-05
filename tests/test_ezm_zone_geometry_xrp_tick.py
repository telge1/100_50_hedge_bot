"""XRP tick + zone geometry: wrong tick must not inflate EMA bands / false stacks."""

from __future__ import annotations

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
    _make_zone,
    _primary_zone_key,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.proximity import (
    candle_ohlc_intersects_zone,
    classify_zone_approach_from_candle_ohlc,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import stacked_zone_label
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    zone_half_width,
)


def test_xrp_tick_is_exchange_price_increment():
    assert tick_size("XRPUSDT") == 0.0001


def test_xrp_zone_half_width_not_dominated_by_wrong_default_tick():
    atr = 0.00312  # ~XRP 5m ATR at marker 2026-08-19 18:36
    hw = zone_half_width(atr, tick=tick_size("XRPUSDT"))
    # Correct: max(0.15*ATR, 5*0.0001) ≈ max(0.000468, 0.0005) = 0.0005
    assert hw == 0.0005
    # Wrong default tick 0.01 would force hw=0.05 (~9% bands)
    assert hw < 0.01


def test_normal_1m_candle_keeps_own_high_low_not_cumulative():
    # Geometry check only — candle range is per-bar, not a running envelope.
    low, high = 1.0627, 1.0645
    assert high - low < 0.01
    assert candle_ohlc_intersects_zone(low=low, high=high, zone_low=1.0, zone_high=1.01) is False


def test_candle_between_emas_without_single_band_contact_is_not_touch():
    tick = tick_size("XRPUSDT")
    atr = 0.00312
    z20 = _make_zone("EMA20", 1.059921, atr, tick)
    z59 = _make_zone("EMA59", 1.049632, atr, tick)
    z200 = _make_zone("EMA200", 1.025170, atr, tick)
    # Candle sits between EMA20 and EMA200, touches none of the tight bands.
    lo, hi, close = 1.0627, 1.0645, 1.0631
    assert not candle_ohlc_intersects_zone(low=lo, high=hi, zone_low=z20.low, zone_high=z20.high)
    assert not candle_ohlc_intersects_zone(low=lo, high=hi, zone_low=z59.low, zone_high=z59.high)
    assert not candle_ohlc_intersects_zone(low=lo, high=hi, zone_low=z200.low, zone_high=z200.high)
    zones = {"EMA20": z20, "EMA59": z59, "EMA200": z200}
    assert stacked_zone_label(zones) is None
    primary = _primary_zone_key(zones, close)
    assert primary is not None
    zkey, zone = primary
    assert zkey == "EMA20"
    ev = classify_zone_approach_from_candle_ohlc(
        low=lo, high=hi, close=close, zone_low=zone.low, zone_high=zone.high
    )
    assert ev["exact_touch"] is False


def test_bullish_sorted_but_far_emas_do_not_form_stack():
    tick = tick_size("XRPUSDT")
    atr = 0.00312
    zones = {
        "EMA20": _make_zone("EMA20", 1.06, atr, tick),
        "EMA59": _make_zone("EMA59", 1.05, atr, tick),
        "EMA200": _make_zone("EMA200", 1.025, atr, tick),
    }
    # bullish order but centers ~1–2% apart; bands ~0.1% wide → no overlap
    assert zones["EMA20"].center > zones["EMA59"].center > zones["EMA200"].center
    assert stacked_zone_label(zones) is None


def test_overlapping_emas_form_valid_stack_and_merged_members_only():
    tick = tick_size("XRPUSDT")
    atr = 0.01  # wider ATR so 0.15*ATR can overlap nearby EMAs
    z20 = _make_zone("EMA20", 100.0, atr, tick)
    z59 = _make_zone("EMA59", 100.0 + z20.half_width * 0.5, atr, tick)
    z200 = _make_zone("EMA200", 90.0, atr, tick)  # far — must not join
    zones = {"EMA20": z20, "EMA59": z59, "EMA200": z200}
    label = stacked_zone_label(zones)
    assert label is not None
    assert "EMA20" in label and "EMA59" in label
    assert "EMA200" not in label
    primary = _primary_zone_key(zones, 100.0)
    assert primary is not None
    zkey, syn = primary
    assert zkey == label
    assert syn.low == min(z20.low, z59.low)
    assert syn.high == max(z20.high, z59.high)
    # merged envelope must not stretch to EMA200
    assert syn.low > z200.high or syn.high < z200.low or not (
        syn.low <= z200.low and syn.high >= z200.high
    )


def test_merged_envelope_only_contact_is_not_exact_touch_when_no_stack():
    """Price in empty space between far EMAs: nearest band exact_touch=False."""
    tick = tick_size("XRPUSDT")
    atr = 0.003
    zones = {
        "EMA20": _make_zone("EMA20", 1.10, atr, tick),
        "EMA59": _make_zone("EMA59", 1.00, atr, tick),
        "EMA200": _make_zone("EMA200", 0.90, atr, tick),
    }
    assert stacked_zone_label(zones) is None
    close = 1.05
    lo, hi = 1.049, 1.051
    primary = _primary_zone_key(zones, close)
    assert primary is not None
    _, zone = primary
    # Candle does not intersect the nearest single band
    assert not candle_ohlc_intersects_zone(low=lo, high=hi, zone_low=zone.low, zone_high=zone.high)
    ev = classify_zone_approach_from_candle_ohlc(
        low=lo, high=hi, close=close, zone_low=zone.low, zone_high=zone.high
    )
    assert ev["exact_touch"] is False


def test_real_ema20_wick_contact_is_exact_touch():
    tick = tick_size("XRPUSDT")
    atr = 0.003
    z20 = _make_zone("EMA20", 1.06, atr, tick)
    # wick dips into band; close stays above
    lo = z20.low - 0.00001
    hi = z20.high + 0.01
    close = z20.high + 0.005
    ev = classify_zone_approach_from_candle_ohlc(
        low=lo, high=hi, close=close, zone_low=z20.low, zone_high=z20.high
    )
    assert ev["exact_touch"] is True
    assert ev["touch_price_basis"] == "candle_ohlc_1m"


def test_valid_stack_contact_is_exact_touch():
    tick = tick_size("XRPUSDT")
    atr = 0.01
    z20 = _make_zone("EMA20", 100.0, atr, tick)
    z59 = _make_zone("EMA59", 100.0 + z20.half_width * 0.5, atr, tick)
    zones = {"EMA20": z20, "EMA59": z59, "EMA200": None}
    label = stacked_zone_label({k: v for k, v in zones.items() if v})
    assert label is not None
    primary = _primary_zone_key({k: v for k, v in zones.items() if v}, 100.0)
    assert primary is not None
    _, syn = primary
    ev = classify_zone_approach_from_candle_ohlc(
        low=syn.low,
        high=syn.high,
        close=(syn.low + syn.high) / 2,
        zone_low=syn.low,
        zone_high=syn.high,
    )
    assert ev["exact_touch"] is True

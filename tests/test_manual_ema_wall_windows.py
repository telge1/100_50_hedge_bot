"""Isolated tests for manual EMA+wall window analysis."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    WINDOWS,
    parse_utc,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.classify import (
    classify_window,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.episodes import (
    dedupe_episodes,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.impact import (
    classify_flow_mechanism,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    classify_trend,
    prepare_5m_indicators,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.outcomes import (
    expected_direction,
    side_mfe_mae,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
    make_zone,
)


def test_windows_are_exactly_plus_minus_30m_utc():
    for w in WINDOWS:
        c = parse_utc(w["center_utc"])
        s = parse_utc(w["start_utc"])
        e = parse_utc(w["end_utc"])
        assert (c - s).total_seconds() == 30 * 60
        assert (e - c).total_seconds() == 30 * 60
        assert w["center_utc"].endswith("Z")
        assert s.tzinfo is not None
        # not shifted to UTC+2/+3
        assert "T" in w["center_utc"]


def test_no_open_5m_in_trend_filter():
    # Build synthetic 1m closes
    times = pd.date_range("2026-08-24 12:00", periods=400, freq="1min", tz="UTC")
    price = 100.0
    rows = []
    for t in times:
        price *= 0.9995  # gentle downtrend
        rows.append({"open_time": t, "open": price, "high": price * 1.0001, "low": price * 0.9999, "close": price})
    candles = pd.DataFrame(rows)
    bars = prepare_5m_indicators(candles)
    asof = datetime(2026, 8, 25, 8, 33, tzinfo=timezone.utc)  # mid open 5m 08:30-08:35
    snap = classify_trend(bars, asof)
    assert snap.last_bar_end is not None
    # last closed must end at or before asof — bar_end for 08:25-08:30 ends 08:30
    end = pd.Timestamp(snap.last_bar_end)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    assert end <= pd.Timestamp(asof)
    # must not use 08:30 open candle close as decision bar end after asof
    assert end <= pd.Timestamp("2026-08-25 08:30:00", tz="UTC")


def test_causal_ema_warmup_required():
    times = pd.date_range("2026-08-25 07:00", periods=30, freq="1min", tz="UTC")
    rows = [
        {"open_time": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
        for t in times
    ]
    bars = prepare_5m_indicators(pd.DataFrame(rows))
    snap = classify_trend(bars, datetime(2026, 8, 25, 7, 30, tzinfo=timezone.utc))
    assert snap.classification == "UNDETERMINED"
    assert snap.warmup_ok is False


def test_ask_defense_vs_absorption_vs_pull():
    assert (
        classify_flow_mechanism(
            attack_side="BUY",
            wall_side="ASK",
            buy_n=500_000,
            sell_n=100_000,
            wall_notional_before=1_000_000,
            wall_notional_after=900_000,
            wall_present_after=True,
            price_held_beyond=False,
            consumed_estimate=100_000,
        )
        == "ASK_DEFENSE"
    )
    assert (
        classify_flow_mechanism(
            attack_side="BUY",
            wall_side="ASK",
            buy_n=800_000,
            sell_n=50_000,
            wall_notional_before=1_000_000,
            wall_notional_after=200_000,
            wall_present_after=False,
            price_held_beyond=True,
            consumed_estimate=800_000,
        )
        == "ASK_ABSORPTION"
    )
    assert (
        classify_flow_mechanism(
            attack_side="BUY",
            wall_side="ASK",
            buy_n=50_000,
            sell_n=10_000,
            wall_notional_before=1_000_000,
            wall_notional_after=0,
            wall_present_after=False,
            price_held_beyond=True,
            consumed_estimate=50_000,
        )
        == "LIQUIDITY_PULL"
    )


def test_bid_defense():
    assert (
        classify_flow_mechanism(
            attack_side="SELL",
            wall_side="BID",
            buy_n=100_000,
            sell_n=400_000,
            wall_notional_before=800_000,
            wall_notional_after=750_000,
            wall_present_after=True,
            price_held_beyond=False,
            consumed_estimate=50_000,
        )
        == "BID_DEFENSE"
    )


def test_breakout_hold_and_false_reclaim():
    zone = make_zone("EMA20", 100.0, atr=10.0)

    class S:
        def __init__(self, ts_ms, mid):
            self.ts_ms = ts_ms
            self.mid = mid

    t0 = 1_000_000
    # pierce then reclaim
    samples = (
        [S(t0 - 1000, 99.0)]
        + [S(t0 + i * 250, 100.0) for i in range(4)]
        + [S(t0 + 5000 + i * 250, 101.5) for i in range(20)]  # brief breakout
        + [S(t0 + 20000 + i * 250, 99.0) for i in range(200)]  # reclaim hold
    )
    tl = classify_window(
        data_incomplete=False,
        incomplete_reason="",
        samples=samples,
        zone=zone,
        zone_role="resistance",
        contact_ts_ms=t0,
        mechanism="ASK_DEFENSE",
        wall_present_before_contact=True,
        wall_present_after_60s=True,
        wall_moved=False,
    )
    # Depending on hold rules may be FALSE_BREAKOUT or RANGE — reclaim path preferred
    assert tl.primary_class in (
        "FALSE_BREAKOUT_RECLAIM",
        "RANGE_AROUND_ZONE",
        "DEFENSE_REJECTION",
    )
    assert tl.classification_at is not None


def test_long_short_mfe_mae_normalization():
    entry = 100.0
    t0 = 0
    path = [(0, 100.0), (60_000, 101.0), (120_000, 99.5)]
    long = side_mfe_mae(path, entry_ts_ms=t0, entry_px=entry, direction="LONG", horizon_s=180)
    short = side_mfe_mae(path, entry_ts_ms=t0, entry_px=entry, direction="SHORT", horizon_s=180)
    assert long["mfe_pct"] == pytest.approx(1.0)
    assert short["mfe_pct"] == pytest.approx(0.5)
    assert long["mae_pct"] == pytest.approx(0.5)
    assert short["mae_pct"] == pytest.approx(1.0)


def test_dedupe_overlapping_episodes():
    rows = [
        {
            "window_id": "circle_1",
            "center_utc": "2026-08-25T08:30:00Z",
            "start_utc": "2026-08-25T08:00:00Z",
            "end_utc": "2026-08-25T09:00:00Z",
            "primary_zone": "EMA20",
            "zone_role": "resistance",
            "primary_class": "DEFENSE_REJECTION",
            "primary_wall_price": 110000.0,
        },
        {
            "window_id": "circle_2",
            "center_utc": "2026-08-25T09:25:00Z",
            "start_utc": "2026-08-25T08:55:00Z",
            "end_utc": "2026-08-25T09:55:00Z",
            "primary_zone": "EMA20",
            "zone_role": "resistance",
            "primary_class": "DEFENSE_REJECTION",
            "primary_wall_price": 110000.0,
        },
        {
            "window_id": "rectangle",
            "center_utc": "2026-08-25T13:35:00Z",
            "start_utc": "2026-08-25T13:05:00Z",
            "end_utc": "2026-08-25T14:05:00Z",
            "primary_zone": "EMA20",
            "zone_role": "resistance",
            "primary_class": "ABSORPTION_THEN_BREAKOUT",
            "primary_wall_price": 111000.0,
        },
        {
            "window_id": "final_circle",
            "center_utc": "2026-08-25T14:35:00Z",
            "start_utc": "2026-08-25T14:05:00Z",
            "end_utc": "2026-08-25T15:05:00Z",
            "primary_zone": "EMA59",
            "zone_role": "resistance",
            "primary_class": "DEFENSE_REJECTION",
            "primary_wall_price": 112000.0,
        },
    ]
    eps = dedupe_episodes(rows)
    assert any(e["n_windows"] >= 2 for e in eps)
    assert any(e["episode_id"] == "ep_sequence_ema20_to_ema59" for e in eps)


def test_data_incomplete_classification():
    tl = classify_window(
        data_incomplete=True,
        incomplete_reason="L2_missing_15:00-15:05",
        samples=[],
        zone=None,
        zone_role="none",
        contact_ts_ms=None,
        mechanism="UNDETERMINED",
        wall_present_before_contact=False,
        wall_present_after_60s=False,
        wall_moved=False,
    )
    assert tl.primary_class == "DATA_INCOMPLETE"


def test_direction_mapping():
    assert expected_direction("DEFENSE_REJECTION", "resistance", "BEARISH") == "SHORT"
    assert expected_direction("ABSORPTION_THEN_BREAKOUT", "resistance", "TRANSITION") == "LONG"
    assert expected_direction("RANGE_AROUND_ZONE", "resistance", "BEARISH") == "NO_TRADE"

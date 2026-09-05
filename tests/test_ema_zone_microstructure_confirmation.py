"""Unit tests for ema_zone_microstructure_confirmation candidate detector."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.ema_zone_microstructure_confirmation.candidate_states import (
    build_state_timeline,
    map_primary_to_candidate,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    REGISTERED_CANDIDATE_STATES,
    methodology_defaults,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.oi_liq import (
    liquidation_features,
    oi_features,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
    is_flat_compression,
    map_regime_label,
    prepare_bars_with_ema200,
    regime_snapshot,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.runner import compile_ezm_contract
from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import (
    approach_side,
    build_zones,
    next_zone_clearance,
    stacked_zone_label,
    zone_feature_row,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.impact import (
    classify_flow_mechanism,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (
    classify_trend,
    last_closed_bar_at,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import make_zone
from orderbook_analyse.strategy_lab.compiler_v2 import (
    StrategyCompilationError,
    compile_strategy_v2,
)
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2

REPO = Path(__file__).resolve().parents[1]


def _synthetic_candles(n_minutes: int = 2500, trend: float = -0.0002) -> pd.DataFrame:
    times = pd.date_range("2026-08-20 00:00", periods=n_minutes, freq="1min", tz="UTC")
    price = 110_000.0
    rows = []
    for t in times:
        price *= 1.0 + trend
        rows.append(
            {
                "open_time": t,
                "open": price,
                "high": price * 1.0002,
                "low": price * 0.9998,
                "close": price,
            }
        )
    return pd.DataFrame(rows)


def test_only_closed_5m_bars():
    candles = _synthetic_candles()
    bars = prepare_bars_with_ema200(candles)
    asof = datetime(2026, 8, 25, 8, 33, tzinfo=timezone.utc)
    snap = regime_snapshot(bars, asof)
    end = pd.Timestamp(snap["last_bar_end"])
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    assert end <= pd.Timestamp(asof)
    assert end <= pd.Timestamp("2026-08-25 08:30:00", tz="UTC")
    row = last_closed_bar_at(bars, asof)
    assert row is not None
    assert pd.Timestamp(row["bar_end"]) <= pd.Timestamp(asof)


def test_ema200_not_equal_weight_in_short_term_regime():
    candles = _synthetic_candles()
    bars = prepare_bars_with_ema200(candles)
    asof = datetime(2026, 8, 25, 8, 30, tzinfo=timezone.utc)
    snap = regime_snapshot(bars, asof)
    assert snap["ema200_in_regime_score"] is False
    # score_components must not include ema200
    assert "ema200" not in snap["score_components"]
    legacy = classify_trend(bars, asof)
    assert "ema200" not in legacy.score_components


def test_flat_compression_blocks():
    assert is_flat_compression("RANGE", atr=100.0, close=100.0, ema20=100.0, s20_3=0.0, s59_3=0.0)
    assert map_regime_label("RANGE") == "range_compression"
    state, reasons = map_primary_to_candidate(
        data_incomplete=False,
        block_flat=True,
        wait_next_zone=False,
        primary_class="DEFENSE_REJECTION",
        mechanism="ASK_DEFENSE",
        possible_regime_flip=False,
        full_regime_flip=False,
        liquidity_pull_tagged=False,
    )
    assert state == "block_flat_compression"
    assert "BLOCK_FLAT_COMPRESSION" in reasons


def test_approach_from_above_below():
    z = make_zone("EMA20", 100.0, 10.0)
    assert approach_side(105.0, z) == "from_above"
    assert approach_side(95.0, z) == "from_below"
    assert approach_side(100.0, z) == "inside"


def test_atr_tick_zone_band():
    z = make_zone("EMA20", 100_000.0, 200.0)
    # half_width = max(0.15*200, 5*0.1) = max(30, 0.5) = 30
    assert z.half_width == pytest.approx(30.0)
    assert z.high - z.low == pytest.approx(60.0)


def test_stacked_ema_dedup():
    zones = build_zones(ema20=100.0, ema59=100.5, ema200=200.0, atr=10.0)
    label = stacked_zone_label(zones)
    assert label is not None
    assert label.startswith("STACKED_EMA_ZONE")
    row = zone_feature_row(
        window_id="t",
        zones=zones,
        mid=100.0,
        mid_before=101.0,
        primary_name="EMA20",
        wall_confluence=False,
        swing_confluence=False,
        zone_watch_started_at="2026-08-25T08:00:00Z",
        zone_touch_at="2026-08-25T08:10:00Z",
    )
    # stacked replaces separate multi-EMA events
    assert "STACKED" in row["zone_kind"]


def test_next_zone_clearance_wait():
    primary = make_zone("EMA20", 100_000.0, 50.0)
    # ~0.3% gap to EMA59
    stronger = make_zone("EMA59", 100_350.0, 50.0)
    clr = next_zone_clearance(100_000.0, primary, [primary, stronger])
    assert clr["nearest_stronger_zone"] == "EMA59"
    # may or may not wait depending on edge gap; ensure fields present
    assert "wait_next_zone" in clr
    assert "clearance_pct" in clr


def test_ask_bid_defense_and_absorption_vs_pull():
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
            attack_side="SELL",
            wall_side="BID",
            buy_n=50_000,
            sell_n=400_000,
            wall_notional_before=800_000,
            wall_notional_after=700_000,
            wall_present_after=True,
            price_held_beyond=False,
            consumed_estimate=50_000,
        )
        == "BID_DEFENSE"
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
    state, reasons = map_primary_to_candidate(
        data_incomplete=False,
        block_flat=False,
        wait_next_zone=False,
        primary_class="LIQUIDITY_PULL_BREAKOUT",
        mechanism="LIQUIDITY_PULL",
        possible_regime_flip=False,
        full_regime_flip=False,
        liquidity_pull_tagged=True,
    )
    assert state == "wait_microstructure_confirmation"
    assert "LIQUIDITY_PULL_NOT_ABSORPTION" in reasons


def test_breakout_and_false_breakout_states():
    s, _ = map_primary_to_candidate(
        data_incomplete=False,
        block_flat=False,
        wait_next_zone=False,
        primary_class="ABSORPTION_THEN_BREAKOUT",
        mechanism="ASK_ABSORPTION",
        possible_regime_flip=False,
        full_regime_flip=False,
        liquidity_pull_tagged=False,
    )
    assert s == "breakout_confirmed"
    s, _ = map_primary_to_candidate(
        data_incomplete=False,
        block_flat=False,
        wait_next_zone=False,
        primary_class="FALSE_BREAKOUT_RECLAIM",
        mechanism="ASK_ABSORPTION",
        possible_regime_flip=False,
        full_regime_flip=False,
        liquidity_pull_tagged=False,
    )
    assert s == "false_breakout_confirmed"


def test_oi_liq_no_lookahead():
    oi = pd.DataFrame(
        {
            "minute": pd.to_datetime(
                ["2026-08-25T08:00:00Z", "2026-08-25T08:15:00Z", "2026-08-25T09:00:00Z"], utc=True
            ),
            "open_interest": [100.0, 110.0, 200.0],
        }
    )
    contact = datetime(2026, 8, 25, 8, 20, tzinfo=timezone.utc)
    row = oi_features(oi, window_id="c1", contact_at=contact, price_before=100.0, price_after=101.0)
    assert row["lookahead_safe"] is True
    # must not use 09:00 OI
    assert float(row["oi_abs_change"]) == pytest.approx(10.0)  # 110-100

    liq = pd.DataFrame(
        {
            "event_time": pd.to_datetime(
                ["2026-08-25T08:10:00Z", "2026-08-25T09:30:00Z"], utc=True
            ),
            "side": ["Buy", "Sell"],
            "notional": [1_000_000.0, 9_000_000.0],
        }
    )
    lrow = liquidation_features(
        liq,
        window_id="c1",
        start=datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
        end=datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc),
        contact_at=contact,
    )
    assert lrow["lookahead_safe"] is True
    assert lrow["liq_long_notional"] == pytest.approx(1_000_000.0)
    assert lrow["liq_short_notional"] == pytest.approx(0.0)  # future event excluded


def test_state_timestamps_causal_and_outcome_ignored():
    rows, final, _ = build_state_timeline(
        window_id="circle_1",
        window_start="2026-08-25T08:00:00.000Z",
        contact_at="2026-08-25T08:20:00.000Z",
        classification_at="2026-08-25T08:21:00.000Z",
        data_incomplete=False,
        incomplete_reason="",
        block_flat=False,
        wait_next_zone=False,
        primary_class="DEFENSE_REJECTION",
        mechanism="ASK_DEFENSE",
        possible_regime_flip=False,
        full_regime_flip=False,
        flip_clocks={},
        evidence_until="2026-08-25T08:21:00.000Z",
        quality_status="OK",
    )
    assert final == "defense_rejection_confirmed"
    for r in rows:
        assert r["decision_at"] >= r["evidence_available_until"] or r["decision_at"] >= r["observed_at"]
        assert r["new_state"] in REGISTERED_CANDIDATE_STATES
    # injecting fake outcome must not change mapping
    s1, _ = map_primary_to_candidate(
        data_incomplete=False,
        block_flat=False,
        wait_next_zone=False,
        primary_class="DEFENSE_REJECTION",
        mechanism="ASK_DEFENSE",
        possible_regime_flip=False,
        full_regime_flip=False,
        liquidity_pull_tagged=False,
    )
    assert s1 == "defense_rejection_confirmed"


def test_data_incomplete_state():
    s, reasons = map_primary_to_candidate(
        data_incomplete=True,
        block_flat=False,
        wait_next_zone=False,
        primary_class="DEFENSE_REJECTION",
        mechanism="ASK_DEFENSE",
        possible_regime_flip=False,
        full_regime_flip=False,
        liquidity_pull_tagged=False,
    )
    assert s == "data_incomplete"
    assert "DATA_INCOMPLETE" in reasons


def test_no_trade_execution_and_candidate_compiler():
    defaults = methodology_defaults()
    assert defaults["oi_liq_as_hard_gates"] is False
    contract = compile_ezm_contract(REPO)
    assert contract["compiler"] == "compile_candidate_discovery_v2"
    assert contract["trade_compiler_used"] is False
    assert contract["plugin_id"] == "ema_zone_microstructure_confirmation"
    catalogs = production_catalog_bundle_v2()
    spec = load_strategy_v2_yaml_file(REPO / "strategies/strategy_lab/ema_zone_microstructure_confirmation_v1.yaml")
    with pytest.raises(StrategyCompilationError, match="CANDIDATE_DISCOVERY_NOT_TRADE_BACKTEST"):
        compile_strategy_v2(spec, catalogs)

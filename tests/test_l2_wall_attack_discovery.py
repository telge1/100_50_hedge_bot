"""Focused tests for L2 wall attack pattern discovery V1."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from orderbook_analyse.l2_wall_attack_discovery.attribution import window_trade_stats
from orderbook_analyse.l2_wall_attack_discovery.classify import _rule_predict
from orderbook_analyse.l2_wall_attack_discovery.labels import classify_resolution
from orderbook_analyse.l2_wall_attack_discovery.models import ATTACK_SIDE_BY_WALL, safe_div, tick_size
from orderbook_analyse.l2_wall_attack_discovery.attacks import build_attack_episodes
from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import WallLifecycle
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def test_tick_normalization() -> None:
    assert tick_size("BTCUSDT") == 0.1
    assert tick_size("DOGEUSDT") == 0.00001


def test_aggressor_side_mapping() -> None:
    assert ATTACK_SIDE_BY_WALL["BID"] == "Sell"
    assert ATTACK_SIDE_BY_WALL["ASK"] == "Buy"


def test_safe_div_no_zero() -> None:
    assert safe_div(1.0, 0.0) is None
    assert safe_div(None, 1.0) is None


def test_trade_window_half_open_no_double_count() -> None:
    trades = pd.DataFrame(
        {
            "ts_ms": [1000, 2000, 3000],
            "trade_id": [1, 2, 3],
            "side": ["Sell", "Sell", "Buy"],
            "price": [100.0, 100.0, 100.1],
            "size": [1.0, 1.0, 1.0],
            "notional": [100.0, 100.0, 100.1],
        }
    )
    w = window_trade_stats(
        trades, start_ms=1000, end_ms=3000, wall_price=100.0, side="BID", symbol="BTCUSDT"
    )
    assert w["trades_present"] is True
    assert w["trade_count"] == 2  # excludes end
    assert w["attack_side_notional"] == 200.0


def test_missing_trades_stay_missing() -> None:
    w = window_trade_stats(
        pd.DataFrame(), start_ms=0, end_ms=1000, wall_price=1.0, side="BID", symbol="BTCUSDT"
    )
    assert w["trades_present"] is False
    assert w["trade_count"] is None
    assert w["attack_side_notional"] is None


def test_primary_attack_one_per_lifecycle() -> None:
    lc = WallLifecycle(
        lifecycle_id="lc_1",
        symbol="BTCUSDT",
        side="BID",
        direction="LONG",
        wall_price=100.0,
        appear_ts=0,
        approach_ts=1000,
        touch_ts=2000,
        absorption_ts=None,
        pull_ts=None,
        break_ts=None,
        reclaim_ts=5000,
        end_ts=6000,
        peak_qty=10.0,
        completion_class="COMPLETE_PRIMARY",
        n_touch_events=1,
        source_file="x",
    )
    samples = [
        SampleRow(
            symbol="BTCUSDT",
            ts_ms=t,
            best_bid=100.0,
            best_ask=100.1,
            mid=100.05,
            spread=0.1,
            spread_bps=1.0,
            microprice=100.05,
            bid_levels=200,
            ask_levels=200,
            bid_qty_l10=1,
            ask_qty_l10=1,
            imbalance_l10=0,
            bid_qty_bps10=1,
            ask_qty_bps10=1,
            imbalance_bps10=0,
            bid_wall_price=100.0,
            bid_wall_qty=10.0,
            ask_wall_price=100.5,
            ask_wall_qty=5.0,
            source_file="x",
            warmup=False,
        )
        for t in range(0, 7000, 250)
    ]
    trades = pd.DataFrame(
        {
            "ts_ms": [2100],
            "trade_id": [1],
            "side": ["Sell"],
            "price": [100.0],
            "size": [1.0],
            "notional": [100.0],
            "trade_ts": [datetime(2026, 8, 25, tzinfo=timezone.utc)],
        }
    )
    eps, _ = build_attack_episodes([lc], {"BTCUSDT": samples}, {"BTCUSDT": trades}, seed=1)
    prim = [e for e in eps if e["is_primary"]]
    assert len(prim) == 1
    assert prim[0]["first_contact_at"] == 2000


def test_flow_died_vs_absorption_rule() -> None:
    assert _rule_predict({"attack_notional": 0, "pull_proxy": False}) == "FLOW_DIED_NO_DEFENSE"
    assert (
        _rule_predict(
            {
                "attack_notional": 1000,
                "resilience_ratio": 0.9,
                "refill_ratio": 0.6,
                "price_response_per_notional": 1e-6,
                "pull_proxy": False,
            }
        )
        == "ABSORBED_REFILLED"
    )


def test_label_pulled_before_contact() -> None:
    ep = {"attack_id": "a", "side": "BID", "resolution_hint_pre": "PULLED_BEFORE_CONTACT"}
    lab = classify_resolution(ep, [], {}, horizon_s=60)
    assert lab["resolution_class"] == "PULLED_BEFORE_CONTACT"
    assert lab["semantic_role"] == "ex_post_label"

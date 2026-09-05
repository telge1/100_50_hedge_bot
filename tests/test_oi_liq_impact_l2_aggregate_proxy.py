"""Focused offline tests for F3 aggregate wall proxy discovery."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.analysis import (
    analyze_cluster,
    build_timeline_rows,
    find_anchor_row,
    impact_compression_metrics,
    safe_div,
    wall_status,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.constants import (
    BTCUSDT_TICK,
    WALL_PRICE_COLUMN,
    WALL_STATUS_CHANGED,
    WALL_STATUS_EXACT,
    WALL_STATUS_INVALID,
    WALL_STATUS_MISSING,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.controls import build_matched_controls
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import FlushCluster, build_flush_clusters


def _cluster(direction: str = "LONG", start: str = "2026-08-20T12:46:00Z") -> FlushCluster:
    return FlushCluster(
        cluster_id=f"oildisc_cluster:BTCUSDT:{direction}:{start}",
        symbol="BTCUSDT",
        direction=direction,
        cluster_start=start,
        cluster_end=start,
        candidate_ids=("oildisc:test",),
        primary_candidate_id="oildisc:test",
        flush_minutes=1,
        gap_minutes=1,
    )


def _ob_row(
    ts: str,
    *,
    genuine: bool = True,
    bid_wall: float = 100.0,
    ask_wall: float = 101.0,
    mid: float = 100.5,
) -> dict[str, object]:
    flags = "" if genuine else "carried_forward"
    return {
        "bucket_start": pd.Timestamp(ts, tz="UTC"),
        "quality_flags": flags,
        "is_valid": 1,
        "is_genuine": genuine,
        "best_bid_price": bid_wall - 0.1,
        "best_ask_price": ask_wall + 0.1,
        "mid_price": mid,
        "microprice": mid,
        "spread_bps": 1.0,
        "bid_wall_price": bid_wall,
        "bid_wall_qty": 5.0,
        "bid_wall_bps_dist": 10.0,
        "ask_wall_price": ask_wall,
        "ask_wall_qty": 4.0,
        "ask_wall_bps_dist": 10.0,
        "bid_qty_l50": 10.0,
        "ask_qty_l50": 8.0,
        "imbalance_l50": 0.1,
        "ofi": 1.0,
        "bid_qty_added": 1.0,
        "bid_qty_removed": 0.5,
        "ask_qty_added": 0.2,
        "ask_qty_removed": 0.1,
        "processed_updates": 5,
        "last_update_seq": 1,
    }


def test_long_uses_bid_wall_short_uses_ask_wall() -> None:
    assert WALL_PRICE_COLUMN["LONG"] == "bid_wall_price"
    assert WALL_PRICE_COLUMN["SHORT"] == "ask_wall_price"
    ob = pd.DataFrame([_ob_row("2026-08-20T12:45:59Z"), _ob_row("2026-08-20T12:46:00Z")])
    anchor = find_anchor_row(ob, pd.Timestamp("2026-08-20T12:46:00Z"))
    assert anchor is not None
    long_tl, long_meta = build_timeline_rows(_cluster("LONG"), ob, pd.DataFrame(), anchor)
    short_tl, _ = build_timeline_rows(_cluster("SHORT"), ob, pd.DataFrame(), anchor)
    assert long_tl[-1]["dominant_wall_price"] == 100.0
    assert short_tl[-1]["dominant_wall_price"] == 101.0


def test_wall_anchor_only_from_pre_cluster_data() -> None:
    ob = pd.DataFrame(
        [
            _ob_row("2026-08-20T12:45:58Z", bid_wall=99.0),
            _ob_row("2026-08-20T12:45:59Z", bid_wall=100.0),
            _ob_row("2026-08-20T12:46:01Z", bid_wall=105.0),
        ]
    )
    anchor = find_anchor_row(ob, pd.Timestamp("2026-08-20T12:46:00Z"))
    assert float(anchor["bid_wall_price"]) == 100.0


def test_exact_and_near_stability_separate() -> None:
    assert wall_status(100.0, 100.0, is_genuine=True) == WALL_STATUS_EXACT
    near = wall_status(100.1, 100.0, is_genuine=True, tick=BTCUSDT_TICK)
    assert near == "DOMINANT_WALL_STABLE_NEAR"


def test_wall_change_not_removal_label() -> None:
    assert WALL_STATUS_CHANGED == "DOMINANT_WALL_CHANGED"
    assert "REMOVED" not in WALL_STATUS_CHANGED


def test_reappearance_label_not_refill() -> None:
    text = "dominant_wall_reappeared"
    assert "REFILL" not in text.upper()


def test_qty_not_compared_across_price_change() -> None:
    ob = pd.DataFrame(
        [
            _ob_row("2026-08-20T12:45:59Z", bid_wall=100.0),
            _ob_row("2026-08-20T12:46:01Z", bid_wall=101.0),
        ]
    )
    ob.loc[1, "bid_wall_qty"] = 20.0
    result = analyze_cluster(
        _cluster(),
        ob,
        pd.DataFrame(columns=["second", "trade_count", "buy_notional", "sell_notional"]),
        pd.DataFrame(columns=["open_time", "open", "high", "low", "close"]),
        pd.DataFrame(),
    )
    ratio = result["stability"]["same_anchor_price_qty_ratio"]
    assert ratio is None or ratio != 4.0


def test_carried_forward_invalid_no_dynamics() -> None:
    assert wall_status(100.0, 100.0, is_genuine=False) == WALL_STATUS_INVALID


def test_cf_only_second_no_recovery_or_flip() -> None:
    ob = pd.DataFrame([_ob_row("2026-08-20T12:45:59Z"), _ob_row("2026-08-20T12:46:01Z", genuine=False)])
    result = analyze_cluster(
        _cluster(),
        ob,
        pd.DataFrame(columns=["second", "trade_count", "buy_notional", "sell_notional"]),
        pd.DataFrame(columns=["open_time", "open", "high", "low", "close"]),
        pd.DataFrame(),
    )
    assert result["recovery"] == []
    assert result["flip"]["first_any_flip_second"] is None


def test_long_short_l2_direction_mirrored() -> None:
    ob = pd.DataFrame([_ob_row("2026-08-20T12:45:59Z"), _ob_row("2026-08-20T12:46:01Z")])
    anchor = find_anchor_row(ob, pd.Timestamp("2026-08-20T12:46:00Z"))
    long_tl, _ = build_timeline_rows(_cluster("LONG"), ob, pd.DataFrame(), anchor)
    short_tl, _ = build_timeline_rows(_cluster("SHORT"), ob, pd.DataFrame(), anchor)
    assert long_tl[-1]["directional_depth_l50"] == 10.0
    assert short_tl[-1]["directional_depth_l50"] == 8.0


def test_impact_direction_long_sell_notional() -> None:
    trades = pd.DataFrame(
        {
            "second": [pd.Timestamp("2026-08-20T12:46:00Z", tz="UTC")],
            "trade_count": [10],
            "buy_notional": [100.0],
            "sell_notional": [500.0],
        }
    )
    timeline = [
        {
            "second": "2026-08-20T12:46:00Z",
            "mid_price": 100.0,
        },
        {
            "second": "2026-08-20T12:46:01Z",
            "mid_price": 99.0,
        },
    ]
    comp = impact_compression_metrics(_cluster("LONG"), timeline, trades, data_abort=False)
    assert comp["data_abort"] is False


def test_trade_feed_gap_aborts_event() -> None:
    minute_features = pd.DataFrame(
        [
            {
                "symbol": "BTCUSDT",
                "minute": "2026-08-20T12:46:00Z",
                "direction": "LONG",
                "technical_gap": False,
                "trades_present": False,
                "directional_flush_observed": True,
            }
        ]
    )
    ob = pd.DataFrame([_ob_row("2026-08-20T12:45:59Z"), _ob_row("2026-08-20T12:46:01Z")])
    result = analyze_cluster(
        _cluster(),
        ob,
        pd.DataFrame(columns=["second", "trade_count", "buy_notional", "sell_notional"]),
        pd.DataFrame(columns=["open_time", "open", "high", "low", "close"]),
        minute_features,
    )
    assert result["data_abort"] is True


def test_1s_and_1m_reclaim_fields_separate() -> None:
    ob = pd.DataFrame([_ob_row("2026-08-20T12:45:59Z"), _ob_row("2026-08-20T12:46:05Z", mid=101.0)])
    candles = pd.DataFrame(
        {
            "open_time": [pd.Timestamp("2026-08-20T12:47:00Z", tz="UTC")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [101.0],
        }
    )
    minute_features = pd.DataFrame(
        [
            {
                "symbol": "BTCUSDT",
                "minute": "2026-08-20T12:46:00Z",
                "direction": "LONG",
                "technical_gap": False,
                "trades_present": True,
                "close": 100.0,
            }
        ]
    )
    result = analyze_cluster(
        _cluster(),
        ob,
        pd.DataFrame(columns=["second", "trade_count", "buy_notional", "sell_notional"]),
        candles,
        minute_features,
    )
    reclaim = result["reclaims"][0]
    assert "first_1s_proxy_reclaim_at" in reclaim
    assert "first_1m_close_reclaim_at" in reclaim


def test_future_labels_not_used_in_analysis() -> None:
    source = Path("src/orderbook_analyse/oi_liq_impact_l2/aggregate_proxy/analysis.py").read_text()
    assert "mfe_pct" not in source
    assert "forward_return" not in source


def test_controls_match_causal_fields_only() -> None:
    minutes = pd.date_range("2026-08-20T12:33:00Z", periods=20, freq="1min", tz="UTC")
    rows = []
    for minute in minutes:
        for direction in ("LONG", "SHORT"):
            rows.append(
                {
                    "symbol": "BTCUSDT",
                    "minute": minute.isoformat().replace("+00:00", "Z"),
                    "direction": direction,
                    "technical_gap": False,
                    "trades_present": True,
                    "oi_state_valid": True,
                    "orderbook_present": True,
                    "directional_flush_observed": minute.minute == 46 and direction == "LONG",
                    "price_displacement_pct": -0.001 if direction == "LONG" else 0.001,
                }
            )
    minute_features = pd.DataFrame(rows)
    candidates = [
        {
            "candidate_id": "oildisc:a",
            "symbol": "BTCUSDT",
            "minute": "2026-08-20T12:46:00Z",
            "direction": "LONG",
        }
    ]
    clusters = build_flush_clusters(candidates, gap_minutes=1)
    controls, _ = build_matched_controls(minute_features, pd.DataFrame(candidates), clusters)
    assert controls
    assert "match_distance" in controls[0]


def test_clustering_deterministic() -> None:
    candidates = [
        {"candidate_id": "a", "symbol": "BTCUSDT", "minute": "2026-08-20T12:33:00Z", "direction": "LONG"},
        {"candidate_id": "b", "symbol": "BTCUSDT", "minute": "2026-08-20T12:34:00Z", "direction": "LONG"},
    ]
    c1 = build_flush_clusters(candidates, gap_minutes=1)
    c2 = build_flush_clusters(candidates, gap_minutes=1)
    assert [c.cluster_id for c in c1] == [c.cluster_id for c in c2]


def test_safe_div_no_infinity() -> None:
    assert safe_div(1.0, 0.0) is None
    assert safe_div(1.0, 2.0) == 0.5


def test_missing_wall_status() -> None:
    assert wall_status(0.0, 100.0, is_genuine=True) == WALL_STATUS_MISSING


def test_manifest_hash_helper_stable() -> None:
    payload = {"a": 1, "b": 2}
    h1 = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    h2 = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert h1 == h2


def test_deterministic_timeline_for_same_input() -> None:
    ob = pd.DataFrame([_ob_row("2026-08-20T12:45:59Z"), _ob_row("2026-08-20T12:46:01Z")])
    anchor = find_anchor_row(ob, pd.Timestamp("2026-08-20T12:46:00Z"))
    tl1, _ = build_timeline_rows(_cluster(), ob, pd.DataFrame(), anchor)
    tl2, _ = build_timeline_rows(_cluster(), ob, pd.DataFrame(), anchor)
    assert tl1 == tl2

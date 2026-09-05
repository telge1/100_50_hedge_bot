"""Focused tests for OB200 V3 causal join / flush / compression / entry."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from orderbook_analyse.ob200_v3_raw_discovery.v3.pipeline import (
    classify_compression,
    classify_flush,
    oi_asof,
    reclaim_and_entry,
    window_trades,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.analysis import safe_div


def test_safe_div_no_zero() -> None:
    assert safe_div(1.0, 0.0) is None
    assert safe_div(None, 1.0) is None
    assert abs(safe_div(10.0, 2.0) - 5.0) < 1e-9


def test_trade_side_aggressor_mapping() -> None:
    from orderbook_analyse.oi_liq_impact_l2.contracts import AGGRESSOR_SIDE_BY_DIRECTION

    assert AGGRESSOR_SIDE_BY_DIRECTION["LONG"] == "Sell"
    assert AGGRESSOR_SIDE_BY_DIRECTION["SHORT"] == "Buy"


def test_liquidation_side_mapping() -> None:
    from orderbook_analyse.oi_liquidation_collector.logic import interpret_liquidated_position_side

    assert interpret_liquidated_position_side("Buy") == "LIQUIDATED_LONG"
    assert interpret_liquidated_position_side("Sell") == "LIQUIDATED_SHORT"


def test_oi_asof_no_future() -> None:
    oi = pd.DataFrame(
        {
            "bucket_time": pd.to_datetime(
                ["2026-08-25T06:00:00Z", "2026-08-25T06:00:05Z", "2026-08-25T06:00:10Z"], utc=True
            ),
            "open_interest": [100.0, 99.0, 98.0],
            "open_interest_value": [1.0, 1.0, 1.0],
        }
    )
    when = datetime(2026, 8, 25, 6, 0, 7, tzinfo=timezone.utc)
    row = oi_asof(oi, when, max_staleness_s=30)
    assert row["status"] == "PRESENT"
    assert row["oi"] == 99.0
    # ensure not using 06:00:10
    assert "06:00:05" in str(row["oi_ts"])


def test_window_trades_half_open() -> None:
    trades = pd.DataFrame(
        {
            "second": pd.to_datetime(
                ["2026-08-25T06:00:00Z", "2026-08-25T06:00:01Z", "2026-08-25T06:00:02Z"], utc=True
            ),
            "trade_count": [1, 1, 1],
            "buy_notional": [10.0, 0.0, 5.0],
            "sell_notional": [0.0, 20.0, 0.0],
        }
    )
    start = datetime(2026, 8, 25, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 25, 6, 0, 2, tzinfo=timezone.utc)
    w = window_trades(trades, start, end, direction="LONG")
    assert w["trades_present"] is True
    assert w["aggressive_sell_notional"] == 20.0
    assert w["trade_count"] == 2  # excludes end second


def test_flush_long_confirmed() -> None:
    flush = classify_flush(
        direction="LONG",
        price_change_bps=-15.0,
        oi_delta=-10.0,
        oi_status="PRESENT",
        trades={"trades_present": True, "aggressive_notional": 5000.0},
        liqs={"matched_liq_notional": 1000.0, "matched_liq_count": 2, "liqs_present": True},
    )
    assert flush["flush_class"] == "CONFIRMED_FLUSH"


def test_flush_invalid_direction() -> None:
    flush = classify_flush(
        direction="LONG",
        price_change_bps=20.0,
        oi_delta=-10.0,
        oi_status="PRESENT",
        trades={"trades_present": True, "aggressive_notional": 5000.0},
        liqs={"matched_liq_notional": 1000.0, "matched_liq_count": 2, "liqs_present": True},
    )
    assert flush["flush_class"] == "INVALID_DIRECTION"


def test_flow_died_not_compression() -> None:
    impact = {
        "first5_trades_present": True,
        "last5_trades_present": True,
        "first5_impact_per_notional": 0.01,
        "last5_impact_per_notional": 0.001,
        "first5_aggressive_notional": 10000.0,
        "last5_aggressive_notional": 100.0,
    }
    c = classify_compression(impact, ratio_cut=0.75)
    assert c["compression_class"] == "FLOW_DIED"


def test_entry_after_confirmed() -> None:
    samples = pd.DataFrame(
        {
            "ts_ms": [1000, 2000, 3000, 4000, 5000],
            "mid": [100.0, 100.1, 100.2, 100.3, 100.4],
        }
    )
    chain = {
        "touch_ts": 1000,
        "reclaim_ts": 3000,
        "wall_price": 100.0,
        "direction": "LONG",
    }
    r = reclaim_and_entry(chain, samples)
    assert r["confirmed_at"] == 3000
    assert r["entry_at"] == 4000
    assert r["entry_at"] > r["confirmed_at"]


def test_strict_requires_flush_and_ic() -> None:
    from orderbook_analyse.ob200_v3_raw_discovery.v3.pipeline import build_full_chain_row

    chain = {
        "chain_id": "c1",
        "lifecycle_id": "l1",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "touch_ts": 1,
        "absorption_ts": 2,
        "pull_ts": None,
        "break_ts": None,
        "reclaim_ts": 3,
        "wall_price": 1,
        "completion_class": "COMPLETE_PRIMARY",
    }
    flush = {"flush_class": "NO_FLUSH"}
    impact = {"first5_trades_present": True, "first5_aggressive_notional": 100}
    compression = {"compression_class": "IC_STRICT"}
    reclaim = {"reclaim_variant": "R3_HOLD_3S", "entry_at": 4, "entry_mid": 1.0}
    row = build_full_chain_row(chain, flush, impact, compression, reclaim)
    assert row["completion_class_v3"] != "FULL_STRATEGY_CHAIN_STRICT"


def test_flush_partial_and_data_unavailable() -> None:
    partial = classify_flush(
        direction="SHORT",
        price_change_bps=12.0,
        oi_delta=-5.0,
        oi_status="PRESENT",
        trades={"trades_present": True, "aggressive_notional": 1000.0},
        liqs={"matched_liq_notional": 0.0, "matched_liq_count": 0, "liqs_present": True},
    )
    assert partial["flush_class"] == "PARTIAL_FLUSH"
    missing = classify_flush(
        direction="LONG",
        price_change_bps=-10.0,
        oi_delta=-1.0,
        oi_status="MISSING",
        trades={"trades_present": True, "aggressive_notional": 1000.0},
        liqs={"matched_liq_notional": 1.0, "matched_liq_count": 1, "liqs_present": True},
    )
    assert missing["flush_class"] == "DATA_UNAVAILABLE"


def test_match_controls_excludes_event_windows() -> None:
    from orderbook_analyse.ob200_v3_raw_discovery.v3.pipeline import match_controls

    # synthetic 1s samples over one hour
    base = 1_787_637_600_000  # 2026-08-25T06:00:00Z
    rows = []
    for i in range(3600):
        rows.append(
            {
                "ts_ms": base + i * 1000,
                "mid": 100.0 + i * 0.001,
                "spread_bps": 1.0,
                "imbalance_l10": 0.0,
                "warmup": False,
            }
        )
    samples = {"BTCUSDT": pd.DataFrame(rows)}
    entries = [
        {
            "chain_id": "e1",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_at": base + 1800_000,
            "entry_mid": 100.5,
            "touch_ts": base + 1700_000,
            "reclaim_ts": base + 1750_000,
            "spread_bps": 1.0,
            "imbalance_l10": 0.0,
        }
    ]
    ctrls, quality = match_controls(entries, samples, seed=42, per_event=2)
    assert quality[0]["n_controls"] == 2
    assert len(ctrls) == 2
    for c in ctrls:
        # outside event exclusion around entry ±2m / +5m
        assert not (base + 1800_000 - 120_000 <= c["entry_at"] <= base + 1800_000 + 300_000)


def test_ic_relaxed_vs_strict() -> None:
    impact = {
        "first5_trades_present": True,
        "last5_trades_present": True,
        "first5_impact_per_notional": 0.01,
        "last5_impact_per_notional": 0.008,  # ratio 0.8 → not strict at 0.75
        "first5_aggressive_notional": 10000.0,
        "last5_aggressive_notional": 8000.0,
    }
    assert classify_compression(impact, ratio_cut=0.75)["compression_class"] == "IC_RELAXED"
    impact2 = dict(impact)
    impact2["last5_impact_per_notional"] = 0.005
    impact2["last5_aggressive_notional"] = 7000.0
    assert classify_compression(impact2, ratio_cut=0.75)["compression_class"] == "IC_STRICT"

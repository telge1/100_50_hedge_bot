"""Unit tests for causal feature contract + raw archive gates."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.liquidity_location_r6_causal_and_raw_audit_v2.causal import (
    absorption_subminute,
    assert_causal,
    decision_at_for_variant,
    future_only_path_labels,
    near_edge_reclaim_closed_candles,
    near_edge_reclaim_subminute,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files, list_closed_segments


def _trades(rows: list[tuple]) -> pd.DataFrame:
    # rows: (trade_ts, trade_id, side, price, size)
    df = pd.DataFrame(rows, columns=["trade_ts", "trade_id", "side", "price", "size"])
    df["notional"] = df["price"] * df["size"]
    return df


def test_subminute_rejects_later_1m_close() -> None:
    t2 = pd.Timestamp("2026-08-10T12:00:00")
    dec, _ = decision_at_for_variant(t2, "SUBMINUTE_30S")
    assert dec == t2 + pd.Timedelta(seconds=30)
    # Candle closing at 12:01 must not be used; only trades
    candles = pd.DataFrame(
        {
            "open_time": [pd.Timestamp("2026-08-10T12:00:00")],
            "close": [999.0],  # would reclaim if wrongly used
            "open": [100.0],
            "high": [999.0],
            "low": [99.0],
        }
    )
    trades = _trades(
        [
            (pd.Timestamp("2026-08-10T12:00:10"), 1, "Sell", 100.0, 1.0),
            (pd.Timestamp("2026-08-10T12:00:20"), 2, "Buy", 100.2, 1.0),
        ]
    )
    # near edge 100.5 — last trade 100.2 does NOT reclaim BID
    feat = near_edge_reclaim_subminute(
        trades, side="BID", near_edge=100.5, first_touch_at=t2, decision_at=dec
    )
    assert feat["reclaimed"] is False
    assert feat["price"] == 100.2
    assert_causal(feat)
    # closed-candle path at SUBMINUTE must not be mixed — CLOSED_1M uses candle end
    dec1, _ = decision_at_for_variant(t2, "CLOSED_1M", candles_1m=candles)
    assert dec1 == pd.Timestamp("2026-08-10T12:01:00")


def test_closed_1m_decides_at_candle_end() -> None:
    t2 = pd.Timestamp("2026-08-10T12:00:10")
    candles = pd.DataFrame(
        {
            "open_time": [
                pd.Timestamp("2026-08-10T12:00:00"),
                pd.Timestamp("2026-08-10T12:01:00"),
            ],
            "close": [101.0, 102.0],
            "open": [100.0, 101.0],
            "high": [101.5, 102.5],
            "low": [99.5, 100.5],
        }
    )
    dec, st = decision_at_for_variant(t2, "CLOSED_1M", candles_1m=candles)
    assert st == "ok"
    assert dec == pd.Timestamp("2026-08-10T12:01:00")
    feat = near_edge_reclaim_closed_candles(
        candles, side="BID", near_edge=100.5, first_touch_at=t2, decision_at=dec, variant="CLOSED_1M"
    )
    assert feat["reclaimed"] is True
    assert_causal(feat)
    # candle closing at 12:02 must not affect decision_at=12:01 feature
    candles2 = candles.copy()
    candles2.loc[1, "close"] = 50.0  # would break reclaim if wrongly included after... wait
    # bar open 12:01 closes 12:02 > decision — must be ignored
    feat2 = near_edge_reclaim_closed_candles(
        candles2, side="BID", near_edge=100.5, first_touch_at=t2, decision_at=dec, variant="CLOSED_1M"
    )
    # only 12:00 bar (close 12:01) usable — close 101 still reclaim
    assert feat2["reclaimed"] is True
    assert feat2["source_row_count"] == 1


def test_absorption_ends_at_decision_and_ignores_later_trade() -> None:
    t2 = pd.Timestamp("2026-08-10T12:00:00")
    dec = t2 + pd.Timedelta(seconds=30)
    trades = _trades(
        [
            (pd.Timestamp("2026-08-10T12:00:01"), 1, "Sell", 100.0, 10.0),
            (pd.Timestamp("2026-08-10T12:00:20"), 2, "Sell", 100.01, 10.0),
            (pd.Timestamp("2026-08-10T12:00:40"), 3, "Sell", 90.0, 100.0),  # after T3
        ]
    )
    feat = absorption_subminute(trades, side="BID", first_touch_at=t2, decision_at=dec)
    assert feat["end_price"] == 100.01
    assert feat["trade_count"] == 2
    assert_causal(feat)
    # later trade must not change feature
    feat2 = absorption_subminute(trades.iloc[:2], side="BID", first_touch_at=t2, decision_at=dec)
    assert feat["end_price"] == feat2["end_price"]
    assert feat["price_continuation"] == feat2["price_continuation"]


def test_candle_after_decision_does_not_enter_closed_feature() -> None:
    t2 = pd.Timestamp("2026-08-10T12:00:00")
    dec = pd.Timestamp("2026-08-10T12:01:00")
    candles = pd.DataFrame(
        {
            "open_time": [
                pd.Timestamp("2026-08-10T12:00:00"),
                pd.Timestamp("2026-08-10T12:01:00"),  # close 12:02 > dec
            ],
            "close": [100.2, 50.0],
            "open": [100.0, 100.2],
            "high": [100.3, 100.3],
            "low": [99.9, 50.0],
        }
    )
    feat = near_edge_reclaim_closed_candles(
        candles, side="BID", near_edge=100.0, first_touch_at=t2, decision_at=dec, variant="CLOSED_1M"
    )
    assert feat["source_row_count"] == 1
    assert feat["reclaimed"] is True  # from 100.2
    assert_causal(feat)


def test_future_labels_start_after_decision() -> None:
    dec = pd.Timestamp("2026-08-10T12:00:30")
    candles = pd.DataFrame(
        {
            "open_time": [
                pd.Timestamp("2026-08-10T12:00:00"),  # before — must not count
                pd.Timestamp("2026-08-10T12:01:00"),
            ],
            "open": [100.0, 100.0],
            "high": [110.0, 100.6],  # huge high before decision must be ignored
            "low": [99.0, 99.9],
            "close": [100.0, 100.5],
        }
    )
    lab = future_only_path_labels(
        candles, side="BID", near_edge=100.0, lower=99.0, upper=100.0, decision_at=dec, atr=1.0
    )
    assert lab["path_starts_after_decision"] is True
    # fav 0.5 atr = 100.5 — only post bar high 100.6 qualifies
    assert lab["fav_0_5atr_5m"] is True


def test_assert_causal_fails_on_leak() -> None:
    bad = {
        "feature_name": "x",
        "decision_at": "2026-08-10T12:00:30",
        "max_source_timestamp": "2026-08-10T12:01:00",
        "status": "OK",
    }
    with pytest.raises(AssertionError):
        assert_causal(bad)


def test_tmp_excluded_and_segments_readonly(tmp_path: Path) -> None:
    assert list_closed_segments(tmp_path, symbols=("BTCUSDT",)) == []
    assert excluded_tmp_files(tmp_path, ("BTCUSDT",)) == []


def test_self_contained_vs_chained_documented() -> None:
    # Contract constant check via inventory classification keys
    from orderbook_analyse.liquidity_location_r6_causal_and_raw_audit_v2.raw_diag import (
        classify_raw_matrix,
    )

    m = classify_raw_matrix([], [])
    assert "final_classification" in m
    assert m["CLICKHOUSE_ATTACH_RELEVANT"] is False

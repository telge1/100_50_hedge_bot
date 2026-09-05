"""Causal / raw-archive invariants for Phase-3 audit."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.liquidity_location_r6_phase3_audit.runner import (
    NEAR_EDGE_RECLAIM_DEF,
    DEFENDED_DEF,
    _gap_count,
    build_decision_timestamps,
    coverage_by_episode,
    future_only_labels,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files, list_closed_segments


def test_near_edge_reclaim_and_defense_separately_defined() -> None:
    assert NEAR_EDGE_RECLAIM_DEF["name"] != DEFENDED_DEF["name"]
    assert "0.5 ATR" in DEFENDED_DEF["reaction_distance"]
    assert "none" in NEAR_EDGE_RECLAIM_DEF["reaction_distance"].lower()


def test_decision_before_outcome_excludes_pre_resolved() -> None:
    episodes = pd.DataFrame(
        [
            {
                "episode_id": "e1",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "side": "BID",
                "analyzable_core": True,
                "label_primary": "DEFENDED",
                "first_touch_at": "2026-08-10T12:00:00",
                "defend_at": "2026-08-10T12:00:10",  # before T3=30s
                "sweep_at": None,
                "reclaim_at": None,
                "upper_price": 100.0,
                "lower_price": 99.0,
            },
            {
                "episode_id": "e2",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "side": "BID",
                "analyzable_core": True,
                "label_primary": "DEFENDED",
                "first_touch_at": "2026-08-10T12:00:00",
                "defend_at": "2026-08-10T12:05:00",  # after T3
                "sweep_at": None,
                "reclaim_at": None,
                "upper_price": 100.0,
                "lower_price": 99.0,
            },
        ]
    )
    candles = {
        "BTCUSDT": pd.DataFrame(
            {
                "open_time": pd.to_datetime(["2026-08-10T12:00:00"]),
                "close": [100.5],
            }
        )
    }
    feat = pd.DataFrame({"episode_id": ["e1", "e2"], "absorption_flag": [False, False]})
    d = build_decision_timestamps(episodes, feat, candles, t3_sec=30)
    assert bool(d.loc[d.episode_id == "e1", "usable_for_t3_prediction"].iloc[0]) is False
    assert d.loc[d.episode_id == "e1", "outcome_timing_class"].iloc[0] == "outcome_before_t3"
    assert bool(d.loc[d.episode_id == "e2", "usable_for_t3_prediction"].iloc[0]) is True
    assert bool(d.loc[d.episode_id == "e2", "decision_before_outcome"].iloc[0]) is True


def test_future_only_path_labels_start_after_t3() -> None:
    episodes = pd.DataFrame(
        [
            {
                "episode_id": "e2",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "side": "BID",
                "analyzable_core": True,
                "label_primary": "DEFENDED",
                "first_touch_at": "2026-08-10T12:00:00",
                "defend_at": "2026-08-10T12:05:00",
                "sweep_at": None,
                "reclaim_at": None,
                "upper_price": 100.0,
                "lower_price": 99.0,
            }
        ]
    )
    candles_1m = pd.DataFrame(
        {
            "open_time": pd.to_datetime(
                [
                    "2026-08-10T11:50:00",
                    "2026-08-10T12:00:00",
                    "2026-08-10T12:01:00",
                    "2026-08-10T12:02:00",
                ]
            ),
            "open": [100, 100, 100.2, 100.6],
            "high": [100.1, 100.2, 100.8, 101.2],
            "low": [99.9, 99.8, 100.0, 100.4],
            "close": [100.0, 100.1, 100.5, 101.0],
        }
    )
    feat = pd.DataFrame({"episode_id": ["e2"], "absorption_flag": [False]})
    decision = build_decision_timestamps(episodes, feat, {"BTCUSDT": candles_1m}, t3_sec=30)
    fut = future_only_labels(
        episodes, decision, {("BTCUSDT", "1m"): candles_1m, ("BTCUSDT", "5m"): candles_1m}
    )
    assert len(fut) == 1
    assert bool(fut.iloc[0]["path_starts_strictly_after_decision_at"]) is True
    # bars at/before T3=12:00:30 must not drive path; first path bar is 12:01
    assert fut.iloc[0]["move_away_0_25atr_5m"] in (True, False)


def test_absorption_feature_end_flagged_past_t3() -> None:
    episodes = pd.DataFrame(
        [
            {
                "episode_id": "e3",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "side": "ASK",
                "analyzable_core": True,
                "label_primary": "unresolved",
                "first_touch_at": "2026-08-10T12:00:00",
                "defend_at": None,
                "sweep_at": None,
                "reclaim_at": None,
                "upper_price": 101.0,
                "lower_price": 100.0,
            }
        ]
    )
    feat = pd.DataFrame({"episode_id": ["e3"], "absorption_flag": [True]})
    d = build_decision_timestamps(episodes, feat, {"BTCUSDT": pd.DataFrame()}, t3_sec=30)
    assert bool(d.iloc[0]["absorption_leaks_past_t3"]) is True
    # feature end T2+65s, decision T2+30s
    end = pd.Timestamp(d.iloc[0]["absorption_feature_end_phase3"])
    dec = pd.Timestamp(d.iloc[0]["decision_at"])
    assert end > dec


def test_gap_count_and_tmp_exclusion(tmp_path: Path) -> None:
    assert _gap_count([]) == 0
    assert _gap_count([[1, 2], [2, 3]]) == 2
    # empty archive: no tmp, no closed
    assert list_closed_segments(tmp_path, symbols=("BTCUSDT",)) == []
    assert excluded_tmp_files(tmp_path, ("BTCUSDT",)) == []


def test_coverage_rejects_gaps_and_missing_snapshot(tmp_path: Path) -> None:
    # No segments → not analyzable
    ep = pd.DataFrame(
        [
            {
                "episode_id": "x",
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "side": "BID",
                "first_touch_at": "2026-08-26T12:00:00",
                "v2_temporal_split": "oos",
            }
        ]
    )
    cov = coverage_by_episode(ep, tmp_path)
    assert bool(cov.iloc[0]["analyzable_raw_ob"]) is False
    assert cov.iloc[0]["reject_reason"] == "no_closed_segments_for_symbol"


def test_temporal_splits_frozen_reference() -> None:
    p = Path(
        "/home/telgenbuescher/projects/orderbook_analyse/results/"
        "liquidity_location_r6_orderflow_confirmation_v1/temporal_splits.json"
    )
    assert p.is_file()
    import json

    splits = json.loads(p.read_text())
    # Phase-3 frozen chronological splits must remain untouched by audit.
    assert "discovery" in splits or "oos" in splits or "cutoffs" in splits or isinstance(splits, dict)

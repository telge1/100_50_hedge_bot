from __future__ import annotations

import copy
import json
from pathlib import Path

from config_validate_v1 import load_and_validate_config
from density_bridge_v1 import FixtureDensitySessionV1
from pool_identity_v1 import pool_still_active_for_pending
from short_research_engine_v1 import run_short_research_backtest

TOOLS = Path(__file__).resolve().parents[1]
CFG = TOOLS / "configs/synthetic_fixture_v1.json"

POOL_FIRST_FIXTURE = {
    "prices": {
        "2026-01-01T12:00:00Z": {"o": 0.09, "h": 0.0902, "l": 0.0898, "c": 0.09},
        "2026-01-01T12:05:00Z": {"o": 0.09, "h": 0.0901, "l": 0.0899, "c": 0.09005},
        "2026-01-01T12:10:00Z": {"o": 0.09005, "h": 0.1005, "l": 0.09, "c": 0.099},
    },
    "snapshots": {
        "2026-01-01T12:00:00Z": {
            "upper_bands": [
                {
                    "side": "upper",
                    "lower_price": "0.0995",
                    "upper_price": "0.1000",
                    "unique_origin_count": 2,
                    "origin_ids": "a|b",
                },
                {
                    "side": "upper",
                    "lower_price": "0.1010",
                    "upper_price": "0.1015",
                    "unique_origin_count": 2,
                    "origin_ids": "e|f",
                },
            ],
            "lower_bands": [
                {
                    "side": "lower",
                    "lower_price": "0.0950",
                    "upper_price": "0.0960",
                    "unique_origin_count": 2,
                    "origin_ids": "c|d",
                }
            ],
        },
        "2026-01-01T12:05:00Z": {
            "upper_bands": [
                {
                    "side": "upper",
                    "lower_price": "0.0995",
                    "upper_price": "0.1000",
                    "unique_origin_count": 2,
                    "origin_ids": "a|b",
                },
                {
                    "side": "upper",
                    "lower_price": "0.1010",
                    "upper_price": "0.1015",
                    "unique_origin_count": 2,
                    "origin_ids": "e|f",
                },
            ],
            "lower_bands": [
                {
                    "side": "lower",
                    "lower_price": "0.0950",
                    "upper_price": "0.0960",
                    "unique_origin_count": 2,
                    "origin_ids": "c|d",
                }
            ],
        },
        "2026-01-01T12:10:00Z": {
            "upper_bands": [
                {
                    "side": "upper",
                    "lower_price": "0.0995",
                    "upper_price": "0.1000",
                    "unique_origin_count": 2,
                    "origin_ids": "a|b",
                },
                {
                    "side": "upper",
                    "lower_price": "0.1010",
                    "upper_price": "0.1015",
                    "unique_origin_count": 2,
                    "origin_ids": "e|f",
                },
            ],
            "lower_bands": [
                {
                    "side": "lower",
                    "lower_price": "0.0950",
                    "upper_price": "0.0960",
                    "unique_origin_count": 2,
                    "origin_ids": "c|d",
                }
            ],
        },
    },
}


def _pool_first_cfg():
    cfg = load_and_validate_config(CFG)
    cfg = copy.deepcopy(cfg)
    cfg["short_entry"]["entry_pool_selection_mode"] = "pool_first_structural_v1"
    cfg["short_entry"]["pool_cancel_identity_mode"] = "origin_set_v1"
    cfg["short_entry"]["pending_limit_fill_model"] = "causal_sell_limit_v2"
    cfg["short_entry"]["entry_price_reference"] = "upper_pool_lower_edge"
    return cfg


def test_pool_first_signals_without_bearish_candle():
    session = FixtureDensitySessionV1(POOL_FIRST_FIXTURE["snapshots"])
    cfg = _pool_first_cfg()
    r = run_short_research_backtest(cfg, session, POOL_FIRST_FIXTURE["prices"])
    assert r["signals"]
    assert r["signals"][0]["decision_close_utc"] == "2026-01-01T12:00:00Z"
    assert r["signals"][0]["entry_level"] == 0.0995


def test_baseline_proximity_unchanged_on_same_fixture():
    session = FixtureDensitySessionV1(POOL_FIRST_FIXTURE["snapshots"])
    cfg = load_and_validate_config(CFG)
    r = run_short_research_backtest(cfg, session, POOL_FIRST_FIXTURE["prices"])
    assert len(r["signals"]) == 0


def test_fill_on_touch_after_activation():
    session = FixtureDensitySessionV1(POOL_FIRST_FIXTURE["snapshots"])
    cfg = _pool_first_cfg()
    r = run_short_research_backtest(cfg, session, POOL_FIRST_FIXTURE["prices"])
    filled = [o for o in r["orders"] if o.get("status") == "FILLED"]
    assert filled
    assert filled[0]["fill_utc"] == "2026-01-01T12:10:00Z"


def test_origin_set_active_after_band_split():
    snap = {
        "upper_bands": [
            {"lower_price": "0.0995", "upper_price": "0.0998", "origin_ids": "a|b|c"},
        ]
    }
    cfg = _pool_first_cfg()
    assert pool_still_active_for_pending(
        cfg, pool_lo=0.0995, pool_hi=0.1, origin_ids="a|b", snapshot=snap
    )
    assert not pool_still_active_for_pending(
        cfg, pool_lo=0.0995, pool_hi=0.1, origin_ids="a|b", snapshot={"upper_bands": []}
    )


def test_no_duplicate_signal_same_pool():
    prices = dict(POOL_FIRST_FIXTURE["prices"])
    prices["2026-01-01T12:15:00Z"] = {"o": 0.099, "h": 0.0992, "l": 0.098, "c": 0.0985}
    snaps = copy.deepcopy(POOL_FIRST_FIXTURE["snapshots"])
    snaps["2026-01-01T12:15:00Z"] = copy.deepcopy(snaps["2026-01-01T12:10:00Z"])
    session = FixtureDensitySessionV1(snaps)
    cfg = _pool_first_cfg()
    r = run_short_research_backtest(cfg, session, prices)
    assert len(r["signals"]) == 1

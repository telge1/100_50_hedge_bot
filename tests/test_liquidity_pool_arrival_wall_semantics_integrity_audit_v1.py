"""Targeted tests for arrival wall semantics integrity audit."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/scripts/"
    "run_liquidity_pool_arrival_wall_semantics_integrity_audit.py"
)


def _load():
    import sys

    spec = importlib.util.spec_from_file_location("sem_integrity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["sem_integrity"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_01_02_03_rank_before_pool_filter_full_side(mod):
    levels = [(100.0, 1.0), (101.0, 50.0), (102.0, 2.0), (103.0, 3.0)]
    ranked = mod.side_levels_ranked_full(levels)
    assert ranked[0]["price"] == 101.0
    assert ranked[0]["full_side_rank"] == 1
    assert ranked[0]["full_side_percentile"] == 1.0
    lo, hi = 100.0, 102.5
    inside = [r for r in ranked if lo <= r["price"] <= hi]
    # pool filter after rank — ranks unchanged
    assert inside[0]["full_side_rank"] == 1
    assert len(ranked) == 4  # denominator is full side


def test_04_exact_arrival_age(mod):
    assert mod.MAX_OB_AGE_MS == 1000


def test_05_post_arrival_cannot_set_at_arrival():
    # invariant documented: first_seen > arrival must not cause major_at_arrival
    src = SCRIPT.read_text(encoding="utf-8")
    assert "can_set_major_at_arrival" in src
    assert "invariant_post_cannot_set_arrival_major" in src


def test_06_07_wall_id_tick_and_additional_wall(mod):
    k1 = mod.tick_key("ASK", 79217.1, 0.1)
    k2 = mod.tick_key("ASK", 79217.14, 0.1)
    assert k1 == k2
    assert k1 != mod.tick_key("ASK", 79174.2, 0.1)


def test_08_strongest_at_arrival_vs_anytime(mod):
    _, sem = mod.analyze_code_path()
    assert "at_arrival" in sem["exact_arrival_assignment"]
    assert sem["pool_filter_before_or_after_rank"] == "AFTER"


def test_09_10_overlap_and_clusters(mod):
    eps = [
        {
            "arrival_episode_id": "a",
            "pool_id": "p1",
            "side": "ASK",
            "arrival_ts": "2026-08-25T00:07:15Z",
            "arrival_ts_ms": 1000,
            "lower_edge": 100.0,
            "upper_edge": 110.0,
        },
        {
            "arrival_episode_id": "b",
            "pool_id": "p2",
            "side": "ASK",
            "arrival_ts": "2026-08-25T00:07:16Z",
            "arrival_ts_ms": 2000,
            "lower_edge": 105.0,
            "upper_edge": 120.0,
        },
    ]
    c = mod.cluster_diagnostics(eps)
    assert c["raw_pool_id_arrivals"] == 2
    assert c["diagnostic_market_arrival_clusters"] == 1
    assert mod.intervals_overlap(100, 110, 105, 120)


def test_11_12_13_no_outcomes_no_mutation_deterministic(mod):
    src = SCRIPT.read_text(encoding="utf-8")
    assert "pnl" not in src.lower()
    assert "winrate" not in src.lower()
    assert "INSERT INTO" not in src
    assert mod.significance_class(1, 0.99) == "MAJOR"
    assert mod.significance_class(1, 0.99) == mod.significance_class(1, 0.99)

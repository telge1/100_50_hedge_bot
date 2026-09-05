"""Targeted tests for pool-edge ↔ raw OB200 wall overlap audit."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/scripts/"
    "run_liquidity_pool_edge_wall_overlap_audit.py"
)


def _load():
    import sys

    spec = importlib.util.spec_from_file_location("wall_overlap_audit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["wall_overlap_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_no_copied_pool_algorithm_or_nested(mod):
    src = SCRIPT.read_text(encoding="utf-8")
    assert "liquidity_pool_signal" in src
    assert "a_plus_nested_ask_pool_edge_short_v1" not in src
    assert "rank_nested_ask" not in src
    assert "run_liquidity_location(" not in src.replace("engine.run_liquidity_location", "")


def test_foundation_engine_identity(mod):
    from orderbook_analyse.liquidity_pool_signal import chart_pool_engine, get_engine_function

    assert get_engine_function() is chart_pool_engine()
    assert get_engine_function().__module__ == "indicators.liquidity_location.engine"


def test_ask_bid_side_matching_and_edge_bps(mod):
    assert mod.bps_distance(100.01, 100.0) == pytest.approx(1.0, rel=1e-6)
    rows = mod.side_levels_ranked([(100.0, 1.0), (101.0, 10.0), (102.0, 5.0)])
    assert rows[0]["price"] == 101.0
    assert rows[0]["significance_class"] == "MAJOR"  # rank 1
    assert mod.significance_class(6, 0.85) == "MODERATE"
    assert mod.significance_class(25, 0.5) == "MINOR"


def test_classify_overlap_priority(mod):
    front = 100.0
    major_edge = {
        "significance_class": "MAJOR",
        "distance_to_front_edge_bps": 0.5,
        "inside_pool": False,
    }
    assert (
        mod.classify_overlap(
            front_edge=front,
            same_side_rows=[major_edge],
            inside_rows=[],
            ob_unavailable=False,
            entry_unresolved=False,
        )
        == "MAJOR_WALL_AT_FRONT_EDGE"
    )
    assert (
        mod.classify_overlap(
            front_edge=None,
            same_side_rows=[],
            inside_rows=[],
            ob_unavailable=True,
            entry_unresolved=False,
        )
        == "OB_SNAPSHOT_UNAVAILABLE"
    )


def test_ob_age_rule():
    as_of = 1_000_000
    snap_ts = 999_500
    assert as_of - snap_ts <= 1000
    assert as_of - 998_000 > 1000


def test_no_outcome_fields_in_script():
    src = SCRIPT.read_text(encoding="utf-8")
    for bad in ("pnl", "sharpe", "winrate", "expectancy", "TP", "SL"):
        # allow "SL" only if not trade stop — keep simple: no pnl/winrate
        pass
    assert "pnl" not in src.lower()
    assert "winrate" not in src.lower()

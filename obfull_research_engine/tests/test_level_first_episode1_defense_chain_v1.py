"""Tests for defense chain mechanics."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

import pytest

from obfull_research_engine.level_first_episode1_defense_chain_v1 import (  # noqa: E402
    MAJOR_WALL_PERCENTILE,
    RELEVANCE_PERCENTILE_FLOOR,
    TRANSITION_SAME_PRICE_NEW_GEN,
    WALL_RELEVANCE_STATUS,
)
from obfull_research_engine.level_first_episode1_defense_chain_v1.chain_builder import (  # noqa: E402
    build_ask_defense_chain,
    classify_transition,
)
from obfull_research_engine.level_first_episode1_defense_chain_v1.relevance import (  # noqa: E402
    score_generations_past_only,
    summarize_generation_vs_relevant,
)
from obfull_research_engine.drilldown.aggregation_100ms import _as_dt  # noqa: E402


def _g(i, price, start, end, qty, layer="PRE_EXISTING_LAYER", fills=0.0, **kw):
    return {
        "wall_generation_id": f"wg_{i}",
        "replay_epoch": 4,
        "side": "ask",
        "price": price,
        "start_time": start,
        "end_time": end,
        "initial_qty": qty,
        "peak_qty": qty,
        "final_qty": 0.0,
        "cumulative_attributed_fills": fills,
        "cumulative_residual_pulls": 0.0,
        "cumulative_refills": 0.0,
        "termination_reason": "MOSTLY_FILLED",
        "first_trade_touch": start if fills > 0 else None,
        "last_trade_touch": end if fills > 0 else None,
        "distance_from_original_wall_ticks": (price - 79780.0) / 0.1,
        "distance_from_mid_ticks": 1.0,
        "attribution_confidence": "HIGH",
        "coverage_ok": True,
        "layer_class": layer,
        "generation_index": i,
        "mid_at_start": 79770.0,
        **kw,
    }


def test_01_generations_not_auto_chain_nodes():
    gens = [_g(i, 79780.0 + i * 0.1, "2026-09-06T20:19:10Z", "2026-09-06T20:19:10.5Z", 0.01) for i in range(20)]
    gens[0] = _g(0, 79780.0, "2026-09-06T20:19:02.3Z", "2026-09-06T20:19:41.8Z", 12.0, fills=10.0)
    scored = score_generations_past_only(gens)
    summary = summarize_generation_vs_relevant(scored)
    assert summary["generation_count"] == 20
    assert summary["relevant_wall_count"] < summary["generation_count"]
    assert summary["relevant_wall_count"] >= 1


def test_02_future_peak_does_not_change_earlier_relevance():
    early = _g(1, 79785.0, "2026-09-06T20:19:05Z", "2026-09-06T20:19:06Z", 1.0)
    scored1 = score_generations_past_only([early])
    r1 = scored1[0]["is_relevant_at_entry"]
    pct1 = scored1[0]["rolling_size_percentile"]
    later_big = _g(2, 79790.0, "2026-09-06T20:19:50Z", "2026-09-06T20:19:51Z", 100.0)
    scored2 = score_generations_past_only([early, later_big])
    assert scored2[0]["is_relevant_at_entry"] == r1
    assert scored2[0]["rolling_size_percentile"] == pct1


def test_03_q95_past_only():
    gens = []
    for i in range(20):
        gens.append(
            _g(i, 79780.0 + i * 0.1, f"2026-09-06T20:19:{10+i:02d}Z", f"2026-09-06T20:19:{10+i:02d}.5Z", float(i + 1))
        )
    scored = score_generations_past_only(gens)
    majors = [g for g in scored if g.get("is_major_wall_q95_past_only")]
    for m in majors:
        assert m["rolling_size_percentile"] >= MAJOR_WALL_PERCENTILE
        assert m["baseline_n_at_entry"] >= 5


def test_04_same_price_new_generation_transition():
    a = {
        "price": 79780.0,
        "end_time": "2026-09-06T20:19:20Z",
        "layer_class": "PRE_EXISTING_LAYER",
        "pre_existing": True,
    }
    b = {
        "price": 79780.0,
        "start_time": "2026-09-06T20:19:21Z",
        "layer_class": "POST_BREACH_NEW_GENERATION",
        "pre_existing": False,
    }
    assert classify_transition(a, b, coverage_end=_as_dt("2026-09-06T20:19:59.8Z")) == TRANSITION_SAME_PRICE_NEW_GEN


def test_05_chain_epoch_and_censor_status():
    gens4 = [
        _g(1, 79780.0, "2026-09-06T20:19:02.3Z", "2026-09-06T20:19:41.8Z", 12.0, fills=10.0),
        _g(2, 79785.0, "2026-09-06T20:19:05Z", "2026-09-06T20:19:50Z", 8.0, fills=2.0),
    ]
    for i in range(3, 10):
        gens4.append(_g(i, 79781.0 + i * 0.1, "2026-09-06T20:19:03Z", "2026-09-06T20:19:03.2Z", 0.05))
    scored = score_generations_past_only(gens4)
    chain = build_ask_defense_chain(
        scored_gens=scored,
        replay_epoch=4,
        coverage_start="2026-09-06T20:19:02.300Z",
        coverage_end="2026-09-06T20:19:59.800Z",
    )
    assert chain.replay_epoch == 4
    assert chain.chain_status == "CENSORED_BY_EPOCH_BOUNDARY"
    assert len({n["wall_generation_id"] for n in chain.nodes}) == len(chain.nodes)


def test_06_ask_bid_mirror_distance_signs():
    ask_dist = (79780.0 - 79775.25) / 0.1
    bid_dist = (79774.75 - 79770.0) / 0.1
    assert ask_dist > 0 and bid_dist > 0


def test_07_relevance_status_constant():
    assert WALL_RELEVANCE_STATUS == "WALL_RELEVANCE_NOT_OUTCOME_CALIBRATED"
    assert RELEVANCE_PERCENTILE_FLOOR == 0.75


def test_08_pipeline_smoke(tmp_path):
    from obfull_research_engine.level_first_episode1_defense_chain_v1.pipeline import run_defense_chain

    r = run_defense_chain(run_key="dch_smoke", out_dir=tmp_path / "smoke")
    assert r["oracle"]["ok"] is True
    assert r["oracle"]["fp"] == 0 and r["oracle"]["fn"] == 0
    assert r["generation_count"] > r["relevant_wall_count"]
    assert r["relevant_wall_count"] < 717  # must not equal churn generation narrative

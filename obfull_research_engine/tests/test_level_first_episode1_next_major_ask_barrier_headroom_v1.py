"""Tests for level_first_episode1_next_major_ask_barrier_headroom_v1."""

from __future__ import annotations

import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1 import (  # noqa: E402
    ENTRY_FEE_PCT,
    EXIT_FEE_PCT,
    REQUIRED_GROSS_HEADROOM_PCT,
    REQUIRED_NET_PROFIT_PCT,
    ROUNDTRIP_FEE_PCT,
)
from obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1.barriers import (  # noqa: E402
    select_barriers,
)
from obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1.clusters import (  # noqa: E402
    build_ask_barrier_clusters,
)
from obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1.costs import (  # noqa: E402
    assert_fee_contract,
    conservative_targets,
    headroom_bundle,
)
from obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1.entry import (  # noqa: E402
    executable_long_entry,
)
from obfull_research_engine.level_first_episode1_next_major_ask_barrier_headroom_v1.walls import (  # noqa: E402
    major_views,
    next_wall_at_percentile,
    wall_candidates_from_book,
)


def test_01_fees_and_gross_requirement():
    assert_fee_contract()
    assert abs((ENTRY_FEE_PCT + EXIT_FEE_PCT) - ROUNDTRIP_FEE_PCT) < 1e-12
    assert abs(ROUNDTRIP_FEE_PCT - 0.110) < 1e-12
    assert abs((REQUIRED_NET_PROFIT_PCT + ROUNDTRIP_FEE_PCT) - REQUIRED_GROSS_HEADROOM_PCT) < 1e-12
    assert abs(REQUIRED_GROSS_HEADROOM_PCT - 0.410) < 1e-12


def test_02_entry_is_ask_not_mid():
    e = executable_long_entry(
        best_bid=100.0,
        best_ask=100.2,
        decision_time="2026-09-06T20:19:41.900Z",
        replay_epoch=4,
    )
    assert e["theoretical_long_entry"] == 100.2
    assert e["midprice_at_decision"] == 100.1
    assert e["theoretical_long_entry"] != e["midprice_at_decision"]


def test_03_wall_below_entry_excluded():
    asks = {99.0: 50.0, 100.5: 10.0, 101.0: 80.0}
    scored = [
        {"price": 99.0, "initial_qty": 50.0, "first_visible_at": "2026-09-06T20:19:00Z", "start_time": "2026-09-06T20:19:00Z"},
        {"price": 101.0, "initial_qty": 80.0, "first_visible_at": "2026-09-06T20:19:00Z", "start_time": "2026-09-06T20:19:00Z"},
    ]
    c = wall_candidates_from_book(
        asks=asks,
        entry_price=100.2,
        decision_time="2026-09-06T20:19:41.900Z",
        replay_epoch=4,
        scored_walls=scored,
        max_input_available_at="2026-09-06T20:19:41.8Z",
        min_qty=1.0,
        scan_max_distance_pct=5.0,
    )
    assert all(x["price"] > 100.2 for x in c)


def test_04_nearest_not_skipped_by_far_giant():
    cands = [
        {
            "price": 100.5,
            "qty_base": 5.0,
            "notional_usdt": 502.5,
            "rolling_size_percentile": 0.96,
            "distance_pct": 0.3,
        },
        {
            "price": 101.5,
            "qty_base": 500.0,
            "notional_usdt": 50750.0,
            "rolling_size_percentile": 0.99,
            "distance_pct": 1.3,
        },
    ]
    for c in cands:
        c.setdefault("local_depth_share", 0.1)
        c.setdefault("first_visible_at", "2026-09-06T20:19:00Z")
        c["effective_size_percentile"] = c["rolling_size_percentile"]
        c["percentile_basis"] = "past_only_wall_generations"
    clusters = build_ask_barrier_clusters(cands, entry_price=100.2, decision_time="2026-09-06T20:19:41.900Z", merge_gap_ticks=1)
    sel = select_barriers(candidates=cands, clusters=clusters, default_major_q=0.95)
    near = sel["NEAREST_MAJOR_BARRIER"]["price"]
    assert near == 100.5
    strong = sel["STRONGEST_VISIBLE_BARRIER"]["wall"]["price"]
    assert strong == 101.5
    assert near < strong


def test_05_q_views_separate():
    cands = []
    for i, (px, q, pct) in enumerate(
        [(100.5, 10, 0.91), (100.8, 20, 0.96), (101.0, 30, 0.98), (101.2, 40, 0.995)]
    ):
        cands.append(
            {
                "price": px,
                "qty_base": q,
                "notional_usdt": px * q,
                "rolling_size_percentile": pct,
                "effective_size_percentile": pct,
                "percentile_basis": "past_only_wall_generations",
                "local_depth_share": 0.1,
                "first_visible_at": "2026-09-06T20:19:00Z",
            }
        )
    views = major_views(cands, entry=100.2)
    assert views["by_percentile"]["Q90"]["price"] == 100.5
    assert views["by_percentile"]["Q95"]["price"] == 100.8
    assert views["by_percentile"]["Q97"]["price"] == 101.0
    assert views["by_percentile"]["Q99"]["price"] == 101.2


def test_06_cluster_unique_prices_no_double_add():
    cands = [
        {"price": 100.5, "qty_base": 10.0, "notional_usdt": 1005.0, "rolling_size_percentile": 0.9, "local_depth_share": 0.2, "first_visible_at": None},
        {"price": 100.6, "qty_base": 20.0, "notional_usdt": 2012.0, "rolling_size_percentile": 0.95, "local_depth_share": 0.4, "first_visible_at": None},
        {"price": 102.0, "qty_base": 5.0, "notional_usdt": 510.0, "rolling_size_percentile": 0.8, "local_depth_share": 0.1, "first_visible_at": None},
    ]
    clusters = build_ask_barrier_clusters(cands, entry_price=100.2, decision_time="2026-09-06T20:19:41.900Z", merge_gap_ticks=5)
    prices = []
    for c in clusters:
        prices.extend(c["member_prices"])
    assert len(prices) == len(set(prices))
    # cluster qty is sum of members once
    c0 = clusters[0]
    assert abs(c0["total_cluster_qty"] - 30.0) < 1e-9


def test_07_slippage_separate_and_before_wall_less_headroom():
    hb = headroom_bundle(executable_entry=100.0, conservative_target=100.5, entry_slippage_pct=0.02, exit_slippage_pct=0.02)
    assert abs(hb["net_headroom_after_fees_pct"] - (hb["gross_headroom_pct"] - 0.110)) < 1e-9
    assert abs(
        hb["net_headroom_after_fees_and_slippage_pct"]
        - (hb["gross_headroom_pct"] - 0.110 - 0.02 - 0.02)
    ) < 1e-9
    assert "estimated_entry_slippage_pct" in hb
    tg = conservative_targets(wall_price=100.5, spread=0.1)
    at_wall = headroom_bundle(executable_entry=100.0, conservative_target=tg["target_at_wall"])
    before = headroom_bundle(executable_entry=100.0, conservative_target=tg["target_1_tick_before_wall"])
    assert before["gross_headroom_pct"] < at_wall["gross_headroom_pct"]


def test_08_future_size_does_not_change_past_percentile_selection():
    scored_past = [
        {"price": 100.5, "initial_qty": 10.0, "first_visible_at": "2026-09-06T20:19:00Z", "start_time": "2026-09-06T20:19:00Z"},
        {"price": 100.6, "initial_qty": 50.0, "first_visible_at": "2026-09-06T20:19:10Z", "start_time": "2026-09-06T20:19:10Z"},
    ]
    asks = {100.5: 10.0, 100.8: 12.0}
    c1 = wall_candidates_from_book(
        asks=asks,
        entry_price=100.2,
        decision_time="2026-09-06T20:19:41.900Z",
        replay_epoch=4,
        scored_walls=scored_past,
        max_input_available_at="2026-09-06T20:19:41Z",
        min_qty=1.0,
    )
    scored_future = scored_past + [
        {"price": 100.8, "initial_qty": 999.0, "first_visible_at": "2026-09-06T20:19:50Z", "start_time": "2026-09-06T20:19:50Z"},
    ]
    c2 = wall_candidates_from_book(
        asks=asks,
        entry_price=100.2,
        decision_time="2026-09-06T20:19:41.900Z",
        replay_epoch=4,
        scored_walls=scored_future,
        max_input_available_at="2026-09-06T20:19:41Z",
        min_qty=1.0,
    )
    # future wall must not enter past baseline at decision
    p1 = {x["price"]: x["rolling_size_percentile"] for x in c1}
    p2 = {x["price"]: x["rolling_size_percentile"] for x in c2}
    assert p1 == p2


def test_09_ask_bid_mirror_distance():
    # ask barrier above entry → positive distance; bid mirror would flip sign
    entry = 100.0
    ask_barrier = 100.5
    bid_barrier = 99.5
    ask_dist = (ask_barrier - entry) / 0.1
    bid_dist = (entry - bid_barrier) / 0.1
    assert ask_dist == bid_dist == 5.0


def test_10_next_wall_percentile_helper():
    cands = [
        {"price": 101.0, "rolling_size_percentile": 0.99, "effective_size_percentile": 0.99},
        {"price": 100.5, "rolling_size_percentile": 0.96, "effective_size_percentile": 0.96},
    ]
    assert next_wall_at_percentile(cands, percentile=0.95)["price"] == 100.5

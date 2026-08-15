"""Unit tests for post-add distance scale_down / skip policy."""

from __future__ import annotations

import pytest

from research.backtests.cobertura_0_notional_strategie.start_distance import (
    projected_post_add_distance_pct,
    projected_total_short_avg_after_add,
    resolve_post_add_qty,
)


def test_projection_and_scale_down_respects_min_distance():
    cur_q, cur_a = 296.365, 1.8171506068270433
    fill = 1.619
    mark = 1.619
    configured = 118.546
    full_avg = projected_total_short_avg_after_add(
        current_total_short_qty=cur_q,
        current_total_short_avg=cur_a,
        candidate_add_qty=configured,
        candidate_fill_price=fill,
    )
    full_dist = projected_post_add_distance_pct(
        projected_total_short_avg=full_avg, current_price=mark
    )
    decision = resolve_post_add_qty(
        configured_candidate_add_qty=configured,
        current_total_short_qty=cur_q,
        current_total_short_avg=cur_a,
        current_overlay_qty=0.0,
        core_qty=296.365,
        candidate_fill_price=fill,
        current_price=mark,
        minimum_post_add_distance_pct=0.05,
        post_add_distance_policy="scale_down",
        max_overlay_qty_multiple=4.0,
        qty_step=0.001,
        min_notional=5.0,
    )
    if full_dist >= 0.05:
        assert decision["action"] == "fill"
        assert decision["actual_add_qty"] == pytest.approx(configured)
    else:
        assert decision["action"] in ("scale_down", "skip")
        if decision["action"] == "scale_down":
            assert decision["actual_add_qty"] < configured
            assert (
                float(decision["projected_post_add_distance_pct"]) + 1e-12 >= 0.05
            )


def test_skip_policy_does_not_partial_fill():
    decision = resolve_post_add_qty(
        configured_candidate_add_qty=118.546,
        current_total_short_qty=296.365,
        current_total_short_avg=1.81715,
        current_overlay_qty=0.0,
        core_qty=296.365,
        candidate_fill_price=1.75,  # above avg → collapses distance
        current_price=1.75,
        minimum_post_add_distance_pct=0.05,
        post_add_distance_policy="skip",
        max_overlay_qty_multiple=4.0,
        qty_step=0.001,
        min_notional=5.0,
    )
    assert decision["action"] == "skip"
    assert decision["actual_add_qty"] == 0.0


def test_disabled_returns_configured_qty():
    decision = resolve_post_add_qty(
        configured_candidate_add_qty=118.546,
        current_total_short_qty=296.365,
        current_total_short_avg=1.81715,
        current_overlay_qty=0.0,
        core_qty=296.365,
        candidate_fill_price=1.619,
        current_price=1.619,
        minimum_post_add_distance_pct=0.05,
        post_add_distance_policy="disabled",
        max_overlay_qty_multiple=4.0,
        qty_step=0.001,
        min_notional=5.0,
    )
    assert decision["action"] == "fill"
    assert decision["actual_add_qty"] == pytest.approx(118.546)


def test_scaled_qty_floored_to_step():
    decision = resolve_post_add_qty(
        configured_candidate_add_qty=100.0,
        current_total_short_qty=100.0,
        current_total_short_avg=1.10,
        current_overlay_qty=0.0,
        core_qty=100.0,
        candidate_fill_price=0.90,
        current_price=1.0,
        minimum_post_add_distance_pct=0.05,
        post_add_distance_policy="scale_down",
        max_overlay_qty_multiple=4.0,
        qty_step=0.001,
        min_notional=5.0,
    )
    qty = float(decision["actual_add_qty"])
    assert abs(qty / 0.001 - round(qty / 0.001)) < 1e-9 or qty == int(qty / 0.001) * 0.001
    # Conservatively floored: qty * 1000 is integer-ish
    assert abs(qty - (int(qty / 0.001) * 0.001)) < 1e-12

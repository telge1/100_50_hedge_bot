"""Unit tests for start-distance projection helpers."""

from __future__ import annotations

import pytest

from research.backtests.cobertura_0_notional_strategie.start_distance import (
    floor_qty_to_step,
    max_allowed_add_qty,
    minimum_allowed_short_avg,
    projected_short_avg_after_neutralization,
    projected_start_distance_pct,
    select_first_causal_start,
)
from research.backtests.cobertura_0_notional_strategie.engine import _parse_ts

APT = dict(
    existing_short_qty=197.59699999999998,
    existing_short_avg=1.864561269615919,
    neutralization_qty=98.76800000000003,
)


def test_apt_projected_avg_at_signal_open():
    avg = projected_short_avg_after_neutralization(
        **APT, neutralization_fill_price=1.7223
    )
    assert avg == pytest.approx(1.8171506068270433)
    dist = projected_start_distance_pct(projected_short_avg=avg, current_price=1.7223)
    assert dist == pytest.approx((1.8171506068270433 - 1.7223) / 1.8171506068270433)
    assert dist == pytest.approx(0.0522, abs=5e-4)


def test_first_causal_start_no_lookahead():
    candles = [
        {"timestamp": "2026-01-19T00:00:00+00:00", "open": 1.7223},
        {"timestamp": "2026-01-19T00:05:00+00:00", "open": 1.70},
        {"timestamp": "2026-01-19T00:10:00+00:00", "open": 1.65},
        {"timestamp": "2026-01-19T00:15:00+00:00", "open": 1.60},
        {"timestamp": "2026-01-19T03:55:00+00:00", "open": 1.6469},
    ]
    # 5% already passes at 00:00; force a higher threshold needing a later candle.
    out = select_first_causal_start(
        candles,
        signal_ts="2026-01-19T00:00:00+00:00",
        minimum_start_distance_pct=0.09,
        parse_ts=_parse_ts,
        **APT,
    )
    assert out["selected"]["timestamp"].startswith("2026-01-19T00:15")
    assert out["selected"]["meets_threshold"] is True
    assert out["selected"]["price"] == pytest.approx(1.60)
    for row in out["scan"][:-1]:
        assert row["meets_threshold"] is False


def test_uses_long_not_allowed_semantics():
    # Distance uses projected short avg only (not long avg).
    avg = projected_short_avg_after_neutralization(
        **APT, neutralization_fill_price=1.65
    )
    dist = projected_start_distance_pct(projected_short_avg=avg, current_price=1.65)
    assert dist > 0.05


def test_floor_qty_conservative():
    assert floor_qty_to_step(1.2345, 0.001) == pytest.approx(1.234)
    assert floor_qty_to_step(0.0004, 0.001) == 0.0


def test_max_allowed_add_qty_formula():
    min_avg = minimum_allowed_short_avg(current_price=1.0, minimum_post_add_distance_pct=0.05)
    assert min_avg == pytest.approx(1.0 / 0.95)
    q = max_allowed_add_qty(
        current_total_short_qty=100.0,
        current_total_short_avg=1.10,
        candidate_fill_price=0.90,
        minimum_post_add_distance_pct=0.05,
        current_price=1.0,
    )
    assert q is not None and q > 0

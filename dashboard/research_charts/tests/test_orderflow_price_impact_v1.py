"""OPI rolling-window tests (P3S: 5m window)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from research_charts.orderflow_price_impact_v1 import (
    WINDOW_MINUTES_ALLOWED,
    _aggregate_window,
    compute_opi_metrics,
    load_orderflow_price_impact,
)


def test_window_minutes_allowed_includes_5():
    assert 5 in WINDOW_MINUTES_ALLOWED
    assert WINDOW_MINUTES_ALLOWED == (5, 15, 30, 60)


def test_invalid_window_minutes_rejects_7():
    from datetime import datetime, timezone

    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="invalid_window_minutes"):
        load_orderflow_price_impact(
            "SOLUSDT", "5m", start, end, window_minutes=7,
        )


def test_aggregate_window_five_closed_1m_bars():
    """Five measured 1m bars in a 5m wall-clock window."""
    t0 = 1_700_000_000
    flow = {}
    for i in range(5):
        ts = t0 + i * 60
        flow[ts] = {
            "status": "MEASURED",
            "buy_usdt": "1000",
            "sell_usdt": "500",
        }
    as_of = t0 + 5 * 60
    buy, sell, total, delta, quality = _aggregate_window(flow, t0, t0 + 5 * 60, as_of)
    assert quality == "ok"
    assert buy == Decimal("5000")
    assert sell == Decimal("2500")
    assert total == Decimal("7500")
    assert delta == Decimal("2500")


def test_aggregate_window_skips_forming_1m_without_gap():
    t0 = 1_700_000_000
    flow = {}
    for i in range(4):
        ts = t0 + i * 60
        flow[ts] = {"status": "MEASURED", "buy_usdt": "100", "sell_usdt": "100"}
    as_of = t0 + 4 * 60 + 30  # inside 5th minute (forming)
    buy, sell, total, delta, quality = _aggregate_window(flow, t0, t0 + 5 * 60, as_of)
    assert quality == "ok"
    assert total == Decimal("800")


def test_aggregate_window_incomplete_when_final_1m_missing():
    t0 = 1_700_000_000
    flow = {
        t0: {"status": "MEASURED", "buy_usdt": "100", "sell_usdt": "100"},
        t0 + 60: {"status": "MEASURED", "buy_usdt": "100", "sell_usdt": "100"},
    }
    as_of = t0 + 5 * 60
    buy, sell, total, delta, quality = _aggregate_window(flow, t0, t0 + 5 * 60, as_of)
    assert quality == "incomplete"
    assert buy is None


def test_compute_opi_metrics_unchanged_formula():
    price = Decimal("2")
    metrics = compute_opi_metrics(
        price_change_pct=price,
        buy_usdt=Decimal("3000"),
        sell_usdt=Decimal("2000"),
        delta_usdt=Decimal("1000"),
        total_usdt=Decimal("5000"),
    )
    assert metrics["quality_total"] == "low_volume"
    assert metrics["opi_total"] is None
    metrics2 = compute_opi_metrics(
        price_change_pct=price,
        buy_usdt=Decimal("12000"),
        sell_usdt=Decimal("8000"),
        delta_usdt=Decimal("4000"),
        total_usdt=Decimal("20000"),
    )
    assert metrics2["quality_total"] == "ok"
    assert metrics2["opi_total"] == "100.0000"
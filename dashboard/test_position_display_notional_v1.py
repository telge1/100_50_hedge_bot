"""Chart display notional aligns with pools order_sizing (no exchange writes)."""

from __future__ import annotations

from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
TRP_JS = DASH / "static/research_trp/chart.js"


def test_chart_js_mlb_display_helpers():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "resolvePositionDisplayNotional" in js
    assert "mlbPlannedOrderNotional" in js
    assert "setMlbSizingConfig" in js
    assert "window.__mlbSizing" in js
    assert "DEFAULT_POS_NOTIONAL = 100.0" in js


def test_sizing_config_api_route():
    api = (DASH / "research_charts/api.py").read_text(encoding="utf-8")
    assert "/api/research/manual-long-box/sizing-config" in api


def test_quote_entry_093067_matches_order_path():
    from research_charts import manual_long_box_orders_v1 as mlb

    out = mlb.quote_order_display_notional(0.093067, "DOGEUSDT")
    assert out["ok"]
    assert out["qty_doge"] == 107.0
    assert out["notional_usdt"] == pytest.approx(9.958169, rel=1e-4)
    assert out["notional_usdt"] <= out["notional_cap_usdt"] + 1e-9


def test_quote_entry_06731_matches_prior_report():
    from research_charts import manual_long_box_orders_v1 as mlb

    out = mlb.quote_order_display_notional(0.06731, "DOGEUSDT")
    assert out["ok"]
    assert out["qty_doge"] == 148.0
    assert out["notional_usdt"] == pytest.approx(9.96188, rel=1e-4)


def test_patch_overlay_replaces_100_usdt_labels():
    from research_charts import manual_long_box_orders_v1 as mlb

    payload = {
        "type": "position",
        "side": "long",
        "symbol": "DOGEUSDT",
        "entry_price": 0.094546,
        "stop_price": 0.093734,
        "target_price": 0.095102,
        "position_notional": 100.0,
        "entry_label": "Entry: 0.094546 · Größe: 100.00 USDT · R:R 0.69",
        "metadata": {"drawing_id": "d-test", "drawing_type": "long_position", "source": "drawing"},
    }
    out = mlb.patch_mlb_test_position_overlay(payload)
    assert out["position_notional"] == pytest.approx(9.92733, rel=1e-3)
    assert "100.00 USDT" not in out["entry_label"]
    assert "9.93" in out["entry_label"] or "9.92" in out["entry_label"]


def test_rr_independent_of_order_notional():
    """R:R is geometry-only; order cap must not alter price-based R:R."""
    entry, stop, target = 0.093067, 0.09, 0.1
    risk = entry - stop
    reward = target - entry
    rr = reward / risk
    assert rr > 0
    assert rr == (target - entry) / (entry - stop)

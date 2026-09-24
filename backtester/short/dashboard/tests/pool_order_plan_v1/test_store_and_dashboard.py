from __future__ import annotations

from pathlib import Path

from pool_order_plan_v1.config import enable_pool_order_plan_v1


def test_flag_default_true():
    assert enable_pool_order_plan_v1({}) is True
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "true"}) is True
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "false"}) is False
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "0"}) is False
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "no"}) is False
    assert enable_pool_order_plan_v1({"ENABLE_POOL_ORDER_PLAN_V1": "off"}) is False


def test_html_keeps_default_and_hides_pool_without_flag():
    html = Path(__file__).resolve().parents[2] / "templates" / "stoch_signale.html"
    text = html.read_text(encoding="utf-8")
    assert 'value="wave_fade_no_be50_v1" selected' in text
    assert 'value="wave_fade_frozen_f16ae32"' in text
    assert 'value="POOL_ORDER_PLAN_V1"' in text
    assert "Pool Order Plan V1 · Research" in text
    js = (Path(__file__).resolve().parents[2] / "static" / "js" / "stoch_signale.js").read_text(
        encoding="utf-8"
    )
    assert 'value || "wave_fade_no_be50_v1"' in js


def test_research_feed_loads_without_pandas():
    from pool_order_plan_v1.research_feed import research_signals_response

    payload = research_signals_response()
    assert payload["success"] is True
    assert payload.get("collector_called") is False
    if payload.get("feed_ready"):
        assert payload["strategy_version"] == "POOL_ORDER_PLAN_V1"
        assert len(payload.get("signals") or []) >= 1
    else:
        assert payload.get("error") == "pool_v1_artifact_unavailable"
    html = Path(__file__).resolve().parents[2] / "templates" / "stoch_signale.html"
    text = html.read_text(encoding="utf-8")
    assert 'value="wave_fade_no_be50_v1" selected' in text
    assert 'value="wave_fade_frozen_f16ae32"' in text
    assert 'value="POOL_ORDER_PLAN_V1"' in text
    assert "Pool Order Plan V1 · Research" in text
    js = (Path(__file__).resolve().parents[2] / "static" / "js" / "stoch_signale.js").read_text(
        encoding="utf-8"
    )
    assert 'value || "wave_fade_no_be50_v1"' in js

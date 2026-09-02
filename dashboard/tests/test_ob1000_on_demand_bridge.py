"""Tests for OB1000 on-demand dashboard Unix socket bridge."""

from __future__ import annotations

import pytest

from research_charts import ob1000_on_demand as mod


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("OB_V3_ON_DEMAND_ENABLE", raising=False)
    with pytest.raises(mod.Ob1000DisabledError):
        mod._call_collector({"operation": "status", "depth": 1000})


def test_collector_unavailable_when_socket_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OB_V3_ON_DEMAND_ENABLE", "true")
    missing = tmp_path / "missing.sock"
    monkeypatch.setattr(mod, "socket_path", lambda: missing)
    with pytest.raises(mod.Ob1000CollectorUnavailableError):
        mod.load_ob1000_levels("BTCUSDT")


def test_lease_acquire_via_mock_collector(monkeypatch):
    monkeypatch.setenv("OB_V3_ON_DEMAND_ENABLE", "true")

    def fake_call(request):
        assert request["operation"] == "acquire"
        return {
            "ok": True,
            "symbol": "BTCUSDT",
            "depth": 1000,
            "subscription_state": "starting",
            "expires_at": "2026-09-02T12:00:00.000Z",
        }

    monkeypatch.setattr(mod, "_call_collector", fake_call)
    payload = mod.lease_acquire(symbol="BTCUSDT", session_id="tab-1", lease_id="tab-1")
    assert payload["depth"] == 1000
    assert payload["subscription_state"] == "starting"


def test_snapshot_via_mock_collector(monkeypatch):
    monkeypatch.setenv("OB_V3_ON_DEMAND_ENABLE", "true")

    def fake_call(request):
        assert request["operation"] == "snapshot"
        return {
            "ok": True,
            "symbol": "BTCUSDT",
            "depth": 1000,
            "subscription_state": "live",
            "timestamp_utc": "2026-09-02T12:00:00.000Z",
            "source": mod.SOURCE_NAME,
            "coverage": "on_demand",
            "freshness_state": "fresh",
            "bids": [{"price": 99.0, "size": 1.0, "side": "bid"}],
            "asks": [{"price": 101.0, "size": 1.0, "side": "ask"}],
        }

    monkeypatch.setattr(mod, "_call_collector", fake_call)
    levels = mod.load_ob1000_levels("BTCUSDT", lease_id="tab-1")
    assert levels["depth"] == 1000
    assert levels["source"] == mod.SOURCE_NAME
    assert levels["bids"]


def test_freshness_from_payload():
    payload = mod.freshness_from_payload({"timestamp_utc": "2026-09-02T12:00:00.000Z"})
    assert payload["freshness_state"] in {"fresh", "delayed", "stale", "unknown"}


def test_ob1000_depth_selector_and_poll_constants():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "templates/research_charts.html").read_text(encoding="utf-8")
    js = (root / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert 'id="researchOblDepth"' in html
    assert ">OB200<" in html and ">OB1000<" in html
    assert "OBL1000_REFRESH_MS = 1 * 1000" in js
    assert "OBL1000_HEARTBEAT_MS = 15 * 1000" in js
    assert "oblInflight" in js
    assert "document.hidden" in js
    assert "beforeunload" in js
    assert "ensureOb1000Lease" in js
    assert "stopOb1000Lease" in js


def test_ob1000_no_direct_bybit_in_frontend():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    chart = (root / "static/research_trp/chart.js").read_text(encoding="utf-8")
    assert "wss://" not in js.lower()
    assert "api.bybit" not in js.lower()
    assert "stream.bybit" not in js.lower()
    assert "api.bybit" not in chart.lower()
    assert "stream.bybit" not in chart.lower()
    assert "/api/research/ob1000" in js

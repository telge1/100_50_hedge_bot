"""Aggregated Orderbook Profile: model, API contracts, UI wiring."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_charts.orderbook_profile import (  # noqa: E402
    FEATURES_FQN,
    MAX_BARS_PER_SIDE,
    _bar_dict,
    _rows_to_bars,
    _snapshot_to_bars,
    clear_orderbook_profile_cache_for_tests,
    empty_profile,
    load_orderbook_profile,
)
from research_charts.workspace_session import (  # noqa: E402
    DEFAULT_ORDERBOOK_PROFILE,
    normalize_orderbook_profile,
)


def test_features_fqn_is_aggregated_table_not_raw():
    assert FEATURES_FQN == "orderbook_analysis.orderbook_features_1s_v2"
    assert "orderbook_deltas" not in FEATURES_FQN
    assert "ob200" not in FEATURES_FQN.lower()


def test_bar_dict_rejects_null_and_zero_price():
    ts = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    assert _bar_dict(
        side="BID",
        price=None,  # type: ignore[arg-type]
        value=Decimal("10"),
        qty=Decimal("1"),
        reference_price=Decimal("100"),
        distance_bps=Decimal("5"),
        timestamp=ts,
        carried_forward=False,
        samples=1,
        quality_flags="",
    ) is None
    assert _bar_dict(
        side="ASK",
        price=Decimal("0"),
        value=Decimal("10"),
        qty=Decimal("1"),
        reference_price=Decimal("100"),
        distance_bps=None,
        timestamp=ts,
        carried_forward=False,
        samples=1,
        quality_flags="",
    ) is None


def test_bar_dict_bid_ask_and_distance():
    ts = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    bar = _bar_dict(
        side="BID",
        price=Decimal("99.5"),
        value=Decimal("1500.25"),
        qty=Decimal("15.0"),
        reference_price=Decimal("100"),
        distance_bps=Decimal("50"),
        timestamp=ts,
        carried_forward=True,
        samples=3,
        quality_flags="carried_forward",
    )
    assert bar is not None
    assert bar["side"] == "BID"
    assert bar["price"] == 99.5
    assert bar["value"] == 1500.25
    assert bar["value_type"] == "notional_quote"
    assert bar["qty"] == 15.0
    assert bar["carried_forward"] is True
    assert bar["distance_bps"] == 50.0
    assert abs(bar["distance_abs"] - 0.5) < 1e-9


def test_snapshot_maps_bid_and_ask():
    ts = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    row = (
        ts,
        Decimal("1.5"),
        Decimal("1.48"),
        Decimal("100"),
        Decimal("148"),
        Decimal("133.3"),
        Decimal("1.52"),
        Decimal("200"),
        Decimal("304"),
        Decimal("133.3"),
        "carried_forward",
        1,
    )
    bars = _snapshot_to_bars("XRPUSDT", row)
    assert len(bars) == 2
    assert bars[0]["side"] == "BID" and bars[0]["price"] == 1.48
    assert bars[1]["side"] == "ASK" and bars[1]["price"] == 1.52
    assert all(b["carried_forward"] for b in bars)
    assert all(b["symbol"] == "XRPUSDT" for b in bars)


def test_rows_to_bars_skips_invalid():
    ts = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    rows = [
        (Decimal("0"), Decimal("10"), Decimal("1"), Decimal("100"), Decimal("1"), ts, 1, 0, ""),
        (Decimal("99"), Decimal("20"), Decimal("2"), Decimal("100"), Decimal("100"), ts, 2, 1, "carried_forward"),
    ]
    bars = _rows_to_bars("BTCUSDT", rows, side="BID")
    assert len(bars) == 1
    assert bars[0]["price"] == 99.0
    assert bars[0]["carried_forward"] is True


def test_empty_profile_shape():
    start = datetime(2026, 8, 25, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    p = empty_profile(symbol="XRPUSDT", start=start, end=end, mode="visible_range")
    assert p["profile_kind"] == "aggregated_orderbook_profile"
    assert p["label"] == "Aggregated Orderbook Profile"
    assert p["bars"] == []
    assert "Not a full L2" in " ".join(p["notes"])


def test_normalize_orderbook_profile_defaults():
    assert normalize_orderbook_profile(None) == DEFAULT_ORDERBOOK_PROFILE
    assert normalize_orderbook_profile({"enabled": True, "bogus": 1})["enabled"] is True
    assert normalize_orderbook_profile({"enabled": True})["enabled"] is True
    assert DEFAULT_ORDERBOOK_PROFILE["enabled"] is False


def test_load_rejects_bad_range_and_unknown(monkeypatch):
    clear_orderbook_profile_cache_for_tests()
    start = datetime(2026, 8, 25, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    with pytest.raises(ValueError, match="invalid_time_range"):
        load_orderbook_profile(symbol="XRPUSDT", start=end, end=start)
    with pytest.raises(ValueError, match="time_range_too_large"):
        load_orderbook_profile(
            symbol="XRPUSDT",
            start=start,
            end=start + timedelta(days=8),
        )
    with pytest.raises(KeyError):
        load_orderbook_profile(
            symbol="NOSUCH",
            start=start,
            end=end,
            known_symbol=False,
        )


def test_api_validation_and_empty(monkeypatch):
    import asyncio

    import httpx
    from fastapi import FastAPI

    from research_charts.api import build_router
    from research_charts.workspace_session import reset_workspace_for_tests

    reset_workspace_for_tests()
    monkeypatch.setattr("research_charts.api.known_symbols", lambda: {"XRPUSDT"})

    def _fake_load(**kwargs):
        if not kwargs.get("known_symbol", True):
            raise KeyError(kwargs.get("symbol"))
        start = kwargs["start"]
        end = kwargs["end"]
        if end <= start:
            raise ValueError("invalid_time_range")
        span = (end - start).total_seconds()
        if span > 7 * 24 * 3600:
            raise ValueError("time_range_too_large")
        return empty_profile(
            symbol=kwargs["symbol"],
            start=start,
            end=end,
            mode="snapshot_at" if kwargs.get("at") else "visible_range",
            warning="no_wall_data",
        )

    monkeypatch.setattr("research_charts.api.load_orderbook_profile", _fake_load)

    app = FastAPI()
    app.include_router(
        build_router(require_auth=lambda: {"username": "t"}, render_template=lambda *a, **k: "")
    )

    class _Client:
        def get(self, path, params=None):
            async def _go():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.get(path, params=params)

            return asyncio.run(_go())

    client = _Client()
    bad = client.get("/api/research/orderbook-profile", params={"symbol": "XRPUSDT", "start": 100, "end": 50})
    assert bad.status_code == 400, bad.text

    unknown = client.get(
        "/api/research/orderbook-profile",
        params={"symbol": "NOSYM", "start": 1_700_000_000, "end": 1_700_003_600},
    )
    assert unknown.status_code == 404, unknown.text

    ok = client.get(
        "/api/research/orderbook-profile",
        params={"symbol": "XRPUSDT", "start": 1_700_000_000, "end": 1_700_003_600},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["profile_kind"] == "aggregated_orderbook_profile"
    assert body["bars"] == []

    causal = client.get(
        "/api/research/orderbook-profile",
        params={
            "symbol": "XRPUSDT",
            "start": 1_700_000_000,
            "end": 1_700_003_600,
            "at": 1_700_001_800,
        },
    )
    assert causal.status_code == 200, causal.text
    assert causal.json()["mode"] == "snapshot_at"


def test_host_ui_contracts():
    host = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    chart = (ROOT / "static" / "research_trp" / "chart.js").read_text(encoding="utf-8")
    pane = (ROOT / "static" / "research_trp" / "pane.html").read_text(encoding="utf-8")
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "researchObpEnabled" in html
    assert "Orderbook Profile" in html
    assert "/api/research/orderbook-profile" in host
    assert "scheduleOrderbookProfile" in host
    assert "clearPaneOrderbookProfile" in host
    assert "setOrderbookProfile" in chart
    assert "clearOrderbookProfile" in chart
    assert "drawOrderbookProfile" in chart
    assert "obp-overlay" in pane
    assert "Aggregated Orderbook Profile" in chart
    assert 'enabled: false, width: "normal", mode: "visible_range"' in host
    assert "researchVpEnabled" in html and "researchObpEnabled" in html
    assert "/api/research/orderbook-profile" in app_py
    obp_mod = (ROOT / "research_charts" / "orderbook_profile.py").read_text(encoding="utf-8")
    assert "never reads Raw OB200" in obp_mod or "Never reads Raw OB200" in obp_mod
    assert "orderbook_deltas" in obp_mod  # mentioned as excluded


def test_causal_snapshot_query_uses_at(monkeypatch):
    """Ensure snapshot path is selected when at is set (no CH needed)."""
    calls = {}

    class _FakeClient:
        def query(self, sql, parameters=None, settings=None):
            calls["sql"] = sql
            calls["params"] = parameters

            class R:
                result_rows = []

            return R()

    monkeypatch.setattr(
        "research_charts.orderbook_profile._client", lambda: _FakeClient()
    )
    clear_orderbook_profile_cache_for_tests()
    start = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    at = start + timedelta(minutes=30)
    payload = load_orderbook_profile(
        symbol="XRPUSDT", start=start, end=end, at=at, known_symbol=True
    )
    assert payload["mode"] == "snapshot_at"
    assert "ORDER BY bucket_start DESC" in calls["sql"]
    assert "LIMIT 1" in calls["sql"]
    assert calls["params"]["at"] <= end


def test_visible_range_groups_by_price(monkeypatch):
    class _FakeClient:
        def query(self, sql, parameters=None, settings=None):
            class R:
                result_rows = []

            if "bid_wall_price" in sql and "GROUP BY" in sql:
                ts = datetime(2026, 8, 25, 12, 30, tzinfo=timezone.utc)
                R.result_rows = [
                    (
                        Decimal("1.48"),
                        Decimal("1000"),
                        Decimal("500"),
                        Decimal("1.50"),
                        Decimal("133"),
                        ts,
                        10,
                        2,
                        "carried_forward",
                    )
                ]
            elif "ask_wall_price" in sql and "GROUP BY" in sql:
                ts = datetime(2026, 8, 25, 12, 30, tzinfo=timezone.utc)
                R.result_rows = [
                    (
                        Decimal("1.52"),
                        Decimal("2000"),
                        Decimal("600"),
                        Decimal("1.50"),
                        Decimal("133"),
                        ts,
                        8,
                        0,
                        "",
                    )
                ]
            return R()

    monkeypatch.setattr(
        "research_charts.orderbook_profile._client", lambda: _FakeClient()
    )
    clear_orderbook_profile_cache_for_tests()
    start = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    payload = load_orderbook_profile(
        symbol="XRPUSDT", start=start, end=end, known_symbol=True
    )
    assert payload["mode"] == "visible_range"
    assert payload["bid_count"] == 1
    assert payload["ask_count"] == 1
    sides = {b["side"] for b in payload["bars"]}
    assert sides == {"BID", "ASK"}
    bid = next(b for b in payload["bars"] if b["side"] == "BID")
    assert bid["price"] == 1.48
    assert bid["value"] == 1000.0
    assert bid["carried_forward"] is True


def test_payload_limit_constant():
    assert MAX_BARS_PER_SIDE <= 100

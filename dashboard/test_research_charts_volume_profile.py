"""Visible-range volume profile: bins, POC/VA, API, UI contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI

DASHBOARD_ROOT = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(DASHBOARD_ROOT))

from research_charts.api import build_router  # noqa: E402
from research_charts.volume_profile import (  # noqa: E402
    ALLOWED_ROWS,
    NO_PUBLIC_TRADE_SYMBOLS,
    TradeRow,
    bin_index,
    build_profile,
    classify_coverage,
    dedupe_trades,
    expand_value_area,
    pick_poc,
    resolve_rows,
)
from research_charts.workspace_session import (  # noqa: E402
    DEFAULT_VOLUME_PROFILE,
    normalize_volume_profile,
    reset_workspace_for_tests,
)

HOST_JS = DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js"
TRP_JS = DASHBOARD_ROOT / "static" / "research_trp" / "chart.js"
PAGE_HTML = DASHBOARD_ROOT / "templates" / "research_charts.html"
PANE_HTML = DASHBOARD_ROOT / "static" / "research_trp" / "pane.html"


class _AsgiClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def _call(self, method: str, path: str, **kwargs):
        import asyncio

        async def _go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await getattr(client, method)(path, **kwargs)

        return asyncio.run(_go())

    def get(self, path: str, **kwargs):
        return self._call("get", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._call("put", path, **kwargs)


def _mini():
    def _auth():
        return {"username": "vp"}

    def _render(name, context):
        return PAGE_HTML.read_text(encoding="utf-8")

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(app)


def _trade(tid, price, size, side, ts, notional=None):
    p = Decimal(str(price))
    s = Decimal(str(size))
    return TradeRow(
        trade_id=str(tid),
        price=p,
        size=s,
        notional=Decimal(str(notional if notional is not None else p * s)),
        side=side,
        trade_ts=ts,
    )


TS = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def test_buy_sell_base_quote_and_delta():
    trades = [
        _trade("a", 10, 2, "Buy", TS, 20),
        _trade("b", 10, 1, "Sell", TS, 10),
    ]
    out = build_profile(trades, rows=24, volume_mode="base")
    assert out["total_buy_volume"] == 2
    assert out["total_sell_volume"] == 1
    assert out["total_volume"] == 3
    assert out["total_delta"] == 1
    assert out["total_buy_quote_volume"] == 20
    assert out["total_sell_quote_volume"] == 10
    q = build_profile(trades, rows=24, volume_mode="quote")
    assert q["total_buy_volume"] == 20
    assert q["total_delta"] == 10


def test_deterministic_bins_and_top_edge():
    assert bin_index(Decimal("10"), Decimal("10"), Decimal("20"), 24) == 0
    assert bin_index(Decimal("20"), Decimal("10"), Decimal("20"), 24) == 23
    trades = [_trade("1", 10, 1, "Buy", TS), _trade("2", 20, 1, "Buy", TS)]
    out = build_profile(trades, rows=24)
    assert out["rows_returned"] == 24
    assert out["bins"][0]["buy_count"] == 1
    assert out["bins"][-1]["buy_count"] == 1
    assert out["bins"][-1]["price_high"] == 20


def test_empty_and_single_price():
    empty = build_profile([], rows=24)
    assert empty["rows_returned"] == 0
    assert empty["total_trade_count"] == 0
    one = build_profile([_trade("1", 5, 3, "Buy", TS)], rows=24)
    assert one["rows_returned"] == 1
    assert one["bins"][0]["price_low"] == one["bins"][0]["price_high"] == 5
    assert one["total_trade_count"] == 1


def test_poc_and_tie_break_closer_to_vwap():
    trades = [
        _trade("a", 10, 5, "Buy", TS),
        _trade("b", 20, 5, "Buy", TS),
        _trade("c", 11, 1, "Buy", TS),
    ]
    out = build_profile(trades, rows=24)
    assert out["poc"] is not None
    # volume at 10 and 20 tied at 5; VWAP pulled toward 11 → lower bin nearer 11 wins (price 10)
    assert out["poc"]["price_low"] <= 11


def test_value_area_70_and_vah_val():
    trades = []
    for i, (price, vol) in enumerate([(10, 10), (11, 70), (12, 10), (13, 5), (14, 5)]):
        trades.append(_trade(str(i), price, vol, "Buy", TS))
    out = build_profile(trades, rows=24)
    assert out["poc"]["is_poc"] is True
    assert out["value_area_percent_actual"] >= 70
    assert out["val"] is not None and out["vah"] is not None
    assert out["val"] <= out["poc"]["price_mid"] <= out["vah"]
    assert out["value_area_method"] == "poc_expand_70pct_base_volume"


def test_trade_and_volume_conservation():
    trades = [
        _trade("a", 1, 2, "Buy", TS, 2),
        _trade("b", 2, 3, "Sell", TS, 6),
        _trade("c", 3, 4, "Buy", TS, 12),
    ]
    out = build_profile(trades, rows=24)
    assert out["total_trade_count"] == 3
    assert abs(out["total_base_volume"] - 9) < 1e-9
    assert abs(sum(b["total_base_volume"] for b in out["bins"]) - 9) < 1e-9


def test_logical_dedup_last_wins_and_conflict_flag():
    rows = [
        _trade("same", 10, 1, "Buy", TS, 10),
        _trade("same", 10, 2, "Sell", TS, 20),
        _trade("other", 11, 1, "Buy", TS, 11),
    ]
    uniq, conflicts, changed = dedupe_trades(rows)
    assert len(uniq) == 2
    assert conflicts == 1
    assert changed is True
    kept = {t.trade_id: t for t in uniq}["same"]
    assert kept.size == Decimal("2")
    assert kept.side == "Sell"
    out = build_profile(uniq, rows=24)
    assert out["total_trade_count"] == 2


def test_archive_live_overlap_idempotent():
    rows = [
        _trade("x", 10, 5, "Buy", TS, 50),
        _trade("x", 10, 5, "Buy", TS + timedelta(seconds=1), 50),
    ]
    uniq, conflicts, changed = dedupe_trades(rows)
    assert len(uniq) == 1
    assert conflicts == 0
    assert changed is True
    assert build_profile(uniq, rows=24)["total_base_volume"] == 5


def test_coverage_codes():
    a = datetime(2026, 8, 10, tzinfo=timezone.utc)
    b = datetime(2026, 8, 17, tzinfo=timezone.utc)
    gap_s = datetime(2026, 8, 16, 23, 59, tzinfo=timezone.utc)
    gap_e = datetime(2026, 8, 17, 11, 24, tzinfo=timezone.utc)
    code, _ = classify_coverage(
        requested_start=datetime(2026, 8, 12, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 13, tzinfo=timezone.utc),
        coverage_start=a,
        coverage_end=b,
        gap_start=gap_s,
        gap_end=gap_e,
        trade_count=10,
    )
    assert code == "FULL"
    code, _ = classify_coverage(
        requested_start=datetime(2026, 8, 16, 12, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
        coverage_start=a,
        coverage_end=b,
        gap_start=gap_s,
        gap_end=gap_e,
        trade_count=10,
    )
    assert code == "GAP_OPEN"
    code, _ = classify_coverage(
        requested_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 2, tzinfo=timezone.utc),
        coverage_start=a,
        coverage_end=b,
        gap_start=gap_s,
        gap_end=gap_e,
        trade_count=0,
    )
    assert code == "NONE"
    code, _ = classify_coverage(
        requested_start=datetime(2026, 8, 8, tzinfo=timezone.utc),
        requested_end=datetime(2026, 8, 12, tzinfo=timezone.utc),
        coverage_start=a,
        coverage_end=b,
        gap_start=gap_s,
        gap_end=gap_e,
        trade_count=10,
    )
    assert code == "PARTIAL"


def test_resolve_rows_and_xau_set():
    assert resolve_rows("auto") == 24
    assert resolve_rows(48) == 48
    try:
        resolve_rows(12)
        assert False
    except ValueError:
        pass
    assert "XAUUSDT" in NO_PUBLIC_TRADE_SYMBOLS


def test_workspace_volume_profile_defaults_backward_compat():
    assert DEFAULT_VOLUME_PROFILE["enabled"] is False
    old = normalize_volume_profile(None)
    assert old["enabled"] is False
    assert old["rows"] == "auto"
    mixed = normalize_volume_profile({"enabled": True, "unknown": 1})
    assert mixed["enabled"] is True
    assert "unknown" not in mixed


def test_api_unknown_symbol_and_invalid_params(monkeypatch):
    reset_workspace_for_tests()
    monkeypatch.setattr("research_charts.api.known_symbols", lambda: {"DOGEUSDT"})
    client = _mini()
    bad = client.get("/api/research/volume-profile", params={
        "symbol": "NOTAREALCOINUSDT",
        "start": 1780000000,
        "end": 1780003600,
    })
    assert bad.status_code == 404
    rng = client.get("/api/research/volume-profile", params={
        "symbol": "DOGEUSDT",
        "start": 100,
        "end": 50,
    })
    assert rng.status_code == 400
    big = client.get("/api/research/volume-profile", params={
        "symbol": "DOGEUSDT",
        "start": 1_700_000_000,
        "end": 1_700_000_000 + 8 * 86400,
    })
    assert big.status_code == 400
    rows = client.get("/api/research/volume-profile", params={
        "symbol": "DOGEUSDT",
        "start": 1780000000,
        "end": 1780003600,
        "rows": "13",
    })
    assert rows.status_code == 400
    mode = client.get("/api/research/volume-profile", params={
        "symbol": "DOGEUSDT",
        "start": 1780000000,
        "end": 1780003600,
        "volume_mode": "ticks",
    })
    assert mode.status_code == 400
    exact = client.get("/api/research/volume-profile", params={
        "symbol": "XAUUSDT",
        "start": 1_700_000_000,
        "end": 1_700_000_000 + 7 * 86400,
    })
    assert exact.status_code == 200


def test_api_xau_no_public_trades():
    reset_workspace_for_tests()
    client = _mini()
    res = client.get("/api/research/volume-profile", params={
        "symbol": "XAUUSDT",
        "start": 1780000000,
        "end": 1780003600,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["coverage_available"] is False
    assert body["coverage_code"] == "NONE"
    assert body["total_trade_count"] == 0


def test_api_query_memory_is_controlled(monkeypatch):
    reset_workspace_for_tests()
    from research_charts.public_trades_profile import VolumeProfileQueryError

    def boom(**kwargs):
        raise VolumeProfileQueryError("query_memory", "ClickHouse query memory limit exceeded")

    monkeypatch.setattr("research_charts.api.load_volume_profile", boom)
    client = _mini()
    res = client.get("/api/research/volume-profile", params={
        "symbol": "DOGEUSDT",
        "start": 1780000000,
        "end": 1780003600,
    })
    assert res.status_code == 503
    assert res.json()["error"] == "query_memory"


def test_api_query_timeout_is_controlled(monkeypatch):
    reset_workspace_for_tests()
    from research_charts.public_trades_profile import VolumeProfileQueryError

    def boom(**kwargs):
        raise VolumeProfileQueryError("query_timeout", "ClickHouse query timeout")

    monkeypatch.setattr("research_charts.api.load_volume_profile", boom)
    client = _mini()
    res = client.get("/api/research/volume-profile", params={
        "symbol": "DOGEUSDT",
        "start": 1780000000,
        "end": 1780003600,
    })
    assert res.status_code == 503
    assert res.json()["error"] == "query_timeout"


def test_query_is_window_scoped_not_full_table():
    src = (DASHBOARD_ROOT / "research_charts" / "public_trades_profile.py").read_text(encoding="utf-8")
    assert "PREWHERE symbol" in src
    assert "trade_ts >=" in src
    assert "FROM orderbook_deltas" not in src
    assert "orderbook_analysis.public_trades_canonical" in src
    assert "max_memory_usage" in src
    assert "max_execution_time" in src
    assert "FINAL" in src


def test_page_toggle_default_off_and_assets():
    html = PAGE_HTML.read_text(encoding="utf-8")
    host = HOST_JS.read_text(encoding="utf-8")
    trp = TRP_JS.read_text(encoding="utf-8")
    pane = PANE_HTML.read_text(encoding="utf-8")
    assert 'id="researchVpEnabled"' in html
    assert "checked" not in html.split('id="researchVpEnabled"')[1].split(">")[0]
    assert "/api/research/volume-profile" in host
    assert "VP_DEBOUNCE_MS = 400" in host
    assert "pane.vpGen" in host
    assert "setVolumeProfile" in trp
    assert "clearVolumeProfile" in trp
    assert "getVisibleTimeRange" in trp
    assert 'id="vp-overlay"' in pane
    assert "TIMEFRAMES = [\"1m\", \"5m\", \"15m\", \"30m\", \"1h\", \"4h\"]" in host
    tf_line = [ln for ln in host.splitlines() if ln.strip().startswith("const TIMEFRAMES")][0]
    assert "1d" not in tf_line


def test_host_does_not_poll_volume_profile_on_forming():
    host = HOST_JS.read_text(encoding="utf-8")
    forming_fn = host[host.index("async function pollForming") : host.index("async function pollIncremental")]
    assert "volume-profile" not in forming_fn
    assert "scheduleVolumeProfile" not in forming_fn
    assert "scheduleVolumeProfile" in host
    assert "AbortController" in host
    assert "clearPaneVolumeProfile(pane)" in host
    assert 'sourceAction: "tf-change"' in host
    assert 'sourceAction: "symbol-switch"' in host


def test_frontend_overlay_and_tooltip_contract():
    trp = TRP_JS.read_text(encoding="utf-8")
    css = (DASHBOARD_ROOT / "static" / "research_trp" / "style.css").read_text(encoding="utf-8")
    pane = PANE_HTML.read_text(encoding="utf-8")
    host = HOST_JS.read_text(encoding="utf-8")
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert "pointer-events: none" in css.split("#vp-overlay")[1].split("}")[0]
    assert "rgba(34, 211, 238" in trp
    assert "rgba(245, 158, 11" in trp
    assert 'ctx.strokeStyle = "#ef4444"' in trp
    assert "vpPayload.vah" in trp
    assert "vpPayload.val" in trp
    assert "Buy " in trp and "Sell " in trp and "Delta " in trp
    assert "Value Area" in trp
    assert "Outside VA" in trp
    assert "updateVolumeProfileHover" in trp
    assert "subscribeVisibleTimeRangeChange" in trp
    assert "drawVolumeProfile();" in trp
    assert 'style.css?v=vp-1' in pane
    assert 'id="researchVpPoc"' in html
    assert 'id="researchVpVa"' in html
    assert "gen !== pane.vpGen" in host
    assert "VP_DEBOUNCE_MS = 400" in host


def test_settings_put_keeps_old_sessions():
    reset_workspace_for_tests()
    client = _mini()
    snap = client.get("/api/research/workspace").json()
    assert snap["volume_profile"]["enabled"] is False
    put = client.put("/api/research/settings", json={"volume_profile": {"enabled": True, "rows": "48"}})
    assert put.status_code == 200
    body = put.json()
    assert body["volume_profile"]["enabled"] is True
    assert body["volume_profile"]["rows"] == "48"
    assert body["ema"] is not None
    assert body["liquidity"] is not None


def test_doge_independent_control_window():
    from research_charts.public_trades_profile import (
        fetch_raw_trades_for_tests,
        load_volume_profile,
        clear_volume_profile_cache_for_tests,
    )
    from research_charts.volume_profile import build_profile, dedupe_trades

    start = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 16, 13, 0, tzinfo=timezone.utc)
    clear_volume_profile_cache_for_tests()
    raw = fetch_raw_trades_for_tests("DOGEUSDT", start, end)
    uniq, _, _ = dedupe_trades(raw)
    expected = build_profile(uniq, rows=24, volume_mode="base")
    api = load_volume_profile(
        symbol="DOGEUSDT",
        start=start,
        end=end,
        rows=24,
        volume_mode="base",
        known_symbol=True,
    )
    assert api["total_trade_count"] == expected["total_trade_count"]
    assert abs(api["total_buy_volume"] - expected["total_buy_volume"]) < 1e-6
    assert abs(api["total_sell_volume"] - expected["total_sell_volume"]) < 1e-6
    assert abs(api["total_delta"] - expected["total_delta"]) < 1e-6
    assert abs(api["poc_price"] - expected["poc_price"]) < 1e-6
    assert abs(api["vah"] - expected["vah"]) < 1e-4
    assert abs(api["val"] - expected["val"]) < 1e-4
    assert api["source"] == "orderbook_analysis.public_trades_canonical"

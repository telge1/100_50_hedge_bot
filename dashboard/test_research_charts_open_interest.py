"""Open Interest lower-pane: causal as-of, workspace toggle, Research + MP UI."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(DASHBOARD_ROOT))

from research_charts.open_interest import (  # noqa: E402
    LIVE_MAX_SECONDS,
    OI_5M_FQN,
    OI_5S_FQN,
    align_asof,
    candles_from_times,
    empty_payload,
    fetch_oi_samples,
    merge_live_over_history,
    payload_from_series,
    plot_and_asof_times,
)
from research_charts.workspace_session import (  # noqa: E402
    reset_workspace_for_tests,
)


HOST_JS = DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js"
TRP_JS = DASHBOARD_ROOT / "static" / "research_trp" / "chart.js"
PAGE_HTML = DASHBOARD_ROOT / "templates" / "research_charts.html"
PANE_HTML = DASHBOARD_ROOT / "static" / "research_trp" / "pane.html"
MP_HTML = DASHBOARD_ROOT / "templates" / "market_profile_v1.html"
MP_JS = DASHBOARD_ROOT / "static" / "market_profile_v1" / "app.js"
MP_CSS = DASHBOARD_ROOT / "static" / "market_profile_v1" / "style.css"


def _ws(tmp_path, monkeypatch):
    import research_charts.workspace_session as ws_mod

    monkeypatch.setattr(ws_mod, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "DRAWINGS_PATH", tmp_path / "drawings.json")
    monkeypatch.setattr(ws_mod, "SETTINGS_PATH", tmp_path / "indicator_settings.json")
    return reset_workspace_for_tests()


def test_align_asof_prefers_live_over_history():
    asof = [1000, 1300, 1600]
    live = [(990, 10.0), (1250, 11.0), (1590, 12.0)]
    hist = [(900, 9.0), (1200, 9.5), (1500, 9.8)]
    vals = align_asof(asof, live)
    assert vals == [10.0, 11.0, 12.0]
    plot = [900, 1200, 1500]
    data, source = merge_live_over_history(plot, asof, live, hist)
    assert source == "open_interest_5s"
    assert [row["time"] for row in data] == plot
    assert [row["value"] for row in data] == [10.0, 11.0, 12.0]


def test_align_asof_falls_back_to_5m_when_live_missing():
    asof = [1000, 4000]
    live = [(5000, 99.0)]  # after as-of — must not leak forward
    hist = [(900, 7.0), (3000, 8.0)]
    data, source = merge_live_over_history([100, 200], asof, live, hist)
    assert source == "open_interest_5m_history"
    assert data[0]["value"] == 7.0
    assert data[1]["value"] == 8.0


def test_mixed_source_when_live_covers_only_recent():
    asof = [1000, 2000]
    live = [(1900, 22.0)]
    hist = [(800, 11.0)]
    data, source = merge_live_over_history([10, 20], asof, live, hist)
    assert source == "mixed"
    assert data[0]["value"] == 11.0
    assert data[1]["value"] == 22.0


def test_plot_times_are_bar_open_asof_is_close():
    class C:
        def __init__(self, t):
            self.timestamp = t

    candles = [
        C(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)),
        C(datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)),
    ]
    now = int(datetime(2026, 9, 1, 12, 7, tzinfo=timezone.utc).timestamp())
    plot, asof = plot_and_asof_times(candles, timeframe="5m", now=now)
    assert plot[1] - plot[0] == 300
    assert asof[0] == plot[0] + 300
    # forming/last bar close is in the future → clamp to now
    assert asof[1] == now


def test_empty_payload_hidden_by_default():
    body = empty_payload()
    assert body["id"] == "open_interest"
    assert body["visible"] is False
    assert body["auto_scale"] is True
    packed = payload_from_series([{"time": 1, "value": 2.5}], source="open_interest_5s")
    assert packed["visible"] is True
    assert packed["series"][0]["id"] == "oi"


def test_workspace_toggle_open_interest_persists(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    assert ws.snapshot()["open_interest"]["enabled"] is False
    snap = ws.set_indicator_enabled("open_interest", True)
    assert snap["open_interest"]["enabled"] is True
    ws2 = reset_workspace_for_tests()
    assert ws2.snapshot()["open_interest"]["enabled"] is True
    ws2.set_indicator_enabled("oi", False)
    assert ws2.snapshot()["open_interest"]["enabled"] is False


def test_pane_bundle_includes_oi_without_ch_when_disabled(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    from research_charts.service import pane_bundle, clear_candle_cache_for_tests

    hits = {"n": 0}

    def boom(*_a, **_k):
        hits["n"] += 1
        raise AssertionError("OI must not query ClickHouse when disabled")

    monkeypatch.setattr("research_charts.open_interest.fetch_oi_samples", boom)
    clear_candle_cache_for_tests()
    packed = pane_bundle(
        "APTUSDT",
        "5m",
        limit=40,
        stochastic={"enabled": False},
        open_interest={"enabled": False},
        liquidity={"enabled": False},
    )
    assert packed["open_interest"]["id"] == "open_interest"
    assert packed["open_interest"]["visible"] is False
    assert hits["n"] == 0


def test_pane_bundle_packs_oi_from_injected_samples(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    from research_charts.service import pane_bundle, clear_candle_cache_for_tests

    def fake_fetch(symbol, start, end, *, now=None):
        live = [(start - 60, 111.0), (end, 222.0)]
        return live, []

    monkeypatch.setattr("research_charts.open_interest.fetch_oi_samples", fake_fetch)
    clear_candle_cache_for_tests()
    packed = pane_bundle(
        "APTUSDT",
        "5m",
        limit=40,
        stochastic={"enabled": False},
        open_interest={"enabled": True},
        liquidity={"enabled": False},
    )
    oi = packed["open_interest"]
    assert oi["visible"] is True
    assert oi["auto_scale"] is True
    series = {row["id"]: row for row in (oi.get("series") or [])}
    assert "oi" in series
    assert series["oi"]["data"]
    assert all("time" in pt and "value" in pt for pt in series["oi"]["data"])


def test_research_and_mp_ui_wire_oi_toggle():
    host = HOST_JS.read_text(encoding="utf-8")
    page = PAGE_HTML.read_text(encoding="utf-8")
    pane = PANE_HTML.read_text(encoding="utf-8")
    trp = TRP_JS.read_text(encoding="utf-8")
    mp_html = MP_HTML.read_text(encoding="utf-8")
    mp_js = MP_JS.read_text(encoding="utf-8")
    mp_css = MP_CSS.read_text(encoding="utf-8")

    assert 'id="researchIndOi"' in page
    assert 'name: "open_interest"' in host
    assert 'refreshIndicatorsVisible("oi-toggle")' in host
    assert "chart.setOiPane" in host
    assert '"stoch+oi"' in trp
    assert "function setOiPane" in trp
    assert 'id="lower-pane"' in pane
    assert 'id="mpShowOi"' in mp_html
    assert 'id="lower-pane"' in mp_html
    assert "refreshOpenInterestDisplay" in mp_js
    assert "setOiPane" in mp_js
    assert "times: times" in mp_js
    assert ".mp-trp-app.lower-open #lower-pane" in mp_css
    assert "ob-levels-19" in host
    assert "ob-levels-19" in pane
    assert "alignOscPointsToCandles" in trp
    assert "fittedOscScaleProvider" in trp
    assert "oscValueRange" in trp
    assert "fitAutoOscToVisibleRange" in trp
    assert "meta.data" in trp


def test_candles_from_times_is_strictly_increasing():
    rows = candles_from_times([100, 100, 90, 200, "nope", {"time": 300}])
    assert [r["time"] for r in rows] == [100, 200, 300]


def test_live_oi_window_follows_view_end_not_wall_clock(monkeypatch):
    seen = []

    def fake_query(table, symbol, start, end):
        seen.append((table, int(start), int(end)))
        return [(int(start), 1.0), (int(end), 2.0)]

    monkeypatch.setattr("research_charts.open_interest._query_samples", fake_query)
    start = 1_787_702_400  # 2026-08-26
    end = 1_788_393_600  # 2026-09-02
    now = end + 14 * 24 * 3600
    fetch_oi_samples("DOGEUSDT", start, end, now=now)
    live_calls = [row for row in seen if row[0] == OI_5S_FQN]
    hist_calls = [row for row in seen if row[0] == OI_5M_FQN]
    assert live_calls, "historical views must still query 5s for the last days of the window"
    assert live_calls[0][1] == end - LIVE_MAX_SECONDS
    assert live_calls[0][2] == end
    assert hist_calls
    assert hist_calls[0][2] == end

"""Tests for the anchored market profile page.

Covers request validation, the read-only guarantee, the unvalidated-shape
disclosure, and the frontend contract strings the renderer depends on.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

DASHBOARD_DIR = Path(__file__).resolve().parent
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from market_profile_v1 import ASSET_V  # noqa: E402
from market_profile_v1.api import build_router  # noqa: E402
from market_profile_v1.service import (  # noqa: E402
    MAX_RANGE_DAYS,
    MAX_WINDOWS,
    ProfileRequestError,
    SUPPORTED_MP_TIMEFRAMES,
    _cache_key,
    _normalize_request,
    clear_cache_for_tests,
)

PAGE_HTML = DASHBOARD_DIR / "templates" / "market_profile_v1.html"
APP_JS = DASHBOARD_DIR / "static" / "market_profile_v1" / "app.js"
STYLE_CSS = DASHBOARD_DIR / "static" / "market_profile_v1" / "style.css"
NAV_HTML = DASHBOARD_DIR / "templates" / "partials" / "nav.html"


class _AsgiClient:
    def __init__(self, app):
        self._app = app

    def get(self, url, params=None):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.get(url, params=params)

        return asyncio.run(_run())


def _mini():
    def _auth():
        return {"username": "mp"}

    def _render(name, context):
        return f"rendered:{name}:{sorted(context.keys())}"

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(app)


DAY = 86400
T_START = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp())
T_END = int(datetime(2026, 8, 25, tzinfo=timezone.utc).timestamp())


def _req(**over):
    base = {
        "symbol": "BTCUSDT",
        "start": T_START,
        "end": T_END,
        "anchor": "day",
        "sessions": None,
        "timeframe": "15m",
        "value_area_pct": 0.70,
        "target_bins": 160,
        "use_final": False,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- validation


def test_a_valid_request_normalizes_to_utc_datetimes():
    out = _normalize_request(**_req())
    assert out["symbol"] == "BTCUSDT"
    assert out["start"].tzinfo is timezone.utc
    assert out["end"] > out["start"]
    assert out["anchor_mode"] == "day"
    assert out["mp_timeframe"] == "day"
    assert out["timeframe"] == "15m"
    assert out["use_final"] is False


def test_mp_timeframe_overrides_legacy_anchor_alias():
    out = _normalize_request(**_req(anchor="day", mp_timeframe="4h"))
    assert out["mp_timeframe"] == "4h"
    assert out["anchor_mode"] == "4h"
    assert out["timeframe"] == "15m"


def test_candle_and_mp_timeframes_normalize_independently():
    out = _normalize_request(**_req(timeframe="1h", mp_timeframe="15m", anchor=None))
    assert out["timeframe"] == "1h"
    assert out["mp_timeframe"] == "15m"
    out2 = _normalize_request(**_req(timeframe="5m", mp_timeframe="4h", anchor=None))
    assert out2["timeframe"] == "5m"
    assert out2["mp_timeframe"] == "4h"


@pytest.mark.parametrize("mp_tf", list(SUPPORTED_MP_TIMEFRAMES))
def test_all_mp_timeframes_are_accepted(mp_tf):
    out = _normalize_request(**_req(mp_timeframe=mp_tf, anchor=None))
    assert out["mp_timeframe"] == mp_tf
    assert out["anchor_mode"] == mp_tf


def test_cache_key_uses_mp_window_not_candle_tf():
    a = _normalize_request(**_req(timeframe="5m", mp_timeframe="4h", anchor=None))
    b = _normalize_request(**_req(timeframe="1h", mp_timeframe="4h", anchor=None))
    c = _normalize_request(**_req(timeframe="5m", mp_timeframe="15m", anchor=None))
    assert _cache_key(a, True) == _cache_key(b, True)
    assert _cache_key(a, True) != _cache_key(c, True)
    assert "4h" in _cache_key(a, True)
    assert a["symbol"] in _cache_key(a, True)
    assert str(a["start_unix"]) in _cache_key(a, True)

@pytest.mark.parametrize("symbol", ["", "   ", "BTC-USDT", "BTC USDT", "BTC/USDT"])
def test_malformed_symbols_are_rejected(symbol):
    with pytest.raises(ProfileRequestError) as exc:
        _normalize_request(**_req(symbol=symbol))
    assert exc.value.code == "bad_symbol"


def test_an_inverted_or_empty_range_is_rejected():
    with pytest.raises(ProfileRequestError) as exc:
        _normalize_request(**_req(start=T_END, end=T_START))
    assert exc.value.code == "bad_range"

    with pytest.raises(ProfileRequestError):
        _normalize_request(**_req(start=T_START, end=T_START))


def test_an_oversized_range_is_refused_before_touching_clickhouse():
    with pytest.raises(ProfileRequestError) as exc:
        _normalize_request(**_req(end=T_START + (MAX_RANGE_DAYS + 5) * DAY))
    assert exc.value.code == "range_too_large"


def test_unknown_anchor_and_session_names_are_rejected():
    with pytest.raises(ProfileRequestError) as exc:
        _normalize_request(**_req(anchor="weekly"))
    assert exc.value.code == "bad_anchor"

    with pytest.raises(ProfileRequestError) as exc:
        _normalize_request(**_req(anchor="session", sessions="asia,lunch"))
    assert exc.value.code == "bad_session"


def test_session_anchor_defaults_to_every_known_session():
    out = _normalize_request(**_req(anchor="session", sessions=None))
    assert set(out["sessions"]) == {"asia", "eu", "us", "late"}


def test_out_of_band_value_area_and_bins_are_rejected():
    for bad in (0.1, 0.99, 1.5):
        with pytest.raises(ProfileRequestError) as exc:
            _normalize_request(**_req(value_area_pct=bad))
        assert exc.value.code == "bad_value_area"

    for bad in (5, 10_000):
        with pytest.raises(ProfileRequestError) as exc:
            _normalize_request(**_req(target_bins=bad))
        assert exc.value.code == "bad_target_bins"


def test_unsupported_timeframe_is_rejected():
    with pytest.raises(ProfileRequestError) as exc:
        _normalize_request(**_req(timeframe="7m"))
    assert exc.value.code == "bad_timeframe"


# ------------------------------------------------------------------- routing


def test_the_page_route_renders_its_own_template():
    body = _mini().get("/live-charts/market-profile").text
    assert "market_profile_v1.html" in body


def test_the_page_context_carries_the_unvalidated_shape_notice():
    body = _mini().get("/live-charts/market-profile").text
    assert "shape_notice" in body
    assert "asset_v" in body


def test_meta_exposes_choices_limits_and_the_shape_caveat():
    res = _mini().get("/api/market-profile/meta")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert "day" in body["anchors"] and "composite" in body["anchors"]
    assert "4h" in body["mp_timeframes"]
    assert "15m" in body["timeframes"]
    assert "1m" in body["timeframes"]
    assert body["limits"]["max_windows"] == MAX_WINDOWS
    assert body.get("profile_timeframe_independent") is True
    # The verdict is drawn on the chart, so the API must admit its status.
    assert body["shape_unvalidated"] is True
    assert "unvalidiert" in body["shape_notice"]
    assert body.get("dual_contract_version") == "market_profile_v1_dual_tpo_volume_v1"

def test_bad_requests_return_a_coded_error_not_a_crash():
    res = _mini().get(
        "/api/market-profile/profiles",
        params={"symbol": "BTC/USDT", "start": T_START, "end": T_END},
    )
    assert res.status_code == 400
    body = res.json()
    assert body["success"] is False
    assert body["error"] == "bad_symbol"
    assert "message" in body


def test_an_inverted_range_is_reported_as_bad_range():
    res = _mini().get(
        "/api/market-profile/profiles",
        params={"symbol": "BTCUSDT", "start": T_END, "end": T_START},
    )
    assert res.status_code == 400
    assert res.json()["error"] == "bad_range"


def test_missing_required_params_yield_422():
    assert _mini().get("/api/market-profile/profiles").status_code == 422


# ------------------------------------------------------- read-only guarantee


def test_the_module_contains_no_write_or_order_surface():
    for path in (
        DASHBOARD_DIR / "market_profile_v1" / "service.py",
        DASHBOARD_DIR / "market_profile_v1" / "api.py",
    ):
        src = path.read_text(encoding="utf-8")
        lowered = src.lower()
        for forbidden in ("insert into", "alter table", "create table", "drop table"):
            assert forbidden not in lowered, f"{path.name} must stay read-only"
        for forbidden in ("place_order", "submit_order", "api_secret", "api_key"):
            assert forbidden not in lowered, f"{path.name} must not touch execution"


def test_the_page_never_imports_the_matplotlib_renderer():
    # render.py is the offline PNG writer; pulling it in would drag a plotting
    # stack into the web process.
    src = (DASHBOARD_DIR / "market_profile_v1" / "service.py").read_text(encoding="utf-8")
    assert "market_profile.render" not in src
    assert "matplotlib" not in src

    oa = (DASHBOARD_DIR / "research_charts" / "oa_import.py").read_text(encoding="utf-8")
    loader = oa.split("def load_market_profile()")[1].split("@lru_cache")[0]
    imports = [
        line.strip()
        for line in loader.splitlines()
        if line.strip().startswith(("from ", "import "))
    ]
    assert imports, "loader should import something"
    assert not any("render" in line for line in imports)


# ------------------------------------------------------- frontend contracts


def test_the_template_wires_the_versioned_assets_and_chart_nodes():
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert "/static/market_profile_v1/app.js?v={{ asset_v }}" in html
    assert "/static/market_profile_v1/style.css?v={{ asset_v }}" in html
    assert "/static/research_trp/vendor/lightweight-charts.min.js" in html
    assert "/static/research_trp/chart.js?v={{ asset_v }}" in html
    for node in (
        "mpChart",
        "mpChartStack",
        "mpOverlay",
        "mpTooltip",
        "mpStatus",
        "mpLoad",
        "mpFullscreenBtn",
        "mpTools",
        "mpResetView",
    ):
        assert 'id="' + node + '"' in html


def test_the_template_offers_both_histogram_and_level_toggles():
    # The user asked for both renderings behind switches.
    html = PAGE_HTML.read_text(encoding="utf-8")
    for node in (
        "mpShowHistogram",
        "mpShowVolumeLine",
        "mpSplitBuySell",
        "mpShowPoc",
        "mpShowValueArea",
        "mpShowHvn",
        "mpShowLvn",
        "mpShowSinglePrints",
        "mpShowNakedPoc",
        "mpExtendLevels",
        "mpShowShape",
    ):
        assert 'id="' + node + '"' in html


def test_the_template_marks_the_shape_verdict_as_unvalidated():
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert "mpShapeNotice" in html
    assert "shape_notice" in html


def test_the_template_offers_a_fullscreen_chart_button():
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert 'id="mpFullscreenBtn"' in html
    assert "trp-fs-dock" in html
    js = APP_JS.read_text(encoding="utf-8")
    for fn in ("expandChartUp", "applyChartHeight", "restoreChartHeight"):
        assert fn in js


def test_the_renderer_reads_dual_tpo_and_volume_from_the_payload():
    js = APP_JS.read_text(encoding="utf-8")
    for key in ("profileTpo", "profileVolume", "drawTpoHistogram", "drawVolumeBuySellBars"):
        assert key in js, f"renderer must implement dual helper {key}"
    hist = js.split("function drawTpoHistogram")[1].split("function drawVolumeBuySellBars")[0]
    assert "tpo_count" in hist
    assert "base_volume" not in hist, "TPO histogram must not use base_volume"
    vol_line = js.split("function drawVolumeProfileLine")[1].split("function drawWindowBackground")[0]
    assert "base_volume" in vol_line
    assert "tpo_count" not in vol_line, "volume line must not use tpo_count"
    buy_sell = js.split("function drawVolumeBuySellBars")[1].split("function drawProfileHistogram")[0]
    assert "buy_volume" in buy_sell and "sell_volume" in buy_sell


def test_the_renderer_reads_every_level_family_from_the_payload():
    js = APP_JS.read_text(encoding="utf-8")
    for key in ("value_area", "single_print_ranges", "naked_poc", "nodes", "tpo", "volume"):
        assert key in js, f"renderer must consume payload key {key}"
    for fn in (
        "drawTpoHistogram",
        "drawVolumeProfileLine",
        "drawWindowBackground",
        "drawWindowLevels",
        "drawShapeLabel",
        "windowXSpan",
    ):
        assert fn in js


def test_shaded_areas_stay_inside_their_own_window():
    # An extended translucent fill repaints over neighbouring windows and
    # buries their histograms, so only line levels may reach forward.
    js = APP_JS.read_text(encoding="utf-8")
    background = js.split("function drawWindowBackground")[1].split("function level")[0]
    assert "fillRect" in background
    assert "region.x1" not in background, "background fills must not extend to the plot edge"


def test_the_histogram_is_drawn_after_the_shaded_background():
    # Draw order decides visibility; the histogram has to win.
    js = APP_JS.read_text(encoding="utf-8")
    body = js.split("function draw()")[1]
    assert body.index("drawWindowBackground(") < body.index("drawProfileHistogram(")
    assert body.index("drawProfileHistogram(") < body.index("drawVolumeProfileLine(")
    assert body.index("drawVolumeProfileLine(") < body.index("drawWindowLevels(")


def test_shape_labels_avoid_stacking_on_each_other():
    js = APP_JS.read_text(encoding="utf-8")
    assert "taken" in js.split("function drawShapeLabel")[1].split("function draw()")[0]


def test_the_renderer_maps_time_through_logical_indices():
    # Anchored windows scroll off screen; timeToCoordinate returns null there,
    # so the renderer has to go through logical bar indices instead.
    js = APP_JS.read_text(encoding="utf-8")
    assert "logicalToCoordinate" in js
    assert "windowBarSpan" in js


def test_display_only_toggles_do_not_trigger_a_refetch():
    js = APP_JS.read_text(encoding="utf-8")
    redraw_block = js.split("Drawing-only toggles")[1].split("These change what gets computed")[0]
    assert "scheduleDraw" in redraw_block
    assert "load()" not in redraw_block


def test_the_overlay_cannot_swallow_chart_interaction():
    css = STYLE_CSS.read_text(encoding="utf-8")
    overlay = css.split(".mp-overlay")[1].split("}")[0]
    assert "pointer-events: none" in overlay


def test_the_nav_exposes_the_page_once():
    nav = NAV_HTML.read_text(encoding="utf-8")
    assert nav.count("/live-charts/market-profile") == 1
    assert "live-charts-market-profile" in nav


def test_the_page_template_activates_its_nav_entry():
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert "set nav_active = 'live-charts-market-profile'" in html


def test_the_app_wires_trp_chart_tools_and_ema():
    js = APP_JS.read_text(encoding="utf-8")
    for token in (
        "chartApi", "setEmaOverlays", "mpTools", "mpResetView", "setHostShift",
        "fetchEmaOverlays", "mpShowLiquidity", "refreshLiquidityLocation",
        "loadResearchPaneForLld", "isUserDrawingOverlay", "mpLldSettings", "renderLldLegend",
    ):
        assert token in js, f"app.js must wire TRP chart feature {token}"


def test_the_template_offers_liquidity_location_toggle():
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert 'id="mpShowLiquidity"' in html
    assert 'id="mpLldSettings"' in html
    assert 'id="modalLld"' in html


def test_the_app_wires_the_router():
    src = (DASHBOARD_DIR / "app.py").read_text(encoding="utf-8")
    assert "market_profile_v1.api import build_router" in src
    assert "_build_market_profile_router(" in src


def test_the_app_auto_loads_on_start_and_defaults_to_30_days():
    js = APP_JS.read_text(encoding="utf-8")
    assert "function start()" in js
    assert "load();" in js
    assert "Chart bridge can miss the ready event" in js or "Auto-load immediately" in js
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert 'value="30" selected' in html
    assert "Chart wird geladen" in html
    assert "Zeitraum wählen und" not in html


def test_the_asset_version_is_a_non_empty_token():
    assert isinstance(ASSET_V, str) and ASSET_V.strip()
    assert ASSET_V == "mp-6"


def test_kerzen_and_market_profile_controls_are_separate():
    from market_profile_v1.service import SUPPORTED_TIMEFRAMES

    html = PAGE_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    assert 'for="mpTimeframe">KERZEN</' in html
    assert 'for="mpAnchor">MARKET PROFILE</' in html
    assert "{% for tf in timeframes %}" in html
    assert "1m" in SUPPORTED_TIMEFRAMES
    for tf in ("5m", "15m", "30m", "1h", "4h"):
        assert f'value="{tf}"' in html  # MP period options (hardcoded)
    assert 'value="day"' in html and 'value="session"' in html and 'value="composite"' in html
    assert "mp_timeframe" in js
    assert "s.mpTimeframe || s.anchor" in js
    assert '["mpSymbol", "mpTimeframe", "mpAnchor", "mpDays"]' in js
    # Candle TF must not be copied into the MP control on restore.
    assert "Never copy candle" in js


def test_the_page_bridge_stubs_crosshair_handlers():
    """Regression: missing on_crosshair_move crashed load as Netzwerkfehler."""
    html = PAGE_HTML.read_text(encoding="utf-8")
    assert "window.bridge = {" in html
    assert "on_crosshair_move:" in html
    assert "on_crosshair_leave:" in html
    assert "on_chart_click:" in html
    js = APP_JS.read_text(encoding="utf-8")
    assert "scheduleDrawDebounced" in js
    assert "Promise.all([" in js
    assert "startLivePoll" in js
    assert "pollForming" in js
    assert "/api/research/forming-bar" in js
    assert "updateFormingBar" in js
    assert "FORMING_MS = 250" in js
    chart = (DASHBOARD_DIR / "static" / "research_trp" / "chart.js").read_text(encoding="utf-8")
    assert 'typeof window.bridge.on_crosshair_move === "function"' in chart
    assert 'typeof window.bridge.on_crosshair_leave === "function"' in chart
    assert "updateFormingBar._stickAt" in chart

# ----------------------------------------------------------- live smoke test


def _clickhouse_available() -> bool:
    try:
        from market_profile_v1.service import _client

        _client().query("SELECT 1")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _clickhouse_available(), reason="ClickHouse not reachable")
def test_a_real_day_anchored_request_returns_usable_profiles():
    from market_profile_v1.service import load_profiles

    clear_cache_for_tests()
    out = load_profiles(
        symbol="BTCUSDT",
        start=int(datetime(2026, 8, 25, tzinfo=timezone.utc).timestamp()),
        end=int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp()),
        anchor="day",
        timeframe="15m",
    )
    assert out["success"] is True
    assert out["candles"], "chart needs candles"
    assert out["profiles"], "expected at least one day profile"
    assert out["meta"]["shape_unvalidated"] is True

    first = out["profiles"][0]
    assert first.get("dual_contract_version") == "market_profile_v1_dual_tpo_volume_v1"
    assert "tpo" in first and "volume" in first
    tpo_va = first["tpo"]["value_area"]
    vol_va = first["volume"]["value_area"]
    assert first["price_low"] < first["price_high"]
    assert tpo_va["val"] <= tpo_va["poc"] <= tpo_va["vah"]
    assert vol_va["val"] <= vol_va["poc"] <= vol_va["vah"]
    assert first["tpo"]["bins"], "TPO histogram needs tpo bins"
    assert first["volume"]["bins"], "volume line needs volume bins"
    assert "bins" not in first or first.get("bins") is None
    assert first["shape"]["kind"]
    assert "naked_poc" in first

    # Candle times must be seconds, which is what lightweight-charts expects.
    assert all(1_500_000_000 < c["time"] < 3_000_000_000 for c in out["candles"])

    # Second identical call must be served from cache.
    again = load_profiles(
        symbol="BTCUSDT",
        start=int(datetime(2026, 8, 25, tzinfo=timezone.utc).timestamp()),
        end=int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp()),
        anchor="day",
        timeframe="15m",
    )
    assert again["cached"] is True


@pytest.mark.skipif(not _clickhouse_available(), reason="ClickHouse not reachable")
def test_candle_tf_change_reuses_mp_cache_and_period_windows_align():
    from market_profile_v1.service import load_profiles

    clear_cache_for_tests()
    start = int(datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc).timestamp())

    first = load_profiles(
        symbol="BTCUSDT",
        start=start,
        end=end,
        mp_timeframe="4h",
        timeframe="5m",
    )
    assert first["success"] is True
    assert first["mp_timeframe"] == "4h"
    assert first["timeframe"] == "5m"
    assert first["meta"]["profile_timeframe_independent"] is True
    assert first["profiles"]
    assert first["meta"]["windows"] == 3
    for p in first["profiles"]:
        assert p["window"]["anchor_mode"] == "4h"

    # Different candle TF, same MP windows → cache hit; candles refreshed.
    second = load_profiles(
        symbol="BTCUSDT",
        start=start,
        end=end,
        mp_timeframe="4h",
        timeframe="1h",
    )
    assert second["cached"] is True
    assert second["timeframe"] == "1h"
    assert second["mp_timeframe"] == "4h"
    assert len(second["profiles"]) == len(first["profiles"])
    assert len(second["candles"]) < len(first["candles"])

    third = load_profiles(
        symbol="BTCUSDT",
        start=start,
        end=end,
        mp_timeframe="15m",
        timeframe="1h",
    )
    assert third["cached"] is False
    assert third["mp_timeframe"] == "15m"
    assert third["timeframe"] == "1h"
    assert third["meta"]["windows"] == 48  # 12h / 15m

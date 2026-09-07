"""Frontend contract strings and regression: Footprint Aus leaves MP path intact."""

from __future__ import annotations

import re
import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

FP_JS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.js"
FP_CSS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.css"
PAGE = DASHBOARD_DIR / "templates" / "market_profile_v1.html"
MP_JS = DASHBOARD_DIR / "static" / "market_profile_v1" / "app.js"


def test_html_has_fp_overlay_and_toggle_default_off():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="fpOverlay"' in html
    assert "pointer-events" in FP_CSS.read_text(encoding="utf-8")
    assert 'id="mpShowFootprint"' in html
    # default unchecked
    assert re.search(r'id="mpShowFootprint"(?!([^>]*checked))', html) or (
        'id="mpShowFootprint">' in html or 'id="mpShowFootprint">' in html.replace(" ", "")
    )
    assert 'checked' not in re.search(
        r'<input[^>]*id="mpShowFootprint"[^>]*>', html
    ).group(0)
    assert "footprint_candles.js" in html
    assert "footprint_candles.css" in html


def test_js_gates_and_no_fetch_when_disabled_contract():
    js = FP_JS.read_text(encoding="utf-8")
    assert "barSpacing >= 60" in js or "FULL_BAR = 60" in js
    assert "DELTA_BAR = 40" in js
    assert "FULL_LEVEL_H = 11" in js
    assert "COMPACT_BAR" in js
    assert "DISPLAY_STEPS" in js
    assert "historyInflight" in js
    assert "formingInflight" in js
    assert "AbortController" in js
    assert "pointer-events" in FP_CSS.read_text(encoding="utf-8")
    assert "function enable" in js and "function disable" in js
    assert "clearCanvas" in js
    assert "if (!state.enabled) return" in js or "if (!state.enabled ||" in js
    # MISSING draws no levels path
    assert 'coverage === "MISSING"' in js
    # imbalance only when COMPLETE in draw path
    assert 'candle.coverage === "COMPLETE"' in js
    # Candle bodies hidden via transparent colors, never series.visible=false
    assert "savedCandleStyle" in js
    assert "applyOptions" in js
    assert "rgba(0, 0, 0, 0)" in js or "rgba(0,0,0,0)" in js
    assert "visible: false" not in js
    assert "restoreCandleBodies" in js
    assert "hideCandleBodies" in js


def test_mp_hooks_are_thin():
    mp = MP_JS.read_text(encoding="utf-8")
    assert "FOOTPRINT_HOOK" in mp
    assert "FootprintCandles" in mp
    # no imbalance math in MP app
    assert "IMBALANCE_RATIO" not in mp
    assert "bucket_index" not in mp
    assert "/api/footprint-candles" not in mp


def test_fp_css_below_mp_overlay_zindex():
    css = FP_CSS.read_text(encoding="utf-8")
    assert "z-index: 3" in css
    mp_css = (DASHBOARD_DIR / "static" / "market_profile_v1" / "style.css").read_text(
        encoding="utf-8"
    )
    assert "z-index: 4" in mp_css


def test_avr_panel_and_oc_ticks_contract():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="fpAvrPanel"' in html
    assert 'id="fpShowAvrPanel"' in html
    assert 'id="fpAvrExpand"' in html
    assert "ERWEITERT" in html
    # Panel must sit outside the chart canvas stack (not over candles)
    assert html.index('id="fpAvrPanel"') < html.index('id="mpChartStack"')
    assert 'id="fpAvrPanel"' not in html[html.index('id="price-pane"') : html.index('id="mpOverlay"')]
    js = FP_JS.read_text(encoding="utf-8")
    assert "drawCandleSilhouette" in js
    assert "AVR_BADGES" in js
    assert "S CTRL" in js
    assert "S ABS" in js
    assert "VAC ↓" in js
    assert "DATA?" not in js.split("AVR_BADGES")[1].split("}")[0]
    assert "updateAvrPanel" in js
    assert "panelExpanded" in js
    assert "STATE_STRIP_H" in js
    assert "dominant_state" in js
    assert "dominant_strength" in js
    assert "state_share" in js
    assert "state_counts" in js
    assert "final_state" in js
    assert "third_states" in js or "EARLY" in js
    assert "EARLY" in js and "MIDDLE" in js and "LATE" in js
    assert "PROVISIONAL" in js or "Forming" in js
    assert "UNVERIFIED" in js
    assert "placeLabel" in js
    css = FP_CSS.read_text(encoding="utf-8")
    assert "position: relative" in css
    assert "fp-avr-expand" in css

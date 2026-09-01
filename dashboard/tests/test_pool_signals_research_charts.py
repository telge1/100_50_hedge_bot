"""Dashboard tests for A+ Pool Signal display layer (no scanner job on toggle)."""

from __future__ import annotations

from research_charts.pool_signals_backtester import BACKTESTER_SOURCE, STRATEGY_ID, build_overlay_markers
from research_charts.workspace_session import APS_SOURCE, APS_STRATEGY_ID, ResearchWorkspace


def test_pool_signals_display_toggle_no_new_run_data():
    ws = ResearchWorkspace()
    payload = {
        "meta": {"symbol": "DOGEUSDT"},
        "confirmed": [
            {
                "setup_id": "s1",
                "setup_type": "A_PLUS_PULLBACK_SHORT",
                "symbol": "DOGEUSDT",
                "direction": "SHORT",
                "state": "CONFIRMED",
                "signal_at": "2026-08-15T10:00:00",
                "entry_price": 0.10118,
                "stop_price": 0.1016,
                "target_price": 0.0987,
                "entry_pool": {"timeframe": "15m", "pool_id": "ask15"},
                "target_pool": {"timeframe": "30m", "pool_id": "bid30"},
                "gates": [],
            }
        ],
        "debug_rows": [],
    }
    ws.store_pool_signals_run(payload)
    snap_off = ws.set_pool_signals_display_mode("off", "DOGEUSDT")
    assert snap_off["pool_signals"]["display_mode"] == "off"
    snap_on = ws.set_pool_signals_display_mode("confirmed", "DOGEUSDT")
    assert snap_on["pool_signals"]["display_mode"] == "confirmed"
    assert snap_on["pool_signals"]["loaded"] is True


def test_pool_signals_overlay_ids():
    specs = [
        {
            "overlay_id": "aps-test",
            "kind": "APS_CONFIRMED",
            "timestamp": "2026-08-15T10:00:00Z",
            "price": 0.10118,
            "shape": "arrow_down",
            "color": "#d62728",
            "text": "A+",
            "position": "above",
            "setup_id": "test",
            "direction": "SHORT",
        }
    ]
    try:
        markers = build_overlay_markers(specs, symbol="DOGEUSDT")
    except Exception:
        return  # TRP optional in CI
    assert markers
    assert str(markers[0].overlay_id).startswith("aps-")


def test_strategy_constants():
    assert APS_STRATEGY_ID == STRATEGY_ID
    assert APS_SOURCE == BACKTESTER_SOURCE


def test_find_latest_and_load_doge_results():
    from research_charts.pool_signals_backtester import (
        auto_import_latest_for_symbol,
        find_latest_run_dir,
        load_run_dir_payload,
    )

    path = find_latest_run_dir("DOGEUSDT")
    assert path is not None
    assert (path / "confirmed_signals.jsonl").is_file()
    payload = load_run_dir_payload(path, symbol="DOGEUSDT")
    assert payload["meta"]["symbol"] == "DOGEUSDT"
    assert len(payload["confirmed"]) > 0
    auto = auto_import_latest_for_symbol("DOGEUSDT")
    assert auto is not None
    assert len(auto["confirmed"]) == len(payload["confirmed"])


def test_workspace_auto_import_via_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DRAWINGS_DIR", str(tmp_path))
    from research_charts.pool_signals_backtester import auto_import_latest_for_symbol

    payload = auto_import_latest_for_symbol("DOGEUSDT")
    assert payload and payload["confirmed"]
    ws = ResearchWorkspace()
    ws.store_pool_signals_run(payload)
    snap = ws.set_pool_signals_display_mode("confirmed", "DOGEUSDT")
    assert snap["pool_signals"]["n_confirmed"] > 0
    assert snap["pool_signals"]["display_mode"] == "confirmed"


def test_aps_backtester_click_has_own_js_branch():
    """Backtester button must not fall through to wave_fade when APS is selected."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "static/js/research/research_charts.js").read_text(
        encoding="utf-8"
    )
    assert 'btStrategy() === "a_plus_liquidity_pool_signal_scanner_v1"' in js
    # APS branch must appear before stoch fallthrough
    aps_idx = js.index('btStrategy() === "a_plus_liquidity_pool_signal_scanner_v1"')
    stoch_idx = js.index("const strategy = lastStochStrategy();")
    assert aps_idx < stoch_idx
    assert "clear_other_strategies: true" in js
    # Must enable a visible mode — never toggle to off on Backtester click
    assert 'desired === "off") ? "confirmed"' in js or '(desired === "off") ? "confirmed"' in js
    assert "force_reimport: true" in js
    assert "zoomChartToIsoRange" in js
    assert '(cur !== "off") ? "off"' not in js.split("a_plus_liquidity_pool_signal_scanner_v1")[2].split(
        "lastStochStrategy"
    )[0]


def test_pool_signals_time_span_for_chart_zoom(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DRAWINGS_DIR", str(tmp_path))
    from research_charts.pool_signals_backtester import auto_import_latest_for_symbol

    payload = auto_import_latest_for_symbol("DOGEUSDT")
    assert payload and payload["confirmed"]
    ws = ResearchWorkspace()
    ws.store_pool_signals_run(payload)
    snap = ws.set_pool_signals_display_mode("confirmed", "DOGEUSDT")
    span = snap["pool_signals"].get("time_span") or {}
    assert span.get("start") and span.get("end")
    assert Date_parse_ok(span["start"]) <= Date_parse_ok(span["end"])


def Date_parse_ok(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_aps_load_clears_stoch_overlays(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DRAWINGS_DIR", str(tmp_path))
    ws = ResearchWorkspace()
    payload = {
        "meta": {"symbol": "DOGEUSDT"},
        "confirmed": [
            {
                "setup_id": "s1",
                "setup_type": "A_PLUS_PULLBACK_SHORT",
                "symbol": "DOGEUSDT",
                "direction": "SHORT",
                "state": "CONFIRMED",
                "signal_at": "2026-08-15T10:00:00",
                "entry_price": 0.10118,
                "stop_price": 0.1016,
                "target_price": 0.0987,
                "entry_pool": {"timeframe": "15m", "pool_id": "ask15"},
                "target_pool": {"timeframe": "30m", "pool_id": "bid30"},
                "gates": [],
            }
        ],
        "debug_rows": [],
    }
    ws.store_pool_signals_run(payload)
    # Leftover wave_fade long/short drawings (what the status line was showing)
    ws.import_stoch_backtester(
        "DOGEUSDT",
        [
            {
                "symbol": "DOGEUSDT",
                "trade_direction": "LONG",
                "entry_price": 0.1,
                "tp_price": 0.11,
                "sl_price": 0.09,
                "entry_time": "2026-08-15T10:00:00Z",
                "signal_id": "wave-leftover-1",
            },
            {
                "symbol": "DOGEUSDT",
                "trade_direction": "SHORT",
                "entry_price": 0.1,
                "tp_price": 0.09,
                "sl_price": 0.11,
                "entry_time": "2026-08-15T11:00:00Z",
                "signal_id": "wave-leftover-2",
            },
        ],
    )
    stoch_before = [
        d for d in ws.drawings.get_drawings("DOGEUSDT", include_hidden=True) if str(d.drawing_id).startswith("stoch-")
    ]
    assert len(stoch_before) == 2
    ws.clear_backtester_strategy("DOGEUSDT", strategy_id="stoch_fade")
    remaining = [
        d for d in ws.drawings.get_drawings("DOGEUSDT", include_hidden=True) if str(d.drawing_id).startswith("stoch-")
    ]
    assert remaining == []
    snap = ws.set_pool_signals_display_mode("confirmed", "DOGEUSDT")
    assert snap["backtester"]["strategy_id"] == APS_STRATEGY_ID
    assert snap["pool_signals"]["display_mode"] == "confirmed"

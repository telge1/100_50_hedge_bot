"""BT-D36 — exact-origin density survives superseded window fetches (GO TO / viewport race)."""

from __future__ import annotations

from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def test_window_fetch_commits_cache_when_load_serial_superseded():
    js = _read("static/js/research/pd1_exact_origin_density_research_v1.js")
    assert "scheduleSupersededDensityRefresh" in js
    assert "if (loadSerial === pane.exactOriginLoadSerial) fetched += 1" in js
    assert "if (reqReplayGen != null && deps.state.replayGen !== reqReplayGen) return" in js
    assert "scheduleSupersededDensityRefresh(pane, opts)" in js
    # Must not discard successful HTTP bodies solely due to loadSerial before cache write.
    idx = js.index("const data = await fetchWindow(w.fromMs, w.toMs, scope);")
    chunk = js[idx : idx + 800]
    assert "if (loadSerial !== pane.exactOriginLoadSerial) return" not in chunk


def test_go_to_suppresses_viewport_density_refresh_during_await():
    host = _read("static/js/research/research_charts.js")
    assert "_suppressExactOriginViewport = true" in host
    assert "phase6xt-bt-d36-density-supersede-v1" in host
    mod = _read("static/js/research/pd1_exact_origin_density_research_v1.js")
    assert "pane._suppressExactOriginViewport" in mod


def test_asset_bust_bt_d36():
    html = _read("templates/research_charts.html")
    assert "phase6xt-bt-d36-density-supersede-v1" in html

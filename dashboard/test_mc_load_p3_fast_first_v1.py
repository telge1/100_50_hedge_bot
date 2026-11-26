"""MC-LOAD-P3 — static regression hooks for fast-first two-phase pane load."""

from pathlib import Path

DASH = Path(__file__).resolve().parent
RC = DASH / "static/js/research/research_charts.js"
CHART = DASH / "static/research_trp/chart.js"
DIAG = DASH / "static/js/research/mc_load_diag_v1.js"
HTML = DASH / "templates/research_charts.html"


def test_mc_load_p3_fast_first_symbols_present():
    js = RC.read_text(encoding="utf-8")
    assert "mcLoadFastFirstEnabled" in js
    assert "resolveMcLoadPhaseA" in js
    assert "mcLoadOptsPinsHistoryWindow" in js
    assert "mcLoadFastFirstFallbackReason" in js
    assert "effective_fast_first" in js
    assert "buildMcLoadPhaseADecision" in js
    assert "mc_load_phase_a_decision" in js
    assert "mcLoadFastFirstPaneRange" in js
    assert "schedulePaneHistoryPhaseB" in js
    assert "history-phase-b" in js
    assert "mcLoadPostLoadDeferred" in js
    assert "first_candles_owner_release" in js
    assert "mcLoadDiagEmitHttp" in js
    assert "afterPaneBundle" in js
    assert "mcLoadSkipEmaCatchUpAfterPaneBundle" in js
    assert "mcLoadSnapshotHistoryLimit" in js
    assert "mcLoadDiagPushPhaseDecision" in js
    assert "abortStalePaneWork" in js
    assert "preserveViewTime" in CHART.read_text(encoding="utf-8")


def test_mc_load_p3_cache_bust_in_template():
    html = HTML.read_text(encoding="utf-8")
    d = DIAG.read_text(encoding="utf-8")
    assert "research-mc-load-p4i-v1" in html
    assert "mc-load-p4i-v1" in html
    assert "phase_decisions" in d


def test_mc_load_p3_diag_timing_keys():
    d = DIAG.read_text(encoding="utf-8")
    assert "timing_key" in d
    assert "render_proxy_scheduled" in d
    assert "_sec" in d
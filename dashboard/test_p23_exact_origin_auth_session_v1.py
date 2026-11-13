"""DOGE-LIVE-P23 — Exact-Origin HTTP-401 session hint + recovery wiring (regression strings)."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
JS = DASH / "static/js/research/pd1_exact_origin_density_research_v1.js"
HTML = DASH / "templates/research_charts.html"


def test_p23_auth_strings_and_pause_gate_in_js():
    js = JS.read_text(encoding="utf-8")
    assert "EXACT_ORIGIN_AUTH_HINT" in js
    assert "Sitzung abgelaufen" in js
    assert "authSessionPaused" in js
    assert "authBatchAbort" in js
    assert "authFailures" in js
    assert "windowHttpError" in js
    assert "err.authFailure" in js
    assert "probeSessionAuthOk" in js
    assert "scheduleAuthSessionRecoveryWatch" in js
    assert "exactOriginFailureHint" in js
    assert "_p23AuthTestHooks" in js
    assert 'if (fetchStats.fetched > 0 && fetchStats.authFailures === 0) authSessionPaused = false' in js


def test_p23_cache_bust_in_research_charts_html():
    html = HTML.read_text(encoding="utf-8")
    assert "pd1-p24f-1h-4h-bounds-v1" in html


def test_p23_auth_window_fetch_uses_same_origin_credentials():
    js = JS.read_text(encoding="utf-8")
    assert 'credentials: "same-origin"' in js
    idx = js.find("async function fetchWindow")
    assert idx >= 0
    chunk = js[idx : idx + 2500]
    assert "windowHttpError" in chunk

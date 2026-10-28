"""BT-D86A: BT-D85 July research overlay in Research Chart backtester."""

from __future__ import annotations


def test_html_dropdown_option() -> None:
    html = open("templates/research_charts.html", encoding="utf-8").read()
    assert "bt_d85_july_ema_v3_no_pause_research" in html
    assert "BT-D85 · Juli 2026 · EMA V3 · 25 Trades" in html


def test_js_wiring() -> None:
    js = open("static/js/research/research_charts.js", encoding="utf-8").read()
    assert "isBtD85JulyResearchStrategy" in js
    assert "applyBtD85JulyResearchBacktester" in js
    assert "bt-d85-july/load" in js


def test_api_routes_registered() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    dash = Path(__file__).resolve().parent
    env = {
        **os.environ,
        "PYTHONPATH": ":".join([str(dash.parent), str(dash), "/home/telgenbuescher/projects/pools+ob+delta_bot"]),
    }
    code = """
import app
paths = sorted({r.path for r in app.app.routes if hasattr(r, 'path') and 'bt-d85-july' in r.path})
print('\\n'.join(paths))
"""
    raw = subprocess.check_output([sys.executable, "-c", code], cwd=str(dash), env=env, text=True)
    assert "/api/research/backtester/bt-d85-july/meta" in raw
    assert "/api/research/backtester/bt-d85-july/load" in raw


def test_ledger_validation() -> None:
    from research_charts.bt_d85_july_research_backtester import (
        EXPECTED_FILLS,
        EXPECTED_SL,
        EXPECTED_TP,
        build_drawing_specs,
        load_trade_ledger,
        validate_ledger,
    )

    rows, _path, errs = load_trade_ledger()
    assert not errs
    assert len(rows) == EXPECTED_FILLS == 25
    val = validate_ledger(rows)
    assert not val
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    assert tp == EXPECTED_TP == 10
    assert sl == EXPECTED_SL == 15
    positions, links = build_drawing_specs(rows)
    assert len(positions) == 25
    assert len(links) == 25


def test_workspace_load_idempotent() -> None:
    from research_charts.bt_d85_july_research_backtester import (
        build_summary,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    rows, path, _ = load_trade_ledger()
    summary = build_summary(rows, ledger_path=path)
    ws = ResearchWorkspace()
    sym = "DOGEUSDT"
    snap1 = ws.load_bt_d85_july_backtester(sym, rows, summary, visible=True)
    n1 = snap1["backtester"]["loaded"]
    snap2 = ws.load_bt_d85_july_backtester(sym, rows, summary, visible=True)
    n2 = snap2["backtester"]["loaded"]
    assert n1 == n2 == 50
    drawings = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if (d.metadata or {}).get("origin") == "bt_d85_july_chart_overlay_v1"
    ]
    assert len(drawings) == 50
    ws.clear_bt_d85_july_backtester(sym)
    drawings_after = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if (d.metadata or {}).get("origin") == "bt_d85_july_chart_overlay_v1"
    ]
    assert not drawings_after

"""LONG V3 Phase 3G — September 2026 SL cap 1.2% chart overlay."""

from __future__ import annotations

import os
from pathlib import Path

POOLS = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")
DASH = Path(__file__).resolve().parent


def test_html_dropdown_option() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "long_v3_phase3g_sept_2026_sl_cap_1p2_research" in html
    assert "LONG V3 — September 2026 — SL max. 1,2 %" in html


def test_js_frozen_month_wiring() -> None:
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "long_v3_phase3g_sept_2026_sl_cap_1p2_research" in js
    assert "/api/research/backtester/bt-long-v3-phase3g-sept-sl-cap/load" in js


def test_api_routes_registered() -> None:
    api = (DASH / "research_charts/api.py").read_text(encoding="utf-8")
    assert "bt-long-v3-phase3g-sept-sl-cap/load" in api
    assert "long_v3_phase3g_sept_2026_sl_cap_1p2_research" in api


def test_ledger_september_slice() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("long_v3_phase3g_sept_sl_cap")
    rows, _path, errs = load_trade_ledger(prof)
    assert not errs, errs
    assert validate_ledger(prof, rows) == []
    assert len(rows) == 15
    assert all(str(r["fill_time_utc"]).startswith("2026-09") for r in rows)
    assert all(r["fill_time_utc"] <= "2026-09-30T23:40:00Z" for r in rows)
    tp = sum(1 for r in rows if r["exit_reason"] in prof.tp_exit_reasons)
    sl = sum(1 for r in rows if r["exit_reason"] in prof.sl_exit_reasons)
    assert tp == 10
    assert sl == 5


def test_sl_cap_visible_on_capped_trades() -> None:
    from research_charts.frozen_month_research_overlay_v1 import get_profile, load_trade_ledger

    prof = get_profile("long_v3_phase3g_sept_sl_cap")
    rows, _, _ = load_trade_ledger(prof)
    capped = 0
    for r in rows:
        e = float(r["entry_price"])
        s = float(r["sl_price"])
        dist = (e - s) / e * 100.0
        if abs(dist - 1.2) < 0.02:
            capped += 1
    assert capped >= 5


def test_workspace_load_30_drawings_no_duplicate() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    prof = get_profile("long_v3_phase3g_sept_sl_cap")
    rows, path, errs = load_trade_ledger(prof)
    assert not errs
    summary = build_summary(prof, rows, ledger_path=path)
    ws = ResearchWorkspace()
    ws.drawings.replace_drawings([])
    sym = "DOGEUSDT"
    s1 = ws.load_frozen_month_research_backtester(
        "long_v3_phase3g_sept_sl_cap", sym, rows, summary, visible=True
    )
    s2 = ws.load_frozen_month_research_backtester(
        "long_v3_phase3g_sept_sl_cap", sym, rows, summary, visible=True
    )
    assert s1["backtester"]["loaded"] == 30
    assert s2["backtester"]["loaded"] == 30


def test_phase3a_unchanged() -> None:
    from research_charts.frozen_month_research_overlay_v1 import get_profile, load_trade_ledger, validate_ledger

    prof = get_profile("long_v3_phase3a")
    rows, _, errs = load_trade_ledger(prof)
    assert not errs
    assert validate_ledger(prof, rows) == []
    assert len(rows) == 36

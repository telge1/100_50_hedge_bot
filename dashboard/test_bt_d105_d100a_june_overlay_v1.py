"""BT-D105: D100-A June gap-anchored overlay on Research Chart backtester."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
POOLS = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")
D100_LEDGER = (
    POOLS
    / "results/phase_bt_d100_gap_anchored_research_v1/d100_a_june/trade_ledger.json"
)


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": ":".join([str(DASH.parent), str(DASH), str(POOLS)]),
    }


def test_html_d100a_dropdown_option() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "bt_d100a_june_gap_anchor_research" in html
    assert "BT-D100-A · Juni 2026 · Gap-Anker · 14 Trades" in html
    assert html.index("bt_d94_june_ema_v3_no_pause_research") < html.index(
        "bt_d100a_june_gap_anchor_research"
    )


def test_js_d100a_wiring() -> None:
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "bt_d100a_june_gap_anchor_research" in js
    assert "/api/research/backtester/bt-d100a-june/load" in js


def test_api_routes_registered() -> None:
    code = """
import app
paths = sorted({r.path for r in app.app.routes if hasattr(r, 'path')})
for p in paths:
    if 'bt-d100a-june' in p:
        print(p)
"""
    raw = subprocess.check_output(
        [sys.executable, "-c", code], cwd=str(DASH), env=_env(), text=True
    )
    assert "/api/research/backtester/bt-d100a-june/meta" in raw
    assert "/api/research/backtester/bt-d100a-june/load" in raw


def test_d100a_ledger_load_counts_and_drawings() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_drawing_specs,
        build_summary,
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("june_d100a")
    assert prof.strategy_id == "bt_d100a_june_gap_anchor_research"
    rows, path, errs = load_trade_ledger(prof)
    assert not errs, errs
    assert path.is_file()
    assert len(rows) == 14
    val = validate_ledger(prof, rows)
    assert not val, val
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    assert tp == 3
    assert sl == 11
    summary = build_summary(prof, rows, ledger_path=path)
    assert summary["drawing_count"] == 28
    positions, links = build_drawing_specs(prof, rows)
    assert len(positions) == 14
    assert len(links) == 14
    assert len(positions) + len(links) == 28
    for spec in positions:
        assert spec["start"].tzinfo == timezone.utc
        assert spec["metadata"]["origin"] == "bt_d100a_june_chart_overlay_v1"


def test_june_original_unchanged_34_trades() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("june")
    rows, _, errs = load_trade_ledger(prof)
    assert not errs
    assert len(rows) == 34
    assert not validate_ledger(prof, rows)


def test_switch_june_original_vs_d100a_workspace() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    sym = "DOGEUSDT"
    ws = ResearchWorkspace()

    for key, expected_loaded, origin in (
        ("june", 68, "bt_d94_june_chart_overlay_v1"),
        ("june_d100a", 28, "bt_d100a_june_chart_overlay_v1"),
        ("july", 50, "bt_d85_july_chart_overlay_v1"),
    ):
        prof = get_profile(key)
        rows, path, _ = load_trade_ledger(prof)
        summary = build_summary(prof, rows, ledger_path=path)
        snap = ws.load_frozen_month_research_backtester(key, sym, rows, summary, visible=True)
        assert snap["backtester"]["loaded"] == expected_loaded
        origins = {
            (d.metadata or {}).get("origin")
            for d in ws.drawings.get_drawings(sym, include_hidden=True)
        }
        assert origins == {origin}


def test_repeat_load_no_duplicate_drawings() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    sym = "DOGEUSDT"
    ws = ResearchWorkspace()
    prof = get_profile("june_d100a")
    rows, path, _ = load_trade_ledger(prof)
    summary = build_summary(prof, rows, ledger_path=path)
    snap1 = ws.load_frozen_month_research_backtester("june_d100a", sym, rows, summary, visible=True)
    snap2 = ws.load_frozen_month_research_backtester("june_d100a", sym, rows, summary, visible=True)
    assert snap1["backtester"]["loaded"] == 28
    assert snap2["backtester"]["loaded"] == 28
    d100a = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if str(getattr(d, "drawing_id", "")).startswith("btd100a-")
    ]
    assert len(d100a) == 28


def test_navigation_anchors_no_fictional_june9_2350_fill() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        get_profile,
        load_trade_ledger,
    )

    rows, _, _ = load_trade_ledger(get_profile("june_d100a"))
    fill_times = {r["fill_time_utc"] for r in rows}
    assert "2026-06-09T23:50:00Z" not in fill_times
    assert "2026-06-13T00:30:00Z" in fill_times
    assert "2026-06-24T08:20:00Z" in fill_times
    for ts in fill_times:
        datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_bundled_fallback_exists() -> None:
    assert (
        DASH / "research_charts/data/bt_d100a_june_trade_ledger.json"
    ).is_file()
    assert D100_LEDGER.is_file()


def test_legacy_load_delegates_d100a_strategy_id() -> None:
    from research_charts.frozen_month_research_overlay_v1 import STRATEGY_ID_TO_KEY

    assert STRATEGY_ID_TO_KEY["bt_d100a_june_gap_anchor_research"] == "june_d100a"

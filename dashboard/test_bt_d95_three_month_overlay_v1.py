"""BT-D95: June / July / August frozen research overlays on Research Chart backtester."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
POOLS = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": ":".join([str(DASH.parent), str(DASH), str(POOLS)]),
    }


def test_html_three_month_dropdown_options() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "bt_d94_june_ema_v3_no_pause_research" in html
    assert "BT-D94R · Juni 2026 · EMA V3 · 34 Trades" in html
    assert "bt_d85_july_ema_v3_no_pause_research" in html
    assert "BT-D85 · Juli 2026 · EMA V3 · 25 Trades" in html
    assert "bt_d91_august_ema_v3_no_pause_research" in html
    assert "BT-D91 · August 2026 · EMA V3 · 25 Trades" in html
    assert "bt-d95-three-month-overlay-v1" in html


def test_js_frozen_month_wiring() -> None:
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "BT_FROZEN_MONTH_RESEARCH" in js
    assert "isBtFrozenMonthResearchStrategy" in js
    assert "applyBtFrozenMonthResearchBacktester" in js
    assert "clearAllBtFrozenMonthResearchLayers" in js
    assert "/api/research/backtester/bt-d94-june/load" in js
    assert "/api/research/backtester/bt-d85-july/load" in js
    assert "/api/research/backtester/bt-d91-august/load" in js


def test_api_routes_registered() -> None:
    code = """
import app
paths = sorted({r.path for r in app.app.routes if hasattr(r, 'path')})
for p in paths:
    if 'bt-d94-june' in p or 'bt-d91-august' in p or 'bt-d85-july' in p:
        print(p)
"""
    raw = subprocess.check_output(
        [sys.executable, "-c", code], cwd=str(DASH), env=_env(), text=True
    )
    assert "/api/research/backtester/bt-d94-june/meta" in raw
    assert "/api/research/backtester/bt-d94-june/load" in raw
    assert "/api/research/backtester/bt-d91-august/meta" in raw
    assert "/api/research/backtester/bt-d91-august/load" in raw
    assert "/api/research/backtester/bt-d85-july/meta" in raw
    assert "/api/research/backtester/bt-d85-july/load" in raw


@pytest.mark.parametrize(
    "month_key,expected_fills,expected_tp,expected_sl,expected_loaded,origin",
    [
        ("june", 34, 9, 25, 68, "bt_d94_june_chart_overlay_v1"),
        ("july", 25, 10, 15, 50, "bt_d85_july_chart_overlay_v1"),
        ("august", 25, 4, 21, 50, "bt_d91_august_chart_overlay_v1"),
    ],
)
def test_ledger_validation_and_drawings(
    month_key: str,
    expected_fills: int,
    expected_tp: int,
    expected_sl: int,
    expected_loaded: int,
    origin: str,
) -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_drawing_specs,
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile(month_key)
    rows, path, errs = load_trade_ledger(prof)
    assert not errs, errs
    assert path.is_file()
    assert len(rows) == expected_fills
    val = validate_ledger(prof, rows)
    assert not val, val
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    assert tp == expected_tp
    assert sl == expected_sl
    positions, links = build_drawing_specs(prof, rows)
    assert len(positions) == expected_fills
    assert len(links) == expected_fills
    assert len(positions) + len(links) == expected_loaded


@pytest.mark.parametrize("month_key,expected_loaded", [("june", 68), ("july", 50), ("august", 50)])
def test_workspace_load_clears_other_months(month_key: str, expected_loaded: int) -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    sym = "DOGEUSDT"
    ws = ResearchWorkspace()
    for key in ("july", "june", "august"):
        prof = get_profile(key)
        rows, path, _ = load_trade_ledger(prof)
        summary = build_summary(prof, rows, ledger_path=path)
        ws.load_frozen_month_research_backtester(key, sym, rows, summary, visible=True)

    prof = get_profile(month_key)
    rows, path, _ = load_trade_ledger(prof)
    summary = build_summary(prof, rows, ledger_path=path)
    snap = ws.load_frozen_month_research_backtester(month_key, sym, rows, summary, visible=True)
    assert snap["backtester"]["loaded"] == expected_loaded
    origins = {
        (d.metadata or {}).get("origin")
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
    }
    assert origins == {get_profile(month_key).backtester_source}


def test_bundled_fallback_files_exist() -> None:
    data = DASH / "research_charts/data"
    for name in (
        "bt_d94_june_trade_ledger_no_pause.json",
        "bt_d85_july_trade_ledger_no_pause.json",
        "bt_d91_august_trade_ledger_no_pause.json",
    ):
        assert (data / name).is_file()


def test_legacy_load_delegates_frozen_strategy_ids() -> None:
    from research_charts.frozen_month_research_overlay_v1 import STRATEGY_ID_TO_KEY

    assert STRATEGY_ID_TO_KEY["bt_d94_june_ema_v3_no_pause_research"] == "june"
    assert STRATEGY_ID_TO_KEY["bt_d85_july_ema_v3_no_pause_research"] == "july"
    assert STRATEGY_ID_TO_KEY["bt_d91_august_ema_v3_no_pause_research"] == "august"

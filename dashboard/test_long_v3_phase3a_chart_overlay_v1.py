"""LONG V3 Phase 3A — Research Chart overlay (frozen-month infrastructure)."""

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
        "PYTHONPATH": ":".join(
            [
                str(DASH.parent),
                str(DASH),
                str(POOLS),
            ]
        ),
    }


def test_html_long_v3_dropdown_option() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "long_v3_phase3a_jun_sep_frozen_a_research" in html
    assert "LONG V3 — Phase 3A — Juni–September 2026 — Frozen A" in html


def test_js_frozen_month_map_includes_long_v3() -> None:
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "long_v3_phase3a_jun_sep_frozen_a_research" in js
    assert "/api/research/backtester/bt-long-v3-phase3a/load" in js


def test_api_routes_registered() -> None:
    api = (DASH / "research_charts/api.py").read_text(encoding="utf-8")
    assert "bt-long-v3-phase3a/load" in api
    assert "long_v3_phase3a_jun_sep_frozen_a_research" in api


def test_ledger_load_and_counts() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_drawing_specs,
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("long_v3_phase3a")
    rows, _path, errs = load_trade_ledger(prof)
    assert not errs, errs
    assert validate_ledger(prof, rows) == []
    assert len(rows) == 36
    tp = sum(1 for r in rows if r["exit_reason"] in prof.tp_exit_reasons)
    sl = sum(1 for r in rows if r["exit_reason"] in prof.sl_exit_reasons)
    assert tp == 23
    assert sl == 13
    positions, links = build_drawing_specs(prof, rows)
    assert len(positions) == 36
    assert len(links) == 36
    assert all(p["drawing_type"] == "long_position" for p in positions)
    assert len({p["drawing_id"] for p in positions}) == 36


def test_monthly_trade_distribution() -> None:
    from collections import Counter

    from research_charts.frozen_month_research_overlay_v1 import get_profile, load_trade_ledger

    prof = get_profile("long_v3_phase3a")
    rows, _, errs = load_trade_ledger(prof)
    assert not errs
    by_month = Counter(r["fill_time_utc"][:7] for r in rows)
    assert by_month["2026-06"] == 8
    assert by_month["2026-07"] == 6
    assert by_month["2026-08"] == 7
    assert by_month["2026-09"] == 15


def test_long_geometry_tp_and_sl_samples() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_drawing_specs,
        get_profile,
        load_trade_ledger,
    )

    prof = get_profile("long_v3_phase3a")
    rows, _, _ = load_trade_ledger(prof)
    positions, _ = build_drawing_specs(prof, rows)
    by_id = {p["metadata"]["position_id"]: p for p in positions}
    tp_row = next(r for r in rows if r["exit_reason"] == "FIXED_UPPER_TP")
    sl_row = next(r for r in rows if r["exit_reason"] == "STRUCTURAL_SL")
    tp_box = by_id[tp_row["position_id"]]
    sl_box = by_id[sl_row["position_id"]]
    assert tp_box["target"] > tp_box["entry"] > tp_box["stop"]
    assert sl_box["target"] > sl_box["entry"] > sl_box["stop"]


def test_workspace_load_creates_72_drawings() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    prof = get_profile("long_v3_phase3a")
    rows, path, errs = load_trade_ledger(prof)
    assert not errs
    summary = build_summary(prof, rows, ledger_path=path)
    ws = ResearchWorkspace()
    sym = "DOGEUSDT"
    snap = ws.load_frozen_month_research_backtester("long_v3_phase3a", sym, rows, summary, visible=True)
    assert snap["backtester"]["loaded"] == 72
    assert snap["backtester"]["strategy_id"] == prof.strategy_id
    prefixed = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if str(getattr(d, "drawing_id", "")).startswith(prof.box_prefix)
        or str(getattr(d, "drawing_id", "")).startswith(prof.link_prefix)
    ]
    assert len(prefixed) == 72


def test_repeat_load_no_duplicate_stack() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    prof = get_profile("long_v3_phase3a")
    rows, path, _ = load_trade_ledger(prof)
    summary = build_summary(prof, rows, ledger_path=path)
    ws = ResearchWorkspace()
    sym = "DOGEUSDT"
    s1 = ws.load_frozen_month_research_backtester("long_v3_phase3a", sym, rows, summary, visible=True)
    s2 = ws.load_frozen_month_research_backtester("long_v3_phase3a", sym, rows, summary, visible=True)
    assert s1["backtester"]["loaded"] == 72
    assert s2["backtester"]["loaded"] == 72


def test_june_short_overlay_still_validates() -> None:
    from research_charts.frozen_month_research_overlay_v1 import get_profile, load_trade_ledger, validate_ledger

    prof = get_profile("june")
    rows, _, errs = load_trade_ledger(prof)
    assert not errs
    assert validate_ledger(prof, rows) == []

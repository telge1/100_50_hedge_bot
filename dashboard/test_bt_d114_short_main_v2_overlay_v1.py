"""BT-D114: SHORT MAIN V2 Phase 3 variant C overlay on Research Chart backtester."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DASH = Path(__file__).resolve().parent
POOLS = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")

MONTHS = (
    ("june", "short_main_v2_june_combined_exit_research", 10, 4, 4, 2, "bt-short-main-v2-june"),
    ("july", "short_main_v2_july_combined_exit_research", 8, 4, 1, 3, "bt-short-main-v2-july"),
    ("august", "short_main_v2_august_combined_exit_research", 3, 3, 0, 0, "bt-short-main-v2-august"),
    (
        "september",
        "short_main_v2_september_partial_combined_exit_research",
        6,
        4,
        0,
        2,
        "bt-short-main-v2-september-partial",
    ),
)


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": ":".join([str(DASH.parent), str(DASH), str(POOLS)]),
    }


def test_html_short_main_v2_dropdown_options() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    for _, sid, *_ in MONTHS:
        assert sid in html
    assert "September 2026 PARTIAL" in html
    assert html.index("bt_d108_aug_structural_sl_research") < html.index(
        "short_main_v2_june_combined_exit_research"
    )


def test_js_short_main_v2_wiring() -> None:
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    for _, sid, *_rest in MONTHS:
        assert sid in js
    assert "/api/research/backtester/bt-short-main-v2-june/load" in js
    assert "/api/research/backtester/bt-short-main-v2-september-partial/load" in js


def test_api_routes_registered() -> None:
    code = """
import app
paths = sorted({r.path for r in app.app.routes if hasattr(r, 'path')})
for p in paths:
    if 'bt-short-main-v2' in p:
        print(p)
"""
    raw = subprocess.check_output(
        [sys.executable, "-c", code], cwd=str(DASH), env=_env(), text=True
    )
    for _m, _sid, *_rest in MONTHS:
        route_slug = _rest[-1]
        assert f"/api/research/backtester/{route_slug}/meta" in raw
        assert f"/api/research/backtester/{route_slug}/load" in raw


def test_tp_segments_audit_only_no_synthetic_fill_without_place() -> None:
    from research_charts.short_main_v2_research_overlay_v1 import (
        assess_tp_limit_audit,
        tp_limit_segments_from_audit,
    )

    audit = [{"action": "TP_LIMIT_FILL", "bar_utc": "2026-06-01T12:00:00Z", "detail": {"price": 0.1}}]
    segs, notes = tp_limit_segments_from_audit(
        audit,
        exit_bar="2026-06-01T12:05:00Z",
        exit_reason="LOWER_POOL_GAP_TP",
    )
    assert segs == []
    assert "tp_fill_without_prior_place" in notes


def test_june_tp_partial_counts_from_real_ledger() -> None:
    from research_charts.short_main_v2_research_overlay_v1 import (
        assess_tp_limit_audit,
        build_summary,
        get_profile,
        load_trade_ledger,
    )

    prof = get_profile("june")
    rows, path, errs = load_trade_ledger(prof)
    assert not errs
    partial = sum(1 for r in rows if assess_tp_limit_audit(r)["tp_limit_audit_status"] == "partial")
    assert partial >= 1
    summary = build_summary(prof, rows, ledger_path=path)
    assert summary["tp_limit_partial_count"] == partial


def test_monthly_ledger_counts_and_drawings() -> None:
    from research_charts.short_main_v2_research_overlay_v1 import (
        build_drawing_specs,
        build_summary,
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    for month_key, strategy_id, trades, sl, gap, ema, _route in MONTHS:
        prof = get_profile(month_key)
        assert prof.strategy_id == strategy_id
        rows, path, errs = load_trade_ledger(prof)
        assert not errs, (month_key, errs)
        assert path.is_file()
        assert len(rows) == trades
        val = validate_ledger(prof, rows)
        assert not val, (month_key, val)
        assert sum(1 for r in rows if r.get("exit_reason") == "STRUCTURAL_SL") == sl
        assert sum(1 for r in rows if r.get("exit_reason") == "LOWER_POOL_GAP_TP") == gap
        assert sum(1 for r in rows if r.get("exit_reason") == "EMA59_200_CROSS") == ema
        summary = build_summary(prof, rows, ledger_path=path)
        assert summary["trade_count"] == trades
        if month_key == "september":
            assert summary["coverage"] == "PARTIAL"
        positions, links, tp_segs = build_drawing_specs(prof, rows)
        assert len(positions) == trades
        assert len(links) == trades
        pids = {str(r["position_id"]) for r in rows}
        assert len(pids) == trades
        for spec in positions:
            assert spec["start"].tzinfo == timezone.utc
            assert spec["metadata"]["origin"] == "short_main_v2_combined_exit_chart_overlay_v1"
            assert spec["metadata"]["position_id"] in pids


def test_workspace_load_and_switch_from_d108() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary as frozen_summary,
        get_profile as frozen_profile,
        load_trade_ledger as frozen_load,
    )
    from research_charts.short_main_v2_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    sym = "DOGEUSDT"
    ws = ResearchWorkspace()
    v2_prof = get_profile("august")
    v2_rows, v2_path, _ = load_trade_ledger(v2_prof)
    v2_sum = build_summary(v2_prof, v2_rows, ledger_path=v2_path)
    snap = ws.load_short_main_v2_research_backtester("august", sym, v2_rows, v2_sum, visible=True)
    positions, links, tp_segs = __import__(
        "research_charts.short_main_v2_research_overlay_v1", fromlist=["build_drawing_specs"]
    ).build_drawing_specs(v2_prof, v2_rows)
    expected_loaded = len(positions) + len(links) + len(tp_segs)
    assert snap["backtester"]["loaded"] == expected_loaded

    d108_prof = frozen_profile("august_d108_structural")
    d108_rows, d108_path, _ = frozen_load(d108_prof)
    d108_sum = frozen_summary(d108_prof, d108_rows, ledger_path=d108_path)
    ws.load_frozen_month_research_backtester("august_d108_structural", sym, d108_rows, d108_sum, visible=True)
    v2_left = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if str(getattr(d, "drawing_id", "")).startswith("btv2aug-")
    ]
    assert v2_left == []


def test_d108_still_validates() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("august_d108_structural")
    rows, _, errs = load_trade_ledger(prof)
    assert not errs
    assert not validate_ledger(prof, rows)


def test_legacy_load_delegates_strategy_ids() -> None:
    from research_charts.short_main_v2_research_overlay_v1 import STRATEGY_ID_TO_MONTH

    assert STRATEGY_ID_TO_MONTH["short_main_v2_june_combined_exit_research"] == "june"
    assert STRATEGY_ID_TO_MONTH["short_main_v2_september_partial_combined_exit_research"] == "september"


def test_bundled_ledgers_present() -> None:
    data = DASH / "research_charts/data"
    for month_key, *_ in MONTHS:
        bundled = data / f"short_main_v2_{month_key}_variant_c_trade_ledger.json"
        pools = (
            POOLS
            / f"results/phase_short_main_v2_combined_exit_v1/phase3_monthly/{month_key}/variant_c_trade_ledger.json"
        )
        assert pools.is_file(), month_key
        assert bundled.is_file(), month_key

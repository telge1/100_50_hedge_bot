"""BT-D111: D108 August structural-SL overlay on Research Chart backtester."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DASH = Path(__file__).resolve().parent
POOLS = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")
SOURCE_LEDGER = (
    POOLS
    / "results/phase_bt_d108_structural_sl_validation_oos_v1/d107_chronological_august/replay_full.json"
)
BUNDLED = DASH / "research_charts/data/bt_d108_aug_structural_sl_replay_full.json"
EXPECTED_SHA256 = "da4288c836e70e7d4af764377297efb53c9216e713fa5d1ef35217eb624c7600"
EXPECTED_SUM_GROSS = -1.882607


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": ":".join([str(DASH.parent), str(DASH), str(POOLS)]),
    }


def test_html_d108_august_dropdown_option() -> None:
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "bt_d108_aug_structural_sl_research" in html
    assert "BT-D108 · August 2026 · Struktur-SL 1,2 % · 3 Trades" in html
    assert html.index("bt_d91_august_ema_v3_no_pause_research") < html.index(
        "bt_d108_aug_structural_sl_research"
    )


def test_js_d108_wiring() -> None:
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "bt_d108_aug_structural_sl_research" in js
    assert "/api/research/backtester/bt-d108-aug-structural-sl/load" in js


def test_api_routes_registered() -> None:
    code = """
import app
paths = sorted({r.path for r in app.app.routes if hasattr(r, 'path')})
for p in paths:
    if 'bt-d108-aug-structural-sl' in p:
        print(p)
"""
    raw = subprocess.check_output(
        [sys.executable, "-c", code], cwd=str(DASH), env=_env(), text=True
    )
    assert "/api/research/backtester/bt-d108-aug-structural-sl/meta" in raw
    assert "/api/research/backtester/bt-d108-aug-structural-sl/load" in raw


def test_d108_ledger_load_counts_drawings_and_sum() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_drawing_specs,
        build_summary,
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("august_d108_structural")
    assert prof.strategy_id == "bt_d108_aug_structural_sl_research"
    rows, path, errs = load_trade_ledger(prof)
    assert not errs, errs
    assert path.is_file()
    assert len(rows) == 3
    val = validate_ledger(prof, rows)
    assert not val, val
    tp = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_TP")
    sl = sum(1 for r in rows if r.get("exit_reason") == "SIMULATED_SL")
    assert tp == 1
    assert sl == 2
    gross = round(sum(float(r["gross_return_pct_short"]) for r in rows), 6)
    assert gross == EXPECTED_SUM_GROSS
    summary = build_summary(prof, rows, ledger_path=path)
    assert summary["drawing_count"] == 6
    positions, links = build_drawing_specs(prof, rows)
    assert len(positions) == 3
    assert len(links) == 3
    for spec in positions:
        assert spec["start"].tzinfo == timezone.utc
        assert spec["metadata"]["origin"] == "bt_d108_aug_structural_sl_chart_overlay_v1"
        assert spec["metadata"].get("structural_sl_meta") is not None


def test_d108_trade_levels_and_utc_alignment() -> None:
    from research_charts.frozen_month_research_overlay_v1 import get_profile, load_trade_ledger

    rows, _, _ = load_trade_ledger(get_profile("august_d108_structural"))
    by_fill = {r["fill_time_utc"]: r for r in rows}
    t1 = by_fill["2026-08-12T23:25:00Z"]
    assert t1["exit_reason"] == "SIMULATED_SL"
    assert round(float(t1["sl_price"]), 9) == round(0.070299592, 9)
    assert t1["tp_price"] == 0.06879
    t2 = by_fill["2026-08-24T08:50:00Z"]
    assert t2["exit_reason"] == "SIMULATED_TP"
    assert round(float(t2["gross_return_pct_short"]), 6) == 0.517393
    for r in rows:
        datetime.fromisoformat(r["fill_time_utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
        datetime.fromisoformat(r["exit_time_utc"].replace("Z", "+00:00")).astimezone(timezone.utc)
        assert r["fill_time_utc"].endswith(":00Z") or r["fill_time_utc"].endswith(":05Z")


def test_switch_d91_vs_d108_workspace() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    sym = "DOGEUSDT"
    for key, expected_loaded, origin in (
        ("august", 50, "bt_d91_august_chart_overlay_v1"),
        ("august_d108_structural", 6, "bt_d108_aug_structural_sl_chart_overlay_v1"),
    ):
        ws = ResearchWorkspace()
        prof = get_profile(key)
        rows, path, _ = load_trade_ledger(prof)
        summary = build_summary(prof, rows, ledger_path=path)
        snap = ws.load_frozen_month_research_backtester(key, sym, rows, summary, visible=True)
        assert snap["backtester"]["loaded"] == expected_loaded
        prefixed = [
            d
            for d in ws.drawings.get_drawings(sym, include_hidden=True)
            if str(getattr(d, "drawing_id", "")).startswith(prof.box_prefix)
            or str(getattr(d, "drawing_id", "")).startswith(prof.link_prefix)
        ]
        assert len(prefixed) == expected_loaded
        assert {(d.metadata or {}).get("origin") for d in prefixed} == {origin}


def test_switch_d91_clears_d108_on_reload() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        build_summary,
        get_profile,
        load_trade_ledger,
    )
    from research_charts.workspace_session import ResearchWorkspace

    sym = "DOGEUSDT"
    ws = ResearchWorkspace()
    for key in ("august_d108_structural", "august"):
        prof = get_profile(key)
        rows, path, _ = load_trade_ledger(prof)
        summary = build_summary(prof, rows, ledger_path=path)
        ws.load_frozen_month_research_backtester(key, sym, rows, summary, visible=True)
    d108 = [
        d
        for d in ws.drawings.get_drawings(sym, include_hidden=True)
        if str(getattr(d, "drawing_id", "")).startswith("btd108aug-")
    ]
    assert d108 == []


def test_bundled_copy_matches_source_hash() -> None:
    import hashlib

    assert SOURCE_LEDGER.is_file()
    assert BUNDLED.is_file()
    for p in (SOURCE_LEDGER, BUNDLED):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        assert h == EXPECTED_SHA256


def test_legacy_load_delegates_d108_strategy_id() -> None:
    from research_charts.frozen_month_research_overlay_v1 import STRATEGY_ID_TO_KEY

    assert STRATEGY_ID_TO_KEY["bt_d108_aug_structural_sl_research"] == "august_d108_structural"


def test_d100a_june_overlay_still_validates() -> None:
    from research_charts.frozen_month_research_overlay_v1 import (
        get_profile,
        load_trade_ledger,
        validate_ledger,
    )

    prof = get_profile("june_d100a")
    rows, _, errs = load_trade_ledger(prof)
    assert not errs
    assert not validate_ledger(prof, rows)

"""Tests for /ema-signale V2 backtest mode and smoke report API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash.parent) not in sys.path:
    sys.path.insert(0, str(_dash.parent))

from dashboard.research_charts.historical_lmv2.decision_snapshot import load_decision_snapshot
from dashboard.research_charts.historical_lmv2.smoke_runs import (
    FROZEN_JULY_2026,
    decision_close_utc,
    paginated_results,
)
from ema_signale_v2_api import api_resolve_config, api_results

TRADE4_ENTRY_OPEN = "2026-07-07T06:45:00+00:00"
TRADE4_DECISION = "2026-07-07T07:00:00+00:00"
TRADE4_POOL = "lldv2:DOGEUSDT:15m:micro:upper:1783406700"


def test_decision_close_from_entry_open():
    assert decision_close_utc(TRADE4_ENTRY_OPEN) == "2026-07-07T07:00:00+00:00"


def test_v2_results_guard_off_30_trades():
    data = api_results("july_2026_pd10_smoke", "15m_pd10_guard_off", 0, 50)
    assert data["success"]
    assert len(data["trades"]) == 30
    assert data["summary"]["trades"] == 30
    assert data["warning"] == "SMOKE / STATE_WARMUP_NOT_PROVEN"


def test_v2_guard_on_separate_variant_same_count():
    off = api_results("july_2026_pd10_smoke", "15m_pd10_guard_off", 0, 50)
    on = api_results("july_2026_pd10_smoke", "15m_pd10_guard_on", 0, 50)
    assert len(off["trades"]) == 30
    assert len(on["trades"]) == 30
    assert off["summary"]["pnl_sum_pct"] == on["summary"]["pnl_sum_pct"]


def test_trade4_in_report_and_snapshot_parity():
    data = api_results("july_2026_pd10_smoke", "15m_pd10_guard_off", 0, 50)
    t4 = next(t for t in data["trades"] if t.get("signal_num") == 4)
    assert t4["decision_close"] == TRADE4_DECISION
    assert t4["pool_id"] == TRADE4_POOL
    assert float(t4["entry_price"]) == 0.07543
    assert float(t4["stop"]) == 0.07564098
    assert float(t4["tp"]) == 0.0747

    snap = load_decision_snapshot(
        catalog_id="doge_multi_month_2026",
        symbol="DOGEUSDT",
        timeframe="15m",
        pool_detail=10,
        decision_close=TRADE4_DECISION,
        expected_frozen_input_id=FROZEN_JULY_2026,
    )
    assert TRADE4_POOL in snap["active_pool_ids"]
    by_id = {p["pool_id"]: p for p in snap["pools"]}
    assert TRADE4_POOL in by_id


def test_pd5_no_report():
    r = api_resolve_config(5, False)
    assert r["available"] is False


def test_pd10_has_report():
    r = api_resolve_config(10, True)
    assert r["available"] is True
    assert r["run_id"] == "july_2026_pd10_smoke"


def test_ema_signale_template_v2_mode_renders():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_dash / "templates")))
    html = env.get_template("ema_signale.html").render(
        user={"username": "tester"},
        view_mode="v2_backtest",
        signals=[],
        symbols=["DOGEUSDT"],
        summary={},
        pagination={"page": 0, "page_size": 50, "total_filtered": 0, "has_prev": False, "has_next": False},
        feed_ready=True,
        feed_message="",
        banner={"title": "V2"},
        window_label="SMOKE",
        filter_symbol="",
        filter_direction="",
        filter_state="",
        filter_start_time="",
        filter_end_time="",
        page_size=50,
        signal_page=0,
    )
    assert "V2 Floor-Guard Backtest" in html
    assert "v2PoolDetail" in html
    assert "ema_signale_v2_chart.js" in html


def test_ema_live_mode_template_preserved():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_dash / "templates")))
    html = env.get_template("ema_signale.html").render(
        user={"username": "tester"},
        view_mode="ema_live",
        signals=[],
        symbols=[],
        summary={"signals": 0, "created": 0, "completed": 0, "cancelled": 0},
        pagination={"page": 0, "page_size": 50, "total_filtered": 0, "has_prev": False, "has_next": False},
        feed_ready=False,
        feed_message="x",
        banner={},
        window_label="",
        filter_symbol="",
        filter_direction="",
        filter_state="",
        filter_start_time="",
        filter_end_time="",
        page_size=50,
        signal_page=0,
    )
    assert "mode=ema_live" in html
    assert "THR" in html

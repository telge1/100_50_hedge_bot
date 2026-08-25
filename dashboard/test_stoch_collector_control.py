"""Tests for Stoch collector service control (no live start/stop of production)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import stoch_collector_control as scc


def test_canonical_argv_always_has_candle_universe():
    off = scc.build_stoch_argv(public_trades=False)
    on = scc.build_stoch_argv(public_trades=True)
    assert "--candle-universe" in off
    assert "config/universe_tradeable_51.json" in off
    assert "--enable-public-trades" not in off
    assert "--enable-public-trades" in on
    assert off.count("--candle-universe") == 1


def test_is_stoch_cmdline_rejects_ob_oi_import():
    assert scc._is_stoch_cmdline("python scripts/run_live_collector_service.py --api-port 8787")
    assert not scc._is_stoch_cmdline("python -m orderbook_analyse.orderbook_v2_live --mode universe51")
    assert not scc._is_stoch_cmdline("python -m orderbook_analyse.oi_liquidation_collector")
    assert not scc._is_stoch_cmdline("python scripts/run_orderbook_v3_30d_import.py")


def test_ob_oi_confirm_required_and_import_blocked():
    from ob_oi_collector_control import apply_ob_oi_action

    assert apply_ob_oi_action("orderbook_live", "start", confirm=False)["error"] == "confirm_required"
    assert apply_ob_oi_action("oi_liquidation", "stop", confirm=False)["error"] == "confirm_required"
    blocked = apply_ob_oi_action("ob_30d", "start", confirm=True)
    assert blocked["ok"] is False
    assert blocked["error"] == "service_not_controllable"


def test_ob_oi_argv_canonical():
    from ob_oi_collector_control import build_ob_argv, build_oi_argv

    ob = " ".join(build_ob_argv())
    oi = " ".join(build_oi_argv())
    assert "orderbook_analyse.orderbook_v2_live" in ob
    assert "--confirm-universe-51" in ob
    assert "universe51" in ob
    assert "orderbook_analyse.oi_liquidation_collector" in oi
    assert "--mode live" in oi
    assert "30d_import" not in ob
    assert "30d_import" not in oi


def test_routes_registered():
    src = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
    assert "/api/collector/services" in src
    assert "apply_service_action" in src


def test_services_overview_shape():
    with mock.patch.object(scc, "fetch_api_status", return_value={"collector_state": "LIVE", "desired_state": "RUNNING", "public_trades_enabled": False, "candle_symbols": ["ETHUSDT"], "signal_symbols": ["XAUUSDT"], "public_trade_symbols": []}):
        with mock.patch.object(scc, "find_stoch_pid", return_value=None):
            with mock.patch.object(scc, "live_feeds_overview", return_value={"orderbook_live": {"running": True, "symbol_count": 51}, "oi_liquidation": {"running": True, "symbol_count": 51}}):
                ov = scc.services_overview()
    assert ov["stoch"]["id"] == "stoch"
    assert ov["public"]["enabled"] is False
    assert ov["orderbook_live"]["controllable"] is True
    assert ov["oi_liquidation"]["controllable"] is True
    assert "--candle-universe" in ov["canonical_argv_public_on"]

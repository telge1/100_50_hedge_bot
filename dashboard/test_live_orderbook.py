"""Unit tests for Live Orderbook dashboard integration (no live CH required)."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from live_orderbook_manager import (
    ALLOWED_REPORT_INTERVALS,
    LiveOrderbookRunnerManager,
    validate_report_interval,
    validate_symbol,
)


def test_symbol_validation():
    assert validate_symbol(" aptusdt ") == "APTUSDT"
    with pytest.raises(ValueError):
        validate_symbol("APT")
    with pytest.raises(ValueError):
        validate_symbol("APT;rm")


def test_interval_allowlist():
    assert validate_report_interval(60) == 60
    assert validate_report_interval(120) == 120
    assert validate_report_interval(300) == 300
    assert ALLOWED_REPORT_INTERVALS == frozenset({60, 120, 300})
    with pytest.raises(ValueError):
        validate_report_interval(3)
    with pytest.raises(ValueError):
        validate_report_interval(90)


def test_start_builds_safe_argv(tmp_path: Path):
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    script = tmp_path / "run_live_level_watch.py"
    script.write_text("# stub\n")
    root = tmp_path / "ob"
    root.mkdir()
    (root / "src").mkdir()
    (root / "results").mkdir()
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=root,
        python_bin=py,
        script_path=script,
        state_file=tmp_path / "state.json",
    )

    fake_proc = MagicMock()
    fake_proc.pid = 424242
    fake_proc.poll.return_value = None

    with patch("live_orderbook_manager.subprocess.Popen", return_value=fake_proc) as popen, patch.object(
        mgr, "_ensure_session_feed", return_value={"success": True, "pid": 111, "owned": True}
    ):
        result = mgr.start_runner(symbol="APTUSDT", report_interval_seconds=60)
        assert result["success"] is True
        args, kwargs = popen.call_args
        cmd = args[0]
        assert kwargs.get("shell") is False
        assert cmd[0] == str(py)
        assert "-u" in cmd
        assert str(script) in cmd
        assert "--symbol" in cmd and "APTUSDT" in cmd
        assert "--report-interval-seconds" in cmd and "60" in cmd
        assert "shell=True" not in str(kwargs)
        # no free path from client
        assert any(str(root / "results") in c for c in cmd)
        assert result["runner"]["feed_recorder_owned"] is True
        assert result["runner"]["feed_recorder_pid"] == 111

    # second start blocked
    again = mgr.start_runner(symbol="APTUSDT", report_interval_seconds=60)
    assert again["success"] is False


def test_stop_sends_sigterm(tmp_path: Path):
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=tmp_path,
        python_bin=tmp_path / "python",
        script_path=tmp_path / "script.py",
        state_file=tmp_path / "state.json",
    )
    (tmp_path / "python").write_text("x")
    (tmp_path / "script.py").write_text("x")
    fake_proc = MagicMock()
    fake_proc.pid = 777
    fake_proc.poll.side_effect = [None, 0, 0]
    fake_proc.wait.return_value = 0
    mgr._proc = fake_proc
    from live_orderbook_manager import RunnerRecord

    mgr._record = RunnerRecord(
        runner_id="t",
        symbol="APTUSDT",
        pid=777,
        status="LIVE",
        output_dir=str(tmp_path),
        feed_recorder_pid=888,
        feed_recorder_owned=True,
    )
    # _refresh_runtime_status and the SIGTERM gate both call _pid_alive;
    # keep the process "alive" until poll() reports exit.
    with patch("live_orderbook_manager.os.kill") as kill, patch(
        "live_orderbook_manager._pid_alive", return_value=True
    ), patch("live_orderbook_manager._terminate_pid") as term:
        result = mgr.stop_runner(timeout_seconds=1)
        assert result["success"] is True
        kill.assert_any_call(777, signal.SIGTERM)
        term.assert_called_once_with(888)
    assert mgr.get_status()["status"] == "STOPPED"
    assert mgr._record.feed_recorder_owned is False
    assert mgr._record.feed_recorder_pid is None


def test_session_feed_reuses_existing_recorder(tmp_path: Path):
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=tmp_path,
        python_bin=tmp_path / "python",
        script_path=tmp_path / "script.py",
        state_file=tmp_path / "state.json",
    )
    (tmp_path / "python").write_text("x")
    (tmp_path / "script.py").write_text("x")
    with patch(
        "live_orderbook_manager.find_recorder_pids_for_symbol", return_value=[4242]
    ), patch("live_orderbook_manager.subprocess.Popen") as popen:
        out = mgr._ensure_session_feed("APTUSDT")
        assert out == {"success": True, "pid": 4242, "owned": False}
        popen.assert_not_called()


def test_start_fails_when_feed_fails(tmp_path: Path):
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    script = tmp_path / "run_live_level_watch.py"
    script.write_text("# stub\n")
    root = tmp_path / "ob"
    root.mkdir()
    (root / "src").mkdir()
    (root / "results").mkdir()
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=root,
        python_bin=py,
        script_path=script,
        state_file=tmp_path / "state.json",
    )
    with patch.object(
        mgr, "_ensure_session_feed", return_value={"success": False, "error": "feed recorder start failed"}
    ), patch("live_orderbook_manager.subprocess.Popen") as popen:
        result = mgr.start_runner(symbol="APTUSDT", report_interval_seconds=60)
        assert result["success"] is False
        assert "feed" in result["error"]
        popen.assert_not_called()



def test_snapshot_missing_outputs(tmp_path: Path):
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=tmp_path,
        python_bin=tmp_path / "python",
        script_path=tmp_path / "script.py",
        state_file=tmp_path / "state.json",
    )
    snap = mgr.get_latest_snapshot()
    assert snap["view"]["resistance"] is None
    assert snap["view"]["absorption"] is None
    assert snap["status"]["status"] == "STOPPED"
    assert "display" in snap["view"]
    assert snap["view"]["display"]["resistance"]["headline"]


def test_logs_are_plain_strings(tmp_path: Path):
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=tmp_path,
        python_bin=tmp_path / "python",
        script_path=tmp_path / "script.py",
        state_file=tmp_path / "state.json",
    )
    from live_orderbook_manager import RunnerRecord

    mgr._record = RunnerRecord(runner_id="t", symbol="APTUSDT", status="STOPPED")
    mgr._append_log("<script>alert(1)</script>")
    lines = mgr.read_recent_logs()
    assert any("<script>" in ln for ln in lines)
    # HTML escaping is done in the browser JS — backend keeps raw text


def test_build_view_prefers_sample_ladder_over_stale_report(tmp_path: Path):
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=tmp_path,
        python_bin=tmp_path / "python",
        script_path=tmp_path / "script.py",
        state_file=tmp_path / "state.json",
    )
    sample = {
        "sample_ts": "2026-08-03T12:00:05+00:00",
        "mid_price": 0.55,
        "data_age_seconds": 1.2,
        "state": "SUPPORT_TEST",
        "setup": {"setup": "NO_TRADE"},
        "ladder": {
            "support": {
                "zone_low": 0.548,
                "zone_high": 0.549,
                "zone_center": 0.5485,
                "notional": 120000,
                "strength": 70,
                "persistence_samples": 8,
            },
            "resistance": {
                "zone_low": 0.56,
                "zone_high": 0.561,
                "zone_center": 0.5605,
                "notional": 90000,
                "strength": 55,
                "persistence_samples": 4,
            },
        },
        "strongest_bid_walls": [
            {"distance_to_mid_bps": 5.0, "wall_notional": 80000},
            {"distance_to_mid_bps": 15.0, "wall_notional": 20000},
        ],
        "strongest_ask_walls": [
            {"distance_to_mid_bps": 8.0, "wall_notional": 40000},
            {"distance_to_mid_bps": 18.0, "wall_notional": 10000},
        ],
    }
    previous = {
        "ladder": {
            "support": {
                "zone_low": 0.547,
                "zone_high": 0.548,
                "zone_center": 0.5475,
                "notional": 100000,
                "strength": 65,
            },
            "resistance": {
                "zone_low": 0.562,
                "zone_high": 0.563,
                "zone_center": 0.5625,
                "notional": 95000,
                "strength": 50,
            },
        }
    }
    # Stale report with different ladder — must NOT override sample levels
    report = {
        "report_ts": "2026-08-03T11:59:00+00:00",
        "mid_price": 0.40,
        "state": "NO_ACTIVE_ZONE",
        "setup": {"setup": "NO_TRADE"},
        "ladder": {
            "support": {
                "zone_low": 0.39,
                "zone_high": 0.391,
                "zone_center": 0.3905,
                "notional": 1,
                "strength": 1,
            }
        },
        "wall_follow": [
            {"label": "ASK / RESISTANCE", "side": "ask", "reading": "building"},
            {"label": "BID / SUPPORT", "side": "bid", "reading": "weakening"},
        ],
        "market_flow": {
            "delta_notional": -5000,
            "delta_ratio": 0.4,
            "buy_notional": 1000,
            "sell_notional": 6000,
            "oi_change_pct": 0.1,
            "price_change_pct": -0.2,
            "buy_liquidation_notional": 9000,
            "sell_liquidation_notional": 1000,
            "liquidation_count": 3,
            "data_complete": True,
        },
        "readings": ["support under pressure"],
    }
    prev_report = {
        "market_flow": {
            "buy_liquidation_notional": 1000,
            "sell_liquidation_notional": 500,
            "data_complete": True,
        }
    }
    status = {
        "status": "LIVE",
        "runner": {"symbol": "APTUSDT", "report_interval_seconds": 60},
    }
    view = mgr._build_view(
        sample=sample,
        previous=previous,
        report=report,
        previous_report=prev_report,
        status=status,
        summary=None,
        transitions=[
            {"new_state": "SUPPORT_TEST"},
            {"new_state": "SUPPORT_TEST"},
            {"new_state": "SUPPORT_HOLDING"},
        ],
        sample_interval_seconds=5,
    )
    assert view["level_source"] == "sample"
    assert view["mid_price"] == 0.55
    assert view["state"] == "SUPPORT_TEST"
    assert view["support"]["current_center"] == 0.5485
    assert view["support"]["direction"] == "up"
    assert view["support"]["notional_change_pct"] == pytest.approx(20.0)
    assert view["near_price"]["bid_share_0_10"] == pytest.approx(80000 / 120000 * 100)
    assert view["level_quality"]["fatigue"] == "MEDIUM"
    assert view["level_quality"]["tests"] == 2
    assert view["liquidations"]["rising"] is True
    assert view["display"]["money_flow"]["regime"] == "NEW_SHORTS"
    assert view["display"]["wall_bias"]["headline"] == "VERKAUFSDRUCK NIMMT ZU"
    assert view["display"]["data_age_label"] == "1s"
    assert view["absorption"] is None
    assert view["display"]["absorption"]["headline"] == "SAMMLE ERSTE DATEN"


def test_near_price_none_near_walls():
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=Path("/tmp"),
        python_bin=Path("/tmp/python"),
        script_path=Path("/tmp/script.py"),
        state_file=Path("/tmp/state.json"),
    )
    out = mgr._near_price_from_sample(
        {
            "strongest_bid_walls": [{"distance_to_mid_bps": 80.0, "wall_notional": 1e6}],
            "strongest_ask_walls": [{"distance_to_mid_bps": 90.0, "wall_notional": 1e6}],
        }
    )
    assert out["bias"] == "NONE_NEAR"


def test_ob_grid_passthrough_from_sample(tmp_path: Path):
    mgr = LiveOrderbookRunnerManager(
        orderbook_root=tmp_path,
        python_bin=tmp_path / "python",
        script_path=tmp_path / "script.py",
        state_file=tmp_path / "state.json",
    )
    grid = {
        "symbol": "APTUSDT",
        "snapshot_ts": "2026-08-05T12:00:00Z",
        "mid_price": 0.56755,
        "status": "LIVE",
        "visible_depth": {"bid_bps": 400.0, "ask_bps": 390.0},
        "search_band_bps": {"min": 100.0, "max": 300.0},
        "bid_levels": [
            {
                "rank_by_distance": 1,
                "price": 0.56,
                "distance_bps": -133.0,
                "distance_pct": -1.33,
                "multiple": 1.4,
                "wall_class": "STRONG_WALL",
                "status": "ACTIVE",
                "policies": ["NEAREST_RELEVANT"],
            },
            {
                "rank_by_distance": 6,
                "price": 0.555,
                "distance_bps": -221.0,
                "distance_pct": -2.21,
                "multiple": 1.8,
                "wall_class": "STRONG_WALL",
                "status": "ACTIVE",
                "policies": ["STRONGEST_RELEVANT"],
            },
        ],
        "ask_levels": [],
        "compact_bid_levels": [
            {
                "rank_by_distance": 1,
                "price": 0.56,
                "distance_bps": -133.0,
                "distance_pct": -1.33,
                "multiple": 1.4,
                "wall_class": "STRONG_WALL",
                "status": "ACTIVE",
                "policies": ["NEAREST_RELEVANT"],
            }
        ],
        "compact_ask_levels": [],
        "candidate_counts": {"bid": 2, "ask": 0},
        "strong_wall_counts": {"bid": 2, "ask": 0},
    }
    sample = {
        "sample_ts": "2026-08-05T12:00:05+00:00",
        "mid_price": 0.56755,
        "ob_grid": grid,
        "strongest_bid_walls": [
            {
                "wall_price": 0.565,
                "wall_notional": 900000,
                "wall_multiple": 3.2,
                "distance_to_mid_bps": 45.0,
                "percentile": 100.0,
                "resolution": "auto_10bps",
            }
        ],
        "strongest_ask_walls": [
            {
                "wall_price": 0.57,
                "wall_notional": 500000,
                "wall_multiple": 2.1,
                "distance_to_mid_bps": 43.0,
                "percentile": 95.0,
                "resolution": "auto_10bps",
            }
        ],
    }
    view = mgr._build_view(
        sample=sample,
        previous=None,
        report=None,
        status={"status": "LIVE", "runner": {"symbol": "APTUSDT", "report_interval_seconds": 60}},
        summary=None,
    )
    assert view["ob_grid"] is not None
    assert view["ob_grid"]["status"] == "LIVE"
    assert view["ob_grid"]["mid_price"] == 0.56755
    assert view["ob_grid"]["bid_levels"][0]["distance_bps"] < 0
    disp_bid = view["ob_grid"]["display_bid_levels"]
    assert len(disp_bid) == 2
    assert any("STRONGEST_RELEVANT" in (L.get("policies") or []) for L in disp_bid)
    assert len(view["ob_grid"]["near_mid_bid_walls"]) == 1
    assert view["ob_grid"]["near_mid_bid_walls"][0]["policies"] == ["NEAR_MID"]
    assert view["ob_grid"]["near_mid_bid_walls"][0]["distance_bps"] < 0
    assert len(view["ob_grid"]["near_mid_ask_walls"]) == 1
    assert view["ob_grid"]["near_mid_ask_walls"][0]["distance_bps"] > 0
    empty = mgr._empty_view()
    assert "ob_grid" in empty
    assert empty["ob_grid"] is None

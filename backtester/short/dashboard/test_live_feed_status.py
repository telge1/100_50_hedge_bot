"""Unit tests for read-only live feed process probes."""

from __future__ import annotations

from pathlib import Path

import live_feed_status as lfs


def test_probe_ob_and_oi_live_processes():
    overview = lfs.live_feeds_overview()
    ob = overview["orderbook_live"]
    oi = overview["oi_liquidation"]
    assert "running" in ob and "symbols" in ob
    assert "running" in oi and "symbols" in oi
    if ob["running"]:
        assert ob["pid"] is not None
        assert "XAUUSDT" not in ob["symbols"]
        assert len(ob["symbols"]) >= 1
    if oi["running"]:
        assert oi["pid"] is not None
        assert "XAUUSDT" not in oi["symbols"]


def test_load_symbols_prefers_supported(tmp_path: Path):
    path = tmp_path / "plan.json"
    path.write_text(
        '{"requested":["AAAUSDT"],"supported":["ETHUSDT","BTCUSDT"]}',
        encoding="utf-8",
    )
    assert lfs._load_symbols_json(path) == ["ETHUSDT", "BTCUSDT"]


def test_route_registered():
    src = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
    assert "/api/collector/live-feeds" in src
    assert "live_feeds_overview" in src

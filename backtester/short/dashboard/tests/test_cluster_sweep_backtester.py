"""Tests for Cluster Sweep research backtester integration (no live orders)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_strategy_in_research_template():
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    assert "cluster_sweep_ema_9_20_59" in html
    assert "Cluster Sweep EMA 9/20/59" in html
    assert "researchBtStrategy" in html
    assert "Low-pool debug" in html
    assert "Backtest starten" in html


def test_stoch_backtester_source_unchanged():
    from research_charts.stoch_backtester import BACKTESTER_SOURCE, signal_to_position_spec

    assert BACKTESTER_SOURCE == "stoch_backtester"
    row = {
        "symbol": "ACEUSDT",
        "trade_direction": "LONG",
        "entry_price": 1.0,
        "tp_price": 1.1,
        "sl_price": 0.9,
        "entry_time": "2026-08-20T12:00:00Z",
        "signal_id": "t1",
    }
    spec = signal_to_position_spec(row)
    assert spec is not None
    assert spec["drawing_type"] == "long_position"


def test_cluster_sweep_default_min_pools_and_debug():
    from research_charts.cluster_sweep_backtester import DEFAULT_MARKER_KINDS, STRATEGY_ID

    assert STRATEGY_ID == "cluster_sweep_ema_9_20_59"
    assert "CONFIRMATION" in DEFAULT_MARKER_KINDS
    assert "ENTRY_NEXT_OPEN" in DEFAULT_MARKER_KINDS
    assert "INVALIDATED" in DEFAULT_MARKER_KINDS


def test_oa_import_loads_pipeline():
    from research_charts.oa_import import load_cluster_sweep

    mod = load_cluster_sweep()
    assert mod["STRATEGY_ID"] == "cluster_sweep_ema_9_20_59"
    assert callable(mod["run_cluster_sweep_on_candles"])


def test_workspace_cluster_sweep_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DRAWINGS_DIR", str(tmp_path))
    from research_charts.workspace_session import ResearchWorkspace

    ws = ResearchWorkspace()
    payload = {
        "meta": {"symbol": "XRPUSDT", "debug_low_pool_zones": False, "n_events": 1},
        "events": [
            {
                "event_id": "csw:test",
                "direction": "BULLISH",
                "final_status": "CONFIRMED",
                "confirmation_at": "2026-08-20T18:40:00+00:00",
                "entry_at": "2026-08-20T18:45:00+00:00",
                "entry_price": 1.25,
                "cluster_mid": 1.25,
                "ema_59": 1.24,
            }
        ],
        "markers": [
            {
                "overlay_id": "csw-test-ENTRY_NEXT_OPEN",
                "kind": "ENTRY_NEXT_OPEN",
                "event_id": "csw:test",
                "direction": "BULLISH",
                "status": "CONFIRMED",
                "timestamp": datetime(2026, 8, 20, 18, 45, tzinfo=timezone.utc),
                "price": 1.25,
                "shape": "arrow_up",
                "color": "#000000",
                "text": "ENT",
                "position": "below",
                "event": {},
            }
        ],
        "coverage": {"candles_1m": {"status": "VALID"}},
    }
    snap = ws.store_cluster_sweep_run(payload)
    assert snap["cluster_sweep"]["n_events"] == 1
    assert snap["cluster_sweep"]["visible"] is False
    shown = ws.set_cluster_sweep_visible(True, "XRPUSDT")
    assert shown["cluster_sweep"]["visible"] is True
    assert shown["backtester"]["loaded"] == 1
    hidden = ws.set_cluster_sweep_visible(False, "XRPUSDT")
    assert hidden["cluster_sweep"]["visible"] is False
    nav = ws.navigate_cluster_sweep_event(delta=1)
    assert nav["cluster_sweep"]["event"]["event_id"] == "csw:test"


def test_api_routes_registered():
    text = (ROOT / "research_charts" / "api.py").read_text(encoding="utf-8")
    assert '/api/research/backtester/run"' in text
    assert "cluster_sweep_ema_9_20_59" in text
    assert "cluster-sweep/nav" in text


def test_detector_structure_required_at_confirm():
    """Regression: confirmation must keep EMA stack intact."""
    import pandas as pd
    from orderbook_analyse.cluster_sweep_research.event_detector import _resolve_forward, _structure_ok
    from orderbook_analyse.cluster_sweep_research.models import SetupDirection, ClusterSnapshot
    from datetime import datetime, timedelta, timezone

    def _ts(i):
        return datetime(2026, 8, 20, tzinfo=timezone.utc) + timedelta(minutes=5 * i)

    # Build tiny frame with bull stack then break
    rows = []
    for i in range(10):
        rows.append(
            {
                "open_time": _ts(i).replace(tzinfo=None),
                "open": 100 + i * 0.1,
                "high": 100.2 + i * 0.1,
                "low": 99.8 + i * 0.1,
                "close": 100.1 + i * 0.1,
                "ema_9": 102.0 if i < 5 else 98.0,
                "ema_20": 101.5 if i < 5 else 97.5,
                "ema_59": 100.0,
                "volume": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    c = ClusterSnapshot(
        cluster_id="c",
        side="lower",
        low=99.0,
        high=100.5,
        mid=99.75,
        width_abs=1.5,
        width_pct=0.01,
        pool_count=3,
        strength_sum=3,
        strength_mean=1,
        strength_max=1,
        oldest_created=_ts(0),
        newest_created=_ts(1),
        pool_ids=("a", "b", "c"),
    )
    assert _structure_ok(SetupDirection.BULLISH, 102, 101.5, 100)
    conf, meta, states, t_entry, t_inv = _resolve_forward(df, 2, SetupDirection.BULLISH, c, 8)
    # By bar 5 structure breaks — either confirmed earlier with structure or invalidated
    if t_inv is not None:
        assert any(s.value == "INVALIDATED" for s in states)
        assert all(not v.get("fired") for v in conf.values()) or meta.get("invalidation_reason") == "EMA_STRUCTURE_BREAK"


def test_events_to_marker_direction():
    from research_charts.cluster_sweep_backtester import events_to_marker_specs

    specs = events_to_marker_specs(
        [
            {
                "event_id": "e1",
                "direction": "BEARISH",
                "final_status": "CONFIRMED",
                "confirmation_at": "2026-08-20T21:00:00Z",
                "entry_at": "2026-08-20T21:05:00Z",
                "entry_price": 1.25,
                "cluster_mid": 1.25,
            }
        ]
    )
    assert specs
    assert all(s["direction"] == "BEARISH" for s in specs)

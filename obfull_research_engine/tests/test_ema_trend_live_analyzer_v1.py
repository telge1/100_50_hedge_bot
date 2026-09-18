"""Unit tests for ema_trend_live_analyzer_v1 (no forensic import)."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Prefer main orderbook_analyse (has .research); drop research-WT shadow if present.
_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_SHADOW = str(_ENGINE_ROOT.parent / "src")
while _SHADOW in sys.path:
    sys.path.remove(_SHADOW)
sys.path.insert(0, str(_ENGINE_ROOT / "src"))
sys.path.insert(0, "/home/telgenbuescher/projects/orderbook_analyse/src")

from obfull_research_engine.ema_trend_live_analyzer_v1.clock import FakeClock
from obfull_research_engine.ema_trend_live_analyzer_v1.decision_evidence import CandidateFSM
from obfull_research_engine.ema_trend_live_analyzer_v1.live_event_adapter import LiveEventAdapter
from obfull_research_engine.ema_trend_live_analyzer_v1.public_trades_adapter import (
    STREAM_AVAILABLE_BUT_NO_TRADES,
    STREAM_MISSING,
    TRADES_PRESENT,
    FakePublicTradesAdapter,
    TradeWatermarkPoller,
)
from obfull_research_engine.ema_trend_live_analyzer_v1.wall_tracker import DynamicWallTracker


def test_no_forensic_import() -> None:
    import obfull_research_engine.ema_trend_live_analyzer_v1.pipeline as pip

    src = open(pip.__file__, encoding="utf-8").read()
    assert "from obfull_research_engine.ema_trend_analyzer_v1" not in src
    assert "import obfull_research_engine.ema_trend_analyzer_v1" not in src
    assert "ema_trend_live_analyzer_v1" in pip.__name__


def test_candidate_not_before_5s() -> None:
    fsm = CandidateFSM(threshold_side="ABOVE_EMA_THRESHOLD")
    now = datetime.now(timezone.utc)
    fsm.update(
        elapsed_s=1.0,
        coverage_ok=True,
        archive_ok=True,
        features={"persistence_ratio": 2.0, "impact_efficiency": 10.0, "qdh": {"qdh_base": 1.0}, "mass": {"attributed_fill_capped": 1.0, "refill": 0.0, "residual_pull": 0.0}},
        now=now,
    )
    assert fsm.state == "EARLY_EVIDENCE"
    assert "CANDIDATE" not in fsm.state
    fsm.update(
        elapsed_s=5.0,
        coverage_ok=True,
        archive_ok=True,
        features={"persistence_ratio": 2.0, "impact_efficiency": 10.0, "qdh": {"qdh_base": 1.0}, "mass": {"attributed_fill_capped": 1.0, "refill": 0.0, "residual_pull": 0.0}, "microprice_change_hint": 1.0},
        now=now,
    )
    assert fsm.state in {"CONTINUATION_EVIDENCE", "CONTINUATION_CANDIDATE", "EARLY_EVIDENCE"}


def test_gap_blocks_adapter() -> None:
    ad = LiveEventAdapter(symbol="X", wall_price=100.0, wall_side="ask")
    ad.ingest_batch(
        [
            {
                "symbol": "X",
                "side": "ask",
                "price": 100.0,
                "new_qty": 1.0,
                "snapshot_generation": 1,
                "sequence_id": 1,
                "update_id": 1,
                "record_ordinal": 1,
                "valid_for_analysis": True,
                "gap": True,
                "outcome": "gap",
                "exchange_event_time": "2026-09-18T00:00:00.000Z",
                "receive_time_ns": 0,
            }
        ]
    )
    assert ad.exact_features_valid is False
    assert ad.blocked_reason == "SEQUENCE_GAP"


def test_wall_tracker_requires_tick() -> None:
    with pytest.raises(ValueError):
        DynamicWallTracker(tick_size=0)


def test_pt_dedupe_and_statuses() -> None:
    rows = [
        {"symbol": "X", "trade_id": "1", "trade_ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "1", "trade_ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "price": 1, "size": 1, "side": "Buy"},
        {"symbol": "X", "trade_id": "2", "trade_ts": datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc), "price": 1, "size": 1, "side": "Buy"},
    ]
    snap = datetime(2026, 1, 1, tzinfo=timezone.utc)
    poller = TradeWatermarkPoller(adapter=FakePublicTradesAdapter(rows), symbol="X", snapshot_ready_at=snap)
    r1 = poller.poll(now=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    assert r1["status"] == TRADES_PRESENT
    assert len(poller.seen_ids) == 2
    r2 = poller.poll(now=datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc))
    assert r2["new_trade_ids"] == []
    empty = TradeWatermarkPoller(adapter=FakePublicTradesAdapter([]), symbol="X", snapshot_ready_at=snap)
    r0 = empty.poll(now=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    assert r0["status"] == STREAM_AVAILABLE_BUT_NO_TRADES
    bad = FakePublicTradesAdapter([])
    bad.force_unreachable = True
    miss = TradeWatermarkPoller(adapter=bad, symbol="X", snapshot_ready_at=snap)
    rm = miss.poll(now=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    assert rm["status"] == STREAM_MISSING

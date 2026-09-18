"""Tests for live trade fanout adapter and live-vs-CH parity."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_SHADOW = str(_ENGINE_ROOT.parent / "src")
while _SHADOW in sys.path:
    sys.path.remove(_SHADOW)
sys.path.insert(0, str(_ENGINE_ROOT / "src"))
sys.path.insert(0, "/home/telgenbuescher/projects/orderbook_analyse/src")

from obfull_research_engine.ema_trend_live_analyzer_v1 import (
    CH_LIVE_FALLBACK_FORBIDDEN,
    LIVE_TRADE_FANOUT_REQUIRED,
    SECOND_PUBLIC_TRADE_WS,
)
from obfull_research_engine.ema_trend_live_analyzer_v1.decision_evidence import CandidateFSM
from obfull_research_engine.ema_trend_live_analyzer_v1.live_trade_adapter import LiveTradeAdapter
from obfull_research_engine.ema_trend_live_analyzer_v1.live_vs_ch_parity import compare_live_vs_ch


def test_live_trade_flags():
    assert LIVE_TRADE_FANOUT_REQUIRED is True
    assert CH_LIVE_FALLBACK_FORBIDDEN is True
    assert SECOND_PUBLIC_TRADE_WS is False


def test_fanout_event_to_canonical_preserves_times():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ad = LiveTradeAdapter(symbol="X", snapshot_ready_at=t0)
    ev = {
        "trade_id": "t1",
        "symbol": "X",
        "side": "Buy",
        "price": "100.5",
        "quantity": "1.2",
        "notional": "120.6",
        "exchange_event_time": "2026-01-01T00:00:01.000Z",
        "receive_time_ns": int((t0 + timedelta(seconds=1, milliseconds=5)).timestamp() * 1e9),
        "record_ordinal": 1,
        "valid_for_analysis": True,
    }
    nodes = ad.ingest_batch([ev])
    assert len(nodes) == 1
    assert nodes[0].collector_received_at is not None
    assert nodes[0].exchange_event_time.startswith("2026-01-01T00:00:01")


def test_trade_before_snapshot_and_after_cutoff_excluded():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ad = LiveTradeAdapter(
        symbol="X",
        snapshot_ready_at=t0,
        feature_cutoff_at=t0 + timedelta(seconds=5),
    )
    ad.ingest_batch(
        [
            {
                "trade_id": "old",
                "symbol": "X",
                "side": "Buy",
                "price": "1",
                "quantity": "1",
                "notional": "1",
                "exchange_event_time": "2025-12-31T23:59:59.000Z",
                "receive_time_ns": 1,
                "valid_for_analysis": True,
            },
            {
                "trade_id": "late",
                "symbol": "X",
                "side": "Buy",
                "price": "1",
                "quantity": "1",
                "notional": "1",
                "exchange_event_time": "2026-01-01T00:00:10.000Z",
                "receive_time_ns": 2,
                "valid_for_analysis": True,
            },
            {
                "trade_id": "ok",
                "symbol": "X",
                "side": "Sell",
                "price": "1",
                "quantity": "1",
                "notional": "1",
                "exchange_event_time": "2026-01-01T00:00:02.000Z",
                "receive_time_ns": 3,
                "valid_for_analysis": True,
            },
        ]
    )
    assert [t.trade_id for t in ad.trades] == ["ok"]


def test_overflow_blocks_and_missing_fanout_blocks_candidate():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ad = LiveTradeAdapter(symbol="X", snapshot_ready_at=t0)
    ad.ingest_batch([{"event_type": "overflow", "overflow": True, "trade_id": "x"}])
    assert ad.coverage_valid is False
    assert ad.blocked_reason == "TRADE_QUEUE_OVERFLOW"

    fsm = CandidateFSM(threshold_side="ABOVE_EMA_THRESHOLD")
    now = datetime.now(timezone.utc)
    fsm.update(
        elapsed_s=10.0,
        coverage_ok=True,
        archive_ok=True,
        trade_fanout_ok=False,
        features={"persistence_ratio": 5.0, "impact_efficiency": 10.0, "qdh": {"qdh_base": 1.0}, "mass": {"attributed_fill_capped": 1.0, "refill": 0.0, "residual_pull": 0.0}},
        now=now,
    )
    assert fsm.state == "BLOCKED_COVERAGE"
    assert any(t.get("reason") == "BLOCKED_LIVE_TRADES" for t in fsm.transitions)


def test_live_vs_ch_parity_fixture():
    live = [
        {"trade_id": "1", "side": "Buy", "price": "1", "quantity": "2", "exchange_event_time": "2026-01-01T00:00:00Z"},
        {"trade_id": "2", "side": "Sell", "price": "1.1", "quantity": "3", "exchange_event_time": "2026-01-01T00:00:01Z"},
    ]
    ch = [
        {"trade_id": "1", "side": "Buy", "price": "1", "size": "2", "ingest_timestamp": "2026-01-01T00:00:00.5Z"},
        {"trade_id": "2", "side": "Sell", "price": "1.1", "size": "3", "ingest_timestamp": "2026-01-01T00:00:01.5Z"},
    ]
    assert compare_live_vs_ch(live, ch).ok is True

    missing = compare_live_vs_ch(live, [ch[0]])
    assert missing.ok is False and missing.missing_in_ch == ["2"]

    extra = compare_live_vs_ch([live[0]], ch)
    assert extra.ok is False and "2" in extra.extra_in_ch

    bad_side = compare_live_vs_ch(live, [{**ch[0], "side": "Sell"}, ch[1]])
    assert bad_side.ok is False and bad_side.field_mismatches

    dups = compare_live_vs_ch(live, ch + [ch[0]])
    assert dups.duplicates_ch == ["1"]

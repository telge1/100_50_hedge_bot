"""Tests for BTC OB fight explanatory audit."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from research.btc_ob_fight.facts import json_safe, window_trade_facts
from research.btc_ob_fight_explanatory_audit.association import (
    ASSOCIATION_LABEL,
    NOT_DIRECT,
    build_association_sensitivity,
)
from research.btc_ob_fight_explanatory_audit.buckets import bucket_liquidations, bucket_trades
from research.btc_ob_fight_explanatory_audit.liquidation_semantics import (
    EXPECTED_AGGRESSOR,
    FORCED_TRADE_DIRECTION,
)
from research.btc_ob_fight_explanatory_audit.market_structure import build_market_structure


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_liquidation_forced_buy_sell_mapping():
    assert FORCED_TRADE_DIRECTION["LIQUIDATED_SHORT"] == "FORCED_BUY"
    assert FORCED_TRADE_DIRECTION["LIQUIDATED_LONG"] == "FORCED_SELL"
    assert EXPECTED_AGGRESSOR["LIQUIDATED_SHORT"] == "Buy"
    assert EXPECTED_AGGRESSOR["LIQUIDATED_LONG"] == "Sell"


def test_public_trade_dedup_and_buckets():
    t0 = _ts("2026-08-31T19:00:00Z")
    trades = [
        {"ts": t0, "trade_id": "a", "side": "Buy", "price": 79000.0, "size": 1.0, "notional": 100.0},
        {"ts": t0, "trade_id": "a", "side": "Buy", "price": 79000.0, "size": 1.0, "notional": 100.0},
        {"ts": t0 + timedelta(seconds=1), "trade_id": "b", "side": "Sell", "price": 79010.0, "size": 1.0, "notional": 50.0},
    ]
    deduped = {t["trade_id"]: t for t in trades}
    assert len(deduped) == 2
    buckets = bucket_trades(list(deduped.values()), start=t0, end=t0 + timedelta(seconds=5), seconds=1)
    assert len(buckets) == 5
    assert buckets[0]["trade_count"] == 1
    assert buckets[1]["trade_count"] == 1


def test_liquidation_buckets_preserve_counts():
    t0 = _ts("2026-08-31T19:00:00Z")
    events = [
        {
            "event_time": "2026-08-31T19:00:00.100Z",
            "liquidated_side": "LIQUIDATED_SHORT",
            "base_volume": 1.0,
            "quote_notional": 1000.0,
        },
        {
            "event_time": "2026-08-31T19:00:00.500Z",
            "liquidated_side": "LIQUIDATED_SHORT",
            "base_volume": 2.0,
            "quote_notional": 2000.0,
        },
    ]
    buckets = bucket_liquidations(events, start=t0, end=t0 + timedelta(seconds=2), seconds=1)
    assert buckets[0]["short_liquidation_count"] == 2
    assert buckets[0]["short_liquidation_quote"] == 3000.0


def test_association_heuristic_not_direct():
    t0 = _ts("2026-08-31T19:00:00Z")
    liqs = [
        {
            "event_time": "2026-08-31T19:00:00.000Z",
            "liquidated_side": "LIQUIDATED_SHORT",
            "bankruptcy_price": 79000.0,
            "quote_notional": 500.0,
            "base_volume": 0.01,
        }
    ]
    trades = [
        {"ts": t0, "trade_id": "1", "side": "Buy", "price": 79000.0, "notional": 500.0, "size": 0.01},
        {"ts": t0, "trade_id": "2", "side": "Buy", "price": 79000.0, "notional": 500.0, "size": 0.01},
    ]
    rows = build_association_sensitivity(liqs, trades, windows_ms=(100,))
    assert rows[0]["identification_status"] == NOT_DIRECT
    assert rows[0]["association_type"] == ASSOCIATION_LABEL
    assert rows[0]["events_with_temporal_buy_match"] == 1
    # overlapping sum may double-count same trade across events — documented risk
    assert rows[0]["overlapping_buy_notional_sum"] == 1000.0


def test_market_structure_lower_high():
    anchor = _ts("2026-08-31T19:00:00Z")
    trades = [
        {"ts": anchor, "price": 79100.0, "side": "Buy", "notional": 1, "size": 1, "trade_id": "1"},
        {"ts": anchor + timedelta(minutes=10), "price": 79280.0, "side": "Buy", "notional": 1, "size": 1, "trade_id": "2"},
        {"ts": anchor + timedelta(minutes=11), "price": 79100.0, "side": "Sell", "notional": 1, "size": 1, "trade_id": "3"},
    ]
    peak_ts = anchor + timedelta(minutes=10)
    reclaim_ts = anchor + timedelta(minutes=11)
    ext = [{"ts": anchor + timedelta(hours=1, minutes=20), "price": 79150.0, "side": "Buy", "notional": 1, "size": 1, "trade_id": "4"}]
    ms = build_market_structure(
        trades,
        peak_ts=peak_ts,
        peak_price=79280.0,
        reclaim_ts=reclaim_ts,
        reclaim_price=79100.0,
        extended_trades=ext,
    )
    assert ms["later_retest"]["classification"] == "LOWER_HIGH"
    assert ms["later_retest"]["higher_high_achieved"] is False


def test_decision_vs_hindsight_separation():
    from research.btc_ob_fight_explanatory_audit.decision_snapshots import build_decision_snapshots

    peak = _ts("2026-08-31T19:10:42Z")
    reclaim = _ts("2026-08-31T19:10:58Z")
    snaps = build_decision_snapshots(
        outer_cross_ts=_ts("2026-08-31T19:08:00Z"),
        peak_ts=peak,
        peak_price=79280.0,
        reclaim_ts=reclaim,
        reclaim_price=79136.0,
        retest_ts=None,
        retest_high=None,
        oi_at={"to_peak_delta": 50.0},
        liq_counts={"to_peak_short": 40},
    )
    assert snaps["C_reclaim"]["hindsight_only"] is False
    assert snaps["E_post_resolution"]["hindsight_only"] is True
    assert "Later price decline" in snaps["C_reclaim"]["missing_confirmation"][0] or "Retest" in snaps["C_reclaim"]["missing_confirmation"][0]


def test_json_safe_no_nan_infinity():
    obj = {"a": float("nan"), "b": float("inf"), "c": {"d": float("-inf")}, "e": [1.0, float("nan")]}
    safe = json_safe(obj)
    dumped = json.dumps(safe)
    assert "NaN" not in dumped
    assert "Infinity" not in dumped
    assert safe["a"] is None
    assert safe["b"] is None


def test_window_trade_facts_subtraction_vs_direct():
    t0 = _ts("2026-08-31T19:00:00Z")
    trades = [
        {"ts": t0, "trade_id": "1", "side": "Buy", "price": 100.0, "notional": 300.0, "size": 3},
        {"ts": t0 + timedelta(minutes=15), "trade_id": "2", "side": "Sell", "price": 99.0, "notional": 200.0, "size": 2},
        {"ts": t0 + timedelta(minutes=25), "trade_id": "3", "side": "Sell", "price": 98.0, "notional": 100.0, "size": 1},
    ]
    w_full = window_trade_facts(trades, t0, t0 + timedelta(minutes=30))
    w_first = window_trade_facts(trades, t0, t0 + timedelta(minutes=10))
    w_second = window_trade_facts(trades, t0 + timedelta(minutes=10), t0 + timedelta(minutes=30))
    assert abs(w_full["delta_notional"] - (w_first["delta_notional"] + w_second["delta_notional"])) < 1e-6

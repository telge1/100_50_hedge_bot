"""Tests for trade facts and JSON safety."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research.btc_ob_fight.facts import build_trade_facts, window_trade_facts, json_safe
from research.btc_ob_fight.loaders import load_public_trades


def _trade(ts, tid, side, price, size=1.0, notional=None):
    return {
        "ts": ts,
        "trade_id": tid,
        "side": side,
        "price": price,
        "size": size,
        "notional": notional if notional is not None else price * size,
    }


def test_public_trade_dedup_and_sort():
    t0 = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    trades = [
        _trade(t0, "2", "Buy", 100.0),
        _trade(t0, "1", "Sell", 99.0),
        _trade(t0, "1", "Sell", 99.0),
    ]
    seen = set()
    out = []
    for tr in sorted(trades, key=lambda x: (x["ts"], x["trade_id"])):
        if tr["trade_id"] in seen:
            continue
        seen.add(tr["trade_id"])
        out.append(tr)
    assert [t["trade_id"] for t in out] == ["1", "2"]


def test_window_trade_facts_division_by_zero_safe():
    t0 = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 31, 19, 1, tzinfo=timezone.utc)
    w = window_trade_facts([], t0, t1)
    assert w["bps_per_million_delta"] is None
    assert json_safe(w)["bps_per_million_delta"] is None


def test_build_trade_facts_relative_windows():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    trades = [
        _trade(anchor, "a", "Buy", 100.0, notional=1_000_000),
        _trade(anchor.replace(minute=5), "b", "Buy", 101.0, notional=1_000_000),
    ]
    facts = build_trade_facts(
        trades,
        anchor,
        anchor - timedelta(minutes=30),
        anchor + timedelta(minutes=30),
    )
    rel = {w["label"]: w for w in facts["relative_windows"]}
    assert rel["anchor_0_10m"]["delta_notional"] > 0

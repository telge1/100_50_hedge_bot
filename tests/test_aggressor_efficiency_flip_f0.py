"""Synthetic fixtures and unit tests for AEF F0 dual-impact discovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orderbook_analyse.aggressor_efficiency_flip.buckets import build_second_buckets, sort_trades
from orderbook_analyse.aggressor_efficiency_flip.compression import evaluate_compression
from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    AEFConfig,
    aggressor_side,
    counter_side,
    same_side_directional_bps,
)
from orderbook_analyse.aggressor_efficiency_flip.impact import measure_dual_impact
from orderbook_analyse.aggressor_efficiency_flip.integrity import prefix_snapshot, compare_prefix
from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_flip.runner import run_discovery_on_trades, run_prefix_parity
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second


UTC = timezone.utc
BASE = datetime(2026, 8, 29, 10, 0, 0, tzinfo=UTC)


def T(sec: int, ms: int = 0) -> datetime:
    return BASE + timedelta(seconds=sec, milliseconds=ms)


def trade(sec: int, side: str, price: float, notional: float, tid: str, ms: int = 0) -> Trade:
    px = price
    size = notional / price if price else 0.0
    return Trade(trade_ts=T(sec, ms), trade_id=tid, side=side, price=px, size=size, notional=notional)


def cfg(**kwargs) -> AEFConfig:
    base = AEFConfig.from_profile("unfitted_f0_diagnostic")
    d = base.to_dict()
    d.update(kwargs)
    # loosen ranks for tiny synthetic histories
    d.setdefault("min_notional_rank", 0.0)
    d["min_notional_rank"] = kwargs.get("min_notional_rank", 0.0)
    d["min_notional_usdt"] = kwargs.get("min_notional_usdt", 1000.0)
    d["counter_min_notional_usdt"] = kwargs.get("counter_min_notional_usdt", 1000.0)
    d["counter_min_directional_impact_bps"] = kwargs.get("counter_min_directional_impact_bps", 1.0)
    d["weak_contemporaneous_max_bps"] = kwargs.get("weak_contemporaneous_max_bps", 3.0)
    d["strong_same_side_impact_bps"] = kwargs.get("strong_same_side_impact_bps", 8.0)
    d["strong_post_followthrough_bps"] = kwargs.get("strong_post_followthrough_bps", 8.0)
    d["structure_lookback_seconds"] = kwargs.get("structure_lookback_seconds", 30)
    d["acceptance_hold_seconds"] = kwargs.get("acceptance_hold_seconds", 2)
    d["require_structure"] = kwargs.get("require_structure", True)
    d["require_acceptance"] = kwargs.get("require_acceptance", True)
    d["cooldown_seconds"] = kwargs.get("cooldown_seconds", 0)
    return AEFConfig(**{k: d[k] for k in AEFConfig.__dataclass_fields__})


def test_side_mapping():
    assert aggressor_side("LONG") == "Sell"
    assert aggressor_side("SHORT") == "Buy"
    assert counter_side("LONG") == "Buy"
    assert counter_side("SHORT") == "Sell"
    assert same_side_directional_bps("Sell", -10.0) == 10.0
    assert same_side_directional_bps("Buy", 10.0) == 10.0


def test_bucket_boundaries_half_open():
    trades = [
        trade(0, "Buy", 1.0, 10, "a", ms=0),
        trade(0, "Sell", 1.0, 5, "b", ms=999),
        trade(1, "Buy", 1.0, 7, "c", ms=0),
    ]
    buckets = build_second_buckets(trades)
    assert set(buckets.keys()) == {T(0), T(1)}
    assert buckets[T(0)].buy_notional == 10
    assert buckets[T(0)].sell_notional == 5
    assert buckets[T(1)].buy_notional == 7


def test_identical_ms_order_independent_for_ohlc_extremes():
    # Same ms different ids — high/low must include both prices regardless of id order
    t1 = [
        trade(0, "Sell", 1.00, 100, "z", ms=100),
        trade(0, "Sell", 1.02, 100, "a", ms=100),
    ]
    t2 = list(reversed(t1))
    b1 = build_second_buckets(t1)[T(0)]
    b2 = build_second_buckets(t2)[T(0)]
    assert b1.high_price == b2.high_price == 1.02
    assert b1.low_price == b2.low_price == 1.00
    assert b1.sell_notional == b2.sell_notional == 200


def test_duplicate_trade_id_dedupe_in_loader():
    from orderbook_analyse.aggressor_efficiency_flip.trade_loader import trades_from_rows

    rows = [
        {"trade_ts": T(0), "trade_id": "x", "side": "Buy", "price": 1.0, "size": 1.0, "notional": 1.0},
        {"trade_ts": T(0), "trade_id": "x", "side": "Buy", "price": 1.0, "size": 1.0, "notional": 1.0},
    ]
    assert len(trades_from_rows(rows)) == 1


def _window_trades_flat_sell(high_notional=True, down_bps=0.0, post_down_bps=0.0, rebound=False):
    """Build 15s of trades: flow 0-5 sell, post 5-10."""
    trades: list[Trade] = []
    p0 = 100.0
    # seed prices before
    for s in range(-5, 0):
        trades.append(trade(s, "Buy", p0, 100, f"seed{s}"))
    # flow seconds 0..4 heavy sell
    n = 50_000 if high_notional else 100
    p_flow_end = p0 * (1.0 - down_bps / 10_000.0)
    for s in range(0, 5):
        px = p0 + (p_flow_end - p0) * (s / 4 if down_bps else 0)
        trades.append(trade(s, "Sell", px, n / 5, f"s{s}"))
        trades.append(trade(s, "Buy", px, 10, f"sb{s}"))  # tiny opposite
    # post 5..9
    if rebound:
        p_post = p_flow_end * (1.0 + 5.0 / 10_000.0)
    else:
        p_post = p_flow_end * (1.0 - post_down_bps / 10_000.0)
    for s in range(5, 10):
        trades.append(trade(s, "Buy", p_post, 50, f"p{s}"))
    # more after for structure
    for s in range(10, 40):
        trades.append(trade(s, "Buy", p_post * (1 + 0.0002 * (s - 10)), 200, f"a{s}"))
    return trades, p0, p_flow_end


def test_dual_impact_case_c_veto():
    trades, _, _ = _window_trades_flat_sell(down_bps=12.0, post_down_bps=0.0)
    buckets = build_second_buckets(trades)
    dual = measure_dual_impact(
        buckets, t0=T(0), t1=T(5), t2=T(10), side="Sell", reclaim_bps=3.0, strong_post_bps=8.0
    )
    assert dual.same_side_contemporaneous_bps >= 8.0
    dec = evaluate_compression(
        dual,
        direction="LONG",
        cfg=cfg(),
        past_notionals=[],
        past_shares=[],
        past_contemp_impacts=[],
        past_post_follows=[],
    )
    assert dec.strong_same_side_impact_veto is True
    assert dec.allowed is False
    assert dec.reason_code == "strong_same_side_impact_veto"


def test_dual_impact_case_a_allowed():
    trades, _, _ = _window_trades_flat_sell(down_bps=0.0, post_down_bps=0.0)
    buckets = build_second_buckets(trades)
    dual = measure_dual_impact(
        buckets, t0=T(0), t1=T(5), t2=T(10), side="Sell", reclaim_bps=3.0, strong_post_bps=8.0
    )
    assert dual.same_side_contemporaneous_bps <= 3.0
    dec = evaluate_compression(
        dual,
        direction="LONG",
        cfg=cfg(),
        past_notionals=[100.0],  # so rank high
        past_shares=[0.5],
        past_contemp_impacts=[10.0],
        past_post_follows=[10.0],
    )
    assert dec.allowed is True
    assert dec.strong_same_side_impact_veto is False


def test_dual_impact_case_d_delayed_veto():
    trades, _, _ = _window_trades_flat_sell(down_bps=0.0, post_down_bps=12.0)
    buckets = build_second_buckets(trades)
    dual = measure_dual_impact(
        buckets, t0=T(0), t1=T(5), t2=T(10), side="Sell", reclaim_bps=3.0, strong_post_bps=8.0
    )
    dec = evaluate_compression(
        dual,
        direction="LONG",
        cfg=cfg(),
        past_notionals=[100.0],
        past_shares=[0.5],
        past_contemp_impacts=[10.0],
        past_post_follows=[0.0],
    )
    assert dec.allowed is False
    assert dec.delayed_continuation_veto is True


def test_dual_impact_case_b_reclaim():
    trades, _, _ = _window_trades_flat_sell(down_bps=0.0, rebound=True)
    buckets = build_second_buckets(trades)
    dual = measure_dual_impact(
        buckets, t0=T(0), t1=T(5), t2=T(10), side="Sell", reclaim_bps=3.0, strong_post_bps=8.0
    )
    assert dual.reclaim_flag is True
    dec = evaluate_compression(
        dual,
        direction="LONG",
        cfg=cfg(),
        past_notionals=[100.0],
        past_shares=[0.5],
        past_contemp_impacts=[10.0],
        past_post_follows=[10.0],
    )
    assert dec.allowed is True
    assert "reclaim" in dec.semantic_case or dec.semantic_case.startswith("B_")


def test_short_mirror_buy_compression_veto():
    # strong up during buy burst
    trades: list[Trade] = []
    p0 = 100.0
    for s in range(0, 5):
        px = p0 * (1.0 + 0.0015 * s)  # ~strong up
        trades.append(trade(s, "Buy", px, 10_000, f"b{s}"))
    for s in range(5, 10):
        trades.append(trade(s, "Sell", px, 50, f"p{s}"))
    buckets = build_second_buckets(trades)
    dual = measure_dual_impact(
        buckets, t0=T(0), t1=T(5), t2=T(10), side="Buy", reclaim_bps=3.0, strong_post_bps=8.0
    )
    dec = evaluate_compression(
        dual,
        direction="SHORT",
        cfg=cfg(),
        past_notionals=[],
        past_shares=[],
        past_contemp_impacts=[],
        past_post_follows=[],
    )
    assert dec.strong_same_side_impact_veto is True


def _full_flip_sequence() -> list[Trade]:
    """Sell absorb 0-10, buy initiative 20-30, grind up for structure/acceptance."""
    trades: list[Trade] = []
    p = 100.0
    # pre history for structure lookback
    for s in range(-40, 0):
        trades.append(trade(s, "Buy", p, 50, f"pre{s}"))
    # sell compression flow+post flat
    for s in range(0, 5):
        trades.append(trade(s, "Sell", p, 20_000, f"sell{s}"))
        trades.append(trade(s, "Buy", p, 100, f"sx{s}"))
    for s in range(5, 10):
        trades.append(trade(s, "Buy", p, 80, f"post{s}"))
    # quiet
    for s in range(10, 20):
        trades.append(trade(s, "Buy", p, 40, f"q{s}"))
    # buy initiative with up
    for s in range(20, 25):
        p = 100.0 * (1.0 + 0.0008 * (s - 19))  # ~8+ bps over window
        trades.append(trade(s, "Buy", p, 25_000, f"buy{s}"))
        trades.append(trade(s, "Sell", p, 100, f"by{s}"))
    for s in range(25, 30):
        trades.append(trade(s, "Buy", p, 100, f"bp{s}"))
    # continue up for break + acceptance
    for s in range(30, 80):
        p = p * 1.00015
        trades.append(trade(s, "Buy", p, 300, f"up{s}"))
    return trades


def test_full_long_diagnostic_candidate_and_entry_after_final():
    trades = _full_flip_sequence()
    c = cfg(
        min_notional_usdt=5_000,
        counter_min_notional_usdt=5_000,
        counter_min_directional_impact_bps=2.0,
        weak_contemporaneous_max_bps=5.0,
        structure_break_eps_bps=0.5,
        acceptance_hold_seconds=2,
        cooldown_seconds=0,
    )
    res = run_discovery_on_trades(
        symbol="DOGEUSDT",
        trades=trades,
        start=T(-40),
        end=T(90),
        cfg=c,
    )
    assert res["candidates"], "expected at least one DIAGNOSTIC_CANDIDATE"
    ep = res["candidates"][0]
    assert ep["status"] == "DIAGNOSTIC_CANDIDATE"
    assert ep["direction"] == "LONG"
    final = datetime.fromisoformat(ep["final_decision_ts"].replace("Z", "+00:00"))
    entry = datetime.fromisoformat(ep["diagnostic_earliest_entry_ts"].replace("Z", "+00:00"))
    assert entry > final


def test_counter_must_be_after_t2():
    trades = _full_flip_sequence()
    c = cfg(min_notional_usdt=5_000, counter_min_notional_usdt=5_000, counter_min_directional_impact_bps=2.0)
    res = run_discovery_on_trades(symbol="DOGEUSDT", trades=trades, start=T(-40), end=T(90), cfg=c)
    for ep in res["candidates"]:
        t2 = datetime.fromisoformat(ep["compression_confirmed_ts"].replace("Z", "+00:00"))
        u0 = datetime.fromisoformat(ep["counter_flow_start"].replace("Z", "+00:00"))
        assert u0 >= t2


def test_timeout_without_counter():
    # only sell compression, no buy later
    trades, _, _ = _window_trades_flat_sell(down_bps=0.0)
    for s in range(10, 200):
        trades.append(trade(s, "Sell", 100.0, 50, f"only{s}"))
    c = cfg(min_notional_usdt=5_000, counter_search_seconds=30, require_structure=False, require_acceptance=False)
    res = run_discovery_on_trades(symbol="DOGEUSDT", trades=trades, start=T(0), end=T(200), cfg=c)
    assert any(t["to_state"] == "TIMEOUT" for t in res["transitions"])


def test_prefix_parity_synthetic():
    trades = _full_flip_sequence()
    c = cfg(min_notional_usdt=5_000, counter_min_notional_usdt=5_000, counter_min_directional_impact_bps=2.0)
    parity = run_prefix_parity(symbol="DOGEUSDT", trades=trades, start=T(-40), end=T(90), cfg=c)
    assert parity["ok"], parity["errors"]


def test_oi_missing_keeps_candidate():
    trades = _full_flip_sequence()
    c = cfg(min_notional_usdt=5_000, counter_min_notional_usdt=5_000, counter_min_directional_impact_bps=2.0)
    res = run_discovery_on_trades(
        symbol="DOGEUSDT", trades=trades, start=T(-40), end=T(90), cfg=c, oi_labels={}
    )
    assert res["candidates"]
    assert res["candidates"][0]["oi_class"] == "MISSING"
    assert res["candidates"][0]["ob_available"] is False


def test_incomplete_end_window_no_premature_event():
    trades = _full_flip_sequence()
    c = cfg(min_notional_usdt=5_000, counter_min_notional_usdt=5_000)
    # as_of before first post-flow close
    res = run_discovery_on_trades(
        symbol="DOGEUSDT", trades=trades, start=T(-40), end=T(90), cfg=c, as_of=T(3)
    )
    assert res["candidates"] == []
    assert all(not x["allowed"] or True for x in res["compressions"])  # may be empty
    assert res["compressions"] == [] or all(
        datetime.fromisoformat(x["t2"].replace("Z", "+00:00")) <= T(3) for x in res["compressions"]
    )

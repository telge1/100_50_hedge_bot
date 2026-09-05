"""Isolated causal tests for public-trade bubbles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orderbook_analyse.public_trade_bubbles.aggregate import (
    aggregate_bubbles,
    bubbles_prefix_parity,
    classify_size,
    filter_display_mode,
    filter_trades_as_of,
    finalize_forming_at_close,
)
from orderbook_analyse.public_trade_bubbles.contract import (
    PublicTradeRecord,
    aggressor_flags,
)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _trade(
    tid: str,
    ts: str,
    side: str,
    price: float,
    qty: float,
    *,
    received: str | None = None,
) -> PublicTradeRecord:
    buy, sell = aggressor_flags(side)
    t = _ts(ts)
    return PublicTradeRecord(
        trade_id=tid,
        symbol="DOGEUSDT",
        trade_timestamp=t,
        received_at=_ts(received) if received else t,
        price=price,
        quantity_base=qty,
        notional_quote=price * qty,
        taker_side="Buy" if buy else "Sell",
        is_aggressive_buy=buy,
        is_aggressive_sell=sell,
        source="test",
        source_quality="ok",
    )


def test_aggressor_semantics():
    assert aggressor_flags("Buy") == (True, False)
    assert aggressor_flags("Sell") == (False, True)


def test_trade_id_dedupe_and_causality():
    trades = [
        _trade("1", "2026-08-28T06:35:00.100Z", "Buy", 0.1, 100),
        _trade("1", "2026-08-28T06:35:00.100Z", "Buy", 0.1, 100),  # dup
        _trade("2", "2026-08-28T06:35:01.000Z", "Sell", 0.1, 50),
        _trade("3", "2026-08-28T06:36:00.000Z", "Buy", 0.1, 10),  # future vs as_of
    ]
    as_of = _ts("2026-08-28T06:35:30Z")
    filtered = filter_trades_as_of(trades, as_of)
    assert [t.trade_id for t in filtered] == ["1", "2"]


def test_received_at_gate():
    trades = [
        _trade(
            "1",
            "2026-08-28T06:35:00Z",
            "Buy",
            0.1,
            10,
            received="2026-08-28T06:36:00Z",
        )
    ]
    as_of = _ts("2026-08-28T06:35:30Z")
    assert filter_trades_as_of(trades, as_of, require_received=True) == []
    assert len(filter_trades_as_of(trades, as_of, require_received=False)) == 1


def test_bucket_aggregation_and_known_at():
    trades = [
        _trade("a", "2026-08-28T06:35:00.100Z", "Buy", 0.08817, 1000),
        _trade("b", "2026-08-28T06:35:00.200Z", "Sell", 0.08817, 400),
        _trade("c", "2026-08-28T06:35:00.900Z", "Buy", 0.08818, 100),
    ]
    as_of = _ts("2026-08-28T06:35:05Z")
    bubbles = aggregate_bubbles(
        trades,
        symbol="DOGEUSDT",
        as_of=as_of,
        time_bucket_s=1,
        price_ticks_per_bucket=1,
        tick_size=1e-5,
        include_forming=False,
    )
    assert bubbles
    for b in bubbles:
        assert b.known_at == b.bucket_end
        assert not b.forming
        assert b.max_feature_timestamp <= b.known_at


def test_forming_then_finalize_immutable():
    trades = [_trade("a", "2026-08-28T06:35:00.100Z", "Buy", 0.08817, 500)]
    as_of = _ts("2026-08-28T06:35:00.500Z")
    bubbles = aggregate_bubbles(
        trades,
        symbol="DOGEUSDT",
        as_of=as_of,
        time_bucket_s=1,
        price_ticks_per_bucket=1,
        tick_size=1e-5,
        include_forming=True,
    )
    forming = [b for b in bubbles if b.forming]
    assert len(forming) == 1
    closed = finalize_forming_at_close(forming[0], closed_as_of=forming[0].bucket_end)
    assert closed.forming is False
    assert closed.known_at == forming[0].bucket_end
    assert closed.bubble_id == forming[0].bubble_id


def test_no_full_window_normalization():
    # Many small closed buckets then one large — early buckets stay UNCALIBRATED/SMALL
    base = _ts("2026-08-28T06:30:00Z")
    trades = []
    for i in range(50):
        ts = base + timedelta(seconds=i)
        trades.append(
            _trade(f"s{i}", ts.isoformat().replace("+00:00", "Z"), "Buy", 0.08, 10)
        )
    # large late
    trades.append(
        _trade("L", (base + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"), "Buy", 0.08, 1_000_000)
    )
    as_of_early = base + timedelta(seconds=10)
    early = aggregate_bubbles(
        trades,
        symbol="DOGEUSDT",
        as_of=as_of_early,
        include_forming=False,
        tick_size=1e-5,
        price_ticks_per_bucket=1,
    )
    assert all(b.size_class in ("UNCALIBRATED", "SMALL", "MEDIUM") for b in early)
    # late as_of may classify LARGE using prior only — large trade's class uses priors before it
    as_of_late = base + timedelta(seconds=61)
    late = aggregate_bubbles(
        trades,
        symbol="DOGEUSDT",
        as_of=as_of_late,
        include_forming=False,
        tick_size=1e-5,
        price_ticks_per_bucket=1,
    )
    big = [b for b in late if b.total_notional > 1000]
    assert big
    # thresholds must come from prior sample_count, not future
    assert big[0].max_feature_timestamp <= big[0].known_at


def test_prefix_replay_parity():
    base = _ts("2026-08-28T06:35:00Z")
    trades = []
    for i in range(30):
        ts = base + timedelta(seconds=i * 2)
        side = "Buy" if i % 2 == 0 else "Sell"
        trades.append(
            _trade(str(i), ts.isoformat().replace("+00:00", "Z"), side, 0.088 + i * 1e-6, 100 + i)
        )
    for as_of in [base + timedelta(seconds=10), base + timedelta(seconds=40), base + timedelta(seconds=58)]:
        full, pref = bubbles_prefix_parity(
            trades,
            symbol="DOGEUSDT",
            as_of=as_of,
            tick_size=1e-5,
            price_ticks_per_bucket=1,
            include_forming=True,
        )
        assert [b.to_dict() for b in full] == [b.to_dict() for b in pref]


def test_display_modes_and_cursor_no_future():
    trades = [
        _trade("1", "2026-08-28T06:35:00Z", "Buy", 0.08, 100),
        _trade("2", "2026-08-28T06:40:00Z", "Sell", 0.08, 100),
    ]
    as_of = _ts("2026-08-28T06:36:00Z")
    bubbles = aggregate_bubbles(trades, symbol="DOGEUSDT", as_of=as_of, include_forming=False)
    assert all(b.bucket_start <= as_of for b in bubbles)
    assert filter_display_mode(bubbles, "off") == []


def test_size_warmup_uncalibrated():
    assert classify_size(100.0, [1.0] * 10)[0] == "UNCALIBRATED"
    cls, meta = classify_size(1000.0, list(range(1, 80)))
    assert cls in ("SMALL", "MEDIUM", "LARGE", "EXTREME")
    assert meta["sample_count"] >= 40

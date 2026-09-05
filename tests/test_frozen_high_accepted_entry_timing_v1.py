"""Tests for FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_contracts import (
    ACCEPTANCE_TO_TRADE_SIDE,
    NO_FIT_ENTRY,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_execution import (
    BookQuote,
    apply_entry_price,
    apply_exit_price,
    first_quote_at_or_after,
    gross_return,
    path_mfe_mae,
    trade_economics,
    trade_side_from_acceptance,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow

UTC = timezone.utc
BASE = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def S(sec: int, bid: float = 100.0, ask: float = 100.2) -> SampleRow:
    ms = int((BASE + timedelta(seconds=sec)).timestamp() * 1000)
    mid = 0.5 * (bid + ask)
    return SampleRow(
        symbol="BTCUSDT",
        ts_ms=ms,
        best_bid=bid,
        best_ask=ask,
        mid=mid,
        spread=ask - bid,
        spread_bps=(ask - bid) / mid * 1e4,
        microprice=mid,
        bid_levels=1,
        ask_levels=1,
        bid_qty_l10=1.0,
        ask_qty_l10=1.0,
        imbalance_l10=0.0,
        bid_qty_bps10=0.0,
        ask_qty_bps10=0.0,
        imbalance_bps10=0.0,
        bid_wall_price=None,
        bid_wall_qty=None,
        ask_wall_price=None,
        ask_wall_qty=None,
        source_file="test",
        warmup=False,
    )


def test_acceptance_direction_mapping():
    assert trade_side_from_acceptance("ACCEPTED_ABOVE") == "LONG"
    assert trade_side_from_acceptance("ACCEPTED_BELOW") == "SHORT"
    assert ACCEPTANCE_TO_TRADE_SIDE["ACCEPTED_ABOVE"] == "LONG"


def test_long_entry_ask_short_entry_bid():
    q = BookQuote(ts=BASE, best_bid=100.0, best_ask=100.2, mid=100.1)
    long_e = apply_entry_price(side="LONG", quote=q, extra_slippage_bps=1.0)
    short_e = apply_entry_price(side="SHORT", quote=q, extra_slippage_bps=1.0)
    assert long_e["raw_entry_price"] == 100.2
    assert short_e["raw_entry_price"] == 100.0
    assert long_e["executable_entry_price"] > long_e["raw_entry_price"]
    assert short_e["executable_entry_price"] < short_e["raw_entry_price"]


def test_long_exit_bid_short_exit_ask():
    q = BookQuote(ts=BASE, best_bid=100.0, best_ask=100.2, mid=100.1)
    long_x = apply_exit_price(side="LONG", quote=q, extra_slippage_bps=1.0)
    short_x = apply_exit_price(side="SHORT", quote=q, extra_slippage_bps=1.0)
    assert long_x["raw_exit_price"] == 100.0
    assert short_x["raw_exit_price"] == 100.2


def test_no_snapshot_before_legal_and_lookup_window():
    samples = [S(0), S(1), S(3)]
    legal = BASE + timedelta(seconds=2)
    q, st = first_quote_at_or_after(samples, legal_ts=legal, max_lookup_seconds=2)
    assert q is not None
    assert q.ts >= legal
    # beyond lookup
    q2, st2 = first_quote_at_or_after(
        samples, legal_ts=BASE + timedelta(seconds=10), max_lookup_seconds=2
    )
    assert q2 is None


def test_return_formulas_and_fees():
    assert gross_return("LONG", 100.0, 101.0) == pytest.approx(0.01)
    assert gross_return("SHORT", 100.0, 99.0) == pytest.approx(100 / 99 - 1)
    eco = trade_economics(
        side="LONG",
        entry_mid=100.0,
        exit_mid=101.0,
        raw_entry=100.1,
        raw_exit=100.9,
        exec_entry=100.2,
        exec_exit=100.8,
        entry_fee_rate=0.00055,
        exit_fee_rate=0.00055,
        notional_usdt=1000.0,
    )
    assert eco["net_pnl_usdt"] == pytest.approx(eco["net_return"] * 1000.0)
    assert eco["total_fee"] == pytest.approx(0.0011)


def test_mfe_mae_from_entry_only():
    samples = [S(i, bid=100 + i * 0.1, ask=100.2 + i * 0.1) for i in range(0, 20)]
    out = path_mfe_mae(
        samples,
        side="LONG",
        entry_ts=BASE + timedelta(seconds=5),
        entry_px=100.5,
        horizon_end=BASE + timedelta(seconds=15),
    )
    assert "mfe" in out and "mae" in out


def test_no_fit_flags():
    assert all(v is False for v in NO_FIT_ENTRY.values())


def test_one_position_chronology():
    # inline logic
    trades = [
        {"entry_book_ts": "2026-08-25T12:00:00Z", "id": 1},
        {"entry_book_ts": "2026-08-25T12:02:00Z", "id": 2},
        {"entry_book_ts": "2026-08-25T12:06:00Z", "id": 3},
    ]
    from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc

    ordered = sorted(trades, key=lambda t: parse_utc(t["entry_book_ts"]))
    out = []
    free_at = None
    for t in ordered:
        ets = parse_utc(t["entry_book_ts"])
        if free_at is not None and ets < free_at:
            continue
        out.append(t)
        free_at = ets + timedelta(seconds=300)
    assert [x["id"] for x in out] == [1, 3]

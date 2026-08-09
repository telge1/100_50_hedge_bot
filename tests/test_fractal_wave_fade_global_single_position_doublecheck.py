"""Unit tests for double-check invariants (synthetic; no MySQL)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.path_replay import (
    MinuteBook,
    UpgradeEvent,
    gross_dir,
    replay_trade_path,
    scan_bar_sl_first,
    ts_utc,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.independent_replay import (
    independent_global_replay,
)


def _book(n=100, px=100.0):
    times = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    o = np.full(n, px)
    return MinuteBook.from_df(
        pd.DataFrame(
            {
                "timestamp": times,
                "open": o,
                "high": o + 0.05,
                "low": o - 0.05,
                "close": o,
                "close_time": times + pd.Timedelta(minutes=1),
            }
        )
    )


def test_no_lookahead_entry_strict():
    b = _book()
    # signal at t0 → entry must be first open AFTER
    sig = ts_utc(b.times[10])  # equals bar open — first AFTER is 11
    i = b.first_after(sig)
    assert i == 11


def test_sl_first_same_bar():
    r, g, amb = scan_bar_sl_first("LONG", 100.0, 101.5, 98.5, 1.0, 1.0)
    assert r == "SL" and amb is True and g == -1.0


def test_no_retroactive_upgrade():
    b = _book(200, px=100.0)
    # price hits +1.05% at bar 20 (would TP on 15m 1%) then later upgrade
    b.highs[20] = 101.05
    b.lows[:] = 99.5
    # without upgrade should TP at 20 with 1%
    rep0 = replay_trade_path(
        side="LONG",
        entry_time=ts_utc(b.times[10]),
        entry_price=100.0,
        first_tf="15m",
        upgrade_events=[],
        book=b,
        max_hold_min=24 * 60,
    )
    assert rep0["exit_reason"] == "TP"
    # If we illegally applied 4h TP4 from start, bar 20 would NOT hit TP — engine must still TP
    ups = [
        UpgradeEvent(
            tf="4h",
            available_at=ts_utc(b.times[50]) - pd.Timedelta(minutes=1),
            apply_at_entry_time=ts_utc(b.times[50]),
        )
    ]
    # clear early TP so path continues — set bar 20 back
    b.highs[20] = 100.05
    # hit 1% TP at bar 30 BEFORE upgrade at 50 — must TP at 30 with old ladder, not wait for 4%
    b.highs[30] = 101.05
    rep = replay_trade_path(
        side="LONG",
        entry_time=ts_utc(b.times[10]),
        entry_price=100.0,
        first_tf="15m",
        upgrade_events=ups,
        book=b,
        max_hold_min=24 * 60,
    )
    assert rep["exit_reason"] == "TP"
    assert ts_utc(rep["exit_time"]) == ts_utc(b.times[30])
    assert abs(rep["gross_return_pct"] - 1.0) < 1e-9


def test_no_overlap_independent_loop():
    doge = _book(300, 10.0)
    apt = _book(300, 5.0)
    doge.highs[:] = 10.05
    doge.lows[:] = 9.95
    apt.highs[:] = 5.02
    apt.lows[:] = 4.98
    rows = []
    for i, (sym, side, ei, book) in enumerate(
        [
            ("DOGEUSDT", "LONG", 10, doge),
            ("APTUSDT", "LONG", 20, apt),
            ("DOGEUSDT", "SHORT", 200, doge),
        ]
    ):
        rows.append(
            {
                "symbol": sym,
                "side": side,
                "signal_tf": "15m",
                "signal_id": i,
                "cluster_key": f"{sym}::{i}",
                "confirmation_available_at": ts_utc(book.times[ei]) - pd.Timedelta(minutes=1),
                "entry_time": ts_utc(book.times[ei]),
                "entry_i": ei,
                "entry_price": float(book.opens[ei]),
            }
        )
    ev = pd.DataFrame(rows)
    first = {(r["symbol"], r["signal_id"]) for _, r in ev.iterrows()}
    res = independent_global_replay(ev, {"DOGEUSDT": doge, "APTUSDT": apt}, first)
    trades = res["trades"]
    for i in range(len(trades) - 1):
        assert ts_utc(trades[i + 1]["entry_time"]) > ts_utc(trades[i]["exit_time"])


def test_fee_parity():
    assert abs(gross_dir("LONG", 100, 101) - 1.0) < 1e-12
    assert abs(gross_dir("SHORT", 100, 99) - 1.0) < 1e-12


def test_timezone_utc():
    t = ts_utc("2024-01-01 00:00:00")
    assert str(t.tz) == "UTC"
    t2 = ts_utc(pd.Timestamp("2024-01-01", tz="UTC"))
    assert t2.tz is not None

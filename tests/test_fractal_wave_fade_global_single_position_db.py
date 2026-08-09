"""Unit tests for global single-position sequencer (synthetic; no MySQL required)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from orderbook_analyse.fractal_wave_fade_global_single_position_db import ENV_FILE, PRIMARY_FEE
from orderbook_analyse.fractal_wave_fade_global_single_position_db.equity import (
    annotate_trade_equities,
    compound_equity,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.global_engine import (
    build_global_event_frame,
    event_sort_key,
    run_global_single_position,
)
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import (
    SymbolBooks,
    _scan_exit,
    run_symbol_backtest,
)


def _books_flat(n: int = 200, start: str = "2024-01-01", px: float = 100.0) -> SymbolBooks:
    times = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    o = np.full(n, px, dtype=float)
    return SymbolBooks(
        high=o + 0.1,
        low=o - 0.1,
        close=o.copy(),
        opens=o.copy(),
        open_times=times.to_numpy(dtype="datetime64[ns]"),
    )


def _sig(
    *,
    symbol: str,
    side: str,
    tf: str,
    entry_i: int,
    books: SymbolBooks,
    signal_id: int,
    conf_offset_min: int = 1,
) -> dict:
    et = pd.Timestamp(books.open_times[entry_i])
    conf = et - pd.Timedelta(minutes=conf_offset_min)
    return {
        "symbol": symbol,
        "side": side,
        "signal_tf": tf,
        "signal_id": signal_id,
        "cluster_id": signal_id,  # each own cluster
        "cluster_key": f"{symbol}::{signal_id}",
        "confirmation_available_at": conf,
        "entry_time": et,
        "entry_i": entry_i,
        "entry_price": float(books.opens[entry_i]),
        "is_tier_a": True,
        "is_q4": True,
    }


def _cluster_map(rows: list[dict]) -> dict[str, list]:
    """One-row clusters keyed by signal_id == cluster index within symbol."""
    by_sym: dict[str, list] = {}
    for r in rows:
        sym = r["symbol"]
        by_sym.setdefault(sym, [])
    # rebuild with contiguous cluster ids per symbol
    out_rows = []
    clusters: dict[str, list] = {}
    counters: dict[str, int] = {}
    for r in rows:
        sym = r["symbol"]
        cid = counters.get(sym, 0)
        counters[sym] = cid + 1
        rr = dict(r)
        rr["cluster_id"] = cid
        rr["cluster_key"] = f"{sym}::{cid}"
        rr["signal_id"] = cid  # first of cluster
        out_rows.append(rr)
        df = pd.DataFrame([rr])
        clusters.setdefault(sym, []).append({"rows": df, "side": rr["side"]})
    return {"rows": out_rows, "clusters": clusters}


def test_never_two_open_trades_simultaneously():
    doge = _books_flat(500, px=10.0)
    apt = _books_flat(500, px=5.0)
    # force long holds: no TP/SL hit (flat ±0.1 on 10 → 1%)
    doge.high[:] = 10.05
    doge.low[:] = 9.95
    apt.high[:] = 5.02
    apt.low[:] = 4.98
    raw = [
        _sig(symbol="DOGEUSDT", side="LONG", tf="15m", entry_i=10, books=doge, signal_id=0),
        _sig(symbol="APTUSDT", side="LONG", tf="15m", entry_i=20, books=apt, signal_id=1),
        _sig(symbol="DOGEUSDT", side="SHORT", tf="15m", entry_i=400, books=doge, signal_id=2),
    ]
    pack = _cluster_map(raw)
    events = pd.DataFrame(pack["rows"])
    events["_sk"] = events.apply(event_sort_key, axis=1)
    events = events.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)
    res = run_global_single_position(
        events,
        {"DOGEUSDT": doge, "APTUSDT": apt},
        pack["clusters"],
        fee_pct=0.11,
    )
    trades = res["trades"]
    assert len(trades) >= 1
    # no overlapping intervals
    for i in range(len(trades)):
        for j in range(i + 1, len(trades)):
            a, b = trades[i], trades[j]
            assert not (
                pd.Timestamp(a["entry_time"]) < pd.Timestamp(b["exit_time"])
                and pd.Timestamp(b["entry_time"]) < pd.Timestamp(a["exit_time"])
            )


def test_no_queued_signal_after_exit():
    doge = _books_flat(300, px=100.0)
    apt = _books_flat(300, px=50.0)
    # DOGE hits SL quickly: low drops 2%
    doge.low[15] = 98.5  # -1.5% enough for 15m SL 1%
    raw = [
        _sig(symbol="DOGEUSDT", side="LONG", tf="15m", entry_i=10, books=doge, signal_id=0),
        # APT signal entry while DOGE still notionally open timeline but after we arrange exit
        _sig(symbol="APTUSDT", side="LONG", tf="15m", entry_i=12, books=apt, signal_id=1),
        # only a NEW signal after exit should open
        _sig(symbol="APTUSDT", side="SHORT", tf="30m", entry_i=50, books=apt, signal_id=2),
    ]
    pack = _cluster_map(raw)
    events = pd.DataFrame(pack["rows"])
    events["_sk"] = events.apply(event_sort_key, axis=1)
    events = events.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)
    res = run_global_single_position(
        events,
        {"DOGEUSDT": doge, "APTUSDT": apt},
        pack["clusters"],
        fee_pct=0.11,
    )
    # mid-trade APT at i=12 must be suppressed, not opened after DOGE exit
    def _u(x):
        t = pd.Timestamp(x)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

    supp_entries = [_u(s["entry_available_at"]) for s in res["suppressed"]]
    assert any(t == _u(apt.open_times[12]) for t in supp_entries)
    apt_trades = [t for t in res["trades"] if t["symbol"] == "APTUSDT"]
    for t in apt_trades:
        assert pd.Timestamp(t["entry_time"]) > pd.Timestamp(res["trades"][0]["exit_time"])


def test_new_signal_strictly_after_exit():
    doge = _books_flat(100, px=100.0)
    apt = _books_flat(100, px=50.0)
    doge.low[11] = 98.5  # SL on bar 11
    raw = [
        _sig(symbol="DOGEUSDT", side="LONG", tf="15m", entry_i=10, books=doge, signal_id=0),
        # entry at exact exit minute should NOT open
        _sig(symbol="APTUSDT", side="LONG", tf="15m", entry_i=11, books=apt, signal_id=1),
    ]
    pack = _cluster_map(raw)
    events = pd.DataFrame(pack["rows"])
    events["_sk"] = events.apply(event_sort_key, axis=1)
    events = events.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)
    res = run_global_single_position(
        events,
        {"DOGEUSDT": doge, "APTUSDT": apt},
        pack["clusters"],
        fee_pct=0.11,
    )
    assert len(res["trades"]) == 1
    assert res["trades"][0]["symbol"] == "DOGEUSDT"
    assert any(
        s["reason"] in ("SUPPRESSED_WHILE_POSITION_OPEN", "SUPPRESSED_ENTRY_NOT_STRICTLY_AFTER_EXIT")
        for s in res["suppressed"]
        if s["symbol"] == "APTUSDT"
    )


def test_p5a_same_symbol_only_and_other_cannot_upgrade():
    doge = _books_flat(400, px=100.0)
    apt = _books_flat(400, px=50.0)
    doge.high[:] = 100.2
    doge.low[:] = 99.8
    apt.high[:] = 50.1
    apt.low[:] = 49.9
    raw = [
        _sig(symbol="DOGEUSDT", side="LONG", tf="15m", entry_i=10, books=doge, signal_id=0),
        # same symbol higher TF → upgrade
        _sig(symbol="DOGEUSDT", side="LONG", tf="1h", entry_i=30, books=doge, signal_id=1),
        # other symbol same side higher TF → suppress, no upgrade
        _sig(symbol="APTUSDT", side="LONG", tf="4h", entry_i=40, books=apt, signal_id=2),
    ]
    pack = _cluster_map(raw)
    events = pd.DataFrame(pack["rows"])
    events["_sk"] = events.apply(event_sort_key, axis=1)
    events = events.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)
    res = run_global_single_position(
        events,
        {"DOGEUSDT": doge, "APTUSDT": apt},
        pack["clusters"],
        fee_pct=0.11,
    )
    assert res["funnel"]["upgrades"] == 1
    doge_tr = [t for t in res["trades"] if t["symbol"] == "DOGEUSDT"]
    assert doge_tr
    assert doge_tr[0]["highest_tf_reached"] == "1h"
    assert any(s["symbol"] == "APTUSDT" for s in res["suppressed"])


def test_deterministic_tie_break_higher_tf_first():
    doge = _books_flat(50, px=100.0)
    apt = _books_flat(50, px=50.0)
    # same entry_time and same conf → higher TF wins
    t = pd.Timestamp(doge.open_times[10])
    conf = t - pd.Timedelta(minutes=1)
    rows = [
        {
            "symbol": "DOGEUSDT",
            "side": "LONG",
            "signal_tf": "15m",
            "signal_id": 0,
            "cluster_id": 0,
            "cluster_key": "DOGEUSDT::0",
            "confirmation_available_at": conf,
            "entry_time": t,
            "entry_i": 10,
            "entry_price": 100.0,
            "is_tier_a": True,
            "is_q4": True,
        },
        {
            "symbol": "APTUSDT",
            "side": "SHORT",
            "signal_tf": "4h",
            "signal_id": 0,
            "cluster_id": 0,
            "cluster_key": "APTUSDT::0",
            "confirmation_available_at": conf,
            "entry_time": t,
            "entry_i": 10,
            "entry_price": 50.0,
            "is_tier_a": True,
            "is_q4": True,
        },
    ]
    clusters = {
        "DOGEUSDT": [{"rows": pd.DataFrame([rows[0]]), "side": "LONG"}],
        "APTUSDT": [{"rows": pd.DataFrame([rows[1]]), "side": "SHORT"}],
    }
    events = pd.DataFrame(rows)
    events["_sk"] = events.apply(event_sort_key, axis=1)
    events = events.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)
    assert events.iloc[0]["signal_tf"] == "4h"
    assert events.iloc[0]["symbol"] == "APTUSDT"
    res = run_global_single_position(
        events,
        {"DOGEUSDT": doge, "APTUSDT": apt},
        clusters,
        fee_pct=0.11,
    )
    assert res["trades"][0]["symbol"] == "APTUSDT"


def test_sl_first():
    high = np.array([101.5, 102.0])  # TP 1% for LONG at 100
    low = np.array([98.5, 99.0])  # SL 1%
    et, eg, exi, amb = _scan_exit("LONG", 100.0, high, low, 0, 0, 1.0, 1.0)
    assert et == "SL"
    assert amb is True
    assert eg == -1.0


def test_fees_net():
    doge = _books_flat(80, px=100.0)
    apt = _books_flat(80, px=50.0)
    doge.high[20] = 102.0  # TP 1% for 15m? need +1% → 101
    doge.high[20] = 101.2
    doge.low[:] = 99.5
    raw = [_sig(symbol="DOGEUSDT", side="LONG", tf="15m", entry_i=10, books=doge, signal_id=0)]
    pack = _cluster_map(raw)
    events = pd.DataFrame(pack["rows"])
    res = run_global_single_position(
        events,
        {"DOGEUSDT": doge, "APTUSDT": apt},
        pack["clusters"],
        fee_pct=0.11,
    )
    assert res["trades"]
    t = res["trades"][0]
    assert abs(t["net_return_pct"] - (t["gross_return_pct"] - 0.11)) < 1e-9


def test_compounding_fractions():
    nets = np.array([10.0, -5.0, 2.0])
    eq100 = compound_equity(nets, start=1000.0, fraction=1.0)
    assert abs(eq100[1] - 1100.0) < 1e-9
    assert abs(eq100[2] - 1100.0 * 0.95) < 1e-9
    eq25 = compound_equity(nets, start=1000.0, fraction=0.25)
    assert abs(eq25[1] - 1000.0 * (1 + 0.25 * 0.10)) < 1e-9
    df = pd.DataFrame(
        {
            "trade_id": [1, 2, 3],
            "exit_time": pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
            "entry_time": pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
            "net_return_pct": nets,
            "symbol": ["DOGEUSDT"] * 3,
            "side": ["LONG"] * 3,
        }
    )
    ann = annotate_trade_equities(df, start=1000.0)
    assert abs(ann.loc[0, "equity_after_100"] - 1100.0) < 1e-9
    assert abs(ann.loc[0, "equity_after_25"] - 1025.0) < 1e-9


def test_restart_deterministic():
    doge = _books_flat(120, px=100.0)
    apt = _books_flat(120, px=50.0)
    doge.low[25] = 98.5
    raw = [
        _sig(symbol="DOGEUSDT", side="LONG", tf="15m", entry_i=10, books=doge, signal_id=0),
        _sig(symbol="APTUSDT", side="SHORT", tf="30m", entry_i=40, books=apt, signal_id=1),
    ]
    pack = _cluster_map(raw)
    events = pd.DataFrame(pack["rows"])
    events["_sk"] = events.apply(event_sort_key, axis=1)
    events = events.sort_values("_sk").drop(columns=["_sk"]).reset_index(drop=True)
    books = {"DOGEUSDT": doge, "APTUSDT": apt}
    a = run_global_single_position(events, books, pack["clusters"], fee_pct=0.11)
    b = run_global_single_position(events, books, pack["clusters"], fee_pct=0.11)
    assert [t["net_return_pct"] for t in a["trades"]] == [t["net_return_pct"] for t in b["trades"]]
    assert [t["exit_reason"] for t in a["trades"]] == [t["exit_reason"] for t in b["trades"]]


def test_old_engine_still_importable_unchanged_api():
    # smoke: per-symbol engine still callable (strategy backtest not modified by this package)
    doge = _books_flat(60, px=100.0)
    sig = pd.DataFrame(
        [
            {
                "side": "LONG",
                "signal_tf": "15m",
                "signal_id": 0,
                "confirmation_available_at": pd.Timestamp(doge.open_times[5]),
                "entry_time": pd.Timestamp(doge.open_times[10]),
                "entry_i": 10,
                "entry_price": 100.0,
                "is_tier_a": True,
                "is_q4": True,
            }
        ]
    )
    res = run_symbol_backtest(
        "DOGEUSDT",
        sig,
        doge,
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=True,
        fee_pct=PRIMARY_FEE,
    )
    assert "trades" in res and "funnel" in res


def test_mysql_only_env_path():
    assert "regime_scanner" in str(ENV_FILE)
    assert ENV_FILE.name == ".env.regime_db"
    # package must not reference clickhouse as input
    from orderbook_analyse.fractal_wave_fade_global_single_position_db import analysis as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "clickhouse" not in src.lower()
    assert "load_mysql_ohlcv_tf" in src

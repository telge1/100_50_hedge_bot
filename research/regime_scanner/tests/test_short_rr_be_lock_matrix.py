"""Unit + smoke tests for Short RR1:2 + BE lock matrix."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.short_rr_be_lock_semantics import (
    CONSERVATIVE_LOCK_MODE,
    COST_PCT,
    PROFILES,
    ExitProfile,
    net_break_even_stop_short,
    round_short_stop_conservative,
    short_lock_trigger_price,
    short_progress,
    short_sl_price,
    short_tp_price,
    simulate_short_exit,
    trade_key,
)
from research.regime_scanner.run_short_rr_be_lock_matrix import run_matrix


def _ohlc(n: int, *, entry: float = 100.0):
    # flat path default
    highs = np.full(n, entry)
    lows = np.full(n, entry)
    closes = np.full(n, entry)
    ts = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return highs, lows, closes, list(ts)


def test_registry_has_exactly_13_profiles() -> None:
    assert len(PROFILES) == 13
    assert PROFILES[0].name == "reference_tp3_sl2"


def test_short_tp_sl_prices() -> None:
    entry = 100.0
    tp = short_tp_price(entry, 3.0)
    sl = short_sl_price(entry, -1.5)
    assert tp == pytest.approx(100 / 1.03)
    assert sl == pytest.approx(100 / 0.985)
    assert tp < entry < sl


def test_progress_thresholds() -> None:
    entry = 100.0
    tp = short_tp_price(entry, 2.0)
    for thr in (0.6, 0.7, 0.8):
        trig = short_lock_trigger_price(entry, tp, thr)
        assert short_progress(entry, trig, tp) == pytest.approx(thr)


def test_net_be_includes_costs_and_tick_round() -> None:
    entry = 100.0
    be, buf = net_break_even_stop_short(entry, cost_pct=0.20, slippage_pct=0.0, symbol="BTCUSDT")
    assert buf == pytest.approx(0.20)
    # BE below entry for short (small favorable lock)
    assert be < entry
    # conservative ceil to 0.1
    assert abs(be / 0.1 - round(be / 0.1)) < 1e-9 or be == round_short_stop_conservative(be, 0.1)


def test_lock_stays_active_and_never_before_trigger() -> None:
    entry = 100.0
    n = 20
    highs, lows, closes, ts = _ohlc(n, entry=entry)
    # bar 2: touch 60% lock trigger but not TP; bar 3+: bounce to BE
    tp = short_tp_price(entry, 2.0)
    trig = short_lock_trigger_price(entry, tp, 0.6)
    lows[2] = trig
    highs[4] = entry  # hit BE region
    lows[4] = entry - 0.01
    closes[4] = entry
    prof = ExitProfile("t", 2.0, -1.0, True, 0.60)
    out = simulate_short_exit(
        profile=prof,
        symbol="BTCUSDT",
        entry=entry,
        fill_i=0,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        n_bars=n,
    )
    assert out["lock_activated"] is True
    assert out["lock_trigger_candle"] == 2
    assert out["lock_active_from_bar"] == 3
    assert out["lock_active_from_bar"] > out["lock_trigger_candle"]
    assert out["final_exit_type"] == "lock_be"


def test_exact_threshold_triggers() -> None:
    entry = 100.0
    n = 10
    highs, lows, closes, ts = _ohlc(n, entry=entry)
    tp = short_tp_price(entry, 3.0)
    trig = short_lock_trigger_price(entry, tp, 0.7)
    lows[1] = trig
    highs[3] = entry * 1.01
    prof = ExitProfile("t", 3.0, -1.5, True, 0.70)
    out = simulate_short_exit(
        profile=prof, symbol="ETHUSDT", entry=entry, fill_i=0,
        highs=highs, lows=lows, closes=closes, timestamps=ts, n_bars=n,
    )
    assert out["lock_activated"] is True


def test_same_bar_trigger_and_old_sl_no_rescue() -> None:
    entry = 100.0
    n = 10
    highs, lows, closes, ts = _ohlc(n, entry=entry)
    # same bar: favorable to lock trigger AND adverse to SL
    tp = short_tp_price(entry, 2.0)
    trig = short_lock_trigger_price(entry, tp, 0.6)
    sl = short_sl_price(entry, -1.0)
    lows[1] = trig
    highs[1] = sl
    prof = ExitProfile("t", 2.0, -1.0, True, 0.60)
    out = simulate_short_exit(
        profile=prof, symbol="BTCUSDT", entry=entry, fill_i=0,
        highs=highs, lows=lows, closes=closes, timestamps=ts, n_bars=n,
    )
    assert out["final_exit_type"] in {"SL", "same_bar_conservative_sl"}
    assert out["lock_activated"] is False or out["final_exit_type"] != "lock_be"


def test_same_bar_trigger_and_tp_allows_tp() -> None:
    entry = 100.0
    n = 10
    highs, lows, closes, ts = _ohlc(n, entry=entry)
    tp = short_tp_price(entry, 2.0)
    lows[1] = tp  # hits TP (and thus also lock threshold)
    highs[1] = entry  # no SL
    prof = ExitProfile("t", 2.0, -1.0, True, 0.60)
    out = simulate_short_exit(
        profile=prof, symbol="BTCUSDT", entry=entry, fill_i=0,
        highs=highs, lows=lows, closes=closes, timestamps=ts, n_bars=n,
    )
    assert out["final_exit_type"] == "TP"


def test_active_lock_and_tp_same_bar_prefers_be() -> None:
    entry = 100.0
    n = 15
    highs, lows, closes, ts = _ohlc(n, entry=entry)
    tp = short_tp_price(entry, 2.0)
    trig = short_lock_trigger_price(entry, tp, 0.6)
    lows[1] = trig
    # after activation (from bar 2), bar 2 hits both BE and TP
    be, _ = net_break_even_stop_short(entry, symbol="BTCUSDT")
    highs[2] = max(be, entry)
    lows[2] = tp
    prof = ExitProfile("t", 2.0, -1.0, True, 0.60)
    out = simulate_short_exit(
        profile=prof, symbol="BTCUSDT", entry=entry, fill_i=0,
        highs=highs, lows=lows, closes=closes, timestamps=ts, n_bars=n,
    )
    assert out["final_exit_type"] == "lock_be"
    assert out["same_bar_ambiguous"] is True


def test_time_exit_after_lock() -> None:
    entry = 100.0
    n = 10
    highs, lows, closes, ts = _ohlc(n, entry=entry)
    tp = short_tp_price(entry, 3.0)
    trig = short_lock_trigger_price(entry, tp, 0.8)
    lows[1] = trig
    be, _ = net_break_even_stop_short(entry, symbol="BTCUSDT")
    # stay strictly between BE stop and TP (no BE/TP touch)
    mid_hi = (be + tp) / 2 - 0.05
    mid_lo = (be + tp) / 2 - 0.15
    assert mid_hi < be and mid_lo > tp
    for i in range(2, n):
        highs[i] = mid_hi
        lows[i] = mid_lo
        closes[i] = (mid_hi + mid_lo) / 2
    prof = ExitProfile("t", 3.0, -1.5, True, 0.80)
    out = simulate_short_exit(
        profile=prof, symbol="BTCUSDT", entry=entry, fill_i=0,
        highs=highs, lows=lows, closes=closes, timestamps=ts, n_bars=n,
        horizon_bars=5,
    )
    assert out["final_exit_type"] in {"time_exit", "data_end"}
    assert out["lock_activated"] is True


def test_no_long_support_in_profiles() -> None:
    for p in PROFILES:
        assert p.sl_pct < 0
        assert p.tp_pct > 0


def test_pair_key_parity_smoke(tmp_path: Path) -> None:
    src = Path(
        "/home/telgenbuescher/projects/signal_research/research/regime_scanner/results/"
        "signal_path_audit_15m_holdout_btc_eth_bnb_tp3_sl2_20260722"
    )
    if not (src / "multicoin_trade_results.csv").exists():
        pytest.skip("holdout audit missing")
    # tiny: only reference + one lock profile on BTC only would still load all frames;
    # use full runner with 2 profiles
    out = tmp_path / "smoke"
    meta = run_matrix(
        input_audit_dir=src,
        output_dir=out,
        coins=["BTCUSDT", "ETHUSDT", "BNBUSDT"],
        profiles=["reference_tp3_sl2", "rr2_tp2_sl1_no_lock", "rr2_tp2_sl1_lock60_be"],
        resume=False,
    )
    assert meta["integrity_ok"] is True
    raw = pd.read_csv(out / "raw_trades.csv")
    assert set(raw["profile"]) == {
        "reference_tp3_sl2",
        "rr2_tp2_sl1_no_lock",
        "rr2_tp2_sl1_lock60_be",
    }
    # pair keys identical across profiles
    keys = {p: set(g.trade_key) for p, g in raw.groupby("profile")}
    assert keys["reference_tp3_sl2"] == keys["rr2_tp2_sl1_lock60_be"]
    assert (raw.final_pnl_pct.notna()).all()
    assert CONSERVATIVE_LOCK_MODE in (out / "frozen_matrix.json").read_text()

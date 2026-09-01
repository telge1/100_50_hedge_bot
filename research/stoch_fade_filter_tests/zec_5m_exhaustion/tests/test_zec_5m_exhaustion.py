"""Unit tests for the frozen 5m exhaustion block and forward-path accounting."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.stoch_fade_filter_tests.zec_5m_exhaustion.forward import (
    aligned_return_pct,
    first_barrier,
    mfe_mae_pct,
    trade_forward_paths,
)
from research.stoch_fade_filter_tests.zec_5m_exhaustion.metrics import pnl_metrics
from research.stoch_fade_filter_tests.zec_5m_exhaustion.rule import (
    STOCH_HIGH,
    STOCH_LOW,
    stoch_exhausted_in_trade_direction,
)


def test_definition_mirrors_long_short():
    assert STOCH_LOW == 20.0
    assert STOCH_HIGH == 80.0
    assert stoch_exhausted_in_trade_direction("LONG", 80.0001) is True
    assert stoch_exhausted_in_trade_direction("LONG", 80.0) is False
    assert stoch_exhausted_in_trade_direction("LONG", 79.9) is False
    assert stoch_exhausted_in_trade_direction("SHORT", 19.999) is True
    assert stoch_exhausted_in_trade_direction("SHORT", 20.0) is False
    assert stoch_exhausted_in_trade_direction("SHORT", 20.1) is False


def test_missing_k_is_not_a_block():
    assert stoch_exhausted_in_trade_direction("LONG", None) is False
    assert stoch_exhausted_in_trade_direction("SHORT", np.nan) is False
    assert stoch_exhausted_in_trade_direction("LONG", float("nan")) is False


def test_d_and_phase_are_not_inputs():
    # Same K must yield the same flag regardless of any D/phase story.
    assert stoch_exhausted_in_trade_direction("SHORT", 10.0) is True
    assert stoch_exhausted_in_trade_direction("LONG", 10.0) is False


def test_last_closed_5m_index_no_lookahead():
    from research.stoch_fade_filter_tests.zec_5m_exhaustion.io import last_closed_index

    avail = np.array(
        [
            np.datetime64("2026-08-16T09:40:00"),
            np.datetime64("2026-08-16T09:45:00"),
            np.datetime64("2026-08-16T09:50:00"),
        ]
    )
    entry = np.datetime64("2026-08-16T09:46:00")
    i = last_closed_index(avail, entry)
    assert i == 1
    assert avail[i] <= entry
    assert avail[2] > entry


def test_block_uses_only_entry_k():
    trades = pd.DataFrame(
        {
            "direction": ["LONG", "SHORT"],
            "tf_5m_stoch_k": [81.0, 12.0],
            "outcome": ["WIN", "LOSS"],
        }
    )
    flags = [
        stoch_exhausted_in_trade_direction(r.direction, r.tf_5m_stoch_k)
        for r in trades.itertuples()
    ]
    assert flags == [True, True]
    trades["later_price"] = [0.0, 0.0]
    flags2 = [
        stoch_exhausted_in_trade_direction(r.direction, r.tf_5m_stoch_k)
        for r in trades.itertuples()
    ]
    assert flags2 == flags


def test_aligned_4h_6h_returns():
    assert abs(aligned_return_pct("LONG", 100.0, 101.0) - 1.0) < 1e-12
    assert abs(aligned_return_pct("SHORT", 100.0, 99.0) - 1.0) < 1e-12
    assert abs(aligned_return_pct("SHORT", 100.0, 101.0) + 1.0) < 1e-12


def test_mfe_mae_until_horizon():
    high = np.array([101.0, 102.0, 100.5])
    low = np.array([99.5, 99.0, 99.8])
    mfe, mae = mfe_mae_pct("LONG", 100.0, high, low)
    assert abs(mfe - 2.0) < 1e-12
    assert abs(mae - 1.0) < 1e-12
    mfe_s, mae_s = mfe_mae_pct("SHORT", 100.0, high, low)
    assert abs(mfe_s - 1.0) < 1e-12
    assert abs(mae_s - 2.0) < 1e-12


def test_sl_first_same_bar():
    high = np.array([102.0])
    low = np.array([98.0])
    kind, idx = first_barrier(direction="LONG", high=high, low=low, tp=101.0, sl=99.0)
    assert kind == "SL"
    assert idx == 0


def _grid(n: int, start: str = "2026-08-16T09:46:00") -> np.ndarray:
    t0 = np.datetime64(start)
    return t0 + np.arange(n).astype("timedelta64[m]")


def test_exit_before_horizon_splits_paths_and_does_not_change_outcome():
    times = _grid(400)
    open_ = np.full(400, 100.0)
    high = np.full(400, 100.2)
    low = np.full(400, 99.8)
    # Adverse spike after a synthetic SL exit at +10m, then recovery.
    high[10] = 102.0
    low[50] = 98.0
    paths = trade_forward_paths(
        direction="SHORT",
        entry_price=100.0,
        tp_price=99.0,
        sl_price=101.5,
        entry_time=times[0],
        exit_time=times[10],
        exit_reason="SL",
        times=times,
        open_=open_,
        high=high,
        low=low,
    )
    assert paths["4h_status"] == "OK"
    assert paths["4h_still_open"] is False
    assert paths["4h_post_exit_available"] is True
    assert paths["original_exit_reason"] == "SL"
    # Market path after exit can be favorable without rewriting the SL outcome.
    assert paths["4h_aligned_return_pct"] == aligned_return_pct("SHORT", 100.0, 100.0)
    assert paths["4h_in_trade_sl_touched"] is True


def test_missing_horizon_is_unavailable_not_zero():
    times = _grid(30)
    paths = trade_forward_paths(
        direction="LONG",
        entry_price=100.0,
        tp_price=101.0,
        sl_price=99.0,
        entry_time=times[0],
        exit_time=None,
        exit_reason=None,
        times=times,
        open_=np.full(30, 100.0),
        high=np.full(30, 100.1),
        low=np.full(30, 99.9),
    )
    assert paths["15m_status"] == "OK"
    assert paths["4h_status"] == "HORIZON_UNAVAILABLE"
    assert paths["6h_status"] == "HORIZON_UNAVAILABLE"
    assert "4h_aligned_return_pct" not in paths or paths.get("4h_aligned_return_pct") in (None,)
    assert paths.get("4h_price") is None


def test_fees_and_baseline_metrics():
    frame = pd.DataFrame(
        {
            "outcome": ["WIN", "LOSS", "OPEN"],
            "pnl_pct_gross": [1.0, -1.0, np.nan],
            "pnl_pct_net": [0.89, -1.11, np.nan],
            "entry_time": pd.to_datetime(["2026-03-01", "2026-03-02", "2026-03-03"], utc=True),
            "signal_id": ["a", "b", "c"],
        }
    )
    m = pnl_metrics(frame, variant="BASELINE")
    assert m["wins"] == 1
    assert m["losses"] == 1
    assert m["open"] == 1
    assert abs(m["gross_sum"] - 0.0) < 1e-12
    assert abs(m["net_sum"] - (0.89 - 1.11)) < 1e-12
    assert abs(m["fees_total_pp"] - 0.22) < 1e-12
    assert m["winrate"] == 0.5


def test_deterministic_sample_seed():
    rng1 = np.random.default_rng(20260817)
    rng2 = np.random.default_rng(20260817)
    assert list(rng1.choice(np.arange(50), size=10, replace=False)) == list(
        rng2.choice(np.arange(50), size=10, replace=False)
    )

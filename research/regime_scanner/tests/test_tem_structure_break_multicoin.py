"""Multicoin / determinism tests for frozen v2 structure-break eval."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from research.regime_scanner.tem_structure_break.eval_common import (
    AAVE_DEV_TRADE_ID,
    CoinFrameCache,
    TradeSpec,
    load_blocker_specs,
    summarize_trade,
)
from research.regime_scanner.tem_structure_break.monitor import run_in_trade_monitor


def _toy_frames(n: int = 40):
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [100.0] * 10 + [98.0] * (n - 10),
            "volume": 1.0,
        }
    )
    trace = frame.copy()
    trace["decision_time"] = ts + pd.Timedelta(minutes=5)
    for col, val in {
        "major_direction": 1,
        "ema_regime_direction": 1,
        "m30_major_direction": 1,
        "h4_major_direction": -1,
        "protected_low": 99.5,
        "protected_high": 102.0,
        "ema_9": 100.0,
        "ema_20": 99.0,
        "ema_59": 98.0,
        "ema_200": 97.0,
        "close_vs_ema_200_pct": 2.0,
    }.items():
        trace[col] = val
    empty = pd.DataFrame(
        columns=[
            "timestamp",
            "htf_close_decision",
            "close",
            "protected_low",
            "protected_high",
            "major_direction",
            "close_break_protected_down",
            "external_bos_down",
            "arm_edge_external_bear",
        ]
    )
    return frame, trace, empty


def test_blocker_specs_mark_aave_as_development() -> None:
    specs = load_blocker_specs()
    assert len(specs) == 27
    aave = [s for s in specs if s.trade_id == AAVE_DEV_TRADE_ID]
    assert len(aave) == 1
    assert aave[0].holdout_bucket == "development"
    assert sum(1 for s in specs if s.holdout_bucket == "holdout") == 26


def test_batch_order_independence_summaries() -> None:
    frame, trace, empty = _toy_frames()
    specs = [
        TradeSpec("X", "X|t|1", "2026-01-01T00:25:00+00:00", 100.0, 5, 20, cohort="blocker"),
        TradeSpec("X", "X|t|2", "2026-01-01T00:30:00+00:00", 100.0, 6, 21, cohort="blocker"),
    ]

    def run_all(order):
        outs = []
        for sp in order:
            rt = run_in_trade_monitor(
                frame_5m=frame,
                entry_bar=sp.start_bar,
                entry_price=sp.entry_price,
                end_bar=sp.end_bar,
                trace=trace,
                h1_frame=empty,
                h4_frame=empty,
            )
            outs.append(summarize_trade(sp, rt, frame=frame)["final_state"])
        return outs

    with (
        patch(
            "research.regime_scanner.tem_structure_break.monitor.build_5m_trace",
            return_value=trace,
        ),
        patch(
            "research.regime_scanner.tem_structure_break.monitor.build_htf_structure_frame",
            return_value=empty,
        ),
    ):
        a = run_all(specs)
        b = run_all(list(reversed(specs)))
    assert a == list(reversed(b))


def test_coin_frame_cache_reuses_instance() -> None:
    cache = CoinFrameCache()
    frame, trace, empty = _toy_frames()

    class Fake:
        pass

    def fake_get(coin, candle_limit=50000):
        if coin in cache._cache:
            return cache._cache[coin]
        from research.regime_scanner.tem_structure_break.eval_common import CoinFrames

        cf = CoinFrames(coin, frame, trace, empty, empty)
        cache._cache[coin] = cf
        return cf

    cache.get = fake_get  # type: ignore[method-assign]
    a = cache.get("AAAUSDT")
    b = cache.get("AAAUSDT")
    assert a is b

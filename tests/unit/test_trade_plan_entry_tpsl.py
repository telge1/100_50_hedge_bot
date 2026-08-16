"""Unit tests for frozen entry + initial TP/SL trade plan."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from signal_generator.pipeline.trade_plan import (
    PRICE_SOURCE,
    attach_resolved_entries,
    levels_for_entry,
    reconstruct_trade_plan,
)
from signal_generator.strategy.wave_fade.parameters import TPSL_BY_TF
from signal_generator.strategy.wave_fade.tpsl import tpsl_for_tf


def test_tpsl_by_tf_matches_parameters():
    for tf, (tp, sl) in TPSL_BY_TF.items():
        assert tpsl_for_tf(tf) == (float(tp), float(sl))
    assert tpsl_for_tf("15m") == (1.0, 1.0)
    assert tpsl_for_tf("30m") == (2.0, 1.5)
    assert tpsl_for_tf("1h") == (2.0, 1.5)
    assert tpsl_for_tf("4h") == (4.0, 2.0)


def test_long_short_initial_levels_and_be50_does_not_overwrite_sl():
    long_lv = levels_for_entry(side="LONG", timeframe="15m", entry_price=100.0)
    assert long_lv["tp_price"] == pytest.approx(101.0)
    assert long_lv["sl_price"] == pytest.approx(99.0)
    assert long_lv["tp_price"] > long_lv["entry_price"] > long_lv["sl_price"]
    assert long_lv["break_even_price"] == 100.0
    assert long_lv["be_trigger_price"] == pytest.approx(100.5)
    # Initial SL remains 99 — BE trigger is separate
    assert long_lv["sl_price"] != long_lv["break_even_price"]

    short_lv = levels_for_entry(side="SHORT", timeframe="15m", entry_price=100.0)
    assert short_lv["tp_price"] == pytest.approx(99.0)
    assert short_lv["sl_price"] == pytest.approx(101.0)
    assert short_lv["tp_price"] < short_lv["entry_price"] < short_lv["sl_price"]


@pytest.mark.parametrize(
    "tf,tp,sl",
    [
        ("15m", 1.0, 1.0),
        ("30m", 2.0, 1.5),
        ("1h", 2.0, 1.5),
        ("4h", 4.0, 2.0),
    ],
)
def test_tf_tpsl_parity(tf, tp, sl):
    lv = levels_for_entry(side="LONG", timeframe=tf, entry_price=50.0)
    assert lv["tp_pct"] == tp
    assert lv["sl_pct"] == sl
    assert lv["tp_price"] == pytest.approx(50.0 * (1 + tp / 100.0))
    assert lv["sl_price"] == pytest.approx(50.0 * (1 - sl / 100.0))


def test_resolve_entries_t0_open_and_attach_tpsl():
    conf = datetime(2026, 8, 10, 12, 15, tzinfo=timezone.utc)
    # open_times tz-naive ns (freeze books)
    open_times = np.array(
        [
            np.datetime64("2026-08-10T12:14:00"),
            np.datetime64("2026-08-10T12:15:00"),
            np.datetime64("2026-08-10T12:16:00"),
            np.datetime64("2026-08-10T12:17:00"),
        ],
        dtype="datetime64[ns]",
    )
    opens = np.array([1.0, 2.0, 3.5, 4.0], dtype=float)
    ev = pd.DataFrame(
        {
            "confirmation_available_at": [conf],
            "side": ["LONG"],
            "signal_tf": ["15m"],
            "timeframe": ["15m"],
        }
    )
    out = attach_resolved_entries(ev, open_times, opens)
    assert bool(out.iloc[0]["entry_valid"]) is True
    assert float(out.iloc[0]["entry_price"]) == pytest.approx(3.5)
    assert float(out.iloc[0]["tp_price"]) == pytest.approx(3.5 * 1.01)
    assert float(out.iloc[0]["sl_price"]) == pytest.approx(3.5 * 0.99)
    assert out.iloc[0]["price_source"] == PRICE_SOURCE


def test_reconstruct_trade_plan_short_30m():
    conf = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    open_times = np.array(
        [np.datetime64("2026-08-10T14:00:00"), np.datetime64("2026-08-10T14:01:00")],
        dtype="datetime64[ns]",
    )
    opens = np.array([0.2, 0.21], dtype=float)
    plan = reconstruct_trade_plan(
        confirmation_available_at=conf,
        side="SHORT",
        timeframe="30m",
        open_times=open_times,
        opens=opens,
    )
    assert plan["entry_valid"] is True
    assert plan["entry_price"] == pytest.approx(0.21)
    assert plan["tp_pct"] == 2.0
    assert plan["sl_pct"] == 1.5
    assert plan["tp_price"] < plan["entry_price"] < plan["sl_price"]

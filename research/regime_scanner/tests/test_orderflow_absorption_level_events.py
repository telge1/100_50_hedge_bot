"""Event dedupe, confirmations, outcomes, controls."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig, default_config
from research.regime_scanner.orderflow_absorption_level.confirmations import (
    build_confirmation_events,
    confirmation_r0,
    confirmation_r1,
    confirmation_r2,
)
from research.regime_scanner.orderflow_absorption_level.controls import treatment_for_event
from research.regime_scanner.orderflow_absorption_level.events import (
    build_absorption_level_events,
    make_event_id,
)
from research.regime_scanner.orderflow_absorption_level.outcomes_level import compute_event_outcomes


def _frame(n: int = 40) -> pd.DataFrame:
    base = pd.Timestamp("2026-04-01", tz="UTC")
    rows = []
    for i in range(n):
        rows.append(
            {
                "symbol": "BTCUSDT",
                "bucket_start": base + pd.Timedelta(minutes=5 * i),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "atr_14": 1.0,
                "sequence_id": 1,
            }
        )
    return pd.DataFrame(rows)


def _asg(i: int, *, level_id: str = "L1", dist: float = 0.1, no_level: bool = False, far: bool = False) -> dict:
    return {
        "symbol": "BTCUSDT",
        "timestamp": str(pd.Timestamp("2026-04-01", tz="UTC") + pd.Timedelta(minutes=5 * i)),
        "anchor_index": i,
        "pattern": "A4",
        "flow_rule": "F1",
        "lookback": 24,
        "level_id": None if no_level else level_id,
        "level_type": None if no_level else "protected",
        "side": "support",
        "level_price": None if no_level else 99.5,
        "distance_atr": None if no_level else dist,
        "distance_bucket": "no_level" if no_level else ("far" if far else "touch"),
        "confluent": False,
        "no_level": no_level,
        "far_from_level": far,
        "atr_reference": 1.0,
        "anchor_price": 100.0,
    }


def test_event_id_deterministic():
    a = make_event_id(
        symbol="BTCUSDT",
        pattern="A4",
        flow_rule="F1",
        lookback=24,
        level_id="L1",
        event_start_timestamp="2026-04-01T00:00:00Z",
    )
    b = make_event_id(
        symbol="BTCUSDT",
        pattern="A4",
        flow_rule="F1",
        lookback=24,
        level_id="L1",
        event_start_timestamp="2026-04-01T00:00:00Z",
    )
    assert a == b


def test_consecutive_a4_deduped():
    df = _frame()
    assigns = [_asg(i) for i in range(10, 15)]
    cfg = default_config()
    events = build_absorption_level_events(df, assigns, patterns=("A4",), cfg=cfg)
    assert len(events) == 1
    assert events[0]["anchor_count"] == 5


def test_false_to_true_starts_event():
    df = _frame()
    # second start after cooldown (6 bars) from first end
    assigns = [_asg(10), _asg(18)]
    cfg = default_config()
    events = build_absorption_level_events(df, assigns, patterns=("A4",), cfg=cfg)
    assert len(events) >= 2


def test_level_id_change_new_event():
    df = _frame()
    assigns = [_asg(10, level_id="L1"), _asg(11, level_id="L2")]
    cfg = default_config()
    events = build_absorption_level_events(df, assigns, patterns=("A4",), cfg=cfg)
    assert len(events) == 2


def test_zone_reentry_new_event():
    df = _frame()
    assigns = [
        _asg(10, dist=0.1),
        _asg(11, dist=0.8, far=True),
        _asg(12, dist=0.1),
    ]
    cfg = default_config()
    events = build_absorption_level_events(df, assigns, patterns=("A4",), cfg=cfg)
    assert len(events) >= 2


def test_r0_entry_equals_event_start():
    ev = {
        "event_id": "e1",
        "event_start_index": 10,
        "event_start_timestamp": "t0",
        "pattern": "A4",
        "direction": "bullish",
    }
    c = confirmation_r0(ev)
    assert c["entry_eligible_index"] == 10
    assert c["confirmation_type"] == "R0"


def test_r1_rejection():
    df = _frame()
    # wick below support, close back above
    df.loc[10, "low"] = 98.0
    df.loc[10, "close"] = 100.2
    df.loc[10, "high"] = 101.0
    ev = {
        "event_id": "e1",
        "event_start_index": 10,
        "event_end_index": 12,
        "event_start_timestamp": str(df["bucket_start"].iloc[10]),
        "level_price": 99.5,
        "level_side": "support",
        "no_level": False,
        "far_from_level": False,
        "pattern": "A4",
        "direction": "bullish",
        "sequence_id": 1,
    }
    cfg = default_config()
    r1 = confirmation_r1(df, ev, cfg)
    assert r1 is not None
    assert r1["confirmation_type"] == "R1"
    assert r1["entry_eligible_index"] >= 10


def test_r2_break_reclaim():
    df = _frame()
    df.loc[10, "low"] = 98.0
    df.loc[10, "close"] = 99.0  # break
    df.loc[11, "close"] = 100.5  # reclaim
    df.loc[11, "low"] = 99.0
    ev = {
        "event_id": "e1",
        "event_start_index": 10,
        "event_end_index": 14,
        "event_start_timestamp": str(df["bucket_start"].iloc[10]),
        "level_price": 99.5,
        "level_side": "support",
        "no_level": False,
        "far_from_level": False,
        "pattern": "A4",
        "direction": "bullish",
        "sequence_id": 1,
    }
    cfg = default_config()
    r2 = confirmation_r2(df, ev, cfg)
    assert r2 is not None
    assert r2["entry_eligible_index"] >= 10


def test_outcomes_start_at_entry_plus_one():
    df = _frame(60)
    # make a clear up move after bar 20
    for i in range(21, 30):
        df.loc[i, "high"] = 100.0 + (i - 20) * 0.5
        df.loc[i, "close"] = 100.0 + (i - 20) * 0.4
        df.loc[i, "low"] = 99.5
    ev = {
        "event_id": "e1",
        "confirmation_id": "e1|R0",
        "confirmation_type": "R0",
        "symbol": "BTCUSDT",
        "pattern": "A4",
        "direction": "bullish",
        "flow_rule": "F1",
        "lookback": 24,
        "level_id": "L1",
        "level_type": "protected",
        "level_side": "support",
        "level_price": 99.5,
        "entry_eligible_index": 20,
        "entry_eligible_timestamp": str(df["bucket_start"].iloc[20]),
        "no_level": False,
        "far_from_level": False,
        "distance_bucket_at_entry": "touch",
        "confluent": False,
        "sequence_id": 1,
    }
    cfg = default_config()
    outs = compute_event_outcomes(df, [ev], cfg)
    assert len(outs) == 1
    assert outs[0]["entry_eligible_index"] == 20
    assert outs[0].get("h6_valid") is True


def test_same_bar_adverse_first_bullish():
    df = _frame(40)
    # same bar hits both sides hard
    df.loc[21, "high"] = 102.0
    df.loc[21, "low"] = 98.0
    df.loc[21, "close"] = 100.0
    ev = {
        "event_id": "e1",
        "confirmation_id": "e1|R0",
        "confirmation_type": "R0",
        "symbol": "BTCUSDT",
        "pattern": "A4",
        "direction": "bullish",
        "flow_rule": "F1",
        "lookback": 24,
        "level_id": "L1",
        "level_type": "protected",
        "level_side": "support",
        "level_price": 99.5,
        "entry_eligible_index": 20,
        "entry_eligible_timestamp": str(df["bucket_start"].iloc[20]),
        "no_level": False,
        "far_from_level": False,
        "distance_bucket_at_entry": "touch",
        "confluent": False,
    }
    cfg = default_config()
    outs = compute_event_outcomes(df, [ev], cfg)
    assert outs
    # primary thr 0.25%
    assert outs[0].get("h6_0_25pct_same_bar") is True
    assert outs[0].get("h6_0_25pct_adverse_first") is True
    assert outs[0].get("h6_0_25pct_favorable_first") is False


def test_treatments():
    ev = {
        "pattern": "A4",
        "no_level": False,
        "far_from_level": False,
        "level_type": "protected",
        "level_side": "support",
        "confluent": True,
    }
    labels = treatment_for_event(ev)
    assert "A4_AT_ANY_SUPPORT" in labels
    assert "A4_AT_PROTECTED_LOW" in labels
    assert "A4_AT_CONFLUENT_SUPPORT" in labels


def test_build_confirmations_separates_types():
    df = _frame()
    df.loc[10, "low"] = 98.0
    df.loc[10, "close"] = 100.2
    events = [
        {
            "event_id": "e1",
            "symbol": "BTCUSDT",
            "sequence_id": 1,
            "pattern": "A4",
            "direction": "bullish",
            "flow_rule": "F1",
            "lookback": 24,
            "level_id": "L1",
            "level_type": "protected",
            "level_side": "support",
            "level_price": 99.5,
            "event_start_index": 10,
            "event_end_index": 12,
            "event_start_timestamp": str(df["bucket_start"].iloc[10]),
            "event_end_timestamp": str(df["bucket_start"].iloc[12]),
            "no_level": False,
            "far_from_level": False,
            "distance_bucket_at_entry": "touch",
            "confluent": False,
        }
    ]
    cfg = default_config()
    confs = build_confirmation_events(df, events, cfg)
    types = {c["confirmation_type"] for c in confs}
    assert "R0" in types

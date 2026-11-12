"""Tests for manual long-box research (§7)."""

from __future__ import annotations

import copy
import json

import pytest

from research.manual_long_box_research_v1.entry_touch import entry_level_touch
from research.manual_long_box_research_v1.l1_confirm import confirm_a2, evaluate_confirmation
from research.manual_long_box_research_v1.recognition import (
    is_manual_long_box,
    recognize_long_boxes,
    setups_invariant_across_timeframe_view,
)
from research.manual_long_box_research_v1.simulator import replay_long_boxes
from research.manual_long_box_research_v1.validation import validate_long_geometry


def _box(
    did: str,
    entry: float,
    stop: float,
    target: float,
    t0: int = 1_700_000_000,
    t1: int = 1_700_010_000,
) -> dict:
    return {
        "drawing_id": did,
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "visible": True,
        "created_on_timeframe": "5m",
        "timeframe_scope": "all",
        "start_timestamp": t0,
        "end_timestamp": t1,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "position_notional": 100.0,
    }


def _payload(*drawings) -> dict:
    return {"version": 1, "drawings": list(drawings)}


def _candle(i: int, o: float, h: float, l: float, c: float, t0: int = 1_700_000_000) -> dict:
    return {"time": t0 + i * 300, "open": o, "high": h, "low": l, "close": c}


def test_one_valid_long_box():
    setups, rejects = recognize_long_boxes(_payload(_box("d-aaa", 0.10, 0.09, 0.12)))
    assert len(setups) == 1
    assert not rejects
    assert setups[0].entry_price == 0.10


def test_two_overlapping_boxes():
    b1 = _box("d-b1", 0.10, 0.09, 0.12)
    b2 = _box("d-b2", 0.11, 0.09, 0.13)
    setups, _ = recognize_long_boxes(_payload(b1, b2))
    assert len(setups) == 2


def test_reject_invalid_sl_tp():
    _, rejects = recognize_long_boxes(_payload(_box("d-bad", 0.10, 0.11, 0.12)))
    assert any(r.reason == "stop_not_below_entry" for r in rejects)


def test_strategy_import_not_manual():
    row = _box("p17ab-pos-1", 0.1, 0.09, 0.12)
    assert not is_manual_long_box(row)


def test_entry_touch_no_lookahead():
    c = _candle(0, 0.11, 0.11, 0.10, 0.105)
    ok, _ = entry_level_touch(c, 0.10)
    assert ok


def test_touch_no_confirm_no_trade():
    candles = [
        _candle(0, 0.11, 0.11, 0.099, 0.105),  # touch entry
        _candle(1, 0.105, 0.106, 0.104, 0.1045),  # red / no A2
        _candle(2, 0.104, 0.105, 0.103, 0.103),
    ]
    res = replay_long_boxes(candles, _payload(_box("d-x", 0.10, 0.09, 0.12)))
    assert not res.trades
    assert any(e.phase in ("TOUCH", "WATCH", "WATCHING") for e in res.events)


def test_touch_and_confirm_entry():
    candles = [
        _candle(0, 0.11, 0.11, 0.099, 0.105),
        _candle(1, 0.105, 0.112, 0.104, 0.111),  # confirm A2
        _candle(2, 0.111, 0.115, 0.110, 0.114),  # fill open
        _candle(3, 0.114, 0.13, 0.113, 0.12),
    ]
    res = replay_long_boxes(candles, _payload(_box("d-y", 0.10, 0.09, 0.12)))
    assert len(res.trades) == 1
    assert res.trades[0].fill_price == candles[2]["open"]


def test_delayed_confirmation_bars_later():
    candles = [
        _candle(0, 0.11, 0.11, 0.099, 0.105),
        _candle(1, 0.105, 0.106, 0.099, 0.1005),
        _candle(2, 0.1005, 0.101, 0.099, 0.100),
        _candle(3, 0.100, 0.108, 0.099, 0.107),  # confirm
        _candle(4, 0.107, 0.11, 0.106, 0.109),  # fill
    ]
    res = replay_long_boxes(candles, _payload(_box("d-z", 0.10, 0.09, 0.12)), watch_window_bars=8)
    assert len(res.trades) == 1


def test_second_entry_level_after_first_breaks_through():
    candles = [
        _candle(0, 0.12, 0.12, 0.104, 0.11),  # through 0.105 to 0.11
        _candle(1, 0.11, 0.112, 0.104, 0.105),
        _candle(2, 0.105, 0.108, 0.099, 0.107),  # touch 0.10 confirm
        _candle(3, 0.107, 0.11, 0.106, 0.109),
    ]
    payload = _payload(
        _box("d-low", 0.105, 0.09, 0.12),
        _box("d-high", 0.10, 0.09, 0.12),
    )
    res = replay_long_boxes(candles, payload, watch_window_bars=10)
    watching = [e for e in res.events if e.phase in ("TOUCH", "WATCH", "WATCHING")]
    assert len(watching) >= 1


def test_no_duplicate_entry():
    candles = [
        _candle(0, 0.11, 0.11, 0.099, 0.105),
        _candle(1, 0.105, 0.112, 0.104, 0.111),
        _candle(2, 0.111, 0.115, 0.110, 0.114),
        _candle(3, 0.114, 0.115, 0.099, 0.11),
        _candle(4, 0.11, 0.112, 0.099, 0.111),
        _candle(5, 0.111, 0.112, 0.104, 0.111),
    ]
    res = replay_long_boxes(candles, _payload(_box("d-dup", 0.10, 0.09, 0.12)))
    assert len(res.trades) == 1


def test_two_boxes_only_one_open_long():
    candles = [
        _candle(0, 0.11, 0.11, 0.098, 0.105),
        _candle(1, 0.105, 0.112, 0.104, 0.111),
        _candle(2, 0.111, 0.115, 0.110, 0.114),
        _candle(3, 0.114, 0.115, 0.099, 0.111),
        _candle(4, 0.111, 0.112, 0.099, 0.112),
    ]
    payload = _payload(_box("d-a", 0.10, 0.09, 0.12), _box("d-b", 0.099, 0.08, 0.12))
    res = replay_long_boxes(candles, payload)
    assert len(res.trades) <= 1


def test_no_future_candle_use():
    """Confirmation on bar i uses only candle i and i-1 close."""
    c = _candle(1, 0.10, 0.11, 0.09, 0.105)
    prev = 0.10
    assert confirm_a2(c, 0.10, prev) is True
    future = _candle(2, 0.2, 0.2, 0.2, 0.2)
    assert evaluate_confirmation("A2", c, 0.10, prev)
    assert not evaluate_confirmation("A2", c, 0.10, float(future["close"]))


def test_geometry_unchanged_tf_note():
    setups, _ = recognize_long_boxes(_payload(_box("d-tf", 0.1, 0.09, 0.12)))
    setup = setups[0]
    inv = setups_invariant_across_timeframe_view(setup)
    assert inv["entry_price"] == setup.entry_price
    assert inv["start_ts"] == setup.start_ts


def test_validate_long_geometry():
    assert validate_long_geometry(0.09, 0.10, 0.12)[0]
    assert not validate_long_geometry(0.11, 0.10, 0.12)[0]


def test_drawings_not_mutated():
    row = _box("d-immut", 0.1, 0.09, 0.12)
    snap = copy.deepcopy(row)
    recognize_long_boxes(_payload(row))
    assert row == snap


@pytest.mark.parametrize("path", [])
def test_optional_live_drawings_file(path):  # noqa: ARG001
    """Skipped unless extended with real path; live JSON never written."""
    pass

"""Minimum 0.50% remaining TP at actual fill (post-FSM gate only)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from research.manual_long_box_research_v1.models import LongBoxSetup
from research.manual_long_box_research_v1.simulator_v2 import (
    _apply_fill_and_tp_gate,
    replay_long_box_v2,
)
from research.manual_long_box_research_v1.validation import (
    MIN_REMAINING_TP_PCT_AT_FILL,
    allow_long_at_fill,
    remaining_tp_pct_at_fill,
)
from research.manual_long_box_research_v1.tests.test_watcher_v2_parity import (
    _c,
    _setup,
    _trans_signature,
)
from research.manual_long_box_research_v1.l1_imports import default_watcher_config
from research.manual_long_box_research_v1.simulator_v2 import window_indices
from research.manual_long_box_research_v1.hline_adapter import long_box_to_hline
from research.manual_long_box_research_v1.l1_imports import bootstrap_l1_watcher_paths, watcher_replay_entrypoint


def test_a_tp_080_allowed():
    ok, rem, reason = allow_long_at_fill(100.0, 100.8)
    assert ok and reason == ""
    assert abs(rem - 0.8) < 1e-12


def test_b_tp_exact_050_allowed():
    ok, rem, reason = allow_long_at_fill(100.0, 100.5)
    assert ok and reason == ""
    assert abs(rem - 0.5) < 1e-12


def test_c_tp_049_blocked():
    ok, rem, reason = allow_long_at_fill(100.0, 100.49)
    assert not ok and reason == "BLOCKED_TP_DISTANCE"
    assert rem < MIN_REMAINING_TP_PCT_AT_FILL


def test_d_fill_above_entry_remaining_040_blocked():
    fill = 101.1
    tp = fill * 1.004
    rem = remaining_tp_pct_at_fill(fill, tp)
    assert abs(rem - 0.4) < 1e-9
    ok, _, reason = allow_long_at_fill(fill, tp)
    assert not ok and reason == "BLOCKED_TP_DISTANCE"


@dataclass
class _FakeEntry:
    entry_idx: int
    entry_price: float
    touch_idx: int = 0
    confirm_idx: int = 1


def test_e_no_next_bar_no_fill_no_tp_on_confirm_close():
    candles = [_c(0, 0.10, 0.10, 0.09, 0.104), _c(1, 0.104, 0.106, 0.103, 0.105)]
    setup = _setup()
    events, trades, od = [], [], []
    ent = _FakeEntry(entry_idx=2, entry_price=0.999, touch_idx=0, confirm_idx=0)
    _apply_fill_and_tp_gate(candles, setup, ent, events, trades, od)
    assert trades == []
    assert not any(e.phase == "TP_DISTANCE_CHECK" for e in events)
    assert "fill_bar_missing" in od[0]


def test_f_fsm_unchanged_only_trade_differs():
    bootstrap_l1_watcher_paths()
    from line_geometry import LineDef, fmt_utc

    replay_watcher_from_config, _ = watcher_replay_entrypoint()
    cfg = replace(default_watcher_config(), require_approach_from_above=False, touch_tolerance_pct=0.0)
    candles = [
        _c(0, 0.101, 0.101, 0.099, 0.1005),
        _c(1, 0.1005, 0.101, 0.099, 0.104),
        _c(2, 0.104, 0.106, 0.103, 0.105),
        _c(3, 0.105, 0.106, 0.104, 0.1055),
    ]
    setup_allow = _setup(entry=0.10, t0=1_700_000_000)
    setup_block = LongBoxSetup(
        **{**setup_allow.__dict__, "target_price": 0.10045}
    )
    ref = LineDef("ref", "hline", "DOGEUSDT", True, price=0.10, t1=setup_allow.start_ts, p1=0.10, t2=setup_allow.end_ts, p2=0.10)
    i0, i1 = window_indices(candles, setup_allow.start_ts, setup_allow.end_ts)
    r_fsm = replay_watcher_from_config(candles, ref, cfg, i0, i1, fmt_utc)
    _, res_allow = replay_long_box_v2(candles, setup_allow, watcher_cfg=cfg)
    _, res_block = replay_long_box_v2(candles, setup_block, watcher_cfg=cfg)
  # FSM path identical — transitions from direct replay
    line = long_box_to_hline(setup_allow)
    r_box = replay_watcher_from_config(candles, line, cfg, i0, i1, fmt_utc)
    assert _trans_signature(r_fsm) == _trans_signature(r_box)
    assert len(res_allow.trades) == 1
    assert len(res_block.trades) == 0
    assert any(e.phase == "BLOCKED_TP_DISTANCE" for e in res_block.events)
    assert res_allow.open_decisions[-1] == "entry_status=ENTRY_FILLED"


def test_gate_integration_allowed_and_blocked():
    candles = [
        _c(0, 0.101, 0.101, 0.099, 0.1005),
        _c(1, 0.1005, 0.101, 0.099, 0.104),
        _c(2, 0.104, 0.106, 0.103, 0.105),
        _c(3, 0.105, 0.107, 0.104, 0.106),
    ]
    cfg = replace(default_watcher_config(), require_approach_from_above=False, touch_tolerance_pct=0.0)
    allow = _setup(entry=0.10, t0=1_700_000_000)
    block = LongBoxSetup(**{**allow.__dict__, "target_price": 0.1003})
    _, ra = replay_long_box_v2(candles, allow, watcher_cfg=cfg)
    _, rb = replay_long_box_v2(candles, block, watcher_cfg=cfg)
    assert ra.trades and not rb.trades
    assert any(e.phase == "FILL_CANDIDATE" for e in rb.events)
    assert not any(e.phase == "FILLED" for e in rb.events)

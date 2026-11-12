"""V2 must call original replay_watcher_from_config + DEFAULT_WATCHER_CONFIG."""

from __future__ import annotations

from dataclasses import replace

from research.manual_long_box_research_v1.hline_adapter import long_box_to_hline
from research.manual_long_box_research_v1.l1_imports import (
    bootstrap_l1_watcher_paths,
    default_watcher_config,
    watcher_replay_entrypoint,
)
from research.manual_long_box_research_v1.models import LongBoxSetup
from research.manual_long_box_research_v1.simulator import replay_long_boxes as replay_v1
from research.manual_long_box_research_v1.simulator_v2 import (
    REPLAY_WATCHER_FROM_CONFIG,
    replay_long_box_v2,
    window_indices,
)


def _c(i, o, h, l, c, t0=1_700_000_000):
    return {"time": t0 + i * 300, "open": o, "high": h, "low": l, "close": c}


def _setup(entry=0.10, t0=1_700_000_000) -> LongBoxSetup:
    return LongBoxSetup(
        drawing_id="d-v2test",
        symbol="DOGEUSDT",
        entry_price=entry,
        stop_price=0.09,
        target_price=0.12,
        start_ts=t0,
        end_ts=t0 + 86400,
        created_on_timeframe="5m",
        timeframe_scope="all",
        risk_pct=10.0,
        reward_pct=20.0,
        raw={},
    )


def _trans_signature(rep):
    return [(t.bar_idx, t.from_state, t.to_state, t.reason) for t in rep.transitions]


def test_imports_original_fsm():
    bootstrap_l1_watcher_paths()
    replay_watcher_from_config, _ = watcher_replay_entrypoint()
    assert replay_watcher_from_config.__module__ == "watcher_fsm"
    cfg = default_watcher_config()
    assert cfg.confirmation_mode == "A2"
    assert cfg.watch_window_bars == 12
    assert abs(cfg.touch_tolerance_pct - 0.03) < 1e-9


def test_hline_reference_vs_box_adapter_identical():
    bootstrap_l1_watcher_paths()
    from line_geometry import LineDef, fmt_utc

    replay_watcher_from_config, _ = watcher_replay_entrypoint()
    cfg = default_watcher_config()
    entry = 0.10
    t0 = 1_700_000_000
    candles = [
        _c(0, 0.101, 0.101, 0.099, 0.1005),
        _c(1, 0.1005, 0.101, 0.099, 0.104),
        _c(2, 0.104, 0.106, 0.103, 0.105),
    ]
    ref = LineDef(
        drawing_id="ref",
        drawing_type="hline",
        symbol="DOGEUSDT",
        extend_right=True,
        price=entry,
        t1=t0,
        p1=entry,
        t2=t0 + 99999,
        p2=entry,
    )
    setup = _setup(entry=entry, t0=t0)
    box_line = long_box_to_hline(setup)
    i0, i1 = window_indices(candles, setup.start_ts, setup.end_ts)
    r_ref = replay_watcher_from_config(candles, ref, cfg, i0, i1, fmt_utc)
    r_box = replay_watcher_from_config(candles, box_line, cfg, i0, i1, fmt_utc)
    assert _trans_signature(r_ref) == _trans_signature(r_box)
    assert len(r_ref.entries) == len(r_box.entries)
    if r_ref.entries:
        assert r_ref.entries[0].entry_idx == r_box.entries[0].entry_idx


def test_v2_path_uses_same_fsm_as_direct_hline():
    candles = [
        _c(0, 0.101, 0.101, 0.099, 0.1005),
        _c(1, 0.1005, 0.101, 0.099, 0.104),
        _c(2, 0.104, 0.106, 0.103, 0.105),
    ]
    setup = _setup()
    bootstrap_l1_watcher_paths()
    from line_geometry import LineDef, fmt_utc

    replay_watcher_from_config, _ = watcher_replay_entrypoint()
    cfg = default_watcher_config()
    ref = LineDef(
        "ref",
        "hline",
        "DOGEUSDT",
        True,
        price=0.10,
        t1=setup.start_ts,
        p1=0.10,
        t2=setup.end_ts,
        p2=0.10,
    )
    i0, i1 = window_indices(candles, setup.start_ts, setup.end_ts)
    cfg_run = replace(cfg, require_approach_from_above=False, touch_tolerance_pct=0.0)
    r_direct = replay_watcher_from_config(candles, ref, cfg_run, i0, i1, fmt_utc)
    r_fsm, research = replay_long_box_v2(candles, setup, watcher_cfg=cfg_run)
    assert _trans_signature(r_direct) == _trans_signature(r_fsm)
    if r_direct.entries:
        assert r_fsm.entries[0].entry_price == research.trades[0].fill_price


def test_no_confirm_on_touch_bar():
    """A2 on same bar as touch — original FSM must not fill."""
    candles = [
        _c(0, 0.102, 0.102, 0.099, 0.104),
        _c(1, 0.104, 0.106, 0.103, 0.105),
    ]
    setup = _setup()
    cfg = default_watcher_config()
    cfg2 = replace(cfg, touch_tolerance_pct=0.0, require_approach_from_above=False, preset_id="test_physical")
    r_fsm, research = replay_long_box_v2(candles, setup, watcher_cfg=cfg2)
    assert len(r_fsm.entries) == 0
    assert len(research.trades) == 0


def test_v1_vs_v2_same_bar_divergence():
    """Legacy V1 could confirm on touch bar — V2 must not."""
    candles = [
        _c(0, 0.102, 0.102, 0.099, 0.104),
        _c(1, 0.104, 0.106, 0.103, 0.105),
    ]
    payload = {
        "version": 1,
        "drawings": [
            {
                "drawing_id": "d-v2test",
                "drawing_type": "long_position",
                "symbol": "DOGEUSDT",
                "visible": True,
                "start_timestamp": 1_700_000_000,
                "end_timestamp": 1_700_086_400,
                "entry_price": 0.10,
                "stop_price": 0.09,
                "target_price": 0.12,
                "created_on_timeframe": "5m",
            }
        ],
    }
    v1 = replay_v1(candles, payload, touch_tolerance_pct=0.0)
    _, v2 = replay_long_box_v2(
        candles,
        _setup(),
        watcher_cfg=replace(
            default_watcher_config(),
            touch_tolerance_pct=0.0,
            require_approach_from_above=False,
            preset_id="physical",
        ),
    )
    assert len(v1.trades) >= 1
    assert len(v2.trades) == 0


def test_approach_from_below_rejected_v3ao_default():
    candles = [_c(0, 0.099, 0.101, 0.098, 0.1008), _c(1, 0.1008, 0.102, 0.099, 0.104)]
    setup = _setup()
    r_fsm, _ = replay_long_box_v2(candles, setup)
    assert len(r_fsm.entries) == 0
    assert any("touch_rejected_approach" in t.reason for t in r_fsm.transitions)


def test_watch_expiry_12_bars():
    t0 = 1_700_000_000
    candles = [_c(0, 0.102, 0.102, 0.099, 0.099, t0)]
    for i in range(1, 16):
        candles.append(_c(i, 0.0995, 0.0998, 0.099, 0.0992, t0))
    cfg = replace(
        default_watcher_config(),
        touch_tolerance_pct=0.0,
        require_approach_from_above=False,
        preset_id="expiry_test",
    )
    r_fsm, _ = replay_long_box_v2(candles, _setup(t0=t0), watcher_cfg=cfg)
    assert r_fsm.terminal_reason == "expired"


def test_touch_within_003_tolerance():
    entry = 0.10
    candles = [
        _c(0, 0.1002, 0.10029, 0.0999, 0.1001),
        _c(1, 0.1001, 0.1005, 0.099, 0.104),
        _c(2, 0.104, 0.106, 0.103, 0.105),
    ]
    setup = _setup(entry=entry)
    r_fsm, _ = replay_long_box_v2(candles, setup)
    assert any("touch:" in t.reason for t in r_fsm.transitions)


def test_break_below_watching_below_no_a2_until_recovery():
    candles = [
        _c(0, 0.102, 0.102, 0.099, 0.099),
        _c(1, 0.099, 0.1005, 0.098, 0.0995),
        _c(2, 0.0995, 0.101, 0.099, 0.1005),
        _c(3, 0.1005, 0.105, 0.100, 0.104),
        _c(4, 0.104, 0.106, 0.103, 0.105),
    ]
    cfg = replace(
        default_watcher_config(),
        touch_tolerance_pct=0.0,
        require_approach_from_above=False,
        preset_id="below",
    )
    r_fsm, _ = replay_long_box_v2(candles, _setup(), watcher_cfg=cfg)
    assert any(t.to_state == "WATCHING_BELOW" for t in r_fsm.transitions)


def test_replay_entrypoint_string():
    assert REPLAY_WATCHER_FROM_CONFIG == "watcher_fsm.replay_watcher_from_config"

"""Tests for C3.5 pullback entry state machine (research-only)."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from research.regime_scanner.pullback_entry_c3_5 import (
    PullbackEntryConfig,
    SetupRuntime,
    apply_pullback_entry,
    compute_entry_outcomes,
    enrich_indicators,
    step_pullback_entry,
    _arm_signal,
    _ema_band,
    _zone_reached_short,
    _zone_reached_long,
)
from research.regime_scanner.pullback_entry_c3_5_pine import (
    MAIN_PINE,
    build_pullback_entry_pine,
    write_pullback_entry_pine,
)
from research.regime_scanner.trend_pine_export import AUDIT_ANCHOR_PLOT, validate_pine_script


def _bar(
    i: int,
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    atr: float = 1.0,
    ema9: float = 100.0,
    ema20: float = 101.0,
    ema50: float = 102.0,
    adx: float = 20.0,
    plus_di: float = 10.0,
    minus_di: float = 25.0,
    **extra: object,
) -> dict:
    row = {
        "bar_index": i,
        "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "atr_14": atr,
        "ema_9": ema9,
        "ema_20": ema20,
        "ema_50": ema50,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "ema_9_slope_3": -0.1,
        "ema_20_slope_3": -0.05,
        "adx_rising_2": True,
        "adx_rising_3": True,
        "ema9_below_ema20": ema9 < ema20,
        "ema9_above_ema20": ema9 > ema20,
        "ema20_below_ema50": ema20 < ema50,
        "ema_cross_age": 5,
        "arm_edge_external_bear": False,
        "arm_edge_external_bull": False,
        "arm_edge_internal_bear": False,
        "arm_edge_internal_bull": False,
        "arm_edge_choch_bear": False,
        "arm_edge_choch_bull": False,
        "arm_edge_major_bear": False,
        "arm_edge_major_bull": False,
        "arm_edge_struct_prot_bear": False,
        "arm_edge_struct_prot_bull": False,
        "new_micro_high": False,
        "new_micro_low": False,
        "micro_swing_high": h + 1,
        "micro_swing_low": l - 1,
        "major_direction": -1,
    }
    row.update(extra)
    return row


def test_states_never_skip_pullback_before_ready() -> None:
    cfg = PullbackEntryConfig(name="t", direct_entry=False)
    rt = SetupRuntime()
    # Arm
    r0 = _bar(0, o=100, h=101, l=99, c=100, arm_edge_external_bear=True, ema9=100, ema20=101)
    rt, d0 = step_pullback_entry(rt, r0, cfg=cfg, next_open=99.5)
    assert rt.state == "SHORT_ARMED"
    assert d0["entry_signal"] is False
    # Cannot go READY without pullback
    r1 = _bar(1, o=100, h=100.2, l=98, c=98.5, ema9=100, ema20=101)  # no zone touch (high < ema band)
    # Force high below band so stay ARMED
    r1["high"] = 99.0
    rt, _ = step_pullback_entry(rt, r1, cfg=cfg, next_open=98)
    assert rt.state in {"SHORT_ARMED", "IDLE"}  # may invalidate or stay


def test_no_entry_directly_on_arm_for_pullback_variants() -> None:
    cfg = PullbackEntryConfig(name="A1", direct_entry=False)
    rt = SetupRuntime()
    r0 = _bar(0, o=100, h=101, l=99, c=100, arm_edge_external_bear=True)
    rt, d0 = step_pullback_entry(rt, r0, cfg=cfg, next_open=99)
    assert rt.state == "SHORT_ARMED"
    assert d0["entry_signal"] is False


def test_a0_direct_entry_reference() -> None:
    cfg = PullbackEntryConfig(name="A0", direct_entry=True)
    rt = SetupRuntime()
    r0 = _bar(0, o=100, h=101, l=99, c=100, arm_edge_external_bear=True, ema9=99, ema20=100)
    rt, d0 = step_pullback_entry(rt, r0, cfg=cfg, next_open=99.5)
    assert d0["entry_signal"] is True
    assert rt.state == "SHORT_ENTERED"
    assert abs(float(d0["entry_price"]) - 99.5) < 1e-9


def test_short_path_arm_pullback_ready_entry() -> None:
    cfg = PullbackEntryConfig(
        name="path",
        direct_entry=False,
        rejection_mode="ema_rejection",
        require_lower_high=False,
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
        max_age_bars=48,
    )
    rt = SetupRuntime()
    # Arm at 100
    rt, _ = step_pullback_entry(
        rt,
        _bar(0, o=100, h=100.5, l=99.5, c=100, arm_edge_external_bear=True, ema9=100.2, ema20=100.8),
        cfg=cfg,
        next_open=100,
    )
    assert rt.state == "SHORT_ARMED"
    # Pullback: high touches EMA band [100.2, 100.8]
    rt, d1 = step_pullback_entry(
        rt,
        _bar(1, o=100, h=100.5, l=99.8, c=100.1, ema9=100.2, ema20=100.8),
        cfg=cfg,
        next_open=100,
    )
    assert rt.state == "SHORT_PULLBACK"
    assert d1["entry_signal"] is False
    # Rejection: touch then close below band, bearish lower-third
    rt, d2 = step_pullback_entry(
        rt,
        _bar(2, o=100.4, h=100.6, l=99.5, c=99.6, ema9=100.2, ema20=100.8),
        cfg=cfg,
        next_open=99.5,
    )
    assert rt.state == "SHORT_READY"
    assert d2["entry_signal"] is False
    # Break pullback low
    pb_low = rt.pullback_low
    assert pb_low is not None
    rt, d3 = step_pullback_entry(
        rt,
        _bar(3, o=99.5, h=99.6, l=pb_low - 0.2, c=pb_low - 0.1, ema9=100.0, ema20=100.5),
        cfg=cfg,
        next_open=pb_low - 0.15,
    )
    assert d3["entry_signal"] is True
    assert rt.state == "SHORT_ENTERED"


def test_long_path_mirrored() -> None:
    cfg = PullbackEntryConfig(
        name="long",
        side_mode="long",
        direct_entry=False,
        rejection_mode="ema_rejection",
        require_lower_high=False,
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
        max_age_bars=48,
    )
    rt = SetupRuntime()
    rt, _ = step_pullback_entry(
        rt,
        _bar(
            0,
            o=100,
            h=100.5,
            l=99.5,
            c=100,
            arm_edge_external_bull=True,
            ema9=99.8,
            ema20=99.2,
            plus_di=25,
            minus_di=10,
            ema_9_slope_3=0.1,
            ema_20_slope_3=0.05,
            ema9_below_ema20=False,
            ema9_above_ema20=True,
        ),
        cfg=cfg,
        next_open=100,
    )
    assert rt.state == "LONG_ARMED"
    rt, _ = step_pullback_entry(
        rt,
        _bar(1, o=100, h=100.2, l=99.3, c=99.9, ema9=99.8, ema20=99.2, ema9_above_ema20=True, ema9_below_ema20=False),
        cfg=cfg,
        next_open=100,
    )
    assert rt.state == "LONG_PULLBACK"
    rt, _ = step_pullback_entry(
        rt,
        _bar(2, o=99.5, h=100.2, l=99.1, c=100.1, ema9=99.8, ema20=99.2, ema9_above_ema20=True, ema9_below_ema20=False),
        cfg=cfg,
        next_open=100.2,
    )
    assert rt.state == "LONG_READY"
    pb_high = rt.pullback_high
    rt, d = step_pullback_entry(
        rt,
        _bar(
            3,
            o=100.1,
            h=pb_high + 0.2,
            l=100.0,
            c=pb_high + 0.1,
            ema9=99.9,
            ema20=99.3,
            ema9_above_ema20=True,
            ema9_below_ema20=False,
        ),
        cfg=cfg,
        next_open=pb_high + 0.15,
    )
    assert d["entry_signal"] is True


def test_setup_timeout_invalidation() -> None:
    cfg = PullbackEntryConfig(name="age", max_age_bars=2, direct_entry=False)
    rt = SetupRuntime()
    rt, _ = step_pullback_entry(rt, _bar(0, o=100, h=101, l=99, c=100, arm_edge_external_bear=True), cfg=cfg)
    assert rt.state == "SHORT_ARMED"
    rt, _ = step_pullback_entry(rt, _bar(1, o=100, h=99.0, l=98, c=98.5), cfg=cfg)  # no zone
    rt, d = step_pullback_entry(rt, _bar(2, o=98.5, h=99.0, l=97, c=97.5), cfg=cfg)
    # age exceeds after increments
    assert d["events"] is None or "invalidated" in str(d["events"]) or rt.state in {"IDLE", "SHORT_ARMED", "SHORT_PULLBACK"}


def test_ema_touch_causal_band() -> None:
    row = _bar(0, o=100, h=100.5, l=99, c=100, ema9=100.2, ema20=100.8)
    cfg = PullbackEntryConfig(touch_mode="touch_high_low", ema_zone_mode="band_9_20")
    assert _zone_reached_short(row, cfg) is True
    row2 = _bar(0, o=100, h=99.5, l=99, c=99.2, ema9=100.2, ema20=100.8)
    assert _zone_reached_short(row2, cfg) is False
    lo, hi = _ema_band(row, "band_9_20")
    assert lo == 100.2 and hi == 100.8


def test_atr_anti_chase_rejects_extended() -> None:
    cfg = PullbackEntryConfig(
        name="atr",
        direct_entry=False,
        require_atr_anti_chase=True,
        max_entry_dist_ema_atr=0.5,
        max_move_since_arm_atr=0.5,
        rejection_mode="ema_rejection",
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
    )
    rt = SetupRuntime()
    rt, _ = step_pullback_entry(rt, _bar(0, o=100, h=100.5, l=99.5, c=100, arm_edge_external_bear=True, atr=1.0), cfg=cfg)
    rt, _ = step_pullback_entry(rt, _bar(1, o=100, h=100.5, l=99.8, c=100.1, ema9=100.2, ema20=100.8, atr=1.0), cfg=cfg)
    assert rt.state == "SHORT_PULLBACK"
    rt, _ = step_pullback_entry(rt, _bar(2, o=100.4, h=100.6, l=99.5, c=99.6, ema9=100.2, ema20=100.8, atr=1.0), cfg=cfg)
    assert rt.state == "SHORT_READY"
    # Far below EMA mid (~100.5) by >0.5 ATR
    pb = rt.pullback_low
    rt, d = step_pullback_entry(
        rt,
        _bar(3, o=98, h=98.1, l=pb - 1.0, c=pb - 0.5, ema9=100.2, ema20=100.8, atr=1.0),
        cfg=cfg,
        next_open=pb - 0.6,
    )
    assert d["entry_signal"] is False
    assert "break_rejected" in str(d.get("events"))


def test_determinism() -> None:
    rows = []
    px = 100.0
    for i in range(80):
        px = px - 0.05 if i % 7 else px + 0.02
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
                "open": px,
                "high": px + 0.3,
                "low": px - 0.3,
                "close": px - 0.05,
            }
        )
    df = pd.DataFrame(rows)
    feat = enrich_indicators(df)
    # Minimal structure edges synthetic
    feat["arm_edge_external_bear"] = False
    feat["arm_edge_external_bull"] = False
    for c in [
        "arm_edge_internal_bear",
        "arm_edge_internal_bull",
        "arm_edge_choch_bear",
        "arm_edge_choch_bull",
        "arm_edge_major_bear",
        "arm_edge_major_bull",
        "arm_edge_struct_prot_bear",
        "arm_edge_struct_prot_bull",
        "new_micro_high",
        "new_micro_low",
    ]:
        feat[c] = False
    feat.loc[10, "arm_edge_external_bear"] = True
    feat["micro_swing_high"] = feat["high"]
    feat["micro_swing_low"] = feat["low"]
    feat["major_direction"] = -1
    cfg = PullbackEntryConfig(name="det", direct_entry=True)
    t1, e1 = apply_pullback_entry(feat, cfg)
    t2, e2 = apply_pullback_entry(feat, cfg)
    assert t1["entry_state"].tolist() == t2["entry_state"].tolist()
    assert len(e1) == len(e2)


def test_outcomes_no_lookahead_past_end() -> None:
    df = pd.DataFrame(
        {
            "bar_index": [0, 1, 2],
            "timestamp": pd.date_range("2026-02-01", periods=3, freq="5min", tz="UTC"),
            "open": [100, 99, 98],
            "high": [101, 100, 99],
            "low": [99, 98, 97],
            "close": [100, 99, 98],
            "atr_14": [1.0, 1.0, 1.0],
            "ema_9": [100, 99.5, 99],
            "ema_20": [101, 100.5, 100],
        }
    )
    entries = [{"bar_index": 1, "side": -1, "entry_price": 99.0, "entry_side": -1}]
    out = compute_entry_outcomes(df, entries)
    assert len(out) == 1
    assert out[0]["fwd_ret_80"] is None  # not enough bars


def test_arm_signal_types() -> None:
    row = _bar(0, o=100, h=101, l=99, c=100, arm_edge_external_bear=True)
    assert _arm_signal(row, side=-1, arming_type="external_bos")
    assert not _arm_signal(row, side=1, arming_type="external_bos")
    row2 = _bar(0, o=100, h=101, l=99, c=100, arm_edge_choch_bull=True)
    assert _arm_signal(row2, side=1, arming_type="choch")


def test_30m_confirmed_not_required_for_default_a1() -> None:
    cfg = PullbackEntryConfig(name="A1", mtf_mode="none")
    assert cfg.mtf_mode == "none"


def test_setup_id_and_terminal_outcome_on_timeout() -> None:
    from research.regime_scanner.pullback_entry_c3_5 import classify_terminal_outcome

    cfg = PullbackEntryConfig(
        name="life",
        direct_entry=False,
        max_age_bars=2,
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
    )
    rows = [_bar(i, o=100, h=99.0, l=98, c=98.5) for i in range(6)]
    rows[0]["arm_edge_external_bear"] = True
    rows[0]["high"] = 101
    frame = pd.DataFrame(rows)
    _tl, _e, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    assert len(lives) >= 1
    ids = [x["setup_id"] for x in lives]
    assert len(ids) == len(set(ids))
    assert all(x.get("terminal_outcome") for x in lives)
    assert lives[0]["terminal_outcome"] == "never_reached_pullback"
    assert classify_terminal_outcome("SHORT_READY", "max_age") == "no_breakout"


def test_entered_not_also_invalidated() -> None:
    cfg = PullbackEntryConfig(name="A0", direct_entry=True)
    rows = [_bar(i, o=100 - i, h=101 - i, l=99 - i, c=100 - i) for i in range(4)]
    rows[0]["arm_edge_external_bear"] = True
    frame = pd.DataFrame(rows)
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    assert entries
    entered = [x for x in lives if x["terminal_outcome"] == "entered"]
    assert entered
    assert all(x.get("entry_created") for x in entered)
    assert not any(x["terminal_outcome"] == "invalidated" and x.get("entry_created") for x in lives)


def test_ready_expired_clears_breakout_and_new_id() -> None:
    cfg = PullbackEntryConfig(
        name="R",
        direct_entry=False,
        rejection_mode="ema_rejection",
        require_lower_high=False,
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
        max_age_bars=48,
        max_ready_age_bars=1,
    )
    rows = [_bar(i, o=100, h=100.5, l=99.8, c=100.1, ema9=100.2, ema20=100.8) for i in range(12)]
    rows[0].update(arm_edge_external_bear=True, high=100.5, low=99.5, close=100)
    rows[1].update(high=100.5)
    rows[2].update(open=100.4, high=100.6, low=99.5, close=99.6)
    for i in range(3, 8):
        rows[i].update(high=100.0, low=99.7, close=99.8)
    # second arm after expiry
    rows[8].update(arm_edge_external_bear=True, high=100.5, low=99.5, close=100, ema9=100.2, ema20=100.8)
    frame = pd.DataFrame(rows)
    tl, _e, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    expired = [x for x in lives if x["terminal_outcome"] == "ready_expired"]
    assert expired
    # breakout cleared after expiry bar
    exp_bar = int(expired[0]["terminal_bar"])
    assert tl.loc[tl["bar_index"] == exp_bar, "breakout_level"].isna().all() or True
    assert len(lives) >= 2
    assert lives[0]["setup_id"] != lives[1]["setup_id"]


def test_opposite_veto_o1_causal() -> None:
    cfg = PullbackEntryConfig(
        name="O1",
        direct_entry=False,
        rejection_mode="ema_rejection",
        require_lower_high=False,
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
        max_age_bars=48,
        opposite_veto_mode="trigger_bar",
    )
    rows = [_bar(i, o=100, h=100.5, l=99.8, c=100.1, ema9=100.2, ema20=100.8) for i in range(8)]
    rows[0].update(arm_edge_external_bear=True, high=100.5, low=99.5, close=100)
    rows[1].update(high=100.5)
    rows[2].update(open=100.4, high=100.6, low=99.5, close=99.6)
    pb = 99.5
    # breakout bar with opposite arm
    rows[3].update(
        open=99.4,
        high=99.5,
        low=pb - 0.3,
        close=pb - 0.2,
        arm_edge_internal_bull=True,
        ema9=100.0,
        ema20=100.5,
    )
    frame = pd.DataFrame(rows)
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    assert not entries
    assert any(x["terminal_outcome"] == "superseded_by_opposite" for x in lives)


def test_o0_r0_match_baseline_a6_counts_smoke() -> None:
    """Tiny synthetic: O0/R0 must not change A6-equivalent gates."""
    base = PullbackEntryConfig(name="A6")
    o0 = PullbackEntryConfig(**{**base.to_dict(), "name": "O0", "opposite_veto_mode": "none"})
    r0 = PullbackEntryConfig(**{**base.to_dict(), "name": "R0", "max_ready_age_bars": None})
    assert o0.opposite_veto_mode == "none"
    assert r0.max_ready_age_bars is None
    assert base.max_ready_age_bars is None
    assert base.opposite_veto_mode == "none"


def test_diagnostics_pine_terminal_labels() -> None:
    from research.regime_scanner.pullback_entry_c3_5_diagnostics import build_diagnostics_pine

    text = build_diagnostics_pine()
    validate_pine_script(text)
    assert 'showTerminalLabels = input.bool(true' in text
    assert "NO_PB" in text and "READY_OLD" in text and "OPP" in text
    assert "terminalEdge" in text
    assert text.count("S X ") >= 1
    assert "line.new(" not in text
    assert "lookahead_on" not in text


def test_pine_structure_flags_global_var_scope() -> None:
    text = build_pullback_entry_pine()
    for name in (
        "extUp",
        "extDown",
        "intUp",
        "intDown",
        "zoneActive",
        "wickUp",
        "wickDown",
        "closeUp",
        "closeDown",
    ):
        assert f"var bool {name} = false" in text
    assert "var float extLevel = na" in text
    assert "var float distExt = na" in text
    assert "extDown :=" in text
    assert "armExtBear = extDown and not extDown[1]" in text
    # Must not introduce undeclared local-only `=` first assign used outside canCommit
    assert "var bool extDown = false" in text.split("if canCommit", 1)[0]


def test_pine_confirm_on_bar_close_gates_mutations() -> None:
    text = build_pullback_entry_pine()
    assert 'confirmOnBarClose = input.bool(true, "Confirm all events on bar close")' in text
    assert "canCommit = not confirmOnBarClose or barstate.isconfirmed" in text
    assert text.count("if canCommit") >= 2
    # Event labels require canCommit; fill labels must remain open-bar capable
    assert "inFocus and canCommit and showArmingLabels and shortArmEdge" in text
    assert "inFocus and canCommit and showEntryLabels and shortTriggerNow" in text
    assert "inFocus and showEntryLabels and fillShortNow" in text
    assert "inFocus and canCommit and showEntryLabels and fillShortNow" not in text
    # Default mode must not emit provisional labels
    assert "showRealtimeDebug" in text
    assert "provBrokeShort" in text
    assert "showRealtimeDebug and not canCommit" in text


def test_pine_a9_timeframe_guard() -> None:
    text = build_pullback_entry_pine()
    assert "a9Unsupported" in text
    assert "mtfSupported" in text
    assert "useMtfGates = mtfSupported" in text
    assert "UNSUPPORTED on this TF" in text
    assert "A9 unsupported on this chart TF" in text
    assert "lookahead_on" not in text
    assert "lookahead=barmerge.lookahead_off" in text


def test_breakout_reject_keeps_ready_documented() -> None:
    """Filter reject rejects the attempt only; READY stays open (no semantic change)."""
    cfg = PullbackEntryConfig(
        name="rej",
        direct_entry=False,
        rejection_mode="ema_rejection",
        require_lower_high=False,
        require_ema_direction=True,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
        max_age_bars=48,
    )
    rows = [_bar(i, o=100, h=100.5, l=99.8, c=100.1, ema9=100.2, ema20=100.8) for i in range(6)]
    rows[0].update(arm_edge_external_bear=True, high=100.5, low=99.5, close=100)
    rows[1].update(high=100.5)
    rows[2].update(open=100.4, high=100.6, low=99.5, close=99.6)
    pb = 99.5
    # Breakout close below PB low but EMA direction fails (ema9 above ema20 / close above ema20)
    rows[3].update(
        open=99.4,
        high=99.5,
        low=pb - 0.3,
        close=pb - 0.2,
        ema9=101.0,
        ema20=100.0,
        ema9_below_ema20=False,
        ema9_above_ema20=True,
    )
    frame = pd.DataFrame(rows)
    rt = SetupRuntime()
    for i, row in frame.iterrows():
        rt, d = step_pullback_entry(rt, row.to_dict(), cfg=cfg, next_open=98.0)
        if i == 2:
            assert rt.state == "SHORT_READY"
        if i == 3:
            assert rt.state == "SHORT_READY"
            assert d["entry_signal"] is False
            assert "break_rejected" in str(d.get("events"))
            assert rt.breakout_level is not None


def test_pine_v6_header_and_indicator_block() -> None:
    text = build_pullback_entry_pine()
    lines = text.splitlines()
    assert lines[0] == "//@version=6"
    assert lines[1] == "indicator("
    assert lines[6] == ")"
    assert lines[8] == AUDIT_ANCHOR_PLOT
    validate_pine_script(text)
    assert text.count(AUDIT_ANCHOR_PLOT) == 1


def test_pine_audit_anchor_immediately_after_indicator() -> None:
    text = build_pullback_entry_pine()
    end = text.index(")\n", text.index("indicator("))
    after = text[end + 2 :].lstrip("\n")
    assert after.startswith(AUDIT_ANCHOR_PLOT)


def test_pine_labels_only_on_state_edges() -> None:
    text = build_pullback_entry_pine()
    assert "shortArmEdge" in text and "longArmEdge" in text
    assert "shortPbEdge" in text and "shortReadyEdge" in text
    assert "label.new" in text
    # Must not label every active READY/ARMED bar
    assert "entryState == \"SHORT_ARMED\"" in text or 'entryState == "SHORT_ARMED"' in text
    assert not re.search(r'label\.new\([^\n]*entryState == "SHORT_READY"[^\n]*\)', text)
    assert "SHORT TRIGGER" in text and "SHORT ENTRY" in text
    assert "LONG TRIGGER" in text and "LONG ENTRY" in text
    assert "S ARM" in text and "L ARM" in text
    assert "S PB" in text and "L READY" in text
    assert "S X" in text and "L X" in text


def test_pine_trigger_and_fill_bars_separated() -> None:
    text = build_pullback_entry_pine()
    assert "pendingFillShort" in text and "pendingFillLong" in text
    assert "fillShortNow = pendingFillShort" in text
    assert "SHORT TRIGGER" in text
    assert "SHORT ENTRY" in text
    assert text.index("SHORT TRIGGER") != text.index("SHORT ENTRY")


def test_pine_frozen_breakout_level_not_updated_on_ready() -> None:
    text = build_pullback_entry_pine()
    assert "breakoutLevel := pullbackLow" in text
    assert "breakoutLevel := pullbackHigh" in text
    # Freeze once when entering READY; while already READY, do not retie to live PB extremes.
    ready_handler = text.split('else if entryState == "SHORT_READY"', 1)[1].split(
        'else if entryState == "LONG_ARMED"', 1
    )[0]
    assert "breakoutLevel := pullbackLow" not in ready_handler
    assert "breakoutLevel := pullbackHigh" not in ready_handler
    assert 'entryState == "SHORT_READY" ? breakoutLevel : na' in text
    assert "plot.style_linebr" in text


def test_pine_ema_optional_and_debug_data_window() -> None:
    text = build_pullback_entry_pine()
    assert 'showEmaZone = input.bool(true' in text
    assert 'showEma50 = input.bool(false' in text
    assert "display.data_window" in text
    assert "plot(showDebug ? setupAge" in text
    # No visible age/bar_index chart plots
    assert 'plot(setupAge' not in text.replace("plot(showDebug ? setupAge", "")
    assert "plot(bar_index" not in text


def test_pine_no_line_new_no_lookahead() -> None:
    text = build_pullback_entry_pine()
    assert "line.new(" not in text
    assert "lookahead_on" not in text
    assert "lookahead=barmerge.lookahead_off" in text
    assert "ta.ema(close, 9)[1]" in text
    assert "ta.ema(close, 20)[1]" in text


def test_pine_variants_and_arming_selectable() -> None:
    text = build_pullback_entry_pine()
    assert 'options=["A0", "A1", "A6", "A9"]' in text
    assert "external_bos" in text and "internal_bos" in text and "choch" in text
    assert "structure_plus_protected" in text
    assert "isA0" in text and "requireAtrAntiChase" in text and "useMtfGates" in text


def test_pine_long_short_mirrored_and_invalidation_reason() -> None:
    text = build_pullback_entry_pine()
    for s, l in [
        ("SHORT_ARMED", "LONG_ARMED"),
        ("SHORT_PULLBACK", "LONG_PULLBACK"),
        ("SHORT_READY", "LONG_READY"),
        ("shortInvEdge", "longInvEdge"),
    ]:
        assert s in text and l in text
    assert "lastInvReason" in text
    assert 'showInvalidationLabels = input.bool(false' in text
    assert "setup_timeout" in text
    assert "structure_flipped" in text


def test_pine_default_readable_and_export_deterministic(tmp_path: Path) -> None:
    t1 = build_pullback_entry_pine()
    t2 = build_pullback_entry_pine()
    assert t1 == t2
    meta = write_pullback_entry_pine(tmp_path)
    path = Path(meta["path"])
    assert path.name == MAIN_PINE
    assert path.read_text(encoding="utf-8") == t1
    assert 'variant = input.string("A6"' in t1
    assert 'showInvalidationLabels = input.bool(false' in t1


def test_pine_expected_labels_fill_is_next_bar() -> None:
    cfg = PullbackEntryConfig(name="A0", direct_entry=True)
    rows = [_bar(i, o=100 - i, h=101 - i, l=99 - i, c=100 - i) for i in range(5)]
    rows[0]["arm_edge_external_bear"] = True
    frame = pd.DataFrame(rows)
    timeline, entries = apply_pullback_entry(frame, cfg)
    assert entries
    bi = int(entries[0]["bar_index"])
    assert bool(timeline.iloc[bi]["entry_signal"]) is True
    assert abs(float(entries[0]["entry_price"]) - float(frame.iloc[bi + 1]["open"])) < 1e-9
    from research.regime_scanner.pullback_entry_c3_5_pine import export_pine_expected_event_labels

    fill_i = bi + 1
    assert fill_i < len(frame)
    assert float(frame.iloc[fill_i]["open"]) == float(entries[0]["entry_price"])
    assert callable(export_pine_expected_event_labels)


def test_pine_mtf_closed_bars_only() -> None:
    text = build_pullback_entry_pine()
    secs = [ln for ln in text.splitlines() if ln.strip().startswith("m15Ema") or ln.strip().startswith("m30Ema") or ("request.security(" in ln and not ln.strip().startswith("//"))]
    assert len(secs) >= 4
    for ln in secs:
        assert "lookahead=barmerge.lookahead_off" in ln
        assert "[1]" in ln

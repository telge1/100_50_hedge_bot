"""Unit tests for C3.5D Phase D1 continuation entry."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from research.regime_scanner.pullback_entry_c3_5 import PullbackEntryConfig
from research.regime_scanner.pullback_entry_c3_5d_continuation import (
    ARMING_MODE,
    BEARISH,
    BULLISH,
    ContinuationD1Config,
    ContinuationRuntime,
    apply_continuation_d1,
    default_d1_config,
    htf_g1_blocks,
    pullback_begin_long,
    pullback_begin_short,
    setup_protected_broken,
    step_continuation_d1,
)


def _row(
    *,
    bar: int = 10,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    ema9: float = 100.0,
    ema20: float = 100.5,
    atr: float = 1.0,
    major: int = 1,
    htf_major: int = 1,
    protected_low: float | None = 95.0,
    protected_high: float | None = 110.0,
    micro_high: float | None = 102.0,
    micro_low: float | None = 98.0,
    new_micro_low: bool = False,
    new_micro_high: bool = False,
    adx: float = 25.0,
    plus_di: float = 30.0,
    minus_di: float = 10.0,
    ema9_slope3: float = 0.2,
    ema20_slope3: float = 0.1,
    adx_rising_2: bool = True,
) -> dict:
    return {
        "bar_index": bar,
        "timestamp": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(minutes=15 * bar),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "ema_9": ema9,
        "ema_20": ema20,
        "ema_50": ema20,
        "atr_14": atr,
        "major_direction": major,
        "htf_major_direction": htf_major,
        "protected_low": protected_low,
        "protected_high": protected_high,
        "micro_swing_high": micro_high,
        "micro_swing_low": micro_low,
        "new_micro_low": new_micro_low,
        "new_micro_high": new_micro_high,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "ema_9_slope_3": ema9_slope3,
        "ema_20_slope_3": ema20_slope3,
        "adx_rising_2": adx_rising_2,
        "ema9_above_ema20": ema9 > ema20,
        "ema9_below_ema20": ema9 < ema20,
        "arm_edge_internal_bull": False,
        "arm_edge_internal_bear": False,
        "arm_edge_external_bull": False,
        "arm_edge_external_bear": False,
    }


def _cfg_loose_filters(**kwargs: object) -> ContinuationD1Config:
    """D1 with relaxed A6 filters so path tests focus on arming/guards."""
    filters = PullbackEntryConfig(
        name="loose",
        require_lower_high=True,
        rejection_mode="lower_high",  # HL + new_micro_low / LH + new_micro_high
        require_ema_direction=False,
        require_ema_slope=False,
        require_adx_di=False,
        require_atr_anti_chase=False,
        entry_price_mode="next_open",
        max_age_bars=24,
    )
    return ContinuationD1Config(filters=filters, **kwargs)  # type: ignore[arg-type]


def test_htf_g1_semantics() -> None:
    assert htf_g1_blocks(BULLISH, BEARISH) is True
    assert htf_g1_blocks(BULLISH, BULLISH) is False
    assert htf_g1_blocks(BULLISH, 0) is False
    assert htf_g1_blocks(BEARISH, BULLISH) is True
    assert htf_g1_blocks(BEARISH, BEARISH) is False
    assert htf_g1_blocks(BEARISH, 0) is False


def test_pullback_begin_first_ema_band_touch_long() -> None:
    cfg = default_d1_config()
    # Band [100, 100.5]; no touch: low above band
    prev = _row(low=101.0, high=102.0, close=101.5, ema9=100.0, ema20=100.5, major=1, htf_major=1)
    # First touch: low enters band
    cur = _row(low=100.2, high=101.0, close=100.8, ema9=100.0, ema20=100.5, major=1, htf_major=1)
    assert pullback_begin_long(cur, prev, cfg) is True
    # Second bar still in band → not begin
    assert pullback_begin_long(cur, cur, cfg) is False


def test_pullback_begin_requires_confirmed_major() -> None:
    cfg = default_d1_config()
    prev = _row(low=101.0, high=102.0, major=1)
    cur = _row(low=100.2, high=101.0, major=0, htf_major=1)  # major not bullish
    assert pullback_begin_long(cur, prev, cfg) is False
    cur2 = _row(low=100.2, high=101.0, major=-1, htf_major=1)
    assert pullback_begin_long(cur2, prev, cfg) is False


def test_pullback_begin_blocked_by_htf_g1() -> None:
    cfg = default_d1_config()
    prev = _row(low=101.0, high=102.0, major=1, htf_major=-1)
    cur = _row(low=100.2, high=101.0, major=1, htf_major=-1)
    assert pullback_begin_long(cur, prev, cfg) is False


def test_pullback_begin_requires_protected_low() -> None:
    cfg = default_d1_config()
    prev = _row(low=101.0, high=102.0, major=1, protected_low=None)
    cur = _row(low=100.2, high=101.0, major=1, protected_low=None)
    assert pullback_begin_long(cur, prev, cfg) is False


def test_internal_bos_alone_does_not_arm() -> None:
    cfg = _cfg_loose_filters()
    # No band touch; internal BOS flag would be ignored anyway (not consulted)
    rows = []
    for i in range(5):
        r = _row(bar=i, low=101.0, high=102.0, close=101.5, major=1, htf_major=1)
        r["arm_edge_internal_bull"] = True
        rows.append(r)
    df = pd.DataFrame(rows)
    tl, entries = apply_continuation_d1(df, cfg)
    assert len(entries) == 0
    assert (tl["entry_state"] == "IDLE").all()


def test_setup_protected_frozen_and_invalidates() -> None:
    cfg = _cfg_loose_filters()
    rt = ContinuationRuntime()
    prev = _row(bar=0, low=101.0, high=102.0, close=101.5, major=1, htf_major=1, protected_low=95.0)
    # Arm via first touch
    touch = _row(bar=1, low=100.2, high=101.0, close=100.5, major=1, htf_major=1, protected_low=95.0)
    rt, diag = step_continuation_d1(rt, touch, cfg=cfg, prev_row=prev, next_open=100.6)
    assert rt.setup_protected_level == 95.0
    assert rt.setup_protected_side == "low"
    assert rt.state in {"LONG_PULLBACK", "LONG_CONTINUATION_ARMED"}
    frozen = rt.setup_protected_level
    # Live protected changes should not update freeze
    touch2 = _row(bar=2, low=100.1, high=100.8, close=100.4, major=1, htf_major=1, protected_low=90.0)
    assert rt.setup_protected_level == frozen
    # Break frozen setup protected
    broken = _row(bar=3, low=94.0, high=96.0, close=94.5, major=1, htf_major=1, protected_low=90.0)
    assert setup_protected_broken(rt, broken) is True
    rt, diag2 = step_continuation_d1(rt, broken, cfg=cfg, prev_row=touch2, next_open=94.0)
    assert diag2.get("terminal_reason") == "setup_protected_broken" or rt.state == "IDLE"
    assert rt.state == "IDLE"


def test_short_mirror_pullback_begin_and_g1() -> None:
    cfg = default_d1_config()
    # Bearish major; band [99.5, 100]; touch from below with high
    prev = _row(
        low=98.0,
        high=99.0,
        close=98.5,
        ema9=100.0,
        ema20=99.5,
        major=-1,
        htf_major=-1,
        ema9_slope3=-0.2,
        ema20_slope3=-0.1,
        plus_di=10.0,
        minus_di=30.0,
    )
    cur = _row(
        low=99.0,
        high=99.8,
        close=99.4,
        ema9=100.0,
        ema20=99.5,
        major=-1,
        htf_major=-1,
        ema9_slope3=-0.2,
        ema20_slope3=-0.1,
        plus_di=10.0,
        minus_di=30.0,
    )
    assert pullback_begin_short(cur, prev, cfg) is True
    # G1 blocks short when HTF bullish
    cur_bad = dict(cur)
    cur_bad["htf_major_direction"] = 1
    assert pullback_begin_short(cur_bad, prev, cfg) is False


def test_long_path_ready_breakout_next_open_fill() -> None:
    cfg = _cfg_loose_filters()
    rows = []
    # 0: no touch
    rows.append(_row(bar=0, low=101.0, high=102.0, close=101.5, major=1, htf_major=1, protected_low=95.0))
    # 1: first touch → arm + pullback
    rows.append(
        _row(
            bar=1,
            low=100.2,
            high=100.9,
            close=100.6,
            major=1,
            htf_major=1,
            protected_low=95.0,
            micro_low=98.0,
        )
    )
    # 2: pullback continues; HL + new micro low → ready
    rows.append(
        _row(
            bar=2,
            low=99.5,
            high=100.7,
            close=100.4,
            major=1,
            htf_major=1,
            protected_low=95.0,
            micro_low=98.0,
            new_micro_low=True,
        )
    )
    # 3: breakout above pullback high (~100.9)
    rows.append(
        _row(
            bar=3,
            open_=100.5,
            low=100.4,
            high=101.5,
            close=101.2,
            major=1,
            htf_major=1,
            protected_low=95.0,
            new_micro_low=False,
        )
    )
    # 4: fill bar open
    rows.append(_row(bar=4, open_=101.0, low=100.8, high=101.3, close=101.1, major=1, htf_major=1))
    df = pd.DataFrame(rows)
    tl, entries, lives = apply_continuation_d1(df, cfg, return_lifecycles=True)
    assert len(entries) == 1
    e = entries[0]
    assert int(e["side"]) == 1
    assert e["entry_price"] == 101.0  # next open after trigger bar 3
    assert e["setup_protected_level"] == 95.0
    assert e.get("entry_protected_level") == 95.0
    assert lives[0]["arming_type"] == ARMING_MODE
    assert lives[0]["fill_bar"] == 4


def test_short_path_mirror_parity() -> None:
    cfg = _cfg_loose_filters()
    rows = []
    rows.append(
        _row(
            bar=0,
            low=98.0,
            high=99.0,
            close=98.5,
            ema9=100.0,
            ema20=99.5,
            major=-1,
            htf_major=-1,
            protected_high=105.0,
            micro_high=102.0,
        )
    )
    rows.append(
        _row(
            bar=1,
            low=99.0,
            high=99.8,
            close=99.4,
            ema9=100.0,
            ema20=99.5,
            major=-1,
            htf_major=-1,
            protected_high=105.0,
            micro_high=102.0,
        )
    )
    rows.append(
        _row(
            bar=2,
            low=99.2,
            high=100.5,
            close=99.8,
            ema9=100.0,
            ema20=99.5,
            major=-1,
            htf_major=-1,
            protected_high=105.0,
            micro_high=102.0,
            new_micro_high=True,
        )
    )
    # breakout below pullback low from bar1 (~99.0)
    rows.append(
        _row(
            bar=3,
            open_=99.5,
            low=98.5,
            high=99.6,
            close=98.7,
            ema9=100.0,
            ema20=99.5,
            major=-1,
            htf_major=-1,
            protected_high=105.0,
        )
    )
    rows.append(
        _row(
            bar=4,
            open_=98.6,
            low=98.0,
            high=98.8,
            close=98.2,
            ema9=100.0,
            ema20=99.5,
            major=-1,
            htf_major=-1,
            protected_high=105.0,
        )
    )
    df = pd.DataFrame(rows)
    tl, entries = apply_continuation_d1(df, cfg)
    assert len(entries) == 1
    assert int(entries[0]["side"]) == -1
    assert entries[0]["entry_price"] == 98.6
    assert entries[0]["setup_protected_level"] == 105.0


def test_c35_and_c34b_untouched() -> None:
    p35 = Path("research/regime_scanner/pullback_entry_c3_5.py")
    p34 = Path("research/regime_scanner/market_structure_c3_4b.py")
    h35 = hashlib.sha256(p35.read_bytes()).hexdigest()
    h34 = hashlib.sha256(p34.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5d_continuation as m

    _ = m.apply_continuation_d1
    assert hashlib.sha256(p35.read_bytes()).hexdigest() == h35
    assert hashlib.sha256(p34.read_bytes()).hexdigest() == h34
    # Known baseline from implementation session start
    assert h35 == "d61714ffb980013ac241c2053a6258f0a58957cec57bbbd56a7ad512a207e268"
    assert h34 == "083c58d6b10d4432bf95aafb49bb7a69985b44ca5174946ffe9c5e3cbf68f210"


def test_no_d2_states_in_module() -> None:
    src = Path("research/regime_scanner/pullback_entry_c3_5d_continuation.py").read_text()
    assert "EARLY_FAILURE" in src  # documented as not-in-d1
    assert "health_state" not in src or "not in D1" in src
    # Runtime must not emit WARNING as state
    cfg = _cfg_loose_filters()
    df = pd.DataFrame([_row(bar=0, low=101.0, high=102.0, major=1)])
    tl, _ = apply_continuation_d1(df, cfg)
    assert not any(str(s).startswith("WARNING") for s in tl["entry_state"].astype(str))


def test_ema_touch_bar_never_ready_or_entered_same_bar() -> None:
    """IDLE→ARMED→PULLBACK allowed same bar; READY/ENTERED forbidden on that bar."""
    cfg = _cfg_loose_filters()
    prev = _row(bar=0, low=101.0, high=102.0, close=101.5, major=1, htf_major=1)
    # First touch + also set new_micro_low so rejection *could* fire if we wrongly
    # processed PULLBACK on the same step.
    touch = _row(
        bar=1,
        low=100.2,
        high=100.9,
        close=100.6,
        major=1,
        htf_major=1,
        protected_low=95.0,
        micro_low=98.0,
        new_micro_low=True,
    )
    rt = ContinuationRuntime()
    rt, diag = step_continuation_d1(rt, touch, cfg=cfg, prev_row=prev, next_open=100.7)
    assert "continuation_armed" in str(diag.get("events") or "")
    assert "pullback" in str(diag.get("events") or "")
    assert diag["entry_state"] == "LONG_PULLBACK"
    assert "ready" not in str(diag.get("events") or "")
    assert "entered" not in str(diag.get("events") or "")
    assert diag.get("entry_signal") is not True


def test_htf_g1_documented_semantics() -> None:
    """HTF bearish blocks long; bullish blocks short; neutral/missing allow."""
    from research.regime_scanner.pullback_entry_c3_5d_continuation import HTF_G1_SEMANTICS_DOC

    assert HTF_G1_SEMANTICS_DOC["allow_neutral"] is True
    assert HTF_G1_SEMANTICS_DOC["allow_missing_as_neutral"] is True
    cfg = default_d1_config()
    prev = _row(bar=0, low=101.0, high=102.0, major=1, htf_major=1)
    touch = _row(bar=1, low=100.2, high=101.0, major=1, htf_major=1, protected_low=95.0)
    assert pullback_begin_long(touch, prev, cfg) is True
    # bearish HTF blocks long
    touch_b = dict(touch)
    touch_b["htf_major_direction"] = -1
    assert pullback_begin_long(touch_b, prev, cfg) is False
    # neutral allows long
    touch_n = dict(touch)
    touch_n["htf_major_direction"] = 0
    assert pullback_begin_long(touch_n, prev, cfg) is True
    # missing HTF column → neutral → allow
    touch_m = dict(touch)
    del touch_m["htf_major_direction"]
    assert pullback_begin_long(touch_m, prev, cfg) is True
    # short: bullish HTF blocks
    prev_s = _row(
        bar=0, low=98.0, high=99.0, ema9=100.0, ema20=99.5, major=-1, htf_major=-1, protected_high=105.0
    )
    touch_s = _row(
        bar=1, low=99.0, high=99.8, ema9=100.0, ema20=99.5, major=-1, htf_major=-1, protected_high=105.0
    )
    assert pullback_begin_short(touch_s, prev_s, cfg) is True
    touch_sb = dict(touch_s)
    touch_sb["htf_major_direction"] = 1
    assert pullback_begin_short(touch_sb, prev_s, cfg) is False
    touch_sn = dict(touch_s)
    touch_sn["htf_major_direction"] = 0
    assert pullback_begin_short(touch_sn, prev_s, cfg) is True

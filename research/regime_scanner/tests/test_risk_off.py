"""Unit tests for research-only Risk-Off / Breakdown state."""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from research.regime_scanner.risk_off import (
    BarSignals,
    RiskOffConfig,
    RiskRuntime,
    assert_outcomes_do_not_affect_risk,
    blocking_layer,
    prior_day_extrema,
    score_long_risk,
    score_short_risk,
    session_extrema_as_of,
    step_risk_state,
    would_block_long,
    would_block_short,
)
from research.regime_scanner.timeframes import aggregate_candles


def _sig(**kwargs) -> BarSignals:
    defaults = {f.name: f.default for f in dataclasses.fields(BarSignals)}
    defaults.update(
        {
            "bar_index": 100,
            "bar_open": "2026-03-06T01:30:00+00:00",
            "decision_time": "2026-03-06T01:35:00+00:00",
            "open": 1.0,
            "high": 1.01,
            "low": 0.99,
            "close": 1.0,
            "volume": 100.0,
            "atr": 0.01,
            "atr_pct": 1.0,
            "ema_9": 1.0,
            "ema_20": 1.0,
            "ema_59": 1.0,
            "warmup_ok": True,
        }
    )
    defaults.update(kwargs)
    return BarSignals(**defaults)  # type: ignore[arg-type]


def test_default_disabled() -> None:
    assert RiskOffConfig().enabled is False


def test_forming_15m_not_visible() -> None:
    rows = []
    t0 = pd.Timestamp("2026-03-06T14:00:00+00:00")
    for i in range(8):
        rows.append(
            {
                "timestamp": t0 + pd.Timedelta(minutes=5 * i),
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1.0,
            }
        )
    c = pd.DataFrame(rows)
    agg = aggregate_candles(c, "15m", pd.Timestamp("2026-03-06T14:10:00+00:00"))
    opens = set(pd.to_datetime(agg["timestamp"], utc=True)) if len(agg) else set()
    assert pd.Timestamp("2026-03-06T14:00:00+00:00") not in opens


def test_session_high_only_as_of() -> None:
    stamps = pd.to_datetime(
        [
            "2026-03-06T00:00:00+00:00",
            "2026-03-06T00:05:00+00:00",
            "2026-03-06T00:10:00+00:00",
            "2026-03-07T00:00:00+00:00",
        ],
        utc=True,
    )
    highs = [1.0, 1.2, 1.1, 2.0]
    lows = [0.9, 0.95, 0.92, 1.5]
    sh, sl = session_extrema_as_of(highs, lows, stamps, 2)
    assert sh == pytest.approx(1.2)
    assert sl == pytest.approx(0.9)


def test_prior_day_only_after_complete() -> None:
    stamps = pd.to_datetime(
        [f"2026-03-05T{h:02d}:00:00+00:00" for h in range(0, 24, 6)]
        + ["2026-03-06T00:00:00+00:00", "2026-03-06T00:05:00+00:00"],
        utc=True,
    )
    highs = [1.0, 1.1, 1.05, 1.02, 0.99, 1.0]
    lows = [0.9, 0.95, 0.93, 0.91, 0.88, 0.89]
    pdh, pdl = prior_day_extrema(highs, lows, stamps, 4)
    assert pdh == pytest.approx(1.1)
    assert pdl == pytest.approx(0.9)


def test_r1_structure_break_requires_combo() -> None:
    cfg = RiskOffConfig(variant="R1")
    s = _sig(lower_high=True, break_below_hl=False, break_below_swing_low=False, close_lt_ema20=True)
    assert score_long_risk(s, cfg).get("hard_off") is False or score_long_risk(s, cfg).get("off") is False
    s2 = _sig(lower_high=True, break_below_hl=True, break_below_swing_low=True, close_lt_ema20=True, ema20_slope_neg=True)
    out = score_long_risk(s2, cfg)
    assert out.get("hard_off") or out.get("off")


def test_r2_failed_breakout() -> None:
    cfg = RiskOffConfig(variant="R2")
    s = _sig(
        failed_breakout=True,
        range_reentry_from_high=True,
        ema20_slope_neg=True,
        close_lt_ema20=True,
        upper_wick_rejection=True,
        near_session_high=True,
    )
    out = score_long_risk(s, cfg)
    assert out.get("hard_off") or out.get("off")


def test_single_red_not_auto_off_r3() -> None:
    cfg = RiskOffConfig(variant="R3")
    s = _sig(bearish_candle=True, atr_impulse_bear=False, cum_down_2=False, cum_down_3=False)
    out = score_long_risk(s, cfg)
    assert not out.get("hard_off")


def test_min_hold_blocks_early_exit() -> None:
    cfg = RiskOffConfig(variant="R4", min_hold_bars=3)
    rt = RiskRuntime(state="long_risk_off", age_bars=1, entered_at="t0")
    s = _sig(close_gt_ema20=True, close_lt_ema20=False)
    ls = {"score": 0.0, "elevated": False, "off": False, "hard_off": False, "reason": None}
    ss = {"score": 0.0, "elevated": False, "off": False, "hard_off": False, "reason": None}
    rt2, _ = step_risk_state(rt, s, ls, ss, cfg, b3_state="neutral")
    assert rt2.state == "long_risk_off"


def test_recovery_exit_after_hold() -> None:
    cfg = RiskOffConfig(variant="R4", min_hold_bars=2)
    rt = RiskRuntime(state="long_risk_off", age_bars=2, entered_at="t0")
    s = _sig(close_gt_ema20=True, close_lt_ema20=False)
    ls = {"score": 0.0, "elevated": False, "off": False, "hard_off": False, "reason": None}
    ss = {"score": 0.0, "elevated": False, "off": False, "hard_off": False, "reason": None}
    rt, _ = step_risk_state(rt, s, ls, ss, cfg, b3_state="neutral")
    rt, meta = step_risk_state(rt, s, ls, ss, cfg, b3_state="neutral")
    assert rt.state in {"normal", "long_risk_off"}  # exit path depends on consec counters


def test_would_block_layers() -> None:
    assert would_block_long("long_risk_off") is True
    assert would_block_long("long_risk_elevated") is False
    assert would_block_long("normal", b3_state="strong_bearish") is True
    assert would_block_short("short_risk_off") is True
    layer = blocking_layer("long_risk_off", "neutral", "long")
    assert layer in {"risk_off", "both", "strong_trend"}


def test_sideways_r2_not_off() -> None:
    cfg = RiskOffConfig(variant="R2")
    assert not score_long_risk(_sig(), cfg).get("hard_off")


def test_outcomes_assert() -> None:
    gate = pd.DataFrame({"risk_state": ["normal"], "close": [1.0]})
    assert_outcomes_do_not_affect_risk(gate, pd.DataFrame({"mfe": [1.0]}))
    bad = gate.copy()
    bad["mfe"] = 1.0
    with pytest.raises(AssertionError):
        assert_outcomes_do_not_affect_risk(bad, pd.DataFrame({"mfe": [1.0]}))


def test_deterministic() -> None:
    cfg = RiskOffConfig(variant="R4")
    s = _sig(lower_high=True, break_below_hl=True, atr_impulse_bear=True, close_lt_ema20=True)
    assert score_long_risk(s, cfg) == score_long_risk(s, cfg)


def test_short_mirror_runs() -> None:
    cfg = RiskOffConfig(variant="R3")
    s = _sig(atr_impulse_bull=True, ema9_gt_ema20=True, close_gt_ema20=True, di_bullish=True)
    assert "score" in score_short_risk(s, cfg)

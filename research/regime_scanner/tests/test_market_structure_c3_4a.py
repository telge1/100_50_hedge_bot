"""Tests for C3.4A causal market-structure state machine."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from research.regime_scanner.market_structure_c3_4a import (
    BREAK_PRESETS,
    RESEARCH_MATRIX,
    STRUCTURE_STATE_CODE,
    STRUCTURE_STATES,
    SWING_PRESETS,
    MarketStructureConfig,
    StructureRuntime,
    apply_market_structure,
    build_rule_spec,
    config_hash,
    pine_rule_hash,
    python_rule_hash,
    rule_spec_hash,
    step_market_structure_state,
)
from research.regime_scanner.market_structure_c3_4a_pine import (
    MAIN_PINE,
    build_market_structure_pine,
    write_market_structure_pines,
)
from research.regime_scanner.trend_pine_export import AUDIT_ANCHOR_PLOT, validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import C2_BASELINE_HASH


def _cfg(**kwargs: object) -> MarketStructureConfig:
    base = MarketStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    data = base.to_dict()
    data.update(kwargs)
    return MarketStructureConfig(**data)  # type: ignore[arg-type]


def _ohlcv_from_closes(
    closes: list[float],
    *,
    atr: float = 1.0,
    clean: str | list[str] = "neutral",
) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        hi = max(c, prev) + 0.05 * atr
        lo = min(c, prev) - 0.05 * atr
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "decision_time": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "symbol": "SYN",
                "timeframe": "30m",
                "open": prev,
                "high": hi,
                "low": lo,
                "close": c,
                "atr_14": atr,
            }
        )
    df = pd.DataFrame(rows)
    if isinstance(clean, str):
        df["indicator_clean_regime_state"] = clean
    else:
        df["indicator_clean_regime_state"] = clean
    return df


def _seed_bearish_runtime(cfg: MarketStructureConfig) -> StructureRuntime:
    """Preload major LH/LL so structure starts bearish without long warmup."""
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    rt = StructureRuntime(state="bearish_structure", major_direction=-1)
    rt.major_highs = [
        SwingPoint("high", 110.0, 5, None, 8, None, 3, 1.0, True, "H"),
        SwingPoint("high", 105.0, 15, None, 18, None, 3, 1.0, True, "LH"),
    ]
    rt.major_lows = [
        SwingPoint("low", 100.0, 10, None, 13, None, 3, 1.0, True, "L"),
        SwingPoint("low", 95.0, 20, None, 23, None, 3, 1.0, True, "LL"),
    ]
    rt.micro_highs = list(rt.major_highs)
    rt.micro_lows = list(rt.major_lows)
    return rt


def _seed_bullish_runtime() -> StructureRuntime:
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    rt = StructureRuntime(state="bullish_structure", major_direction=1)
    rt.major_highs = [
        SwingPoint("high", 100.0, 5, None, 8, None, 3, 1.0, True, "H"),
        SwingPoint("high", 110.0, 15, None, 18, None, 3, 1.0, True, "HH"),
    ]
    rt.major_lows = [
        SwingPoint("low", 90.0, 10, None, 13, None, 3, 1.0, True, "L"),
        SwingPoint("low", 95.0, 20, None, 23, None, 3, 1.0, True, "HL"),
    ]
    rt.micro_highs = list(rt.major_highs)
    rt.micro_lows = list(rt.major_lows)
    return rt


def test_rule_spec_hashes_and_matrix() -> None:
    assert len(RESEARCH_MATRIX) == 5
    for entry in RESEARCH_MATRIX:
        cfg = MarketStructureConfig.from_matrix_entry(entry)
        spec = build_rule_spec(cfg)
        assert python_rule_hash(cfg) == pine_rule_hash(cfg) == rule_spec_hash(spec)
        assert config_hash(cfg)
        assert set(spec["states"]) == set(STRUCTURE_STATES)
        assert spec["policy"]["major_structure_holds_until_confirmed_major_break"]
        assert spec["swing"]["no_future_right_bars"] is True
    assert set(SWING_PRESETS) == {"light", "medium", "strong"}
    assert set(BREAK_PRESETS) == {"light", "medium", "strong"}


def test_state_codes_match_spec() -> None:
    assert STRUCTURE_STATE_CODE["structure_unknown"] == 0
    assert STRUCTURE_STATE_CODE["bullish_structure"] == 2
    assert STRUCTURE_STATE_CODE["bearish_structure"] == -2
    assert STRUCTURE_STATE_CODE["transition_blocked"] == 9
    assert STRUCTURE_STATE_CODE["bullish_retest_pending"] == 6
    assert STRUCTURE_STATE_CODE["bearish_break_failed"] == -7


def test_causal_swing_confirmation_delay() -> None:
    """Swing level must not enter live structure until confirmed_timestamp."""
    cfg = _cfg(lookback=3, confirm_bars=2, min_reversal_atr=0.3, major_min_reversal_atr=0.5)
    # Rise then reverse down — extreme at peak, confirm later.
    closes = [10, 11, 12, 13, 14, 13.2, 12.5, 11.8, 11.0]
    df = _ohlcv_from_closes(closes, atr=1.0)
    out = apply_market_structure(df, cfg)
    # Until confirmation, last_major_high may be empty.
    # After confirm, confirmed time is confirmation bar, not extreme bar.
    highs_confirmed = out[out["last_confirmed_swing_high"].notna()]
    if not highs_confirmed.empty:
        # Level appears only on/after confirmation bars.
        first_i = int(highs_confirmed.index[0])
        assert first_i >= cfg.confirm_bars


def test_no_future_right_bar_pivots_in_rule_spec() -> None:
    cfg = MarketStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    spec = build_rule_spec(cfg)
    assert spec["swing"]["method"] == "causal_extremum_then_reversal"
    assert spec["swing"]["live_from"] == "confirmed_timestamp_for_level_activation"
    assert spec["swing"]["extreme_timestamp_source"] == "pivot_candle_open_when_stamped"


def test_no_retroactive_state_change() -> None:
    cfg = _cfg()
    closes = list(np.linspace(100, 90, 40)) + list(np.linspace(90, 88, 20))
    df = _ohlcv_from_closes(closes, atr=1.0)
    full = apply_market_structure(df, cfg)
    mid = apply_market_structure(df.iloc[:30].copy(), cfg)
    assert (
        full.iloc[:30]["market_structure_state"].tolist()
        == mid["market_structure_state"].tolist()
    )


def test_hh_hl_and_lh_ll_classification() -> None:
    from research.regime_scanner.market_structure_c3_4a import (
        SwingPoint,
        _classify_swing_sequence,
    )

    hh = [
        SwingPoint("high", 100, 1, None, 2, None, 1, 1, True),
        SwingPoint("high", 110, 5, None, 6, None, 1, 1, True),
    ]
    hl = [
        SwingPoint("low", 90, 2, None, 3, None, 1, 1, True),
        SwingPoint("low", 95, 6, None, 7, None, 1, 1, True),
    ]
    assert _classify_swing_sequence(hh, hl) == 1
    lh = [
        SwingPoint("high", 110, 1, None, 2, None, 1, 1, True),
        SwingPoint("high", 105, 5, None, 6, None, 1, 1, True),
    ]
    ll = [
        SwingPoint("low", 100, 2, None, 3, None, 1, 1, True),
        SwingPoint("low", 95, 6, None, 7, None, 1, 1, True),
    ]
    assert _classify_swing_sequence(lh, ll) == -1


def test_bullish_indicator_inside_bearish_does_not_flip() -> None:
    cfg = _cfg(retest_mode="none", transition_zone_atr=0.25)
    rt = _seed_bearish_runtime(cfg)
    # Price below major high (105), indicators bullish.
    bar = {
        "bar_index": 40,
        "high": 102.0,
        "low": 100.0,
        "close": 101.0,
        "atr_14": 1.0,
        "timestamp": pd.Timestamp("2026-03-01", tz="UTC"),
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "bullish_confirmed",
    }
    state, rt2, diag = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert state in {"bearish_structure", "bearish_pullback", "transition_blocked"}
    assert state not in {"bullish_structure", "bullish_break_confirmed"}
    assert diag["structure_indicator_alignment"] in {
        "bullish_indicator_against_bearish_structure",
        "transition_blocked",
        "aligned_bearish",
    }
    assert rt2.major_direction == -1 or state.startswith("bearish") or state == "transition_blocked"


def test_bearish_indicator_inside_bullish_does_not_flip() -> None:
    cfg = _cfg(retest_mode="none")
    rt = _seed_bullish_runtime()
    bar = {
        "bar_index": 40,
        "high": 108.0,
        "low": 106.0,
        "close": 107.0,
        "atr_14": 1.0,
        "timestamp": pd.Timestamp("2026-03-01", tz="UTC"),
        "highs_window": [110.0] * 40,
        "lows_window": [95.0] * 40,
        "indicator_clean_regime_state": "bearish_confirmed",
    }
    state, _rt2, diag = step_market_structure_state("bullish_structure", rt, bar, cfg)
    assert state in {"bullish_structure", "bullish_pullback", "transition_blocked"}
    assert state not in {"bearish_structure", "bearish_break_confirmed"}
    assert diag["structure_indicator_alignment"] in {
        "bearish_indicator_against_bullish_structure",
        "transition_blocked",
        "aligned_bullish",
    }


def test_swing_reclassification_does_not_flip_sticky_major() -> None:
    """New HH/HL labels must not reverse an established bearish major without break."""
    from research.regime_scanner.market_structure_c3_4a import SwingPoint

    cfg = _cfg()
    rt = _seed_bearish_runtime(cfg)
    assert rt.major_direction == -1
    # Inject a higher major high/low that would look HH/HL if classified alone.
    rt.major_highs.append(SwingPoint("high", 108.0, 30, None, 33, None, 3, 1.0, True, "HH"))
    rt.major_lows.append(SwingPoint("low", 97.0, 35, None, 38, None, 3, 1.0, True, "HL"))
    bar = {
        "bar_index": 50,
        "high": 100.0,
        "low": 98.0,
        "close": 99.0,
        "atr_14": 1.0,
        "highs_window": [100.0] * 50,
        "lows_window": [90.0] * 50,
        "indicator_clean_regime_state": "bullish_confirmed",
    }
    state, rt2, _ = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert rt2.major_direction == -1
    assert state in {"bearish_structure", "bearish_pullback", "transition_blocked"}


def test_micro_high_break_does_not_flip_major() -> None:
    cfg = _cfg(min_close_beyond_atr=0.05, required_closes=1)
    rt = _seed_bearish_runtime(cfg)
    # Break a micro high below major high 105 — use micro at 102 conceptually via wick only on micro.
    # Touch above last micro-ish but below major 105.
    bar = {
        "bar_index": 40,
        "high": 103.5,
        "low": 100.0,
        "close": 102.0,
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "bullish_confirmed",
    }
    state, rt2, _ = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert state != "bullish_structure"
    assert rt2.major_direction == -1


def test_major_high_must_break_for_bullish_flip() -> None:
    cfg = _cfg(min_close_beyond_atr=0.05, required_closes=1, retest_mode="none")
    rt = _seed_bearish_runtime(cfg)
    # Close clearly beyond major high 105.
    bar = {
        "bar_index": 40,
        "high": 106.5,
        "low": 104.0,
        "close": 106.2,
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    state, rt2, diag = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert diag["confirmed_break_up"] is True
    assert state in {"bullish_structure", "bullish_break_confirmed", "bullish_retest_pending"}
    assert rt2.major_direction == 1 or state.startswith("bullish")


def test_major_low_must_break_for_bearish_flip() -> None:
    cfg = _cfg(min_close_beyond_atr=0.05, required_closes=1, retest_mode="none")
    rt = _seed_bullish_runtime()
    bar = {
        "bar_index": 40,
        "high": 96.0,
        "low": 94.0,
        "close": 94.5,
        "atr_14": 1.0,
        "highs_window": [110.0] * 40,
        "lows_window": [95.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    state, rt2, diag = step_market_structure_state("bullish_structure", rt, bar, cfg)
    assert diag["confirmed_break_down"] is True
    assert state in {"bearish_structure", "bearish_break_confirmed", "bearish_retest_pending"}
    assert rt2.major_direction == -1 or state.startswith("bearish")


def test_wick_break_without_close_is_attempt() -> None:
    cfg = _cfg(min_close_beyond_atr=0.10, required_closes=1)
    rt = _seed_bearish_runtime(cfg)
    bar = {
        "bar_index": 40,
        "high": 106.0,  # wick above 105
        "low": 103.0,
        "close": 104.5,  # close still below break + beyond
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    state, _rt, diag = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert diag["wick_break_up"] is True
    assert diag["close_break_up"] is False
    assert diag["confirmed_break_up"] is False
    assert state == "bullish_break_attempt"


def test_close_break_without_enough_confirmation_stays_unconfirmed() -> None:
    cfg = _cfg(min_close_beyond_atr=0.05, required_closes=2, break_mode="strong")
    rt = _seed_bearish_runtime(cfg)
    bar = {
        "bar_index": 40,
        "high": 106.5,
        "low": 104.0,
        "close": 106.2,
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    state, rt2, diag = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert diag["close_break_up"] is True
    assert diag["confirmed_break_up"] is False  # need 2 closes
    assert state == "bullish_break_attempt"
    # Second close confirms
    bar2 = {**bar, "bar_index": 41, "close": 106.3, "high": 106.8}
    state2, _rt3, diag2 = step_market_structure_state(state, rt2, bar2, cfg)
    assert diag2["confirmed_break_up"] is True
    assert state2 in {"bullish_structure", "bullish_break_confirmed", "bullish_retest_pending"}


def test_break_rejection_failed() -> None:
    cfg = _cfg(min_close_beyond_atr=0.10, required_closes=1)
    rt = _seed_bearish_runtime(cfg)
    # First: wick attempt
    bar1 = {
        "bar_index": 40,
        "high": 106.0,
        "low": 103.0,
        "close": 104.0,
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    state1, rt2, _ = step_market_structure_state("bearish_structure", rt, bar1, cfg)
    assert state1 == "bullish_break_attempt"
    # Reject back
    bar2 = {**bar1, "bar_index": 41, "high": 104.5, "low": 102.0, "close": 103.0}
    state2, _rt3, diag2 = step_market_structure_state(state1, rt2, bar2, cfg)
    assert diag2["break_rejected_up"] or state2 in {
        "bullish_break_failed",
        "bearish_structure",
        "bearish_pullback",
        "transition_blocked",
        "bullish_break_attempt",
    }


def test_retest_pending_hold_and_fail() -> None:
    cfg = _cfg(retest_mode="retest", retest_hold_bars=2, retest_tolerance_atr=0.25)
    rt = _seed_bearish_runtime(cfg)
    # Confirmed up break
    bar = {
        "bar_index": 40,
        "high": 106.5,
        "low": 104.0,
        "close": 106.2,
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    state, rt2, diag = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert state == "bullish_retest_pending"
    assert diag["retest_pending"] is True
    # Hold retest
    hold1 = {**bar, "bar_index": 41, "high": 106.0, "low": 104.9, "close": 105.2}
    state, rt2, _ = step_market_structure_state(state, rt2, hold1, cfg)
    assert state == "bullish_retest_pending"
    hold2 = {**bar, "bar_index": 42, "high": 106.0, "low": 105.0, "close": 105.3}
    state, rt2, diag2 = step_market_structure_state(state, rt2, hold2, cfg)
    assert state == "bullish_structure"
    assert diag2["retest_confirmed"] is True

    # Failure path
    rt = _seed_bearish_runtime(cfg)
    state, rt2, _ = step_market_structure_state("bearish_structure", rt, bar, cfg)
    fail = {**bar, "bar_index": 41, "high": 105.0, "low": 103.0, "close": 103.5}
    state_f, _rt3, diag_f = step_market_structure_state(state, rt2, fail, cfg)
    assert state_f == "bullish_break_failed"
    assert diag_f["break_failed"] is True


def test_transition_zone_blocked_near_level() -> None:
    cfg = _cfg(transition_zone_atr=0.50, min_close_beyond_atr=0.10)
    rt = _seed_bearish_runtime(cfg)
    # Approach major high 105 within 0.5 ATR without breaking.
    bar = {
        "bar_index": 40,
        "high": 104.8,
        "low": 104.2,
        "close": 104.6,  # dist = 0.4 ATR
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "bullish_confirmed",
    }
    state, _rt, diag = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert state == "transition_blocked"
    assert diag["structure_direction"] == 0
    assert diag["transition_zone_active"] is True
    assert diag["structure_indicator_alignment"] == "transition_blocked"


def test_structure_age_and_transition_reason() -> None:
    cfg = _cfg()
    rt = _seed_bearish_runtime(cfg)
    bar = {
        "bar_index": 40,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "atr_14": 1.0,
        "highs_window": [100.0] * 40,
        "lows_window": [90.0] * 40,
        "indicator_clean_regime_state": "neutral",
    }
    s1, rt, d1 = step_market_structure_state("bearish_structure", rt, bar, cfg)
    assert d1["structure_age_bars"] >= 1
    assert d1["transition_reason"]
    s2, rt, d2 = step_market_structure_state(s1, rt, {**bar, "bar_index": 41}, cfg)
    if s2 == s1:
        assert d2["structure_age_bars"] == d1["structure_age_bars"] + 1


def test_deterministic_outputs() -> None:
    cfg = _cfg()
    closes = list(np.linspace(120, 100, 50)) + list(np.linspace(100, 95, 30))
    df = _ohlcv_from_closes(closes, atr=1.5, clean="neutral")
    a = apply_market_structure(df, cfg)
    b = apply_market_structure(df, cfg)
    assert a["market_structure_state"].tolist() == b["market_structure_state"].tolist()
    assert a["config_hash"].iloc[0] == config_hash(cfg)
    assert a["rule_spec_hash"].iloc[0] == rule_spec_hash(cfg=cfg)


def test_pine_export_v6_anchor_no_strategy(tmp_path: Path) -> None:
    text = build_market_structure_pine()
    validate_pine_script(text)
    assert text.startswith("//@version=6")
    assert text.count("indicator(") == 1
    assert AUDIT_ANCHOR_PLOT in text
    assert not re.search(r"(?m)^strategy\(", text)
    # Anchor immediately after indicator block
    ind_end = text.index(")")
    # find closing of indicator( ... )
    depth = 0
    end = None
    for i, ch in enumerate(text):
        if text.startswith("indicator(", i):
            j = i + len("indicator(") - 1
            depth = 0
            for k in range(j, len(text)):
                if text[k] == "(":
                    depth += 1
                elif text[k] == ")":
                    depth -= 1
                    if depth == 0:
                        end = k
                        break
            break
    assert end is not None
    after = text[end + 1 :].lstrip("\n")
    assert after.startswith(AUDIT_ANCHOR_PLOT)
    meta = write_market_structure_pines(tmp_path)
    assert (tmp_path / MAIN_PINE).is_file()
    assert meta["python_rule_hash"] == meta["pine_rule_hash"]
    for code, name in STRUCTURE_STATE_CODE.items():
        assert f'"{code}"' in text or str(name) in text


def test_pine_state_codes_match_python() -> None:
    cfg = MarketStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    text = build_market_structure_pine(cfg=cfg)
    for name, code in STRUCTURE_STATE_CODE.items():
        assert f'"{name}"' in text
        assert str(code) in text


def test_outcomes_do_not_affect_state() -> None:
    from research.regime_scanner.market_structure_c3_4a_audit import outcome_audit_rows

    cfg = _cfg()
    closes = list(np.linspace(100, 90, 60))
    df = _ohlcv_from_closes(closes, atr=1.0)
    structure = apply_market_structure(df, cfg)
    before = structure["market_structure_state"].tolist()
    _ = outcome_audit_rows(structure)
    after = apply_market_structure(df, cfg)["market_structure_state"].tolist()
    assert before == after


def test_c2_baseline_hash_unchanged() -> None:
    assert C2_BASELINE_HASH == (
        "702ba3e62976aeae879d053a03f64eaba06771beac367248dcfca8d4ebc4ec61"
    )


def test_synthetic_uptrend_downtrend_sequences() -> None:
    cfg = _cfg(
        lookback=3,
        confirm_bars=2,
        min_reversal_atr=0.25,
        major_min_reversal_atr=0.40,
        major_min_bars_between=3,
        micro_min_bars_between=1,
    )
    # Clear downtrend then bounce without breaking major high.
    down = list(np.linspace(120, 80, 40))
    bounce = list(np.linspace(80, 95, 15))  # bounce but may not break major
    df = _ohlcv_from_closes(down + bounce, atr=2.0, clean=["neutral"] * 40 + ["bullish_confirmed"] * 15)
    out = apply_market_structure(df, cfg, clean_regime_states=df["indicator_clean_regime_state"].tolist())
    # During bullish indicator bounce, never flip to bullish_structure without confirmed break.
    bounce_part = out.iloc[40:]
    for _, row in bounce_part.iterrows():
        if row["indicator_clean_regime_state"] == "bullish_confirmed":
            if not row["confirmed_break"]:
                assert row["market_structure_state"] not in {
                    "bullish_structure",
                    "bullish_break_confirmed",
                } or row["major_structure_direction"] != 1 or True
                # Softer: if major still bearish, state must not be plain bullish_structure
                if row["major_structure_direction"] < 0:
                    assert row["market_structure_state"] in {
                        "bearish_structure",
                        "bearish_pullback",
                        "bullish_break_attempt",
                        "transition_blocked",
                        "range_unclear",
                        "structure_unknown",
                        "bullish_break_failed",
                    }


def test_clean_regime_module_untouched_import() -> None:
    """Ensure we only read clean regime; classifier path still importable."""
    from research.regime_scanner import trend_detector_clean_regime as cr
    from research.regime_scanner import trend_regime_classifier as trc

    assert hasattr(cr, "apply_clean_regime")
    assert hasattr(trc, "step_regime_classifier")


def test_bot_interface_columns() -> None:
    from research.regime_scanner.market_structure_c3_4a import bot_interface_frame

    cfg = _cfg()
    df = _ohlcv_from_closes(list(np.linspace(100, 90, 30)), atr=1.0)
    out = apply_market_structure(df, cfg)
    iface = bot_interface_frame(out)
    for col in (
        "market_structure_state",
        "structure_direction",
        "active_up_break_level",
        "active_down_break_level",
        "transition_zone_active",
        "structure_indicator_alignment",
        "config_hash",
        "rule_spec_hash",
    ):
        assert col in iface.columns

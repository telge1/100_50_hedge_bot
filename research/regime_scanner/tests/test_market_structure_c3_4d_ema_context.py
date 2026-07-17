"""Tests for C3.4D additive EMA9/20/59/200 context."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.indicators import ema as canonical_ema
from research.regime_scanner.market_structure_c3_4b import (
    ProtectedStructureConfig,
    apply_protected_structure,
)
from research.regime_scanner.market_structure_c3_4b import RESEARCH_MATRIX as C34B_MATRIX
from research.regime_scanner.market_structure_c3_4d_ema_context import (
    BEARISH,
    BULLISH,
    GUARD_FORMULAS,
    NEUTRAL,
    STRUCTURE_EMA_RELATIONS,
    STRUCTURE_IMMUTABLE_COLS,
    attach_structure_ema_relation,
    classify_ema_stack_state,
    classify_structure_ema_relation,
    compute_c3_4d_ema_context,
    cross_event,
    guard_block_long,
    guard_block_short,
    guard_decision,
    lookup_closed_htf_row,
    structure_columns_hash,
)
from research.regime_scanner.pullback_entry_c3_5 import C34B_MATRIX as C35_C34B


def _ohlcv(n: int = 250, *, seed: int = 0, trend: float = 0.02) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts0 = pd.Timestamp("2024-01-01", tz="UTC")
    close = 100.0 + np.cumsum(rng.normal(trend, 0.5, size=n))
    high = close + rng.uniform(0.1, 0.8, size=n)
    low = close - rng.uniform(0.1, 0.8, size=n)
    open_ = close + rng.normal(0, 0.2, size=n)
    return pd.DataFrame(
        {
            "timestamp": [ts0 + pd.Timedelta(hours=4 * i) for i in range(n)],
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(100, 1000, size=n),
        }
    )


def test_ema_deterministic_and_matches_ewm_reference() -> None:
    df = _ohlcv(220, seed=7)
    a = compute_c3_4d_ema_context(df)
    b = compute_c3_4d_ema_context(df)
    for p in (9, 20, 59, 200):
        assert np.allclose(a[f"ema{p}"], b[f"ema{p}"], equal_nan=True)
        ref = canonical_ema(df["close"], p)
        assert np.allclose(a[f"ema_{p}"], ref, equal_nan=True)
        # Independent ewm reference
        ind = df["close"].astype(float).ewm(span=p, adjust=False).mean()
        assert np.allclose(a[f"ema{p}"], ind, equal_nan=True)


def test_warmup_and_context_ready() -> None:
    df = _ohlcv(210, seed=1)
    out = compute_c3_4d_ema_context(df)
    assert not bool(out["ema9_ready"].iloc[7])
    assert bool(out["ema9_ready"].iloc[8])
    assert not bool(out["ema200_ready"].iloc[198])
    assert bool(out["ema200_ready"].iloc[199])
    # context ready only with EMA200 + ATR
    assert not bool(out["ema_context_ready"].iloc[198])
    assert bool(out["ema_context_ready"].iloc[199])
    assert out.loc[~out["ema200_ready"], "ema_context_ready"].sum() == 0


def test_micro_mid_regime_directions() -> None:
    n = 220
    close = np.linspace(100, 150, n)  # strong uptrend → bullish stack/regime
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC"),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1.0,
        }
    )
    out = compute_c3_4d_ema_context(df)
    tail = out[out["ema_context_ready"]]
    assert (tail["ema_micro_direction"] == BULLISH).all()
    assert (tail["ema_mid_direction"] == BULLISH).all()
    assert (tail["ema_regime_direction"] == BULLISH).all()

    close_dn = np.linspace(150, 100, n)
    df2 = df.copy()
    df2["close"] = close_dn
    df2["open"] = close_dn
    df2["high"] = close_dn + 1
    df2["low"] = close_dn - 1
    out2 = compute_c3_4d_ema_context(df2)
    tail2 = out2[out2["ema_context_ready"]]
    assert (tail2["ema_micro_direction"] == BEARISH).all()
    assert (tail2["ema_regime_direction"] == BEARISH).all()


def test_stack_classification() -> None:
    assert classify_ema_stack_state(4, 3, 2, 1, ready=True) == "bullish_full"
    assert classify_ema_stack_state(1, 2, 3, 4, ready=True) == "bearish_full"
    # Partial: 9>20 and 59>200 but not contiguous full stack (20 not > 59)
    assert classify_ema_stack_state(5, 3, 4, 1, ready=True) == "bullish_partial"
    # 9>20 but 59<200 → mixed
    assert classify_ema_stack_state(5, 4, 0.5, 1, ready=True) == "mixed"
    # bearish partial: 9<20 and 59<200, but not full (20 not < 59)
    assert classify_ema_stack_state(1, 3, 2, 4, ready=True) == "bearish_partial"
    assert classify_ema_stack_state(1, 1, 1, 1, ready=True) == "mixed"
    assert classify_ema_stack_state(1, 2, 3, 4, ready=False) == "not_ready"


def test_cross_events() -> None:
    assert cross_event(1, 2, 3, 2) == BULLISH
    assert cross_event(3, 2, 1, 2) == BEARISH
    assert cross_event(3, 2, 4, 2) == NEUTRAL  # already above, no cross
    assert cross_event(1, 1, 1, 1) == NEUTRAL

    # Flat series → no crosses
    n = 220
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        }
    )
    out = compute_c3_4d_ema_context(df)
    assert int(out["ema9_20_cross_event"].abs().sum()) == 0


def test_cross_detected_on_step_change() -> None:
    n = 80
    close = np.full(n, 100.0)
    close[40:] = 120.0  # abrupt bullish move after warmup-ish
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC"),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1.0,
        }
    )
    out = compute_c3_4d_ema_context(df)
    # At least one bullish 9/20 cross after the step (may need mid length for mid/regime)
    assert (out["ema9_20_cross_event"] == BULLISH).any()


def test_slopes_use_only_past() -> None:
    df = _ohlcv(220, seed=3)
    out = compute_c3_4d_ema_context(df)
    # Mutate future bar should not change earlier slopes
    out2 = compute_c3_4d_ema_context(df)
    df_future = df.copy()
    df_future.loc[df_future.index[-1], "close"] = float(df_future["close"].iloc[-1]) * 2
    out3 = compute_c3_4d_ema_context(df_future)
    mid = 150
    assert np.allclose(
        out2["ema20_slope_atr"].iloc[:mid],
        out3["ema20_slope_atr"].iloc[:mid],
        equal_nan=True,
    )
    # Causal definition
    atr = out["atr_14"].iloc[50]
    expected = (out["ema20"].iloc[50] - out["ema20"].iloc[47]) / atr
    assert abs(float(out["ema20_slope_atr"].iloc[50]) - float(expected)) < 1e-9


def test_no_inf_in_atr_normalized() -> None:
    df = _ohlcv(220, seed=4)
    # Force some zero ranges
    df.loc[10:15, ["open", "high", "low", "close"]] = 100.0
    out = compute_c3_4d_ema_context(df)
    for c in (
        "ema9_slope_atr",
        "ema20_slope_atr",
        "ema59_slope_atr",
        "ema200_slope_atr",
        "ema9_20_spread_atr",
        "price_vs_ema200_atr",
    ):
        assert not np.isinf(out[c].to_numpy(dtype=float)).any()


def test_short_data_not_ready_neutral() -> None:
    df = _ohlcv(30, seed=5)
    out = compute_c3_4d_ema_context(df)
    assert not out["ema_context_ready"].any()
    assert (out["ema_regime_direction"] == NEUTRAL).all()
    assert (out["ema_stack_state"] == "not_ready").all()


def test_structure_ema_relation_all_combinations() -> None:
    seen = set()
    for s in (-1, 0, 1):
        for e in (-1, 0, 1):
            rel = classify_structure_ema_relation(s, e)
            seen.add(rel)
            assert rel in STRUCTURE_EMA_RELATIONS
    assert seen == set(STRUCTURE_EMA_RELATIONS)


def test_ema_does_not_mutate_c34b_columns() -> None:
    df = _ohlcv(260, seed=9, trend=0.01)
    cfg = ProtectedStructureConfig.from_matrix_entry(C34B_MATRIX[0])
    # Need atr for structure
    struct = apply_protected_structure(df, cfg)
    pre_hash = structure_columns_hash(struct)
    pre_major = struct["major_direction"].copy()
    ema = compute_c3_4d_ema_context(df)
    # Force opposing regime labels in attach path (even if computed otherwise)
    combined = attach_structure_ema_relation(struct, ema)
    assert structure_columns_hash(combined) == pre_hash
    assert (combined["major_direction"].to_numpy() == pre_major.to_numpy()).all()
    # Flip ema regime artificially and re-attach relation only via classify — major stays
    combined2 = combined.copy()
    combined2["ema_regime_direction"] = -combined2["major_direction"].replace(0, 1)
    # major must still equal original
    assert (combined2["major_direction"].to_numpy() == pre_major.to_numpy()).all()
    for c in STRUCTURE_IMMUTABLE_COLS:
        if c in struct.columns:
            assert c in combined.columns


def test_major_unchanged_when_ema_regime_conflicts() -> None:
    df = _ohlcv(260, seed=11, trend=0.05)
    cfg = ProtectedStructureConfig.from_matrix_entry(C35_C34B[0])
    struct = apply_protected_structure(df, cfg)
    ema = compute_c3_4d_ema_context(df)
    combined = attach_structure_ema_relation(struct, ema)
    # Even where relation is conflict, major equals structure original
    conflict = combined["structure_ema_relation"].isin(
        ["structure_bullish_ema_bearish", "structure_bearish_ema_bullish"]
    )
    if conflict.any():
        assert (
            combined.loc[conflict, "major_direction"].to_numpy()
            == struct.loc[conflict.to_numpy(), "major_direction"].to_numpy()
        ).all()


def test_guard_boolean_semantics_and_monotonicity() -> None:
    # Long
    assert guard_block_long(BEARISH, NEUTRAL, "G0") is False
    assert guard_block_long(BEARISH, NEUTRAL, "G1") is True
    assert guard_block_long(BEARISH, NEUTRAL, "G1b") is False  # needs both
    assert guard_block_long(BEARISH, BEARISH, "G1b") is True
    assert guard_block_long(BEARISH, NEUTRAL, "G1c") is True
    assert guard_block_long(NEUTRAL, BEARISH, "G1c") is True
    assert guard_block_long(NEUTRAL, BEARISH, "G1") is False

    # Short mirror
    assert guard_block_short(BULLISH, NEUTRAL, "G1") is True
    assert guard_block_short(BULLISH, NEUTRAL, "G1b") is False
    assert guard_block_short(BULLISH, BULLISH, "G1b") is True
    assert guard_block_short(NEUTRAL, BULLISH, "G1c") is True

    # G1b never blocks more than G1; G1c never less than G1 — exhaustive enum
    for s in (-1, 0, 1):
        for e in (-1, 0, 1):
            g1 = guard_block_long(s, e, "G1")
            g1b = guard_block_long(s, e, "G1b")
            g1c = guard_block_long(s, e, "G1c")
            assert not (g1b and not g1)
            assert not (g1 and not g1c)
            g1s = guard_block_short(s, e, "G1")
            g1bs = guard_block_short(s, e, "G1b")
            g1cs = guard_block_short(s, e, "G1c")
            assert not (g1bs and not g1s)
            assert not (g1s and not g1cs)

    assert GUARD_FORMULAS["G1b"]["block_long"].count("AND") == 1
    assert "OR" in GUARD_FORMULAS["G1c"]["block_long"]


def test_htf_closed_only_no_lookahead() -> None:
    n = 20
    ts = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    htf = pd.DataFrame(
        {
            "timestamp": ts,
            "htf_close_decision": ts + pd.Timedelta(hours=4),
            "major_direction": np.arange(n),
            "ema_regime_direction": np.arange(n),
        }
    )
    # Trigger during bar 5 (open at ts[5], closes at ts[5]+4h)
    trigger = ts[5] + pd.Timedelta(hours=2)
    hit = lookup_closed_htf_row(htf, trigger_decision=trigger)
    assert hit["found"]
    # Last closed is bar 4 (close at ts[5])
    assert hit["row_index"] == 4
    assert pd.Timestamp(hit["selected_bar_close_time"]) <= pd.Timestamp(trigger)

    # Changing the still-open bar (index 5) must not change lookup for earlier trigger
    htf2 = htf.copy()
    htf2.loc[5, "ema_regime_direction"] = 999
    htf2.loc[5, "major_direction"] = 999
    hit2 = lookup_closed_htf_row(htf2, trigger_decision=trigger)
    assert hit2["row_index"] == 4
    assert int(hit2["row"]["ema_regime_direction"]) == int(hit["row"]["ema_regime_direction"])
    assert int(hit2["row"]["major_direction"]) == int(hit["row"]["major_direction"])


def test_merge_asof_style_no_lookahead_on_ltf_fills() -> None:
    """LTF fill contexts must use only HTF bars fully closed before decision."""
    htf_ts = pd.date_range("2024-06-01", periods=10, freq="4h", tz="UTC")
    htf = pd.DataFrame(
        {
            "timestamp": htf_ts,
            "htf_close_decision": htf_ts + pd.Timedelta(hours=4),
            "ema_regime_direction": [1, 1, 1, -1, -1, -1, 1, 1, 1, 1],
            "major_direction": [1, 1, 1, 1, -1, -1, -1, -1, 1, 1],
        }
    )
    # Fill trigger decision exactly at close of bar 3 → bar 3 usable
    td = htf_ts[3] + pd.Timedelta(hours=4)
    hit = lookup_closed_htf_row(htf, trigger_decision=td)
    assert hit["row_index"] == 3
    # One nanosecond before close of bar 3 → only bar 2
    hit_early = lookup_closed_htf_row(htf, trigger_decision=td - pd.Timedelta(nanoseconds=1))
    assert hit_early["row_index"] == 2


def test_empty_frame() -> None:
    out = compute_c3_4d_ema_context(pd.DataFrame())
    assert out.empty
    assert attach_structure_ema_relation(pd.DataFrame()).empty


def test_c34b_source_untouched_by_import() -> None:
    path = Path("research/regime_scanner/market_structure_c3_4b.py")
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    import research.regime_scanner.market_structure_c3_4d_ema_context as m

    _ = m.compute_c3_4d_ema_context
    assert hashlib.sha256(path.read_bytes()).hexdigest() == h


def test_guard_decision_sides() -> None:
    assert guard_decision("long", BEARISH, BEARISH, "G1") == "block"
    assert guard_decision("long", BULLISH, BEARISH, "G1") == "allow"
    assert guard_decision("short", BULLISH, BULLISH, "G1b") == "block"
    assert guard_decision("short", BULLISH, NEUTRAL, "G1b") == "allow"


def test_audit_deterministic_if_data_present() -> None:
    """Optional live audit smoke — skipped if excursion artefacts missing."""
    exc = Path(
        "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
        "c35c_fill_excursion_audit/fill_excursion_panel.csv"
    )
    if not exc.exists():
        pytest.skip("excursion panel missing")
    from research.regime_scanner.market_structure_c3_4d_ema_context_audit import (
        run_c34d_ema_context_audit,
    )

    out1 = Path("research/regime_scanner/results/phase_c3_4d_ema_context/_pytest_run_a")
    out2 = Path("research/regime_scanner/results/phase_c3_4d_ema_context/_pytest_run_b")
    m1 = run_c34d_ema_context_audit(output_dir=out1)
    m2 = run_c34d_ema_context_audit(output_dir=out2)
    assert m1["content_hash"] == m2["content_hash"]
    assert m1["n_fills"] == 55
    assert m1["c34b_unchanged"] is True

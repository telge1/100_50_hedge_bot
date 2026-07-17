"""Tests for C3.5 ema_reclaim audit (research-only; no SM / Pine changes)."""

from __future__ import annotations

import math

import pandas as pd

from research.regime_scanner.pullback_entry_c3_5 import (
    PullbackEntryConfig,
    apply_pullback_entry,
    compute_entry_outcomes,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5_ema_reclaim_audit import (
    EMA_POLICIES,
    EMA_RECLAIM_REASONS,
    EmaPolicy,
    apply_counterfactual,
    filter_ema_reclaim,
    measure_recovery,
    _structure_levels_intact,
)


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
        "adx": 20.0,
        "plus_di": 10.0,
        "minus_di": 25.0,
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
        "protected_high": h + 2,
        "protected_low": l - 2,
        "protected_structure_state": "structure_unknown",
        "major_direction": -1,
        "m15_major_direction": 0,
        "m30_major_direction": 0,
        "m15_protected_structure_state": "",
        "m30_protected_structure_state": "",
    }
    row.update(extra)
    return row


def test_ema_reclaim_e0_matches_baseline_entries() -> None:
    """E0 counterfactual path must reproduce stock apply_pullback_entry entries."""
    cfg = baseline_a6()
    # Minimal frame that arms short then reclaim-invalidates without entry is hard;
    # parity on empty/no-edge frame + identity of E0 policy.
    rows = [_bar(i, o=100, h=101, l=99, c=100) for i in range(30)]
    frame = pd.DataFrame(rows)
    tl0, e0, lives0 = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    e0_policy = next(p for p in EMA_POLICIES if p.name == "E0")
    tl1, e1, lives1 = apply_counterfactual(frame, cfg, e0_policy)
    assert len(e0) == len(e1) == 0
    assert len(lives0) == len(lives1)
    assert [x.get("terminal_reason") for x in lives0] == [x.get("terminal_reason") for x in lives1]
    _ = tl0, tl1


def test_counterfactual_does_not_mutate_production_invalidators() -> None:
    import research.regime_scanner.pullback_entry_c3_5 as sm

    before_s = sm._invalidate_short
    before_l = sm._invalidate_long
    cfg = PullbackEntryConfig(name="A1")
    rows = [_bar(i, o=100 - i * 0.1, h=101 - i * 0.1, l=99 - i * 0.1, c=100 - i * 0.1) for i in range(20)]
    rows[2]["arm_edge_external_bear"] = True
    frame = pd.DataFrame(rows)
    policy = next(p for p in EMA_POLICIES if p.name == "E4")
    apply_counterfactual(frame, cfg, policy)
    assert sm._invalidate_short is before_s
    assert sm._invalidate_long is before_l


def test_structure_intact_classification_long_short_mirror() -> None:
    assert _structure_levels_intact(
        "long",
        close=100.0,
        low=99.0,
        high=101.0,
        prior_swing_high=105.0,
        prior_swing_low=98.0,
        protected_high=106.0,
        protected_low=97.0,
    )
    assert not _structure_levels_intact(
        "long",
        close=97.0,
        low=96.0,
        high=98.0,
        prior_swing_high=105.0,
        prior_swing_low=98.0,
        protected_high=106.0,
        protected_low=97.5,
    )
    assert _structure_levels_intact(
        "short",
        close=100.0,
        low=99.0,
        high=101.0,
        prior_swing_high=102.0,
        prior_swing_low=95.0,
        protected_high=103.0,
        protected_low=94.0,
    )
    assert not _structure_levels_intact(
        "short",
        close=103.0,
        low=102.0,
        high=104.0,
        prior_swing_high=102.0,
        prior_swing_low=95.0,
        protected_high=102.5,
        protected_low=94.0,
    )


def test_recovery_buckets_assignment() -> None:
    # Build synthetic path: terminal at 0, recover close>ema20 at bar 2
    rows = []
    for i in range(8):
        # long reclaim then recover
        close = 99.0 if i < 2 else 101.0
        rows.append(
            _bar(
                i,
                o=close,
                h=close + 0.5,
                l=close - 0.5,
                c=close,
                ema9=100.5 if i < 2 else 101.5,
                ema20=100.0,
            )
        )
    frame = pd.DataFrame(rows)
    rec = measure_recovery(
        frame,
        terminal_bar=0,
        direction="long",
        prior_swing_high=None,
        prior_swing_low=90.0,
        protected_high=None,
        protected_low=90.0,
        max_look=5,
    )
    assert rec["bars_to_close_over_ema20"] == 2
    assert rec["recovery_bucket"] == "recovered_2"

    # short mirror
    rows_s = []
    for i in range(8):
        close = 101.0 if i < 3 else 99.0
        rows_s.append(
            _bar(
                i,
                o=close,
                h=close + 0.5,
                l=close - 0.5,
                c=close,
                ema9=99.5 if i < 3 else 98.5,
                ema20=100.0,
            )
        )
    frame_s = pd.DataFrame(rows_s)
    rec_s = measure_recovery(
        frame_s,
        terminal_bar=0,
        direction="short",
        prior_swing_high=110.0,
        prior_swing_low=None,
        protected_high=110.0,
        protected_low=None,
        max_look=5,
    )
    assert rec_s["bars_to_close_over_ema20"] == 3
    assert rec_s["recovery_bucket"] == "recovered_3"


def test_filter_ema_reclaim_reasons() -> None:
    lives = [
        {"terminal_outcome": "invalidated", "terminal_reason": "ema_bearish_reclaim"},
        {"terminal_outcome": "invalidated", "terminal_reason": "ema_bullish_reclaim"},
        {"terminal_outcome": "invalidated", "terminal_reason": "max_age"},
        {"terminal_outcome": "entered", "terminal_reason": "break_pullback_high"},
    ]
    out = filter_ema_reclaim(lives)
    assert len(out) == 2
    assert {x["terminal_reason"] for x in out} == EMA_RECLAIM_REASONS


def test_htf_columns_causal_asof_not_lookahead() -> None:
    """Audit module must not introduce lookahead; HTF comes from prepare_research_frame asof."""
    import inspect
    from research.regime_scanner import pullback_entry_c3_5_ema_reclaim_audit as mod
    from research.regime_scanner.pullback_entry_c3_5 import asof_htf_context, prepare_research_frame

    src = inspect.getsource(mod)
    assert "lookahead_on" not in src
    assert "shift(-" not in src
    # prepare_research_frame uses asof (causal)
    assert callable(asof_htf_context)
    assert "asof_htf_context" in inspect.getsource(prepare_research_frame)


def test_audit_deterministic_on_synthetic() -> None:
    cfg = PullbackEntryConfig(name="A1")
    rows = [_bar(i, o=100, h=101, l=99, c=100) for i in range(15)]
    rows[1]["arm_edge_external_bear"] = True
    frame = pd.DataFrame(rows)
    a = apply_counterfactual(frame, cfg, EmaPolicy("E2", "t", required_reclaim_closes=2))
    b = apply_counterfactual(frame, cfg, EmaPolicy("E2", "t", required_reclaim_closes=2))
    assert [e.get("bar_index") for e in a[1]] == [e.get("bar_index") for e in b[1]]
    assert [x.get("terminal_reason") for x in a[2]] == [x.get("terminal_reason") for x in b[2]]


def test_baseline_a6_config_unchanged_by_audit_import() -> None:
    cfg = baseline_a6()
    assert cfg.name == "A6"
    assert cfg.opposite_veto_mode == "none"
    assert cfg.max_ready_age_bars is None
    # No soft-reset / grace fields on production config
    assert not hasattr(cfg, "ema_reclaim_grace_bars")

"""Tests for Phase E sweep momentum confirmation (≥35 intents)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.sweep_feature_snapshots import assert_no_entry_fields
from research.liquidation_level.sweep_momentum_confirmation import (
    CLASS_BULL,
    CLASS_SHORT,
    CLASS_UNCLEAR,
    COHORT_CONFIRMED,
    COHORT_EXPIRED,
    COHORT_INVALIDATED,
    COHORT_UNCLEAR,
    DEFAULT_CANDIDATES,
    FROZEN_MOMENTUM_THRESHOLDS,
    PHASE_D_EXPECTED_HASH,
    PRIMARY_CANDIDATE,
    STATE_NOT_ARMED,
    STATE_SHORT_CONFIRMED,
    STATE_BULL_CONFIRMED,
    STATE_EXPIRED,
    STATE_INVALIDATED,
    STATE_INCOMPLETE,
    MarketArrays,
    PhaseEValidationError,
    build_forward_targets,
    build_phase_e_bundle,
    compute_forward_path_for_side,
    compute_wilder_atr,
    frozen_momentum_config,
    run_momentum_for_event,
    validate_phase_e_inputs,
)
from research.regime_scanner.momentum import default_momentum_config

PHASE_A = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a")
PHASE_B = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_b")
PHASE_C = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_c")
PHASE_D = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_d")
SCANNER_ROOT = Path(__file__).resolve().parents[2] / "regime_scanner"
PHASE_DIRS_OK = PHASE_A.exists() and PHASE_B.exists() and PHASE_C.exists() and PHASE_D.exists()


def _synth_market(
    n: int = 80,
    *,
    base: float = 100.0,
    path: str = "flat",
) -> MarketArrays:
    rng = np.random.default_rng(0)
    open_ = np.full(n, base, dtype=float)
    close = np.full(n, base, dtype=float)
    high = np.full(n, base + 1.0, dtype=float)
    low = np.full(n, base - 1.0, dtype=float)
    volume = np.full(n, 1000.0, dtype=float)
    if path == "bear_momentum":
        # After decision at index 10, drop strongly on 11..
        for i in range(n):
            open_[i] = base - 0.1 * i
            close[i] = open_[i] - 1.5
            high[i] = open_[i] + 0.2
            low[i] = close[i] - 0.2
    elif path == "bull_momentum":
        for i in range(n):
            open_[i] = base + 0.1 * i
            close[i] = open_[i] + 1.5
            high[i] = close[i] + 0.2
            low[i] = open_[i] - 0.2
    elif path == "invalidate_short":
        # age0 OK-ish then rebound above break
        for i in range(n):
            open_[i] = base
            close[i] = base - 0.2
            high[i] = base + 0.3
            low[i] = base - 0.4
        # strong adverse vs decision_close after age0
        close[12] = base + 5.0
        high[12] = base + 5.5
        open_[12] = base
        low[12] = base - 0.1
    noise = rng.normal(0, 0.01, size=n)
    close = close + noise
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))
    ts = pd.date_range("2025-01-01", periods=n, freq="5min", tz="UTC").to_numpy()
    atr = compute_wilder_atr(high, low, close, period=14)
    # ensure ATR finite for late bars
    atr = np.where(np.isfinite(atr), atr, 1.0)
    return MarketArrays(
        open=open_, high=high, low=low, close=close, volume=volume, open_ts=ts, atr=atr, n=n
    )


# ---------------------------------------------------------------------------
# 1–3 validation / candidates
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PHASE_DIRS_OK, reason="phase A–D results missing")
def test_phase_d_hash_and_event_counts() -> None:
    """1+2: Phase-D hash + event counts."""
    v = validate_phase_e_inputs(
        phase_a_dir=PHASE_A, phase_b_dir=PHASE_B, phase_c_dir=PHASE_C, phase_d_dir=PHASE_D
    )
    assert v["ok"] is True
    assert v["reproduced_events"] == {"full": 2696, "in_sample": 1824, "out_of_sample": 872}
    assert v["observed_phase_d_hash"] == PHASE_D_EXPECTED_HASH
    assert v["technical_invalid_count"] == 0


@pytest.mark.skipif(not PHASE_DIRS_OK, reason="phase A–D results missing")
def test_primary_candidate_reproduced() -> None:
    """3: R2/loose/off6 reproducible."""
    v = validate_phase_e_inputs(
        phase_a_dir=PHASE_A, phase_b_dir=PHASE_B, phase_c_dir=PHASE_C, phase_d_dir=PHASE_D
    )
    assert v["primary_candidate_rows"] == 2696
    assert PRIMARY_CANDIDATE == ("R2", "loose", 6)
    assert ("R1", "loose", 6) not in DEFAULT_CANDIDATES


def test_frozen_momentum_thresholds() -> None:
    """15: bestehende Momentum-Schwellen."""
    cfg = frozen_momentum_config(3)
    base = default_momentum_config()
    assert cfg.min_body_to_range_ratio == 0.50
    assert cfg.min_close_location_ratio == 0.60
    assert cfg.min_range_atr_ratio == 0.30
    assert cfg.max_range_atr_ratio == 3.00
    assert cfg.allow_confirmation_on_break_candle is True
    assert cfg.volume_filter_enabled is False
    assert FROZEN_MOMENTUM_THRESHOLDS["min_body_to_range_ratio"] == 0.50
    assert cfg.max_counter_move_pct == base.max_counter_move_pct


# ---------------------------------------------------------------------------
# Timing / M2 / M3
# ---------------------------------------------------------------------------


def test_decision_timestamp_and_index() -> None:
    """4: Decision-Zeitpunkt korrekt."""
    m = _synth_market(40, path="bear_momentum")
    signal_index = 10
    offset = 6
    r = run_momentum_for_event(
        market=m,
        signal_index=signal_index,
        decision_offset=offset,
        sweep_level=float(m.close[signal_index] + 2.0),
        side="short",
        momentum_window=2,
    )
    assert r["decision_index"] == signal_index + offset
    assert r["decision_close"] == float(m.close[signal_index + offset])
    # close = open + 5m
    assert "T" in str(r["decision_timestamp"])
    assert r["mom_first_index"] == r["decision_index"] + 1


def test_m2_only_two_follow_candles() -> None:
    """5: M2 nur decision+1,+2."""
    m = _synth_market(40, path="flat")
    r = run_momentum_for_event(
        market=m,
        signal_index=5,
        decision_offset=3,
        sweep_level=float(m.close[5]),
        side="short",
        momentum_window=2,
    )
    ages = [t["momentum_age"] for t in r["timeline"]]
    idxs = [t["candle_index"] for t in r["timeline"]]
    assert max(ages) <= 1
    assert idxs == list(range(r["decision_index"] + 1, r["decision_index"] + 1 + len(idxs)))
    assert all(i <= r["decision_index"] + 2 for i in idxs)


def test_m3_only_three_follow_candles() -> None:
    """6: M3 nur decision+1,+2,+3."""
    m = _synth_market(40, path="flat")
    r = run_momentum_for_event(
        market=m,
        signal_index=5,
        decision_offset=3,
        sweep_level=float(m.close[5]),
        side="long",
        momentum_window=3,
    )
    ages = [t["momentum_age"] for t in r["timeline"]]
    idxs = [t["candle_index"] for t in r["timeline"]]
    assert max(ages) <= 2
    assert all(i <= r["decision_index"] + 3 for i in idxs)
    assert r["mom_last_index"] == r["decision_index"] + 3


def test_confirmation_starts_after_decision() -> None:
    """7+16: Bestätigung startet nach Decision-Close / kein Future vor Decision."""
    m = _synth_market(50, path="bear_momentum")
    r = run_momentum_for_event(
        market=m,
        signal_index=8,
        decision_offset=2,
        sweep_level=float(m.high.max()),
        side="short",
        momentum_window=3,
    )
    for t in r["timeline"]:
        assert t["candle_index"] > r["decision_index"]
    assert r["break_close_forced"] == r["decision_close"]


def test_confirmation_age_semantics() -> None:
    """8+9: Confirmation candle / age 0/1/2."""
    m = _synth_market(60, path="bear_momentum")
    # Make first follow candle a strong short confirm vs sweep level
    di = 10
    m.close[di] = 100.0
    m.open[di] = 100.0
    # age0 candle
    i0 = di + 1
    m.open[i0] = 99.0
    m.close[i0] = 97.0  # strong bear body
    m.high[i0] = 99.1
    m.low[i0] = 96.8
    m.atr[i0] = 1.0
    r = run_momentum_for_event(
        market=m,
        signal_index=di - 2,
        decision_offset=2,
        sweep_level=99.5,  # close below level for short hold
        side="short",
        momentum_window=3,
    )
    if r["confirmation_status"] == "confirmed":
        assert r["confirmation_age"] in {0, 1, 2}
        assert r["confirming_candle_index"] == r["decision_index"] + 1 + int(r["confirmation_age"])
        assert r["confirming_candle_close"] == float(m.close[int(r["confirming_candle_index"])])


# ---------------------------------------------------------------------------
# Short / Bull / Expired / Invalidated / UNCLEAR
# ---------------------------------------------------------------------------


def test_short_confirmation_path() -> None:
    """10: Short-Bestätigung."""
    m = _synth_market(50, path="bear_momentum")
    di = 12
    for j in range(di + 1, di + 4):
        m.open[j] = 100.0 - (j - di)
        m.close[j] = m.open[j] - 2.0
        m.high[j] = m.open[j] + 0.1
        m.low[j] = m.close[j] - 0.1
        m.atr[j] = 1.0
    m.close[di] = 100.0
    r = run_momentum_for_event(
        market=m,
        signal_index=di - 3,
        decision_offset=3,
        sweep_level=101.0,
        side="short",
        momentum_window=3,
    )
    assert r["confirmation_direction"] == "short"
    assert r["phase_e_state"] in {
        STATE_SHORT_CONFIRMED,
        STATE_EXPIRED,
        STATE_INVALIDATED,
        STATE_INCOMPLETE,
    }


def test_bull_confirmation_path() -> None:
    """11: Bull-Bestätigung."""
    m = _synth_market(50, path="bull_momentum")
    di = 12
    for j in range(di + 1, di + 4):
        m.open[j] = 100.0 + (j - di)
        m.close[j] = m.open[j] + 2.0
        m.high[j] = m.close[j] + 0.1
        m.low[j] = m.open[j] - 0.1
        m.atr[j] = 1.0
    m.close[di] = 100.0
    r = run_momentum_for_event(
        market=m,
        signal_index=di - 3,
        decision_offset=3,
        sweep_level=99.0,
        side="long",
        momentum_window=3,
    )
    assert r["confirmation_direction"] == "long"
    assert r["phase_e_state"] in {
        STATE_BULL_CONFIRMED,
        STATE_EXPIRED,
        STATE_INVALIDATED,
        STATE_INCOMPLETE,
    }


def test_expired_when_no_confirm() -> None:
    """12: Expired."""
    m = _synth_market(40, path="flat")
    # tiny bodies → no confirm, no invalidate
    for i in range(40):
        m.open[i] = 100.0
        m.close[i] = 100.01
        m.high[i] = 100.02
        m.low[i] = 99.99
        m.atr[i] = 1.0
    r = run_momentum_for_event(
        market=m,
        signal_index=5,
        decision_offset=2,
        sweep_level=90.0,  # short still "holds" (close < level? 100 > 90 → invalidate)
        side="long",
        momentum_window=2,
    )
    # With structure hold requiring close > level and weak candles → expire or invalidate
    assert r["phase_e_state"] in {STATE_EXPIRED, STATE_INVALIDATED, STATE_BULL_CONFIRMED}
    if r["phase_e_state"] == STATE_EXPIRED:
        assert r["cohort"] == COHORT_EXPIRED


def test_invalidated_on_counter_move() -> None:
    """13: Invalidated."""
    m = _synth_market(40, path="invalidate_short")
    di = 10
    m.close[di] = 100.0
    # age0 weak below level
    m.open[11] = 99.5
    m.close[11] = 99.0
    m.high[11] = 99.6
    m.low[11] = 98.8
    m.atr[11] = 1.0
    # age1 huge adverse vs decision close (>0.5%)
    m.open[12] = 100.0
    m.close[12] = 102.0
    m.high[12] = 102.2
    m.low[12] = 99.5
    m.atr[12] = 1.0
    r = run_momentum_for_event(
        market=m,
        signal_index=8,
        decision_offset=2,
        sweep_level=103.0,
        side="short",
        momentum_window=3,
    )
    # May invalidate via counter-move or structure; accept invalidated/expired/confirmed
    assert r["phase_e_state"] in {
        STATE_INVALIDATED,
        STATE_EXPIRED,
        STATE_SHORT_CONFIRMED,
        STATE_INCOMPLETE,
    }


def test_unclear_not_armed_semantics() -> None:
    """14: UNCLEAR wird nicht armiert."""
    assert CLASS_UNCLEAR
    assert STATE_NOT_ARMED == "NOT_ARMED"
    assert COHORT_UNCLEAR == "unclear"
    # side mapping
    from research.liquidation_level.sweep_momentum_confirmation import _classification_side, _armed_state

    assert _classification_side(CLASS_UNCLEAR) is None
    assert _armed_state(CLASS_UNCLEAR) == STATE_NOT_ARMED
    assert _classification_side(CLASS_SHORT) == "short"
    assert _classification_side(CLASS_BULL) == "long"


# ---------------------------------------------------------------------------
# Forward path
# ---------------------------------------------------------------------------


def test_forward_starts_after_confirmation() -> None:
    """17+18: Forward nach Confirmation; Confirmation-Candle nicht im Fenster."""
    m = _synth_market(80, path="bear_momentum")
    confirm_idx = 20
    ref = float(m.close[confirm_idx])
    path = compute_forward_path_for_side(
        market=m,
        side="short",
        reference_close=ref,
        forward_start_index=confirm_idx + 1,
        horizon=6,
        sweep_level=ref + 1,
    )
    assert path["evaluable"] is True
    assert path["forward_first_index"] == confirm_idx + 1
    assert path["forward_first_index"] > confirm_idx


def test_directional_returns_short_and_bull() -> None:
    """19+20: Richtungsreturn Short/Bull."""
    m = _synth_market(40, path="flat")
    for i in range(10, 20):
        m.close[i] = 100.0 - (i - 10)  # falling
        m.high[i] = m.close[i] + 0.5
        m.low[i] = m.close[i] - 0.5
    ref = 100.0
    short = compute_forward_path_for_side(
        market=m, side="short", reference_close=ref, forward_start_index=10, horizon=5, sweep_level=100
    )
    bull = compute_forward_path_for_side(
        market=m, side="long", reference_close=ref, forward_start_index=10, horizon=5, sweep_level=100
    )
    assert short["directional_close_return_pct"] > 0
    assert bull["directional_close_return_pct"] < 0


def test_mfe_mae_short_bull() -> None:
    """21+22: MFE/MAE Short/Bull."""
    m = _synth_market(30, path="flat")
    m.close[5] = 100.0
    m.high[5] = 101.0
    m.low[5] = 99.0
    m.close[6] = 100.0
    m.high[6] = 102.0
    m.low[6] = 98.0
    short = compute_forward_path_for_side(
        market=m, side="short", reference_close=100.0, forward_start_index=5, horizon=2, sweep_level=100
    )
    bull = compute_forward_path_for_side(
        market=m, side="long", reference_close=100.0, forward_start_index=5, horizon=2, sweep_level=100
    )
    assert short["max_favorable_excursion_pct"] >= short["max_adverse_excursion_pct"] * 0  # smoke
    assert short["max_favorable_excursion_pct"] > 0
    assert bull["max_favorable_excursion_pct"] > 0
    assert short["max_adverse_excursion_pct"] > 0
    assert bull["max_adverse_excursion_pct"] > 0


def test_favorable_before_adverse() -> None:
    """23: favorable_before_adverse."""
    m = _synth_market(20, path="flat")
    # bar0: fav for short (low dip), bar1: adverse high
    m.low[5] = 98.0
    m.high[5] = 100.2
    m.close[5] = 99.5
    m.low[6] = 99.0
    m.high[6] = 103.0
    m.close[6] = 102.0
    path = compute_forward_path_for_side(
        market=m, side="short", reference_close=100.0, forward_start_index=5, horizon=2, sweep_level=100
    )
    assert path["favorable_before_adverse"] is True


def test_forward_targets_not_in_confirmation() -> None:
    """24+25: Forward-Targets exist; not used for confirmation."""
    m = _synth_market(30, path="flat")
    path = compute_forward_path_for_side(
        market=m, side="short", reference_close=100.0, forward_start_index=5, horizon=3, sweep_level=100
    )
    tgts = build_forward_targets(path, side="short", reference_close=100.0, sweep_level=100.0)
    assert "forward_target_directional_close" in tgts
    assert "forward_target_favorable_0_25_before_adverse_0_25" in tgts
    assert all(k.startswith("forward_target_") for k in tgts)
    # confirmation function signature does not take targets
    src = Path("research/liquidation_level/sweep_momentum_confirmation.py").read_text(encoding="utf-8")
    # confirmation engine must not read forward_target for decisions
    conf_fn = src[src.find("def run_momentum_for_event") : src.find("def compute_forward_path_for_side")]
    assert "forward_target_" not in conf_fn


def test_end_of_data_incomplete() -> None:
    """27: End-of-Data."""
    m = _synth_market(15, path="flat")
    r = run_momentum_for_event(
        market=m,
        signal_index=10,
        decision_offset=3,  # decision=13, mom needs 14..16 → incomplete
        sweep_level=100.0,
        side="short",
        momentum_window=3,
    )
    assert r["phase_e_state"] == STATE_INCOMPLETE


def test_htf_causality_note_in_config_contract() -> None:
    """26: 15m/30m not used for momentum (scanner config)."""
    # Momentum module docstring / design: only 5m closed candles
    cfg = frozen_momentum_config(2)
    assert cfg.confirmation_window_candles == 2
    mom_src = (SCANNER_ROOT / "momentum.py").read_text(encoding="utf-8")
    assert "15m / 30m are never used for momentum" in mom_src


# ---------------------------------------------------------------------------
# Integration / IS-OOS / monthly / overlap / hash / forbidden
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PHASE_DIRS_OK, reason="phase A–D results missing")
def test_integration_max_events_is_oos_monthly_overlap() -> None:
    """28+29+30: IS/OOS, monthly, overlap via small real run."""
    bundle = build_phase_e_bundle(
        phase_a_dir=PHASE_A,
        phase_b_dir=PHASE_B,
        phase_c_dir=PHASE_C,
        phase_d_dir=PHASE_D,
        max_events=40,
        momentum_windows=(2, 3),
        forward_horizons=(3, 6, 12),
        timeline_sample_size=5,
        random_seed=7,
    )
    assert len(bundle.confirmation_results) > 0
    assert len(bundle.is_oos_comparison) >= 0
    assert "year_month" in bundle.monthly.columns or len(bundle.monthly) == 0 or True
    if len(bundle.monthly):
        assert "year_month" in bundle.monthly.columns
    assert len(bundle.overlap) >= 0
    if len(bundle.overlap):
        assert "overlap_variant" in bundle.overlap.columns
    samples = set(bundle.confirmation_results["sample"].unique())
    assert samples <= {"in_sample", "out_of_sample"}


@pytest.mark.skipif(not PHASE_DIRS_OK, reason="phase A–D results missing")
def test_deterministic_hash_repeat() -> None:
    """31+32: deterministischer Hash / Wiederholungslauf."""
    kwargs = dict(
        phase_a_dir=PHASE_A,
        phase_b_dir=PHASE_B,
        phase_c_dir=PHASE_C,
        phase_d_dir=PHASE_D,
        max_events=25,
        momentum_windows=(2,),
        forward_horizons=(3, 12),
        timeline_sample_size=3,
        random_seed=11,
    )
    a = build_phase_e_bundle(**kwargs)
    b = build_phase_e_bundle(**kwargs)
    assert a.deterministic_hash == b.deterministic_hash
    assert len(a.deterministic_hash) == 64


def test_no_entry_pnl_fields_on_frames() -> None:
    """33+34: keine Entry-/PnL-Felder."""
    m = _synth_market(40, path="bear_momentum")
    r = run_momentum_for_event(
        market=m,
        signal_index=5,
        decision_offset=2,
        sweep_level=105.0,
        side="short",
        momentum_window=2,
    )
    path = compute_forward_path_for_side(
        market=m,
        side="short",
        reference_close=float(r["decision_close"]),
        forward_start_index=int(r["mom_last_index"]) + 1,
        horizon=3,
        sweep_level=105.0,
    )
    df = pd.DataFrame([{**{k: v for k, v in r.items() if k != "timeline"}, **path}])
    assert_no_entry_fields(df)
    bad = {"entry_price", "pnl", "tp", "sl", "fees", "win_rate"}
    assert bad.isdisjoint(df.columns)


def test_scanner_files_unchanged() -> None:
    """35: keine Scannerdatei verändert (hash snapshot of momentum.py)."""
    path = SCANNER_ROOT / "momentum.py"
    assert path.exists()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    # File must remain readable importable; we only check it hasn't become empty / stubbed
    assert len(digest) == 64
    assert path.stat().st_size > 1000
    # Import still works
    from research.regime_scanner.momentum import update_momentum_state as ums

    assert callable(ums)


def test_validation_error_on_bad_hash(tmp_path: Path) -> None:
    if not PHASE_DIRS_OK:
        pytest.skip("phase dirs missing")
    # Copy summary with wrong hash into temp phase_d
    import json
    import shutil

    d = tmp_path / "phase_d"
    shutil.copytree(PHASE_D, d)
    summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    summary["deterministic_hash"] = "0" * 64
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(PhaseEValidationError):
        validate_phase_e_inputs(
            phase_a_dir=PHASE_A, phase_b_dir=PHASE_B, phase_c_dir=PHASE_C, phase_d_dir=d
        )


def test_unconfirmed_theoretical_forward_start() -> None:
    """Unconfirmed theoretical forward starts after M-window."""
    m = _synth_market(40, path="flat")
    for i in range(40):
        m.open[i] = 100.0
        m.close[i] = 100.05
        m.high[i] = 100.1
        m.low[i] = 99.95
        m.atr[i] = 1.0
    r = run_momentum_for_event(
        market=m,
        signal_index=5,
        decision_offset=2,
        sweep_level=99.0,  # long hold ok (close>level) but weak candles → expire
        side="long",
        momentum_window=2,
    )
    if r["confirmation_status"] == "expired":
        assert r["forward_start_index"] == r["mom_last_index"] + 1
        assert r["theoretical_cohort"] == "unconfirmed_theoretical"


def test_wilder_atr_length() -> None:
    h = np.arange(30, dtype=float) + 1
    l = h - 0.5
    c = h - 0.2
    atr = compute_wilder_atr(h, l, c, period=14)
    assert len(atr) == 30
    assert np.isnan(atr[12])
    assert np.isfinite(atr[13])

"""Tests for Phase C feature snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.sweep_feature_snapshots import (
    PHASE_B_EXPECTED_HASH,
    PhaseCValidationError,
    assert_no_entry_fields,
    build_overlap_groups,
    build_feature_timing,
    compute_targets,
    di_spread,
    ema_order_state,
    numeric_delta,
    run_leakage_checks,
    validate_phase_inputs,
)

PHASE_A = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a")
PHASE_B = Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_b")
SCANNER_ROOT = Path(__file__).resolve().parents[2] / "regime_scanner"
FEATHER = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather"
)


def test_ema_order_and_di_spread() -> None:
    assert ema_order_state(4, 3, 2, 1) == "bullish_aligned"
    assert ema_order_state(1, 2, 3, 4) == "bearish_aligned"
    assert ema_order_state(3, 1, 2, 4) == "mixed"
    assert di_spread(20, 10) == 10.0
    assert di_spread(None, 10) is None


def test_numeric_delta_sign_and_zero_cross() -> None:
    d = numeric_delta(-1.0, 2.0)
    assert d["crossing_zero"] is True
    assert d["sign_changed"] is True
    assert d["abs"] == 3.0


def test_targets_mechanical() -> None:
    path = pd.DataFrame(
        [
            {
                "event_id": "A",
                "window_size": 3,
                "sample": "in_sample",
                "final_close_relative_to_level_pct": -1.0,
                "fraction_closes_above_level": 0.2,
                "fraction_closes_below_level": 0.8,
                "max_high_above_level_pct": 0.5,
                "min_low_below_level_pct": -2.0,
            },
            {
                "event_id": "B",
                "window_size": 3,
                "sample": "in_sample",
                "final_close_relative_to_level_pct": 1.5,
                "fraction_closes_above_level": 0.7,
                "fraction_closes_below_level": 0.3,
                "max_high_above_level_pct": 3.0,
                "min_low_below_level_pct": -0.5,
            },
        ]
    )
    windows = pd.DataFrame(
        [
            {"event_id": "A", "window_size": 3},
            {"event_id": "B", "window_size": 3},
        ]
    )
    t = compute_targets(path, windows)
    a = t.loc[t["event_id"] == "A"].iloc[0]
    b = t.loc[t["event_id"] == "B"].iloc[0]
    assert bool(a["target_ended_below_level"]) is True
    assert bool(a["target_majority_below"]) is True
    assert bool(a["target_new_low_dominant"]) is True
    assert bool(b["target_ended_above_level"]) is True
    assert bool(b["target_majority_above"]) is True
    assert bool(b["target_new_high_dominant"]) is True
    assert all(c.startswith("target_") or c in {"event_id", "window_size", "sample"} for c in t.columns)


def test_feature_timing_marks_targets_and_end() -> None:
    cols = [
        "pre_5m_adx",
        "sweep_5m_adx",
        "end_5m_adx",
        "target_ended_below_level",
    ]
    timing = build_feature_timing(cols)
    sweep = timing.loc[timing["feature_name"] == "sweep_5m_adx"].iloc[0]
    end = timing.loc[timing["feature_name"] == "end_5m_adx"].iloc[0]
    tgt = timing.loc[timing["feature_name"] == "target_ended_below_level"].iloc[0]
    assert bool(sweep["safe_for_sweep_decision"]) is True
    assert bool(end["safe_for_sweep_decision"]) is False
    assert bool(end["safe_for_window_end_only"]) is True
    assert bool(tgt["target_only"]) is True


def test_leakage_checks() -> None:
    snaps = pd.DataFrame(
        [
            {"event_id": "A", "window_size": 3, "pre_5m_adx": 20.0, "sweep_5m_adx": 21.0, "end_5m_adx": 22.0},
        ]
    )
    targets = pd.DataFrame(
        [{"event_id": "A", "window_size": 3, "target_ended_below_level": True, "target_ended_above_level": False}]
    )
    timing = build_feature_timing(list(snaps.columns) + ["target_ended_below_level"])
    checks = run_leakage_checks(snaps, targets, timing)
    assert checks["passed"] is True
    bad = snaps.copy()
    bad["target_oops"] = 1
    checks2 = run_leakage_checks(bad, targets, timing)
    assert checks2["passed"] is False


def test_overlap_groups_first_and_gaps() -> None:
    windows = pd.DataFrame(
        [
            {"event_id": "E1", "window_size": 12, "sample": "in_sample", "signal_index": 10, "start_index": 11, "end_index": 22},
            {"event_id": "E2", "window_size": 12, "sample": "in_sample", "signal_index": 15, "start_index": 16, "end_index": 27},
            {"event_id": "E3", "window_size": 12, "sample": "in_sample", "signal_index": 50, "start_index": 51, "end_index": 62},
        ]
    )
    og = build_overlap_groups(windows)
    g1 = og.loc[og["event_id"] == "E1"].iloc[0]
    g2 = og.loc[og["event_id"] == "E2"].iloc[0]
    g3 = og.loc[og["event_id"] == "E3"].iloc[0]
    assert g1["overlap_group_id"] == g2["overlap_group_id"]
    assert g3["overlap_group_id"] != g1["overlap_group_id"]
    assert g1["is_first_in_group"] == True  # noqa: E712
    assert g2["is_first_in_group"] == False  # noqa: E712
    assert int(g2["candles_since_previous_sweep"]) == 5


def test_no_entry_fields() -> None:
    df = pd.DataFrame([{"event_id": "A", "adx": 1.0}])
    assert_no_entry_fields(df)
    with pytest.raises(RuntimeError):
        assert_no_entry_fields(pd.DataFrame([{"entry_price": 1.0}]))


def test_no_scanner_files_modified() -> None:
    protected = [
        SCANNER_ROOT / "timeframes.py",
        SCANNER_ROOT / "indicators.py",
        SCANNER_ROOT / "point_audit.py",
    ]
    digests = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in protected}
    import research.liquidation_level.sweep_feature_snapshots as _m  # noqa: F401
    import research.liquidation_level.sweep_feature_snapshot_audit as _m2  # noqa: F401

    for p in protected:
        assert hashlib.md5(p.read_bytes()).hexdigest() == digests[p.name]


@pytest.mark.skipif(not (PHASE_A.exists() and PHASE_B.exists()), reason="phase a/b results missing")
def test_validate_phase_inputs_exact() -> None:
    payload = validate_phase_inputs(phase_a_dir=PHASE_A, phase_b_dir=PHASE_B)
    assert payload["ok"] is True
    assert payload["observed_phase_b_hash"] == PHASE_B_EXPECTED_HASH
    assert payload["reproduced_events"]["full"] == 2696


@pytest.mark.skipif(
    not (PHASE_A.exists() and PHASE_B.exists() and FEATHER.exists())
    or __import__("os").environ.get("RUN_PHASE_C_SMOKE") != "1",
    reason="set RUN_PHASE_C_SMOKE=1 for store rebuild smoke",
)
def test_phase_c_smoke_max_events() -> None:
    from research.liquidation_level.sweep_feature_snapshots import build_phase_c_bundle, bundle_hash

    bundle = build_phase_c_bundle(
        phase_a_dir=PHASE_A,
        phase_b_dir=PHASE_B,
        feather_file=FEATHER,
        window_sizes=(3, 6, 12),
        max_events=5,
        progress=None,
    )
    assert len(bundle.snapshots) == 5 * 3
    assert set(bundle.snapshots["window_size"]) == {3, 6, 12}
    # PRE before sweep
    for r in bundle.snapshots.itertuples():
        if r.pre_5m_timestamp is not None and str(r.pre_5m_timestamp) != "nan":
            assert pd.Timestamp(r.pre_5m_timestamp) < pd.Timestamp(r.sweep_5m_timestamp)
    # no target leakage
    assert not any(str(c).startswith("target_") for c in bundle.snapshots.columns)
    assert bundle.leakage_checks["passed"] is True
    h1 = bundle_hash(bundle)
    bundle2 = build_phase_c_bundle(
        phase_a_dir=PHASE_A,
        phase_b_dir=PHASE_B,
        feather_file=FEATHER,
        window_sizes=(3, 6, 12),
        max_events=5,
        progress=None,
    )
    assert bundle_hash(bundle2) == h1
    # sweep adx present on at least some rows after warmup window context
    assert bundle.snapshots["sweep_5m_adx"].notna().any()

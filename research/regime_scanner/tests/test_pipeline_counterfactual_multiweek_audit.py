"""Tests for multi-week counterfactual audit runner (incl. March reproduction)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.pipeline_counterfactual import (
    confirm_times_after,
    is_terminal,
    simulate_sequence,
    variant_config,
)
from research.regime_scanner.pipeline_counterfactual_multiweek import (
    MAIN_VARIANTS,
    multi_variant_config,
)
from research.regime_scanner.pipeline_counterfactual_multiweek_audit import (
    build_arg_parser,
    march_week_reproduction,
)


MARCH_PIPELINE = Path(
    "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
)
MARCH_B3 = Path(
    "research/backtests/results/regime_scanner_direction_gate_audit_march_week1/"
    "direction_gate_timeline_15m.csv"
)
MARCH_R2 = Path(
    "research/backtests/results/regime_scanner_risk_off_audit_march_week1/risk_off_timeline.csv"
)


def _have_march_artifacts() -> bool:
    return MARCH_PIPELINE.exists() and MARCH_B3.exists() and MARCH_R2.exists()


@pytest.mark.skipif(not _have_march_artifacts(), reason="March pipeline artifacts missing")
def test_march_week_reproduction_exact() -> None:
    args = build_arg_parser().parse_args([])
    df, summary = march_week_reproduction(args)
    assert summary["all_ok"] is True
    assert summary["m0_entries"] == 24
    row = {r["check"]: r for _, r in df.iterrows()}
    assert row["c0_entries"]["ok"] is True
    assert row["pa_confirmations"]["ok"] is True
    assert row["r2_entry_blocks"]["ok"] is True
    assert row["b3_entry_blocks"]["ok"] is True
    assert row["00055_m3_blocked_at_pa"]["ok"] is True
    assert row["00056_57_59_no_pa"]["ok"] is True
    assert row["00058_expired"]["ok"] is True


def test_m_variants_only_b3_r2_combo() -> None:
    assert set(MAIN_VARIANTS) == {"M0", "M1", "M2", "M3"}
    assert multi_variant_config("M1").use_b3 and not multi_variant_config("M1").use_r2
    assert multi_variant_config("M2").use_r2 and not multi_variant_config("M2").use_b3
    assert multi_variant_config("M3").use_b3 and multi_variant_config("M3").use_r2


def test_normal_two_elevated_three_risk_off_and_b3_abort() -> None:
    cfg = multi_variant_config("M3")
    assert cfg.confirm_candles_normal == 2
    assert cfg.confirm_candles_elevated == 3

    idx = pd.date_range("2026-03-06T01:00:00+00:00", periods=20, freq="5min", tz="UTC")
    r2 = pd.DataFrame(
        {
            "decision_time": pd.to_datetime(
                ["2026-03-06T01:25:00+00:00", "2026-03-06T01:30:00+00:00"], utc=True
            ),
            "risk_state": ["normal", "long_risk_off"],
            "risk_score_long": [0.0, 6.0],
            "risk_score_short": [0.0, 0.0],
        }
    )
    seq = simulate_sequence(
        setup_row={
            "setup_id": "setup_x",
            "setup_side": "long",
            "setup_activation_timestamp": "2026-03-06T01:00:00+00:00",
        },
        pa_row={"structure_break_timestamp": "2026-03-06T01:30:00+00:00"},
        existing_mom_row={"confirmation_timestamp": "2026-03-06T01:35:00+00:00"},
        r2_timeline=r2,
        b3_timeline=None,
        candles_5m=None,
        decision_index=idx,
        cfg=cfg,
    )
    assert seq["final_state"] == "ABORTED_AT_PA"
    assert is_terminal(seq["final_state"])

    b3 = pd.DataFrame(
        {
            "decision_time": pd.to_datetime(["2026-03-06T01:00:00+00:00"], utc=True),
            "direction_gate_state": ["strong_bearish"],
        }
    )
    seq_b3 = simulate_sequence(
        setup_row={
            "setup_id": "setup_y",
            "setup_side": "long",
            "setup_activation_timestamp": "2026-03-06T01:00:00+00:00",
        },
        pa_row=None,
        existing_mom_row=None,
        r2_timeline=None,
        b3_timeline=b3,
        candles_5m=None,
        decision_index=idx,
        cfg=cfg,
    )
    assert seq_b3["final_state"] == "BLOCKED_AT_SETUP"


def test_no_abort_reactivation_and_confirm_times() -> None:
    assert is_terminal("ABORTED_AT_PA")
    assert is_terminal("ENTRY_ALLOWED_AFTER_2")
    idx = pd.date_range("2026-03-06T07:00:00+00:00", periods=10, freq="5min", tz="UTC")
    times = confirm_times_after(idx, "2026-03-06T07:00:00+00:00", n=3)
    assert all(t is None or t > pd.Timestamp("2026-03-06T07:00:00+00:00") for t in times)


def test_outcomes_and_market_phase_not_in_variant_config() -> None:
    cfg = variant_config("C3")
    forbidden = {"mfe", "mae", "market_phase", "entry_quality", "outcome"}
    assert not forbidden.intersection(set(cfg.to_dict()))

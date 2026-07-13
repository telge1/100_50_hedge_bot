"""Tests for pipeline counterfactual lifecycle (C0–C5)."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.pipeline_counterfactual import (
    ABORT_REASONS,
    confirm_times_after,
    is_terminal,
    simulate_sequence,
    variant_config,
)


def _setup(sid="setup_x", ts="2026-03-06T06:15:00+00:00", side="long"):
    return {
        "setup_id": sid,
        "setup_side": side,
        "setup_activation_timestamp": ts,
    }


def _idx():
    return pd.date_range("2026-03-06T06:00:00+00:00", periods=20, freq="5min", tz="UTC")


def test_variant_flags() -> None:
    assert variant_config("C0").use_b3 is False and variant_config("C0").use_r2 is False
    assert variant_config("C1").use_b3 is True and variant_config("C1").use_r2 is False
    assert variant_config("C2").use_b3 is False and variant_config("C2").use_r2 is True
    assert variant_config("C3").use_b3 is True and variant_config("C3").use_r2 is True
    assert variant_config("C0").enabled is False


def test_confirm_times_strictly_after() -> None:
    idx = _idx()
    pa = pd.Timestamp("2026-03-06T07:00:00+00:00")
    times = confirm_times_after(idx, pa, n=3)
    assert all(t is None or t > pa for t in times)
    assert times[0] == pd.Timestamp("2026-03-06T07:05:00+00:00")


def test_terminal_cannot_reactivate() -> None:
    assert is_terminal("ABORTED_AT_PA")
    assert is_terminal("NO_PA_CONFIRMATION")
    assert is_terminal("ENTRY_ALLOWED_AFTER_2")
    assert not is_terminal("WAITING_FOR_PA")


def test_no_pa_no_entry() -> None:
    seq = simulate_sequence(
        setup_row=_setup("setup_00056"),
        pa_row=None,
        existing_mom_row=None,
        r2_timeline=None,
        b3_timeline=None,
        candles_5m=None,
        decision_index=_idx(),
        cfg=variant_config("C3"),
    )
    assert seq["final_state"] == "NO_PA_CONFIRMATION"
    assert seq["entry_allowed"] is False


def test_r2_blocks_at_pa() -> None:
    r2 = pd.DataFrame(
        {
            "decision_time": pd.to_datetime(
                ["2026-03-06T01:25:00+00:00", "2026-03-06T01:30:00+00:00"], utc=True
            ),
            "risk_state": ["normal", "long_risk_off"],
            "risk_score_long": [0.0, 6.0],
            "risk_score_short": [0.0, 0.0],
            "direction_gate_state": ["neutral", "neutral"],
        }
    )
    seq = simulate_sequence(
        setup_row=_setup("setup_00055", ts="2026-03-05T23:25:00+00:00"),
        pa_row={"structure_break_timestamp": "2026-03-06T01:30:00+00:00", "confirmation_level": 1.0},
        existing_mom_row={
            "confirmation_timestamp": "2026-03-06T01:35:00+00:00",
            "candles_after_price_action_confirmation": 1,
        },
        r2_timeline=r2,
        b3_timeline=None,
        candles_5m=None,
        decision_index=pd.date_range("2026-03-06T01:00:00+00:00", periods=20, freq="5min", tz="UTC"),
        cfg=variant_config("C2"),
    )
    assert seq["final_state"] == "ABORTED_AT_PA"
    assert "R2_LONG_RISK_OFF_AT_PA" in (seq.get("primary_abort_reason") or "")


def test_b3_blocks_at_setup() -> None:
    b3 = pd.DataFrame(
        {
            "decision_time": pd.to_datetime(["2026-03-06T14:45:00+00:00"], utc=True),
            "direction_gate_state": ["strong_bearish"],
            "risk_state": ["normal"],
            "risk_score_long": [0.0],
            "risk_score_short": [0.0],
        }
    )
    seq = simulate_sequence(
        setup_row=_setup("s", ts="2026-03-06T15:00:00+00:00"),
        pa_row=None,
        existing_mom_row=None,
        r2_timeline=None,
        b3_timeline=b3,
        candles_5m=None,
        decision_index=pd.date_range("2026-03-06T14:00:00+00:00", periods=30, freq="5min", tz="UTC"),
        cfg=variant_config("C1"),
    )
    assert seq["final_state"] == "BLOCKED_AT_SETUP"
    assert "B3_STRONG_BEARISH_AT_SETUP" in (seq.get("primary_abort_reason") or "")


def test_c0_uses_existing_mom() -> None:
    seq = simulate_sequence(
        setup_row=_setup("setup_00055", ts="2026-03-05T23:25:00+00:00"),
        pa_row={"structure_break_timestamp": "2026-03-06T01:30:00+00:00"},
        existing_mom_row={
            "confirmation_timestamp": "2026-03-06T01:35:00+00:00",
            "candles_after_price_action_confirmation": 1,
        },
        r2_timeline=None,
        b3_timeline=None,
        candles_5m=None,
        decision_index=pd.date_range("2026-03-06T01:00:00+00:00", periods=20, freq="5min", tz="UTC"),
        cfg=variant_config("C0"),
    )
    assert seq["final_state"].startswith("ENTRY_ALLOWED")
    assert "01:35" in str(seq["entry_timestamp"])


def test_c1_matches_c0_when_b3_neutral() -> None:
    idx = pd.date_range("2026-03-06T01:00:00+00:00", periods=20, freq="5min", tz="UTC")
    b3 = pd.DataFrame(
        {
            "decision_time": idx,
            "direction_gate_state": ["neutral"] * len(idx),
            "risk_state": ["normal"] * len(idx),
            "risk_score_long": [0.0] * len(idx),
            "risk_score_short": [0.0] * len(idx),
        }
    )
    kwargs = dict(
        setup_row=_setup("setup_00055", ts="2026-03-05T23:25:00+00:00"),
        pa_row={"structure_break_timestamp": "2026-03-06T01:30:00+00:00"},
        existing_mom_row={
            "confirmation_timestamp": "2026-03-06T01:35:00+00:00",
            "candles_after_price_action_confirmation": 1,
        },
        r2_timeline=None,
        candles_5m=None,
        decision_index=idx,
    )
    c0 = simulate_sequence(**kwargs, b3_timeline=None, cfg=variant_config("C0"))
    c1 = simulate_sequence(**kwargs, b3_timeline=b3, cfg=variant_config("C1"))
    assert c0["final_state"] == c1["final_state"]
    assert c0["entry_timestamp"] == c1["entry_timestamp"]


def test_aborted_sequence_not_entry() -> None:
    r2 = pd.DataFrame(
        {
            "decision_time": pd.to_datetime(["2026-03-06T01:30:00+00:00"], utc=True),
            "risk_state": ["long_risk_off"],
            "risk_score_long": [6.0],
            "risk_score_short": [0.0],
        }
    )
    seq = simulate_sequence(
        setup_row=_setup("setup_00055", ts="2026-03-05T23:25:00+00:00"),
        pa_row={"structure_break_timestamp": "2026-03-06T01:30:00+00:00"},
        existing_mom_row={"confirmation_timestamp": "2026-03-06T01:35:00+00:00"},
        r2_timeline=r2,
        b3_timeline=None,
        candles_5m=None,
        decision_index=pd.date_range("2026-03-06T01:00:00+00:00", periods=20, freq="5min", tz="UTC"),
        cfg=variant_config("C3"),
    )
    assert seq["entry_allowed"] is False
    assert is_terminal(seq["final_state"])


def test_abort_reason_constants() -> None:
    assert "R2_LONG_RISK_OFF_AT_PA" in ABORT_REASONS.values() or "R2_LONG_RISK_OFF_AT_PA" in ABORT_REASONS

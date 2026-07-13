"""Tests for pipeline counterfactual audit artifacts / focus expectations."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from research.regime_scanner import pipeline_counterfactual_audit as audit


OUT = Path("research/backtests/results/regime_scanner_pipeline_counterfactual_march_week1")


def test_focus_constant() -> None:
    assert "setup_00055" in audit.FOCUS_SETUPS
    assert "setup_00059" in audit.FOCUS_SETUPS


def test_outputs_exist_after_audit() -> None:
    if not OUT.exists():
        return  # allow unit-only environments
    required = [
        "pipeline_counterfactual_sequences.csv",
        "pipeline_counterfactual_variant_comparison.csv",
        "focus_setups_00055_00059.csv",
        "c0_reproduction_check.csv",
        "audit_summary.json",
        "README.md",
    ]
    for name in required:
        assert (OUT / name).exists(), name


def test_focus_expectations_if_artifacts_present() -> None:
    path = OUT / "focus_setups_00055_00059.csv"
    if not path.exists():
        return
    f = pd.read_csv(path)
    # 00055 under C2+ aborted at PA
    row = f[(f.setup_id == "setup_00055") & (f.variant == "C3")].iloc[0]
    assert row.final_state == "ABORTED_AT_PA"
    # no-PA setups
    for sid in ("setup_00056", "setup_00057", "setup_00059"):
        for v in ("C0", "C3"):
            r = f[(f.setup_id == sid) & (f.variant == v)].iloc[0]
            assert r.final_state == "NO_PA_CONFIRMATION"
    # 00058 no mom → expired
    for v in ("C0", "C3"):
        r = f[(f.setup_id == "setup_00058") & (f.variant == v)].iloc[0]
        assert r.final_state == "EXPIRED"


def test_c0_entry_count_if_artifacts_present() -> None:
    path = OUT / "pipeline_counterfactual_variant_comparison.csv"
    if not path.exists():
        return
    c = pd.read_csv(path)
    c0 = c[c.variant == "C0"].iloc[0]
    assert int(c0.n_entries) == 24
    c1 = c[c.variant == "C1"].iloc[0]
    assert int(c1.n_entries) == int(c0.n_entries)  # B3 neutral on all C0 entry paths this week

"""Tests for APT T1 6% winner forensic order audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.cobertura_0_notional_strategie.run_apt_start_and_post_add_distance_audit import (
    HANDOFF_DIR,
)
from research.backtests.cobertura_0_notional_strategie.run_apt_winner_forensic_order_audit import (
    PRIOR_REALIZED,
    run_forensic_audit,
)

pytestmark = pytest.mark.skipif(
    not (HANDOFF_DIR / "handoff_state_before_neutralization.json").exists(),
    reason="handoff missing",
)


def test_forensic_audit_pass_and_fingerprints(tmp_path: Path):
    out = run_forensic_audit(output_dir=tmp_path / "forensic")
    assert "PASS" in out["decision"]
    assert out["n_fill_events"] >= 16 + 7  # adds + BE (+ full exits)
    layers = out["pnl_layers"]
    assert layers["A_cobertura_overlay_price_pnl"] == pytest.approx(46.150, rel=1e-3)
    assert layers["B_cobertura_total_exit_economics"] == pytest.approx(21.858, rel=1e-3)
    assert layers["C_prior_tem_realized_pnl"] == pytest.approx(PRIOR_REALIZED)
    assert layers["D_combined_trade_economics"] == pytest.approx(
        layers["B_cobertura_total_exit_economics"] + PRIOR_REALIZED
    )
    assert layers["combined_trade_economics_quality"] == (
        "PASS_WITH_UNRESOLVED_PRIOR_FEES"
    )
    assert out["invariants"]["multi_blocker_release_allowed"] is True
    # Required artifacts
    root = Path(out["output_dir"])
    for name in (
        "source_run_provenance.json",
        "start_trigger_audit.json",
        "all_order_events.csv",
        "all_fill_events.csv",
        "fill_position_reconciliation.csv",
        "same_candle_event_audit.csv",
        "overlay_add_audit.csv",
        "shared_be_round_audit.csv",
        "pnl_layers.json",
        "invariants.json",
        "MANUAL_ORDER_WALKTHROUGH.md",
        "REPORT.md",
    ):
        assert (root / name).exists(), name
        if name.endswith(".csv") and name == "fill_position_reconciliation.csv":
            assert (root / name).stat().st_size > 0, "fill_position_reconciliation empty"


def test_refuse_overwrite(tmp_path: Path):
    root = tmp_path / "f2"
    root.mkdir()
    (root / "invariants.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_forensic_audit(output_dir=root)


def test_start_trigger_t1_not_0000(tmp_path: Path):
    out = run_forensic_audit(output_dir=tmp_path / "f3")
    start = __import__("json").loads(
        (Path(out["output_dir"]) / "start_trigger_audit.json").read_text()
    )
    assert start["timing_mode"] == "T1"
    assert start["signal_open_meets_6pct"] is False
    assert start["fill_timestamp"].startswith("2026-01-19T00:05:00")
    assert start["fill_price"] == pytest.approx(1.6447)
    assert start["used_low_as_fill"] is False

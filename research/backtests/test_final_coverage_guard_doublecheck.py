"""Final coverage-guard double-check tests (no strategy-economy changes)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from research.backtests import final_coverage_guard_doublecheck_proofs as proofs
from research.backtests.run_c4_undercoverage_fix_validation import DEFAULT_OUT as REVAL_OUT


def test_insufficient_coverage_blocks_basket_and_keeps_residual() -> None:
    result = proofs.build_insufficient_block_case()
    assert result["pass"] is True
    assert result["reason_code"] == "coverage_blocked_insufficient_basket"
    assert result["residual_stage_still_active"] is True
    assert result["flat_cancel_blocked"] is True
    assert result["economic_undercoverage_closed"] == 0


def test_tolerance_boundary_below_exact_above() -> None:
    result = proofs.build_tolerance_boundary_cases()
    assert result["pass"] is True
    by_name = {c["name"]: c for c in result["cases"]}
    assert by_name["synthetic_just_below"]["sufficient"] is False
    assert by_name["synthetic_exact_tolerance"]["sufficient"] is True
    assert by_name["synthetic_just_above"]["sufficient"] is True
    assert by_name["live_low_price_insufficient"]["sufficient"] is False


def test_runtime_races_a_through_f() -> None:
    result = proofs.build_runtime_race_results()
    assert result["pass"] is True
    for key in (
        "A_same_candle_uses_fresh_economics",
        "B_open_basket_while_stage_fills",
        "C_stage_before_exit_cancel_ack",
        "D_partial_basket_fill",
        "E_restart_between_stages",
        "F_duplicate_late_fill_idempotent",
    ):
        assert result["races"][key]["pass"] is True, key


def test_legacy_parity_from_revalidation_artifacts() -> None:
    result = proofs.build_legacy_parity_check()
    assert result["legacy_parity"] is True
    assert result["invalid_partial_sum"] == 0
    assert result["over_close_sum"] == 0
    assert result["duplicate_stage_sum"] == 0


def test_revalidation_covered_cases_have_no_late_orphan_fills() -> None:
    rows = list(
        csv.DictReader((REVAL_OUT / "revalidation_rows.csv").open(encoding="utf-8"))
    )
    covered = [r for r in rows if int(float(r.get("covered_by_basket_exit") or 0)) == 1]
    assert covered, "expected covered_by_basket_exit cases from prior revalidation"
    for row in covered:
        late = row.get("stage_late_stage_fills_after_exit") or "[]"
        assert late in {"[]", ""} or late == "[]"
        assert int(float(row.get("trade_flat") or 0)) == 1
        assert int(float(row.get("economic_undercoverage_closed") or 0)) == 0


def test_identity_helper_eps_consistent() -> None:
    from research.backtests.run_c4_undercoverage_fix_validation import _identity

    ok = _identity(lhs=1.0, rhs=1.0 + 1e-12)
    # 1e-12 may exceed IDENTITY_EPS (1e-9)? 1e-12 < 1e-9 so pass
    assert ok["pass"] is True
    bad = _identity(lhs=1.0, rhs=1.0 + 1e-6)
    assert bad["pass"] is False

"""Economic undercoverage classification for FULL_DYNAMIC TEM (research-only).

Distinguishes cycle-pair audit undercoverage (partial SHORT_REDUCE vs LONG_ADD)
from FinalExitEconomics.sufficient / C4 ``covered_by_basket_exit``.
"""

from __future__ import annotations

from typing import Any

from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.run_c4_undercoverage_fix_validation import _classify_economic


EXIT_PURPOSES = frozenset({"LONG_TP_EXIT", "SHORT_SL_EXIT", "LONG_SL_EXIT", "SHORT_TP_EXIT"})


def _fills(result: Any) -> list[dict[str, Any]]:
    return list(getattr(result, "fills_log", None) or getattr(result, "fill_log", None) or [])


def extract_last_coverage(result: Any) -> dict[str, Any]:
    ex = getattr(result, "final_strategy_state_excerpt", None) or {}
    last = ex.get("last_basket_exit_coverage_decision")
    return dict(last) if isinstance(last, dict) else {}


def cycle_pair_undercoverage_rows(result: Any) -> list[dict[str, Any]]:
    return [
        row
        for row in build_pnl_coverage_audit(result)
        if "undercover" in str(row.get("status") or "").lower()
    ]


def classify_closed_economics(result: Any) -> dict[str, Any]:
    """Return cycle-pair vs economic classification for a closed (or open) run."""
    status = str(getattr(result, "final_status", "") or "")
    flat = status == "closed" or (
        float(getattr(result, "final_long_qty", 0) or 0) <= 1e-12
        and float(getattr(result, "final_short_qty", 0) or 0) <= 1e-12
        and status not in {"error", "open"}
    )
    last = extract_last_coverage(result)
    pair_rows = cycle_pair_undercoverage_rows(result)
    pair = pair_rows[0] if pair_rows else None
    fills = _fills(result)
    stage_info = {
        "late_stage_fills_after_exit": 0,
        "last_coverage_ok": last.get("coverage_ok"),
        "last_sufficient": last.get("sufficient"),
        "exit_fills": sum(1 for f in fills if str(f.get("purpose") or "") in EXIT_PURPOSES),
        "cancelled_stages": int(
            bool((getattr(result, "final_strategy_state_excerpt", None) or {}).get("research_fd_replan_events"))
        ),
    }
    economic = _classify_economic(
        status="closed" if flat or status == "closed" else status,
        cycle_pair=pair,
        stage_info=stage_info,
    )
    sufficient = last.get("sufficient")
    economic_uc = int(economic == "economic_undercoverage_closed")
    sufficient_false_closed = int(bool(flat) and sufficient is False)
    return {
        "flat": bool(flat),
        "final_status": status,
        "cycle_pair_undercoverage_count": len(pair_rows),
        "cycle_pair_status": None if pair is None else pair.get("status"),
        "cycle_pair_missing_pnl": None if pair is None else pair.get("missing_pnl"),
        "economic_class": economic,
        "economic_undercoverage_closed": economic_uc,
        "sufficient_false_closed": sufficient_false_closed,
        "last_sufficient": sufficient,
        "last_coverage_ok": last.get("coverage_ok"),
        "last_reason_code": last.get("reason_code"),
        "expected_total_net_after_exit": last.get("expected_total_net_after_exit"),
        "min_required_total_usdt": last.get("min_required_total_usdt"),
        "target_delta_usdt": last.get("target_delta_usdt"),
        "tolerance_usdt": last.get("tolerance_usdt"),
    }


def root_cause_category(row: dict[str, Any]) -> str:
    """Map a classified closed flat to the user root-cause taxonomy."""
    economic = str(row.get("economic_class") or "")
    reason = str(row.get("last_reason_code") or "")
    if economic == "economic_undercoverage_closed":
        if reason == "coverage_skipped_not_staged":
            return "F.flat_cancel_bypasses_coverage_gate"
        if row.get("last_sufficient") is False:
            return "G.cycle_marked_complete_before_sufficient"
        return "J.other_true_economic_undercoverage"
    if economic == "covered_by_basket_exit":
        if reason == "coverage_skipped_not_staged":
            return "F.skipped_staging_gate_but_sufficient_true"
        return "covered_by_basket_exit_not_economic_uc"
    if "undercover" in str(row.get("cycle_pair_status") or "").lower():
        missing = float(row.get("cycle_pair_missing_pnl") or 0.0)
        if missing < 0.02:
            return "I.fee_or_rounding_gap_cycle_pair_only"
        return "cycle_pair_undercovered_basket_compensated"
    return "J.other"

"""Research gate policies (not production); test precedence consequences."""

from __future__ import annotations

from typing import Any


def _pool_ok(row: dict[str, Any]) -> bool:
    return row.get("pool_validity") in ("OK", "OK_D59_CACHE")


def evaluate_policies(row: dict[str, Any], *, short_paused: bool) -> dict[str, Any]:
    if row.get("ema_validity") != "VALIDATED_TRP_D60":
        return _decision("UNCERTAIN", "EMA_HISTORY_NOT_VALIDATED", short_paused)
    if short_paused:
        return _decision("SHORT_NO", "SHORT_PAUSED", short_paused)

    r5 = bool(row.get("R5"))
    r6 = bool(row.get("R6_sideways"))
    pool_req_fail = not _pool_ok(row) and (row.get("R3") and not row.get("R4"))

    # Policy P1: Variant E only (BT-D61 ablation E)
    p1 = "SHORT_OK" if r5 else "SHORT_NO"

    # Policy P2: R6 sideways veto over R5
    if r6:
        p2 = "SHORT_NO"
        p2_reason = "R6_SIDEWAYS_VETO"
    elif r5:
        p2 = "SHORT_OK"
        p2_reason = "PASS_R5_NO_R6"
    elif pool_req_fail:
        p2 = "UNCERTAIN"
        p2_reason = "POOL_MISSING_FOR_R4_R5"
    else:
        p2 = "SHORT_NO"
        p2_reason = row.get("first_variant_drop", "FAIL_RULE")

    # Policy P3: require R3 + not R6 (ignore isolation if pool missing → UNCERTAIN)
    if r6:
        p3, p3_reason = "SHORT_NO", "R6_SIDEWAYS_VETO"
    elif not row.get("R3"):
        p3, p3_reason = "SHORT_NO", "FAIL_R3"
    elif not _pool_ok(row):
        p3, p3_reason = "UNCERTAIN", "POOL_MISSING"
    elif r5:
        p3, p3_reason = "SHORT_OK", "PASS_R5"
    else:
        p3, p3_reason = "SHORT_NO", row.get("first_variant_drop", "FAIL_R4_R5")

    return {
        "policy_p1_variant_e": p1,
        "policy_p2_r6_veto": p2,
        "policy_p2_reason": p2_reason,
        "policy_p3_r3_not_r6_plus_isolation": p3,
        "policy_p3_reason": p3_reason,
        "recommended_research_gate": p2,
        "recommended_reason": p2_reason,
    }


def _decision(verdict: str, reason: str, short_paused: bool) -> dict[str, Any]:
    return {
        "policy_p1_variant_e": verdict if verdict != "SHORT_OK" else "SHORT_NO",
        "policy_p2_r6_veto": verdict,
        "policy_p2_reason": reason,
        "policy_p3_r3_not_r6_plus_isolation": verdict,
        "policy_p3_reason": reason,
        "recommended_research_gate": verdict,
        "recommended_reason": reason,
        "short_paused": short_paused,
    }

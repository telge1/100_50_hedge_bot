"""Combined research decision states — transparent rules, no blackbox score."""

from __future__ import annotations

from typing import Any, Optional


def classify_combined(
    *,
    efficiency: dict[str, Any],
    trap: dict[str, Any],
    acceptance: dict[str, Any],
    checkpoint_s: int,
) -> dict[str, Any]:
    codes: list[str] = []
    eff_ok = efficiency.get("efficiency_status") == "OK"
    if not eff_ok:
        codes.append("EFFICIENCY_UNKNOWN")
        return {
            "state": "MIXED_OR_UNKNOWN",
            "explanation_codes": codes,
            "tradable_research_candidate": False,
            "data_quality_gate": "FAIL",
        }

    compressed = bool(efficiency.get("compression_flag"))
    veto = bool(efficiency.get("strong_same_side_impact_veto"))
    fav = float(efficiency.get("favorable_progress_bps") or 0.0)
    if compressed:
        codes.append("AGGRESSOR_INEFFICIENT_COMPRESSION")
    elif veto or fav >= 8.0:
        codes.append("AGGRESSOR_EFFICIENT")
    else:
        codes.append("AGGRESSOR_MIXED_EFFICIENCY")

    cp = (trap.get("checkpoints") or {}).get(f"cp_{checkpoint_s}s") or {}
    trap_label = cp.get("trap_label") or trap.get("final_trap_label") or "UNKNOWN_DATA"
    if trap_label == "TRAP_CONFIRMED":
        codes.append("AGGRESSORS_TRAPPED")
    elif trap_label == "TEMPORARY_UNDERWATER":
        codes.append("TEMPORARY_UNDERWATER")
    elif trap_label == "VWAP_RECLAIMED":
        codes.append("VWAP_RECLAIMED")
    elif trap_label == "NEVER_TRAPPED":
        codes.append("NEVER_TRAPPED")
    else:
        codes.append("TRAP_UNKNOWN")

    acc_cp = (acceptance.get("checkpoints") or {}).get(f"cp_{checkpoint_s}s") or {}
    acc_state = acc_cp.get("state") or acceptance.get("final_acceptance_state") or "UNKNOWN_EDGE"
    codes.append(f"ACCEPT_{acc_state}")

    # Rule table
    state = "MIXED_OR_UNKNOWN"
    if acc_state == "UNKNOWN_EDGE":
        if compressed and trap_label in {"TRAP_CONFIRMED", "TEMPORARY_UNDERWATER"}:
            state = "ABSORPTION_NO_RESOLUTION"
        elif compressed:
            state = "ABSORPTION_NO_RESOLUTION"
        elif "AGGRESSOR_EFFICIENT" in codes and trap_label == "NEVER_TRAPPED":
            state = "BREAK_WITHOUT_HEALTHY_FLOW"  # efficient but no edge to confirm
        else:
            state = "MIXED_OR_UNKNOWN"
    elif acc_state in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"} and "AGGRESSOR_EFFICIENT" in codes and trap_label != "TRAP_CONFIRMED":
        state = "ATTACKER_WINNING"
        codes.append("ATTACKER_WINNING")
    elif trap_label == "TRAP_CONFIRMED" and acc_state in {"BREAK_RECLAIMED", "FAILED_BREAK", "NO_BREAK", "BREAK_UNCONFIRMED"}:
        state = "ATTACKER_TRAPPED_REJECTION"
        codes.append("ATTACKER_TRAPPED_REJECTION")
    elif compressed and acc_state in {"BREAK_RECLAIMED", "FAILED_BREAK"}:
        state = "ATTACKER_TRAPPED_REJECTION"
        codes.append("INEFFICIENT_PLUS_RECLAIM")
    elif acc_state in {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"} and not compressed and trap_label != "TRAP_CONFIRMED":
        if "AGGRESSOR_EFFICIENT" not in codes:
            state = "BREAK_WITHOUT_HEALTHY_FLOW"
            codes.append("BREAK_WITHOUT_HEALTHY_FLOW")
        else:
            state = "ATTACKER_WINNING"
    elif compressed and acc_state in {"NO_BREAK", "BREAK_UNCONFIRMED", "UNKNOWN_DATA"}:
        state = "ABSORPTION_NO_RESOLUTION"
    elif acc_state == "CHOP_AROUND_EDGE":
        state = "MIXED_OR_UNKNOWN"
        codes.append("CHOP")
    else:
        state = "MIXED_OR_UNKNOWN"

    tradable = state in {"ATTACKER_WINNING", "ATTACKER_TRAPPED_REJECTION"} and "TRAP_UNKNOWN" not in codes
    dq = "PASS" if eff_ok and trap.get("trap_status") in {"OK", "UNKNOWN_DATA"} else "FAIL"
    return {
        "state": state,
        "explanation_codes": codes,
        "tradable_research_candidate": bool(tradable),
        "data_quality_gate": dq,
        "trap_label_at_cp": trap_label,
        "acceptance_state_at_cp": acc_state,
    }


def build_decision_ladder(
    efficiency: dict[str, Any],
    trap: dict[str, Any],
    acceptance: dict[str, Any],
) -> dict[str, Any]:
    ladder = {}
    for cp in (5, 10, 30, 60):
        ladder[f"decision_state_{cp}s"] = classify_combined(
            efficiency=efficiency, trap=trap, acceptance=acceptance, checkpoint_s=cp
        )
    # final uses 60s if available else last OK
    final = ladder["decision_state_60s"]
    return {
        **{k: v["state"] for k, v in ladder.items()},
        "decision_detail_5s": ladder["decision_state_5s"],
        "decision_detail_10s": ladder["decision_state_10s"],
        "decision_detail_30s": ladder["decision_state_30s"],
        "decision_detail_60s": ladder["decision_state_60s"],
        "final_research_state": final["state"],
        "explanation_codes": final["explanation_codes"],
        "data_quality_gate": final["data_quality_gate"],
        "tradable_research_candidate": final["tradable_research_candidate"],
    }

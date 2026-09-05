"""Manual-window comparison hypotheses (never detector gates)."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import MANUAL_HYPOTHESES


def compare_window(
    *,
    window_id: str,
    candidate_state: str,
    primary_zone: str,
    mechanism: str,
    primary_class: str,
    regime: str,
    data_coverage: str,
) -> dict[str, Any]:
    hyp = MANUAL_HYPOTHESES.get(window_id, "")
    agree = "UNKNOWN"
    notes = []

    if window_id in ("circle_1", "circle_2", "circle_3", "circle_5"):
        ok = (
            primary_zone == "EMA20"
            and mechanism == "ASK_DEFENSE"
            and candidate_state == "defense_rejection_confirmed"
            and regime in ("bearish", "transition")
        )
        partial = primary_zone == "EMA20" and (
            mechanism in ("ASK_DEFENSE", "UNDETERMINED")
            or candidate_state in ("defense_rejection_confirmed", "wait_microstructure_confirmation")
        )
        agree = "AGREE" if ok else ("PARTIAL" if partial else "DIVERGE")
        notes.append(f"hyp={hyp}")

    elif window_id == "circle_4":
        ok = candidate_state in ("false_breakout_confirmed", "breakout_confirmed", "wait_microstructure_confirmation")
        agree = "AGREE" if ok and "reclaim" in hyp else ("PARTIAL" if ok else "DIVERGE")
        notes.append(f"hyp={hyp}; class={primary_class}")

    elif window_id == "rectangle":
        # ask consumed, breakout failed → false breakout or wait; then move toward EMA59
        ok = (
            mechanism in ("ASK_ABSORPTION", "LIQUIDITY_PULL", "UNDETERMINED")
            and candidate_state in (
                "false_breakout_confirmed",
                "breakout_confirmed",
                "wait_microstructure_confirmation",
            )
        )
        agree = "AGREE" if ok and primary_class in (
            "FALSE_BREAKOUT_RECLAIM",
            "ABSORPTION_THEN_BREAKOUT",
            "LIQUIDITY_PULL_BREAKOUT",
            "BREAKOUT_WITHOUT_CONFIRMED_ABSORPTION",
        ) else ("PARTIAL" if ok else "DIVERGE")
        notes.append(f"hyp={hyp}; mech={mechanism}; class={primary_class}")

    elif window_id == "final_circle":
        if data_coverage == "DATA_INCOMPLETE":
            notes.append("L2_after_15:00_possibly_incomplete")
        ok = primary_zone == "EMA59" and candidate_state in (
            "false_breakout_confirmed",
            "defense_rejection_confirmed",
            "wait_microstructure_confirmation",
            "data_incomplete",
        )
        agree = "AGREE" if ok else ("PARTIAL" if primary_zone == "EMA59" else "DIVERGE")
        notes.append(f"hyp={hyp}")

    return {
        "window_id": window_id,
        "manual_hypothesis": hyp,
        "detector_candidate_state": candidate_state,
        "detector_primary_zone": primary_zone,
        "detector_mechanism": mechanism,
        "detector_primary_class": primary_class,
        "detector_regime": regime,
        "data_coverage": data_coverage,
        "parity": agree,
        "notes": "|".join(notes),
        "hardcoded_into_detector": False,
    }

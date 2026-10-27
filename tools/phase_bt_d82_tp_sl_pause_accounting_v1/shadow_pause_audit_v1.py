"""SHADOW pool+EMA candidates during SHORT_PAUSED (not executable)."""

from __future__ import annotations

from typing import Any


def extract_shadow_candidates_during_pause(replay: dict[str, Any]) -> list[dict[str, Any]]:
    """Bars where pool signal exists, EMA-V3 would authorize, but pause blocks intent."""
    out: list[dict[str, Any]] = []
    for row in replay.get("gate_decisions") or []:
        if row.get("pause_state") != "SHORT_PAUSED":
            continue
        if not row.get("pool_candidate_present"):
            continue
        if row.get("ema_decision") != "SHORT_OK":
            continue
        if row.get("allow_new_short_intent"):
            continue
        reasons = list(row.get("reasons") or [])
        if not any(r.startswith("PAUSE_") for r in reasons):
            continue
        out.append(
            {
                "decision_close_utc": row.get("decision_close_utc"),
                "pool_key": row.get("baseline_signal_pool_key"),
                "ema_decision": row.get("ema_decision"),
                "ema_reason_code": row.get("ema_reason_code"),
                "pause_state": row.get("pause_state"),
                "block_reasons": reasons,
                "shadow_class": "POOL_EMA_V3_OK_BLOCKED_BY_SHORT_PAUSED",
                "executable": False,
            }
        )
    return out

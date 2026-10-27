"""Replay SHORT_PAUSED after modeled SL (D46); suppress intents while paused."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def sl_events() -> list[dict[str, str]]:
    path = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv"
    out = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r.get("event") == "POSITION_CLOSED_SL":
            out.append(r)
    return out


def replay_pause_impact(
    intents: list[dict[str, Any]],
    gate_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fail-closed pause: after each SL, block until explicit research requal (not implemented)."""
    sls = sorted(sl_events(), key=lambda x: x.get("decision_close_utc") or "")
    paused_from: str | None = None
    impact: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []

    gate_by_ts = {g["decision_close_utc"]: g for g in gate_rows}

    for intent in intents:
        ts = intent["decision_close_utc"]
        for sl in sls:
            sl_ts = sl.get("decision_close_utc") or ""
            if sl_ts < ts and (paused_from is None or sl_ts >= paused_from):
                paused_from = sl_ts
        paused = paused_from is not None and ts > paused_from
        g = gate_by_ts.get(ts, {})
        rec = {
            "decision_close_utc": ts,
            "intent_id": intent.get("intent_id"),
            "short_paused_active": paused,
            "paused_after_sl_utc": paused_from,
            "d46_would_create_pending": intent.get("d46_event") == "PENDING_CREATE",
            "research_gate_without_pause": g.get("policy_p2_r6_veto"),
            "research_gate_with_pause": "SHORT_NO" if paused else g.get("policy_p2_r6_veto"),
        }
        impact.append(rec)
        if paused and intent.get("d46_event") == "PENDING_CREATE":
            suppressed.append({**rec, "suppression_reason": "SHORT_PAUSED_AFTER_SL"})
    return impact, suppressed

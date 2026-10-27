"""Extract causal Short intent creation times from D46 + mandatory references."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

MANDATORY_INTENTS = [
    {
        "intent_id": "ref_sep9_1630",
        "decision_close_utc": "2026-09-09T16:30:00Z",
        "source": "user_reference_desired_short",
        "evidence_class": "development_reference",
        "d46_event": None,
    },
    {
        "intent_id": "ref_sep9_2055",
        "decision_close_utc": "2026-09-09T20:55:00Z",
        "source": "user_reference_desired_short",
        "evidence_class": "development_reference",
        "d46_event": None,
    },
    {
        "intent_id": "ref_sep11_1205",
        "decision_close_utc": "2026-09-11T12:05:00Z",
        "source": "d46_pending_create_sustained",
        "evidence_class": "development_reference_problem",
        "d46_event": "PENDING_CREATE",
        "note": "Sustained pending before 12:25 fill; not the fill bar",
    },
    {
        "intent_id": "ref_sep11_1230",
        "decision_close_utc": "2026-09-11T12:30:00Z",
        "source": "d46_pending_create",
        "evidence_class": "development_reference_problem",
        "d46_event": "PENDING_CREATE",
        "note": "Before 12:35 fill",
    },
]


def load_d46_pending_intents() -> list[dict[str, Any]]:
    path = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        if r.get("event") != "PENDING_CREATE":
            continue
        dc = r.get("decision_close_utc") or ""
        rows.append(
            {
                "intent_id": f"d46_{r.get('seq')}_{dc.replace(':', '').replace('-', '')}",
                "decision_close_utc": dc,
                "source": "d46_chronological_events",
                "evidence_class": "chronological_control",
                "d46_event": "PENDING_CREATE",
                "order_id": r.get("order_id"),
                "pending_active_from_utc": r.get("pending_active_from_utc"),
                "machine_state": r.get("machine_state"),
                "selection_reason": r.get("selection_reason"),
            }
        )
    return rows


def build_intent_timeline() -> list[dict[str, Any]]:
    d46 = load_d46_pending_intents()
    by_ts = {m["decision_close_utc"]: m for m in MANDATORY_INTENTS}
    for row in d46:
        ts = row["decision_close_utc"]
        if ts not in by_ts:
            by_ts[ts] = row
        else:
            # Enrich mandatory with D46 fields when same timestamp
            by_ts[ts] = {**by_ts[ts], **{k: v for k, v in row.items() if v}}
    out = sorted(by_ts.values(), key=lambda x: x["decision_close_utc"])
    return out

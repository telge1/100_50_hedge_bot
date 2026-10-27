"""SL pause requalification research from BT-D46 events (no unlock)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def build_sl_pause_audit(rows_by_bar: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    events_path = ROOT / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv"
    if not events_path.is_file():
        return [{"status": "D46_EVENTS_MISSING", "path": str(events_path)}]

    sl_rows = [r for r in csv.DictReader(events_path.open()) if r.get("event") == "POSITION_CLOSED_SL"]
    out: list[dict[str, Any]] = []
    for e in sl_rows:
        sl_bar = e.get("decision_close_utc") or ""
        if not sl_bar.startswith("2026-09-11"):
            continue
        at = rows_by_bar.get(sl_bar)
        before = rows_by_bar.get(_offset_bar(sl_bar, -3))
        entry: dict[str, Any] = {
            "sl_bar_utc": sl_bar,
            "position_id": e.get("position_id"),
            "in_gegenprobe_dataset": at is not None,
            "research_note": "SL does not prove downtrend ended; no SHORT_PAUSED unlock",
        }
        if at:
            entry["regime_at_sl"] = at.get("research_regime")
            entry["ema_still_bearish"] = bool(at.get("ema9_lt_20_lt_59_lt_200"))
            entry["sideways_stack_warning"] = at.get("ablation", {}).get("research_block_sideways_stack")
            entry["requalification_evidence"] = _requal_hint(at)
        if before:
            entry["regime_3b_before_sl"] = before.get("research_regime")
            entry["first_warning_before_sl"] = before.get("ablation", {}).get("research_block_sideways_stack")
        out.append(entry)
    return out


def _offset_bar(bar: str, delta: int) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.fromisoformat(bar.replace("Z", "+00:00")) + timedelta(minutes=5 * delta)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _requal_hint(row: dict[str, Any]) -> str:
    if row.get("research_regime") == "DOWNTREND_CANDIDATE" and not row.get("ablation", {}).get(
        "research_block_sideways_stack"
    ):
        return "STRUCTURE_STILL_BEARISH_BUT_SL_OCCURRED_REVIEW_ISOLATION"
    if row.get("research_regime") == "SIDEWAYS_CANDIDATE":
        return "CHOP_STACK_WARN_PREFER_PAUSE"
    return "UNCERTAIN_REQUAL"

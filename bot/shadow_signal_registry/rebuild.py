"""Rebuild snapshot rows from append-only events."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bot.shadow_signal_registry.models import BLOCK_REASON_NONE, Side, empty_snapshot_row


def normalize_block_reason(row: dict[str, Any]) -> None:
    """Registry presentation only; does not change allow/block decisions."""
    if row.get("allowed") and not row.get("blocked"):
        row["block_reason"] = BLOCK_REASON_NONE
        return
    if row.get("blocked") and row.get("block_reason") is None:
        status = row.get("signal_status") or ""
        if status == "LADDER_BLOCKED":
            row["block_reason"] = "LADDER24"
        elif row.get("hypothetical") and row.get("e1r_state") == "ACTIVE":
            row["block_reason"] = "E1R_ACTIVE"


def export_row(row: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(row)
    normalize_block_reason(out)
    return out


def rebuild_snapshots_from_events(events: list[dict[str, Any]], side: Side) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for ev in events:
        sid = ev.get("signal_id")
        if not sid:
            continue
        if sid not in snapshots:
            snapshots[sid] = empty_snapshot_row(side, sid, ev.get("event_time") or "")
        row = snapshots[sid]
        et = ev.get("event_type") or ""
        payload = ev.get("payload") or {}

        if et == "SIGNAL_DETECTED":
            if "row" in payload:
                row.update(deepcopy(payload["row"]))
            elif payload.get("pool_id"):
                row["pool_id"] = payload["pool_id"]
        elif et == "SIGNAL_ALLOWED":
            row.update(
                {
                    "allowed": True,
                    "blocked": False,
                    "block_reason": BLOCK_REASON_NONE,
                    "hypothetical": False,
                    "signal_status": "ALLOWED",
                }
            )
        elif et == "SIGNAL_BLOCKED":
            br = payload.get("block_reason") or row.get("block_reason")
            row.update(
                {
                    "allowed": False,
                    "blocked": True,
                    "block_reason": br,
                    "hypothetical": True,
                    "signal_status": row.get("signal_status") or "BLOCKED",
                }
            )
            if br == "LADDER24":
                row["signal_status"] = "LADDER_BLOCKED"
                row["raw_block_reason"] = row.get("raw_block_reason") or "LADDER24"
        elif et == "SHADOW_OPENED":
            if "row" in payload:
                row.update(deepcopy(payload["row"]))
            row["tracking_status"] = "OPEN"
            if payload.get("hypothetical") is not None:
                row["hypothetical"] = bool(payload["hypothetical"])
        elif et == "BE_TRIGGERED":
            row["be_triggered"] = True
            if payload.get("be_trigger_time"):
                row["be_trigger_time"] = payload["be_trigger_time"]
        elif et.startswith("SHADOW_") and et != "SHADOW_OPENED":
            outcome = et.replace("SHADOW_", "")
            if outcome == "INTRABAR_AMBIGUOUS":
                outcome = "BE" if side == "long" else "SL"
            row["tracking_status"] = "CLOSED"
            row["outcome"] = payload.get("outcome") or outcome
            row["signal_status"] = row["outcome"]
            if payload.get("pnl_pct") is not None:
                row["pnl_pct"] = payload["pnl_pct"]
            for key in (
                "exit_time",
                "exit_price",
                "mae_pct",
                "mfe_pct",
                "duration_min",
                "horizon_time",
                "last_processed_1m",
            ):
                if key in payload:
                    row[key] = payload[key]

    for row in snapshots.values():
        normalize_block_reason(row)
    return snapshots

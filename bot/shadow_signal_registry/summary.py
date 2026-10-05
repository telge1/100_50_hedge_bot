"""Aggregate summary.json per side."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

Side = Literal["long", "short"]


def build_summary(side: Side, snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    allowed = [s for s in snapshots if s.get("allowed")]
    blocked = [s for s in snapshots if s.get("blocked")]
    open_rows = [s for s in snapshots if s.get("tracking_status") == "OPEN"]
    closed = [s for s in snapshots if s.get("tracking_status") == "CLOSED"]

    def pnl_sum(rows: list[dict]) -> float:
        return round(sum(float(r.get("pnl_pct") or 0) for r in rows if r.get("pnl_pct") is not None), 4)

    def count_outcome(rows: list[dict], o: str) -> int:
        return sum(1 for r in rows if r.get("outcome") == o)

    hyp_blocked = [s for s in blocked if s.get("hypothetical")]
    allowed_closed = [s for s in allowed if s.get("tracking_status") == "CLOSED"]

    base: dict[str, Any] = {
        "side": side,
        "total_signals": len(snapshots),
        "allowed": len(allowed),
        "blocked": len(blocked),
        "open": len(open_rows),
        "TP": count_outcome(closed, "TP"),
        "SL": count_outcome(closed, "SL") + count_outcome(closed, "INTRABAR_AMBIGUOUS"),
        "horizon": count_outcome(closed, "HORIZON"),
        "allowed_pnl_sum": pnl_sum(allowed_closed),
        "blocked_hypothetical_pnl_sum": pnl_sum([s for s in hyp_blocked if s.get("tracking_status") == "CLOSED"]),
        "last_updated": now,
    }

    if side == "long":
        base["BE"] = count_outcome(closed, "BE")
        hyp_closed = [s for s in hyp_blocked if s.get("tracking_status") == "CLOSED"]
        base["ladder_saved_losses"] = sum(
            1 for s in hyp_closed if (s.get("pnl_pct") or 0) < 0 and s.get("block_reason") == "LADDER24"
        )
        base["ladder_blocked_winners"] = sum(
            1 for s in hyp_closed if (s.get("pnl_pct") or 0) > 0 and s.get("block_reason") == "LADDER24"
        )
    else:
        hyp_closed = [s for s in hyp_blocked if s.get("tracking_status") == "CLOSED"]
        base["e1r_saved_losses"] = sum(
            1 for s in hyp_closed if (s.get("pnl_pct") or 0) < 0 and s.get("block_reason") == "E1R_ACTIVE"
        )
        base["e1r_blocked_winners"] = sum(
            1 for s in hyp_closed if (s.get("pnl_pct") or 0) > 0 and s.get("block_reason") == "E1R_ACTIVE"
        )
        base["floorguard_saved_losses"] = sum(
            1 for s in hyp_closed if (s.get("pnl_pct") or 0) < 0 and s.get("block_reason") == "FLOOR_GUARD"
        )
        base["floorguard_blocked_winners"] = sum(
            1 for s in hyp_closed if (s.get("pnl_pct") or 0) > 0 and s.get("block_reason") == "FLOOR_GUARD"
        )
    return base

"""Coverage gate for v2 events."""

from __future__ import annotations

from typing import Any


def evaluate_coverage_gate(
    *,
    n_trades_raw: int,
    attribution_stats: dict[str, Any] | None,
    linkage_status: str | None,
    has_wall: bool,
    n_lc: int | None = None,
) -> dict[str, Any]:
    """Return {pass, blockers[], warnings[]} — hard blockers fail the event for primary analysis."""
    blockers: list[str] = []
    warnings: list[str] = []
    stats = attribution_stats or {}

    if int(n_trades_raw or 0) <= 0:
        blockers.append("PUBLIC_TRADE_COVERAGE_MISSING")
    if int(stats.get("cross_epoch") or 0) > 0:
        blockers.append("REPLAY_EPOCH_MIX_OR_CHANGE")
    if int(stats.get("seq_gaps") or 0) > 0:
        blockers.append("BOOK_SEQUENCE_GAP")
    if linkage_status == "DATA_INCOMPLETE":
        blockers.append("LINKAGE_DATA_INCOMPLETE")
    if n_lc is not None and int(n_lc) <= 0:
        blockers.append("NO_LEVEL_CHANGES")
    if not has_wall and linkage_status not in (
        "NO_CANONICAL_WALL",
        "AMBIGUOUS_MULTIPLE_WALLS",
        "DATA_INCOMPLETE",
    ):
        warnings.append("NO_WALL_SELECTED")

    return {
        "pass": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "cross_epoch": int(stats.get("cross_epoch") or 0),
        "seq_gaps": int(stats.get("seq_gaps") or 0),
        "invalid_intervals": int(stats.get("invalid_intervals") or 0),
        "n_trades_raw": int(n_trades_raw or 0),
    }

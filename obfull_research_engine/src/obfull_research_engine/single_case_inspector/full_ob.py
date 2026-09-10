"""Full-OB availability for single-case inspector (never invent values)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import (
    DEFAULT_ARCHIVE_ROOT,
)

from ..localized_coverage import POLICY_ID, STRICT_POLICY_ID, VERDICT_COMPLETE
from ..localized_coverage.check import check_localized_coverage
from ..localized_coverage.gap_scan import scan_window_gaps
from ..localized_coverage.spans import window_inside_usable_span
from ..timeparse import format_utc_z
from . import FULL_OB_STATUS

FULL_OB_AVAILABLE = "FULL_OB_AVAILABLE"

FULL_OB_NULL_FIELDS = {
    "full_ob_walls": "NOT_AVAILABLE",
    "wall_defense": "NOT_AVAILABLE",
    "wall_consumption": "NOT_AVAILABLE",
    "full_ob_refill": "NOT_AVAILABLE",
    "full_ob_absorption": "NOT_AVAILABLE",
    "full_ob_vacuum": "NOT_AVAILABLE",
    "book_imbalance": "NOT_AVAILABLE",
    "full_depth_shape": "NOT_AVAILABLE",
    "full_ob_control": "NOT_AVAILABLE",
}


def assess_full_ob(
    *,
    symbol: str,
    focus_ts: datetime,
    pre_seconds: int,
    post_seconds: int,
    archive_root: Path | None = None,
    coverage_policy: str = STRICT_POLICY_ID,
) -> dict[str, Any]:
    """Assess Full-OB availability on the exact case window [focus-pre, focus+post).

    STRICT_WHOLE_WINDOW (default): historical inspector contract — never claims
    Full-OB available (`_any_usable` remains False). Existing run-keys stay stable.

    LOCALIZED_EXCLUSION_V1: reuses ``check_localized_coverage`` on the exact case
    window. Full-OB is available only when the entire window sits inside one
    usable span and the raw scan has 0 sequence gaps and 0 tainted intervals.
    """
    focus_ts = focus_ts.astimezone(timezone.utc)
    start = focus_ts - timedelta(seconds=int(pre_seconds))
    end = focus_ts + timedelta(seconds=int(post_seconds))
    root = Path(archive_root or DEFAULT_ARCHIVE_ROOT)
    policy = coverage_policy or STRICT_POLICY_ID

    localized = None
    if policy == POLICY_ID:
        localized = check_localized_coverage(
            symbol=symbol,
            start=start,
            end=end,
            archive_root=root,
            focus_ts=focus_ts,
            include_strict_shadow=True,
        )
        gap_report = localized.get("gap_report") or {}
    else:
        gap_report = scan_window_gaps(symbol=symbol, start=start, end=end, archive_root=root)

    focus_in_gap = False
    containing: dict[str, Any] | None = None
    for t in gap_report.get("tainted_intervals") or []:
        a = datetime.fromisoformat(str(t["start"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        b_raw = t.get("end")
        b = (
            datetime.fromisoformat(str(b_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            if b_raw
            else end
        )
        if a <= focus_ts < b:
            focus_in_gap = True
            containing = t
            break

    recovery = None
    gap_start = None
    gap_end = None
    gap_reason = None
    if containing:
        gap_start = containing.get("start")
        gap_end = containing.get("end")
        gap_reason = containing.get("reason") or "TAINTED_SEQUENCE_GAP"
        recovery = containing.get("end")
    elif focus_in_gap is False:
        for g in gap_report.get("sequence_gaps") or []:
            gs = g.get("gap_ts")
            if not gs:
                continue
            a = datetime.fromisoformat(str(gs).replace("Z", "+00:00")).astimezone(timezone.utc)
            ge = g.get("recovery_checkpoint_ts")
            b = (
                datetime.fromisoformat(str(ge).replace("Z", "+00:00")).astimezone(timezone.utc)
                if ge
                else end
            )
            if a <= focus_ts < b:
                focus_in_gap = True
                gap_start = format_utc_z(a)
                gap_end = format_utc_z(b) if ge else None
                gap_reason = "HARD_LOCAL_GAP"
                recovery = ge
                break

    n_gaps = len(gap_report.get("sequence_gaps") or [])
    n_tainted = len(gap_report.get("tainted_intervals") or [])
    containing_span = None
    if policy == POLICY_ID and localized is not None:
        containing_span = window_inside_usable_span(
            start, end, list(localized.get("usable_spans") or [])
        )
        raw_complete = (
            n_gaps == 0
            and n_tainted == 0
            and containing_span is not None
            and (localized.get("verdict") in {VERDICT_COMPLETE, "DATA_USABLE_WITH_EXCLUSIONS"})
            and not focus_in_gap
        )
        status = FULL_OB_AVAILABLE if raw_complete else FULL_OB_STATUS
    else:
        status = FULL_OB_STATUS if focus_in_gap or not _any_usable(gap_report, focus_ts) else FULL_OB_AVAILABLE
        if focus_in_gap:
            status = FULL_OB_STATUS

    return {
        "schema_version": "full_ob_availability_v1",
        "symbol": symbol.upper(),
        "focus_ts": format_utc_z(focus_ts),
        "window_start": format_utc_z(start),
        "window_end": format_utc_z(end),
        "coverage_policy": policy,
        "status": status,
        "focus_in_gap": focus_in_gap,
        "gap_start": gap_start,
        "gap_end": gap_end,
        "gap_reason": gap_reason,
        "next_validated_recovery_checkpoint": recovery,
        "unavailable_fields": dict(FULL_OB_NULL_FIELDS) if status != FULL_OB_AVAILABLE else {},
        "no_estimate": True,
        "no_ob200_substitute": True,
        "no_public_trade_proxy_as_full_ob": True,
        "gap_report_summary": {
            "n_sequence_gaps": n_gaps,
            "n_tainted_intervals": n_tainted,
        },
        "tainted_intervals": gap_report.get("tainted_intervals") or [],
        "sequence_gaps": gap_report.get("sequence_gaps") or [],
        "containing_usable_span": None
        if containing_span is None
        else {
            "span_id": containing_span.get("span_id"),
            "start": containing_span.get("start"),
            "end": containing_span.get("end"),
        },
        "localized_verdict": None if localized is None else localized.get("verdict"),
        "localized_coverage": localized,
        "strict_shadow": None if localized is None else localized.get("strict_shadow"),
    }


def _any_usable(gap_report: dict[str, Any], focus_ts: datetime) -> bool:
    """STRICT default: never claim Full-OB without an explicit localized reconstruction."""
    _ = gap_report, focus_ts
    return False

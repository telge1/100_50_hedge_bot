"""Focus-ts eligibility against usable spans and causal lookbacks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..timeparse import format_utc_z
from .spans import point_in_spans


def check_focus_ts(
    *,
    focus_ts: datetime,
    window_start: datetime,
    window_end: datetime,
    usable_spans: list[dict[str, Any]],
    excluded_spans: list[dict[str, Any]],
    lookback_full_ob_s: int = 0,
    lookback_avr_s: int = 1800,
    lookback_multiscale_s: int = 300,
    lookback_candidate_s: int = 0,
    outcome_horizon_s: int = 1800,
) -> dict[str, Any]:
    """Decide if focus timestamp is analysable under LOCALIZED_EXCLUSION_V1."""
    focus_ts = focus_ts.astimezone(timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    window_end = window_end.astimezone(timezone.utc)

    out: dict[str, Any] = {
        "focus_ts": format_utc_z(focus_ts),
        "window_start": format_utc_z(window_start),
        "window_end": format_utc_z(window_end),
        "status": "NOT_ELIGIBLE",
        "required_lookbacks": {
            "full_ob_s": lookback_full_ob_s,
            "avr_baseline_s": lookback_avr_s,
            "avr_multiscale_s": lookback_multiscale_s,
            "candidate_s": lookback_candidate_s,
            "outcome_horizon_s": outcome_horizon_s,
        },
        "blocking_interval": None,
        "span_id": None,
        "reasons": [],
    }

    if not (window_start <= focus_ts < window_end):
        out["reasons"].append("FOCUS_OUTSIDE_WINDOW")
        return out

    span = point_in_spans(focus_ts, usable_spans)
    if span is None:
        # find blocking exclusion
        block = None
        for e in excluded_spans:
            if e.get("reason") == "OI_STALE":
                continue  # OI optional for focus eligibility of Full-OB analysis
            a = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(e["end"].replace("Z", "+00:00"))
            if a <= focus_ts < b:
                block = e
                break
        out["blocking_interval"] = block
        out["reasons"].append("FOCUS_IN_TAINTED_OR_EXCLUDED")
        return out

    out["span_id"] = span["span_id"]
    # Required lookback must stay inside the same usable span
    need_back = max(lookback_full_ob_s, lookback_avr_s, lookback_multiscale_s, lookback_candidate_s)
    look_start = focus_ts - timedelta(seconds=need_back)
    span_start = datetime.fromisoformat(span["start"].replace("Z", "+00:00")).astimezone(timezone.utc)
    span_end = datetime.fromisoformat(span["end"].replace("Z", "+00:00")).astimezone(timezone.utc)
    if look_start < span_start:
        out["blocking_interval"] = {
            "start": format_utc_z(look_start),
            "end": format_utc_z(span_start),
            "reason": "LOOKBACK_CROSSES_SPAN_BOUNDARY_OR_GAP",
        }
        out["reasons"].append("FOCUS_LOOKBACK_OVER_GAP")
        return out

    eligible = datetime.fromisoformat(span["analysis_eligible_start"].replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    if focus_ts < eligible and "WARMUP_INCOMPLETE_IN_SPAN" not in (span.get("quality_flags") or []):
        # still allow if warmup flag set; else require eligible
        if focus_ts < eligible:
            out["blocking_interval"] = {
                "start": format_utc_z(span_start),
                "end": format_utc_z(eligible),
                "reason": "BEFORE_ANALYSIS_ELIGIBLE_START",
            }
            out["reasons"].append("FOCUS_BEFORE_WARMUP_ELIGIBLE")
            # For research focus we soft-warn but can still mark PARTIAL — keep NOT_ELIGIBLE for AVR
            return out

    # Outcome future must reach focus+horizon within public trades tip — checked by caller tip;
    # here only ensure we don't require Full-OB after detection
    out["status"] = "ELIGIBLE"
    out["span"] = span
    out["post_detection_note"] = (
        "Public-trade outcomes may extend past a later Full-OB gap; "
        "flag FULL_OB_POST_DETECTION_GAP_CONTEXT; censor Full-OB post metrics at gap"
    )
    return out

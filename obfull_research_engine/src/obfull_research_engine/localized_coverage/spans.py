"""Build usable / excluded spans from tainted + OI + modality coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..timeparse import format_utc_z
from . import (
    INTERVAL_HARD_LOCAL_GAP,
    INTERVAL_OI_STALE,
    INTERVAL_TAINTED_SEQUENCE_GAP,
    MAX_LOCAL_EXCLUSION_DURATION_SECONDS,
    VERDICT_COMPLETE,
    VERDICT_NOT_COMPLETE,
    VERDICT_USABLE,
)


def _p(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_spans(
    *,
    start: datetime,
    end: datetime,
    tainted_intervals: list[dict[str, Any]],
    oi_stale_intervals: list[dict[str, Any]] | None = None,
    max_local_exclusion_s: int = MAX_LOCAL_EXCLUSION_DURATION_SECONDS,
    min_usable_span_seconds: int = 60,
    feature_warmup_seconds: int = 1800,
    public_trades_ok: bool = True,
    liquidations_ok: bool = True,
) -> dict[str, Any]:
    """Subtract Full-OB tainted intervals from [start,end) to form usable spans.

    OI stale intervals are recorded as soft exclusions (do not split Full-OB usable
    spans by default — OI is optional context). They appear in excluded_spans with
    reason OI_STALE for reporting.
    """
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)

    excluded: list[dict[str, Any]] = []
    for t in tainted_intervals:
        ts, te = _p(t["start"]), _p(t["end"])
        if ts is None or te is None or te <= ts:
            continue
        dur = (te - ts).total_seconds()
        if dur <= max_local_exclusion_s:
            reason = INTERVAL_TAINTED_SEQUENCE_GAP
            hard = False
        else:
            reason = INTERVAL_HARD_LOCAL_GAP
            hard = True
        excluded.append(
            {
                "span_id": f"ex_ob_{format_utc_z(ts)}",
                "start": format_utc_z(ts),
                "end": format_utc_z(te),
                "duration_seconds": dur,
                "reason": reason,
                "hard_local_gap": hard,
                "tainted_duration_ms": t.get("tainted_duration_ms"),
                "n_source_gaps": t.get("n_source_gaps"),
            }
        )

    for oi in oi_stale_intervals or []:
        ts, te = _p(oi["start"]), _p(oi["end"])
        if ts is None or te is None:
            continue
        excluded.append(
            {
                "span_id": f"ex_oi_{format_utc_z(ts)}",
                "start": format_utc_z(ts),
                "end": format_utc_z(te),
                "duration_seconds": float(oi.get("duration_seconds") or (te - ts).total_seconds()),
                "reason": INTERVAL_OI_STALE,
                "hard_local_gap": False,
                "oi_optional": True,
            }
        )

    # Usable = window minus Full-OB tainted only
    cuts: list[tuple[datetime, datetime]] = []
    cursor = start
    ob_ex = sorted(
        [e for e in excluded if e["reason"] in {INTERVAL_TAINTED_SEQUENCE_GAP, INTERVAL_HARD_LOCAL_GAP}],
        key=lambda e: e["start"],
    )
    for e in ob_ex:
        es, ee = _p(e["start"]), _p(e["end"])
        assert es and ee
        if cursor < es:
            cuts.append((cursor, es))
        cursor = max(cursor, ee)
    if cursor < end:
        cuts.append((cursor, end))

    usable: list[dict[str, Any]] = []
    for i, (a, b) in enumerate(cuts):
        dur = (b - a).total_seconds()
        if dur < min_usable_span_seconds:
            excluded.append(
                {
                    "span_id": f"ex_tiny_{format_utc_z(a)}",
                    "start": format_utc_z(a),
                    "end": format_utc_z(b),
                    "duration_seconds": dur,
                    "reason": "TOO_SHORT_AFTER_EXCLUSION",
                    "hard_local_gap": False,
                }
            )
            continue
        warmup_start = a  # baseline must not cross gap — warmup confined inside span
        # analysis eligible after warmup inside span (or span start if shorter)
        eligible = a + timedelta(seconds=min(feature_warmup_seconds, max(0, int(dur) - 1)))
        if eligible >= b:
            # span shorter than warmup — still usable for non-AVR stages if marked
            eligible = a
            flags = ["WARMUP_INCOMPLETE_IN_SPAN"]
        else:
            flags = []
        usable.append(
            {
                "span_id": f"span_{i:03d}_{format_utc_z(a)}",
                "start": format_utc_z(a),
                "end": format_utc_z(b),
                "duration_seconds": dur,
                "replay_anchor": format_utc_z(a),
                "replay_chain_valid": True,
                "full_ob_valid": True,
                "price_valid": True,
                "public_trades_valid": public_trades_ok,
                "oi_valid_or_optional": True,
                "liquidations_source_valid": liquidations_ok,
                "feature_warmup_start": format_utc_z(warmup_start),
                "analysis_eligible_start": format_utc_z(eligible),
                "quality_flags": flags,
            }
        )

    total_ex_ob = sum(
        float(e["duration_seconds"])
        for e in excluded
        if e["reason"] in {INTERVAL_TAINTED_SEQUENCE_GAP, INTERVAL_HARD_LOCAL_GAP}
    )
    window_s = (end - start).total_seconds()
    usable_s = sum(float(u["duration_seconds"]) for u in usable)

    if not usable:
        verdict = VERDICT_NOT_COMPLETE
    elif total_ex_ob <= 0 and usable_s >= window_s - 1e-6:
        verdict = VERDICT_COMPLETE
    else:
        verdict = VERDICT_USABLE

    return {
        "verdict": verdict,
        "usable_spans": usable,
        "excluded_spans": excluded,
        "window_duration_seconds": window_s,
        "usable_duration_seconds": usable_s,
        "excluded_ob_duration_seconds": total_ex_ob,
        "n_usable_spans": len(usable),
        "n_excluded_spans": len(excluded),
        "max_local_exclusion_duration_seconds": max_local_exclusion_s,
    }


def window_inside_usable_span(
    start: datetime,
    end: datetime,
    spans: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the usable span that fully contains [start, end), else None."""
    a0 = start.astimezone(timezone.utc)
    b0 = end.astimezone(timezone.utc)
    for s in spans:
        a, b = _p(s["start"]), _p(s["end"])
        if a is None or b is None:
            continue
        if a <= a0 and b0 <= b:
            return s
    return None


def point_in_spans(ts: datetime, spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    t = ts.astimezone(timezone.utc)
    for s in spans:
        a, b = _p(s["start"]), _p(s["end"])
        if a is None or b is None:
            continue
        if a <= t < b:
            return s
    return None

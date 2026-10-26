"""Read-only timestamp identity matrix for manual reference cases."""

from __future__ import annotations

from typing import Any

# Sources actually available in-repo for Phase 1D (no screenshots on disk).
SOURCES_CHECKED = [
    "Signal_definierung.md",
    "reports/DOGEUSDT_5M_PD1_DEPTH2_LANDSCAPE_PARITY_AUDIT_V1.md",
    "reports/DOGEUSDT_PHASE1B_5M_PD1_DEPTH2_LANDSCAPE_PILOT_V1.md",
    "reports/DOGEUSDT_PHASE1C_DEPTH2_MANUAL_CASE_PARITY_V1.md",
    "results/phase1c_depth2_manual_case_parity_v1/",
    "agent_transcripts (keyword search only)",
]

FROZEN_PRIOR: dict[str, dict[str, Any]] = {
    "S1": {
        "must_not_change": True,
        "as_of_utc": "2026-09-15T01:45:00Z",
        "depth2_at_hint": "NOT_COMPATIBLE_WITH_DEPTH2",
        "note": "Depth-1 at hint; nearest Depth-2 upper 0.08386–0.08389",
    },
    "S5": {
        "must_not_change": True,
        "as_of_utc": "2026-09-13T02:45:00Z",
        "depth2_upper": "0.08506–0.08508 depth 3",
        "parity": True,
    },
    "N3": {
        "must_not_change": True,
        "status": "NEEDS_TIMESTAMP_VERIFICATION",
    },
}

CASE_TIMESTAMP_MATRIX: list[dict[str, Any]] = [
    {
        "case_id": "N3",
        "direction": "no_short",
        "fallbuch": "Chart-local ~04:20–04:30; screenshot image(20261022-091843).png cited",
        "screenshot_in_repo": False,
        "go_to_metadata_found": False,
        "candle_price_match_attempted": False,
        "identity_verdict": "NEEDS_TIMESTAMP_VERIFICATION",
        "rationale": (
            "Screenshot file not present in workspace; filename timestamp is capture-time "
            "(2026-10-22 09:18:43 local), not chart history. No UTC date in fallbuch. "
            "Profitable later move excluded as identity proof per charter."
        ),
    },
    {
        "case_id": "N2",
        "direction": "no_short",
        "fallbuch": "image(20261022-083227).png; UTC date unverified",
        "screenshot_in_repo": False,
        "identity_verdict": "NEEDS_TIMESTAMP_VERIFICATION",
        "rationale": "No calendar anchor in fallbuch; screenshot absent.",
    },
    {
        "case_id": "N4",
        "direction": "no_short",
        "fallbuch": "image(20261022-092447).png; documented 22.10.2026 only",
        "screenshot_in_repo": False,
        "identity_verdict": "NEEDS_TIMESTAMP_VERIFICATION",
        "rationale": "Documentation date is write-date, not market as-of.",
    },
    {
        "case_id": "N5",
        "direction": "no_short",
        "fallbuch": "~15:30 chart time; paired with S7 ~16:45; no calendar date",
        "screenshot_in_repo": False,
        "identity_verdict": "NEEDS_TIMESTAMP_VERIFICATION",
        "rationale": "Intraday clock without historical UTC day.",
    },
    {
        "case_id": "S3",
        "direction": "short",
        "fallbuch": "2026-09-16 ca. 11:40–12:00 UTC; entry ~0.080247",
        "screenshot_in_repo": False,
        "identity_verdict": "VERIFIED_WINDOW",
        "canonical_as_of_utc": "2026-09-16T11:45:00Z",
        "rationale": (
            "Fallbuch gives UTC calendar day and 20m window; 5m grid anchor = "
            "last close in window at :45 (same convention as S1/S5 Phase 1C). "
            "Depth-2 policy: documented NOT_COMPATIBLE_WITH_DEPTH2 (Depth-1 micro)."
        ),
        "price_hint": 0.080247,
    },
    {
        "case_id": "N1",
        "direction": "no_long",
        "fallbuch": "2026-09-15 evening ca. 21–22 UTC",
        "screenshot_in_repo": False,
        "identity_verdict": "VERIFIED_WINDOW",
        "canonical_as_of_utc": "2026-09-15T21:45:00Z",
        "rationale": (
            "Calendar day + 1h window in fallbuch; mid-window 5m close at 21:45. "
            "Rejected trade (no long), not short — usable for landscape-only contrast."
        ),
        "price_hint": None,
    },
    {
        "case_id": "S5",
        "direction": "short_accepted",
        "fallbuch": "2026-09-13 ca. 02:30–03:00",
        "identity_verdict": "VERIFIED",
        "canonical_as_of_utc": "2026-09-13T02:45:00Z",
        "rationale": "Phase 1C SSOT probe + parity; frozen.",
        "price_hint": 0.085071,
    },
]


def build_timestamp_resolution_report() -> dict[str, Any]:
    return {
        "sources_checked": SOURCES_CHECKED,
        "frozen_prior_findings": FROZEN_PRIOR,
        "cases": CASE_TIMESTAMP_MATRIX,
        "n3_summary": next(c for c in CASE_TIMESTAMP_MATRIX if c["case_id"] == "N3"),
        "verified_rejected_short_available": False,
        "verified_rejected_short_note": (
            "No N1–N5 short-rejection case has both calendar UTC and SSOT probe "
            "in this pilot. N3/N4/N5 remain unanchored."
        ),
        "new_ssot_probes_planned": ["S3", "N1"],
        "reuse_without_reprobe": ["S5"],
    }

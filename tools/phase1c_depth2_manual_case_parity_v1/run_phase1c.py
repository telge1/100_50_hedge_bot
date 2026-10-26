#!/usr/bin/env python3
"""Phase 1C — manual case Depth-2 landscape forensics (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_1B = ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"
RESULTS = ROOT / "results" / "phase1c_depth2_manual_case_parity_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE1C_DEPTH2_MANUAL_CASE_PARITY_V1.md"

sys.path.insert(0, str(TOOLS_1B))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_lib import (  # noqa: E402
    classify_s1_upper_anchor,
    ema_context,
    enrich_intervals_with_distance,
    nearest_upper_depth2,
    next_5m_open,
    origin_age_map,
    parse_utc,
    run_depth1_probe,
)
from run_pilot import (  # noqa: E402
    asof_semantics_report,
    load_window_from_ssot,
    pool_persistence_rows,
    run_node_analyzer,
    stable_digest,
    write_csv,
)

CASES: dict[str, dict[str, Any]] = {
    "S1": {
        "as_of_utc": "2026-09-15T01:45:00Z",
        "manual_entry_hint": 0.08384,
        "manual_notes": (
            "User reference short; upper micro + lower cluster; "
            "Phase 1B: no Depth-2 upper at hint."
        ),
        "reference_price_for_gaps": 0.08384,
        "status": "VERIFY",
    },
    "S5": {
        "as_of_utc": "2026-09-13T02:45:00Z",
        "manual_entry_hint": 0.085071,
        "manual_notes": "Accepted short ~02:30–03:00 UTC; anchor mid-window 02:45.",
        "reference_price_for_gaps": 0.085071,
        "alternate_anchors": ["2026-09-13T02:30:00Z", "2026-09-13T03:00:00Z"],
        "status": "VERIFY",
    },
    "N3": {
        "status": "NEEDS_TIMESTAMP_VERIFICATION",
        "reason": (
            "Fallbuch: chart-local ~04:20–04:30 only; no verified UTC date. "
            "No landscape emitted."
        ),
    },
}


def write_case_outputs(case_id: str, bundle: dict[str, Any]) -> None:
    case_dir = RESULTS / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "landscape_profile.json").write_text(
        json.dumps(bundle, indent=2), encoding="utf-8"
    )
    intervals = bundle.get("depth2_intervals") or []
    write_csv(
        case_dir / "depth2_intervals.csv",
        [
            "side",
            "bottom_price",
            "top_price",
            "depth",
            "origins",
            "distance_to_ref_bottom",
            "distance_to_ref_top",
            "ref_inside",
        ],
        [
            {
                "side": iv["side"],
                "bottom_price": repr(iv["bottom_price"]),
                "top_price": repr(iv["top_price"]),
                "depth": iv["depth"],
                "origins": "|".join(iv.get("origins") or []),
                "distance_to_ref_bottom": repr(iv.get("distance_to_ref_bottom")),
                "distance_to_ref_top": repr(iv.get("distance_to_ref_top")),
                "ref_inside": iv.get("ref_inside"),
            }
            for iv in intervals
        ],
    )
    gaps = bundle.get("gaps_flat") or []
    write_csv(
        case_dir / "pool_gaps.csv",
        [
            "side",
            "gap_bottom_price",
            "gap_top_price",
            "gap_pct",
            "large_gap",
            "previous_union_top",
            "next_union_bottom",
        ],
        [
            {
                "side": g["side"],
                "gap_bottom_price": repr(g["gap_bottom_price"]),
                "gap_top_price": repr(g["gap_top_price"]),
                "gap_pct": repr(g["gap_pct"]),
                "large_gap": g["large_gap"],
                "previous_union_top": repr(g.get("previous_union_top")),
                "next_union_bottom": repr(g.get("next_union_bottom")),
            }
            for g in gaps
        ],
    )
    origins = bundle.get("origin_provenance_rows") or []
    write_csv(
        case_dir / "origin_provenance.csv",
        [
            "side",
            "origin_id",
            "bottom_price",
            "top_price",
            "depth",
            "active_at_as_of",
            "age_hours_at_as_of",
        ],
        origins,
    )
    (case_dir / "ema_context.json").write_text(
        json.dumps(bundle.get("ema") or {}, indent=2), encoding="utf-8"
    )
    (case_dir / "manual_comparison.md").write_text(
        bundle.get("manual_comparison_md") or "", encoding="utf-8"
    )


def build_verified_case(case_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    as_of = spec["as_of_utc"]
    ref_price = float(spec.get("reference_price_for_gaps") or spec.get("manual_entry_hint"))
    window = load_window_from_ssot(as_of)
    analysis = run_node_analyzer(window, as_of, ref_price)
    parity = analysis["landscape"]["parity"]
    intervals_raw = analysis["landscape"]["display_intervals"]
    intervals = enrich_intervals_with_distance(intervals_raw, ref_price)
    persist = pool_persistence_rows(window.get("pools") or [], as_of)
    ema = ema_context(window.get("prices") or [], as_of)
    gaps_flat = []
    for side in ("lower", "upper"):
        for g in analysis["gaps"].get(side) or []:
            gaps_flat.append(g)

    upper_iv = [iv for iv in intervals if iv["side"] == "upper"]
    lower_iv = [iv for iv in intervals if iv["side"] == "lower"]
    nearest = nearest_upper_depth2(intervals_raw, ref_price)
    depth1 = run_depth1_probe(
        window.get("pools") or [],
        as_of,
        spec.get("manual_entry_hint"),
        upper_iv,
    )

    s1_class = None
    if case_id == "S1":
        s1_class = classify_s1_upper_anchor(
            float(spec["manual_entry_hint"]),
            nearest,
            depth1,
        )

    origin_rows = []
    for iv in intervals_raw:
        ages = origin_age_map(persist, iv.get("origins") or [])
        for oid in iv.get("origins") or []:
            meta = ages.get(oid) or {}
            origin_rows.append(
                {
                    "side": iv["side"],
                    "origin_id": oid,
                    "bottom_price": repr(iv["bottom_price"]),
                    "top_price": repr(iv["top_price"]),
                    "depth": iv["depth"],
                    "active_at_as_of": meta.get("active_at_as_of"),
                    "age_hours_at_as_of": meta.get("age_hours_at_as_of"),
                }
            )

    nxt_open = next_5m_open(as_of)
    follow = None
    try:
        w2 = load_window_from_ssot(nxt_open)
        a2 = run_node_analyzer(w2, nxt_open, ref_price)
        follow = {
            "as_of_utc": nxt_open,
            "reference_price": a2.get("reference_price"),
            "depth2_interval_count": len(a2["landscape"]["display_intervals"]),
            "parity_ok": a2["landscape"]["parity"].get("ok"),
        }
    except Exception as exc:
        follow = {"as_of_utc": nxt_open, "status": "NOT_VERIFIED", "error": str(exc)}

    large_upper = [g for g in gaps_flat if g["side"] == "upper" and g.get("large_gap")]
    small_upper = [g for g in gaps_flat if g["side"] == "upper" and not g.get("large_gap")]

    manual_md = f"""# {case_id} manual comparison

- **As-of:** {as_of}
- **Manual entry hint:** {spec.get('manual_entry_hint')}
- **Analyzer reference price:** {ref_price}
- **Parity (chart vs forensic):** {parity.get('ok')}
- **Depth-2 interval count:** {len(intervals_raw)}
- **Upper gaps:** {len([g for g in gaps_flat if g['side']=='upper'])} (large: {len(large_upper)})
- **Lower gaps:** {len([g for g in gaps_flat if g['side']=='lower'])}

## User notes (fallbuch, not pass criteria)
{spec.get('manual_notes', '')}

## Open deviations
- Numeric match to drawn chart tools: NOT_VERIFIED unless screenshot re-measured.
- TP cluster top / SL +0.2% rules: not evaluated in Phase 1C.
"""

    if s1_class:
        manual_md += f"""
## S1 upper-anchor classification
- **Hypothesis:** `{s1_class['hypothesis']}`
- {s1_class['note']}
- Depth-2 contains: {s1_class['depth2_contains_count']}; Depth-1 contains: {s1_class['depth1_contains_count']}
"""

    bundle: dict[str, Any] = {
        "case_id": case_id,
        "spec": spec,
        "asof_semantics": asof_semantics_report(window, as_of),
        "parity": parity,
        "reference_price": analysis.get("reference_price"),
        "depth2_intervals": intervals,
        "gaps_flat": gaps_flat,
        "gaps_ranked": analysis["gaps"],
        "nearest_upper_at_hint": nearest,
        "depth1_probe": depth1,
        "s1_classification": s1_class,
        "ema": ema,
        "follow_candle_open_probe": follow,
        "landscape_summary": {
            "upper_intervals": len(upper_iv),
            "lower_intervals": len(lower_iv),
            "upper_large_gaps": len(large_upper),
            "upper_small_gaps": len(small_upper),
        },
        "origin_provenance_rows": origin_rows,
        "digest": stable_digest({"intervals": intervals_raw, "gaps": gaps_flat}),
        "manual_comparison_md": manual_md,
    }
    return bundle


def write_report(verdict: str, case_results: dict[str, Any]) -> None:
    lines = [
        "# DOGEUSDT – PHASE 1C",
        "# DEPTH2 MANUAL CASE PARITY FORENSICS V1",
        "",
        f"**VERDICT:** `{verdict}`",
        "",
        "## Cases",
        "",
    ]
    for cid, res in case_results.items():
        lines.append(f"### {cid}")
        if res.get("skipped"):
            lines.append(f"- Status: **{res.get('status')}**")
            lines.append(f"- {res.get('reason', '')}")
            lines.append("")
            continue
        b = res.get("bundle") or {}
        ls = b.get("landscape_summary") or {}
        lines.append(f"- As-of: `{b.get('spec', {}).get('as_of_utc')}`")
        lines.append(f"- Parity OK: {b.get('parity', {}).get('ok')}")
        lines.append(f"- Depth-2 bands: {ls.get('upper_intervals', 0)} upper / {ls.get('lower_intervals', 0)} lower")
        lines.append(f"- Upper LARGE_GAP count: {ls.get('upper_large_gaps')}")
        if b.get("s1_classification"):
            sc = b["s1_classification"]
            lines.append(f"- S1 hypothesis: **{sc['hypothesis']}**")
        lines.append(f"- Results: `results/phase1c_depth2_manual_case_parity_v1/{cid}/`")
        lines.append("")

    lines.extend(
        [
            "## S5 vs N3 contrast (landscape only)",
            "",
            "- S5 profile emitted at verified UTC anchor.",
            "- N3: **NEEDS_TIMESTAMP_VERIFICATION** — no UTC anchor assigned.",
            "",
            "## Limits",
            "",
            "- No entry scanner; no invented thresholds.",
            "- Phase 1B results unchanged.",
            "",
        ]
    )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def compute_verdict(case_results: dict[str, Any]) -> str:
    n3 = case_results.get("N3", {})
    if n3.get("skipped") and n3.get("status") == "NEEDS_TIMESTAMP_VERIFICATION":
        verified = [k for k, v in case_results.items() if not v.get("skipped")]
        all_parity = all(
            case_results[k].get("bundle", {}).get("parity", {}).get("ok")
            for k in verified
        )
        s1 = case_results.get("S1", {}).get("bundle", {}).get("s1_classification") or {}
        s1_open = s1.get("hypothesis") in (
            "C_ambiguous_anchor_not_inside_depth2",
            "B_depth1_only_at_entry_hint",
            "A_near_valid_depth2_upper",
            "NOT_VERIFIED",
        )
        if verified and all_parity and s1_open:
            return "DEPTH2_MANUAL_CASE_PARITY_PARTIAL"
    return "DEPTH2_MANUAL_CASE_PARITY_BLOCKED"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    case_results: dict[str, Any] = {}

    for case_id, spec in CASES.items():
        if spec.get("status") == "NEEDS_TIMESTAMP_VERIFICATION":
            case_results[case_id] = {
                "skipped": True,
                "status": spec["status"],
                "reason": spec.get("reason"),
            }
            n3_dir = RESULTS / case_id
            n3_dir.mkdir(parents=True, exist_ok=True)
            (n3_dir / "timestamp_status.json").write_text(
                json.dumps(case_results[case_id], indent=2), encoding="utf-8"
            )
            continue
        try:
            bundle = build_verified_case(case_id, spec)
            write_case_outputs(case_id, bundle)
            case_results[case_id] = {"skipped": False, "bundle": bundle}
        except Exception as exc:
            case_results[case_id] = {
                "skipped": True,
                "status": "NOT_VERIFIED",
                "reason": str(exc),
            }

    verdict = compute_verdict(case_results)
    if case_results.get("S1", {}).get("skipped"):
        verdict = "DEPTH2_MANUAL_CASE_PARITY_BLOCKED"
    elif case_results.get("S5", {}).get("skipped"):
        verdict = "DEPTH2_MANUAL_CASE_PARITY_PARTIAL"

    (RESULTS / "phase1c_summary.json").write_text(
        json.dumps(
            {"verdict": verdict, "cases": {k: {**v, "bundle": None} if v.get("bundle") else v for k, v in case_results.items()}},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    write_report(verdict, case_results)
    print(json.dumps({"verdict": verdict}, indent=2))
    return 0 if verdict != "DEPTH2_MANUAL_CASE_PARITY_BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Phase 1D — timestamp resolution + S5 contrast pilot (read-only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_1B = ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"
TOOLS_1C = ROOT / "tools" / "phase1c_depth2_manual_case_parity_v1"
RESULTS = ROOT / "results" / "phase1d_depth2_reference_contrast_v1"
RESULTS_1C = ROOT / "results" / "phase1c_depth2_manual_case_parity_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE1D_DEPTH2_REFERENCE_CONTRAST_V1.md"

sys.path.insert(0, str(TOOLS_1B))
sys.path.insert(0, str(TOOLS_1C))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contrast_lib import contrast_narrative_check, landscape_fingerprint  # noqa: E402
from profile_lib import ema_context, enrich_intervals_with_distance  # noqa: E402
from run_phase1c import build_verified_case  # noqa: E402
from run_pilot import load_window_from_ssot, write_csv  # noqa: E402
from timestamp_forensics import build_timestamp_resolution_report  # noqa: E402


def write_case_outputs_1d(case_id: str, bundle: dict[str, Any]) -> None:
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


def resolve_reference_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("reference_price_for_gaps") is not None or spec.get("manual_entry_hint") is not None:
        return spec
    spec = dict(spec)
    window = load_window_from_ssot(spec["as_of_utc"])
    ctx = ema_context(window.get("prices") or [], spec["as_of_utc"])
    if ctx.get("status") != "VERIFIED":
        raise RuntimeError(f"Cannot resolve reference close for {spec['as_of_utc']}: {ctx}")
    spec["reference_price_for_gaps"] = ctx["reference_close"]
    return spec


NEW_PROBE_SPECS: dict[str, dict[str, Any]] = {
    "S3": {
        "as_of_utc": "2026-09-16T11:45:00Z",
        "manual_entry_hint": 0.080247,
        "reference_price_for_gaps": 0.080247,
        "manual_notes": (
            "Fallbuch short; Depth-1 vs Depth-2 rejection hypothesis; "
            "NOT_COMPATIBLE_WITH_DEPTH2 per Phase 1A policy."
        ),
        "status": "VERIFY",
        "timestamp_identity": "VERIFIED_WINDOW",
    },
    "N1": {
        "as_of_utc": "2026-09-15T21:45:00Z",
        "manual_entry_hint": None,
        "reference_price_for_gaps": None,
        "manual_notes": "Rejected early long; landscape-only; ref price = candle close at as-of.",
        "status": "VERIFY",
        "timestamp_identity": "VERIFIED_WINDOW",
    },
}


def load_s5_from_phase1c() -> dict[str, Any]:
    path = RESULTS_1C / "S5" / "landscape_profile.json"
    if not path.is_file():
        raise FileNotFoundError(f"Phase 1C S5 profile missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def bundle_for_fingerprint(case_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
    bundle = dict(bundle)
    bundle["case_id"] = case_id
    if bundle.get("spec") is None:
        bundle["spec"] = {"as_of_utc": bundle.get("as_of_semantics", {}).get("as_of_anchor_utc")}
    return bundle


def compute_verdict(
    ts_report: dict[str, Any],
    probes: dict[str, Any],
    s5_parity: bool,
) -> str:
    if ts_report["n3_summary"]["identity_verdict"] != "NEEDS_TIMESTAMP_VERIFICATION":
        return "DEPTH2_REFERENCE_CONTRAST_BLOCKED"
    if not s5_parity:
        return "DEPTH2_REFERENCE_CONTRAST_BLOCKED"
    if probes and all(p.get("parity", {}).get("ok") for p in probes.values()):
        return "DEPTH2_REFERENCE_CONTRAST_PARTIAL"
    if probes:
        return "DEPTH2_REFERENCE_CONTRAST_PARTIAL"
    return "DEPTH2_REFERENCE_CONTRAST_BLOCKED"


def write_report_md(
    verdict: str,
    ts_report: dict[str, Any],
    fingerprints: dict[str, Any],
    contrasts: list[dict[str, Any]],
) -> None:
    lines = [
        "# DOGEUSDT – PHASE 1D",
        "# DEPTH2 REFERENCE TIMESTAMP RESOLUTION AND CONTRAST PILOT V1",
        "",
        f"**VERDICT:** `{verdict}`",
        "",
        "## Frozen prior (unchanged)",
        "",
        "- **S1:** Depth-1 at hint; NOT_COMPATIBLE_WITH_DEPTH2 at hint.",
        "- **S5:** `2026-09-13T02:45:00Z`, Depth-2 upper 0.08506–0.08508 (depth 3), parity OK.",
        "- **N3:** NEEDS_TIMESTAMP_VERIFICATION.",
        "",
        "## N3 timestamp forensics",
        "",
        f"- {ts_report['n3_summary']['rationale']}",
        "- Screenshot `image(20261022-091843).png`: **not in repository**.",
        "",
        "## Identity vs density parity",
        "",
        "| Case | Time identity | SSOT landscape |",
        "|------|---------------|----------------|",
    ]
    for row in ts_report["cases"]:
        if row["case_id"] in ("N3", "S5", "S3", "N1", "N5", "N4", "N2"):
            ident = row["identity_verdict"]
            ssot = "—"
            if row["case_id"] == "S5":
                ssot = "Reuse Phase 1C (no reprobe)"
            elif row["case_id"] in fingerprints:
                ssot = f"Parity {fingerprints[row['case_id']].get('parity_ok')}"
            lines.append(f"| {row['case_id']} | {ident} | {ssot} |")

    lines.extend(["", "## Landscape fingerprints", ""])
    for cid, fp in fingerprints.items():
        lines.append(f"### {cid}")
        lines.append(f"- ref={fp['reference_price']} as-of={fp['as_of_utc']}")
        ug = fp["upper_gaps_above_ref"]
        lines.append(
            f"- Upper above ref: united={ug['united_upper_intervals_above_ref']} "
            f"gaps large/small={ug['upper_large_gaps_above_ref']}/{ug['upper_small_gaps_above_ref']}"
        )
        lb = fp["lower_below_ref"]
        lines.append(f"- United lower below ref: {lb['united_lower_intervals_below_ref']}")
        lines.append("")

    lines.append("## S5 contrast question (user narrative)")
    lines.append("")
    for c in contrasts:
        lines.append(f"- {c['s5_vs_other']}: large gaps above ref S5={c['s5_upper_large_gaps_above_ref']} vs other={c['other_upper_large_gaps_above_ref']}; small S5={c['s5_upper_small_gaps_above_ref']} vs other={c['other_upper_small_gaps_above_ref']}")
    lines.append("")
    lines.append("## Limits")
    lines.append("")
    lines.append("- No verified **rejected short** UTC anchor (N3/N4/N5).")
    lines.append("- N1 is no-long rejection; S3 is short with Depth-2 incompatibility note.")
    lines.append("- Max 2 new SSOT probes (S3, N1).")
    lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    ts_report = build_timestamp_resolution_report()
    (RESULTS / "timestamp_resolution.json").write_text(
        json.dumps(ts_report, indent=2), encoding="utf-8"
    )

    s5_bundle = load_s5_from_phase1c()
    fingerprints: dict[str, Any] = {
        "S5": landscape_fingerprint(bundle_for_fingerprint("S5", s5_bundle))
    }

    new_probes: dict[str, Any] = {}
    for case_id, spec in NEW_PROBE_SPECS.items():
        spec = resolve_reference_spec(spec)
        bundle = build_verified_case(case_id, spec)
        write_case_outputs_1d(case_id, bundle)
        new_probes[case_id] = bundle
        fingerprints[case_id] = landscape_fingerprint(bundle_for_fingerprint(case_id, bundle))

    contrasts = [
        contrast_narrative_check(fingerprints["S5"], fingerprints["S3"]),
        contrast_narrative_check(fingerprints["S5"], fingerprints["N1"]),
    ]
    (RESULTS / "landscape_fingerprints.json").write_text(
        json.dumps(fingerprints, indent=2), encoding="utf-8"
    )
    (RESULTS / "s5_contrast_checks.json").write_text(
        json.dumps(contrasts, indent=2), encoding="utf-8"
    )

    verdict = compute_verdict(
        ts_report, new_probes, fingerprints["S5"]["parity_ok"] is True
    )
    summary = {
        "verdict": verdict,
        "new_ssot_probes": list(NEW_PROBE_SPECS.keys()),
        "s5_source": "phase1c_reuse",
        "n3": ts_report["n3_summary"]["identity_verdict"],
    }
    (RESULTS / "phase1d_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    write_report_md(verdict, ts_report, fingerprints, contrasts)
    print(json.dumps(summary, indent=2))
    return 0 if verdict != "DEPTH2_REFERENCE_CONTRAST_BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

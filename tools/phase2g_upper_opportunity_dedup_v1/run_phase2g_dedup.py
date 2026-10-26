#!/usr/bin/env python3
"""Phase 2G — upper-pool opportunity deduplication (read-only research)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
RESULTS = ROOT / "results" / "phase2g_upper_opportunity_dedup_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE2G_UPPER_OPPORTUNITY_DEDUP_V1.md"
DEDUP_JS = Path(__file__).resolve().parent / "opportunity_dedup.js"
SCAN_TO = "2026-09-13T02:45:00Z"

GROUND_TRUTH = {
    "group": ["C010", "C011", "C013"],
    "primary": "C010",
    "duplicates": ["C011", "C013"],
    "source": "user_chart_confirmation_phase2g",
}

sys.path.insert(0, str(ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"))
from run_pilot import load_window_from_ssot  # noqa: E402


def load_phase2a_candidates() -> list[dict[str, Any]]:
    data = json.loads((PHASE2A / "candidates.json").read_text(encoding="utf-8"))
    cands = data.get("candidates") or []
    for c in cands:
        cid = c["candidate_id"]
        prof = PHASE2A / "candidate_landscape_profiles" / f"{cid}.json"
        if prof.is_file():
            p = json.loads(prof.read_text(encoding="utf-8"))
            cand = p.get("candidate") or {}
            c["upper_pool_origins"] = cand.get("upper_pool_origins") or []
    return cands


def run_dedup(window: dict[str, Any], candidates: list[dict]) -> dict[str, Any]:
    payload = {
        "pools": window.get("pools") or [],
        "adapter_path": None,
        "candidates": candidates,
        "min_pool_age_bars": 5,
        "min_pool_gap_pct": 0.5,
    }
    proc = subprocess.run(
        ["node", str(DEDUP_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(DEDUP_JS.parent),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "dedup failed")
    return json.loads(proc.stdout)


def candidate_key(cid: str) -> str:
    return cid.split("_", 1)[0]


def write_forensics(result: dict[str, Any]) -> str:
    by_key = {candidate_key(r["candidate_id"]): r for r in result["assignments"]}
    lines = [
        "# C010 / C011 / C013 — opportunity identity forensics",
        "",
        "## Manual ground truth (not used by algorithm)",
        "",
        f"- Expected group: {', '.join(GROUND_TRUTH['group'])}",
        f"- PRIMARY: {GROUND_TRUTH['primary']}",
        f"- Duplicates: {', '.join(GROUND_TRUTH['duplicates'])}",
        "",
        "## Automatic grouping result",
        "",
    ]
    opp_ids = {k: by_key[k]["opportunity_id"] for k in GROUND_TRUTH["group"] if k in by_key}
    same_opp = len(set(opp_ids.values())) == 1 if opp_ids else False
    primary_ok = (
        by_key.get("C010", {}).get("dedup_reason") == "PRIMARY"
        if "C010" in by_key
        else False
    )
    lines.append(f"- Same `opportunity_id` for all three: **{same_opp}**")
    if opp_ids:
        lines.append(f"- opportunity_ids: `{json.dumps(opp_ids)}`")
    lines.append(f"- C010 is PRIMARY: **{primary_ok}**")
    lines.append("")

    for key in GROUND_TRUTH["group"]:
        r = by_key.get(key)
        if not r:
            lines.append(f"### {key} — missing")
            continue
        lines.append(f"### {key} — `{r['candidate_id']}`")
        lines.append(f"- signal: `{r['signal_close_utc']}`")
        lines.append(
            f"- zone @ signal: [{r['upper_zone_lower']}, {r['upper_zone_upper']}]"
        )
        lines.append(f"- opportunity_id: `{r['opportunity_id']}`")
        lines.append(f"- dedup_reason: `{r['dedup_reason']}`")
        lines.append(f"- continuity: `{r['pool_continuity_status']}`")
        lines.append(f"- identity_confidence: `{r['identity_confidence']}`")
        lines.append(f"- shared_origin_ids (vs opp anchor): {r['shared_origin_ids']}")
        lines.append(f"- price_overlap_score: {r['price_overlap_score']}")
        lines.append(f"- link_evidence: `{json.dumps(r.get('link_evidence', {}))}`")
        lines.append("")

    timeline = [
        e
        for e in result.get("opportunity_timeline", [])
        if any(
            candidate_key(e.get("candidate_id", "")) in GROUND_TRUTH["group"]
            for _ in [0]
        )
        and candidate_key(e.get("candidate_id", "")) in GROUND_TRUTH["group"]
    ]
    lines.append("## Timeline events (reference trio)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(timeline, indent=2))
    lines.append("```")
    lines.append("")

    if same_opp and primary_ok:
        lines.append("## Verdict (reference case)")
        lines.append("")
        lines.append("Automatic method **reconstructs** user grouping for C010/C011/C013.")
    else:
        lines.append("## Verdict (reference case)")
        lines.append("")
        lines.append(
            "Automatic method **does not** fully match ground truth — see diagnosis above; "
            "treat as NOT_VERIFIED for production use without rule refinement."
        )

    text = "\n".join(lines)
    (RESULTS / "C010_C011_C013_forensics.md").write_text(text, encoding="utf-8")
    return text


def verdict(result: dict[str, Any], ref_ok: bool) -> str:
    not_verified = sum(
        1 for r in result["assignments"] if r["identity_confidence"] == "NOT_VERIFIED"
    )
    if not ref_ok:
        return "OPPORTUNITY_DEDUP_PARTIAL"
    if not_verified > 0:
        return "OPPORTUNITY_DEDUP_PARTIAL"
    return "OPPORTUNITY_DEDUP_READY"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    candidates = load_phase2a_candidates()
    window = load_window_from_ssot(SCAN_TO)
    result = run_dedup(window, candidates)

    (RESULTS / "opportunity_identity_timeline.json").write_text(
        json.dumps(
            {
                "version": result.get("version"),
                "timeline": result.get("opportunity_timeline"),
                "grouped_opportunities": result.get("grouped_opportunities"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    assign_fields = [
        "candidate_id",
        "signal_close_utc",
        "entry_open_utc",
        "opportunity_id",
        "first_candidate_id",
        "first_signal_utc",
        "upper_zone_lower",
        "upper_zone_upper",
        "shared_origin_ids",
        "price_overlap_score",
        "pool_continuity_status",
        "duplicate_of_candidate_id",
        "dedup_reason",
        "identity_confidence",
        "manual_label",
    ]
    rows = []
    for r in result["assignments"]:
        row = {k: r.get(k) for k in assign_fields}
        row["shared_origin_ids"] = json.dumps(r.get("shared_origin_ids") or [])
        rows.append(row)

    with (RESULTS / "raw_candidates_with_opportunity_id.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        w = csv.DictWriter(f, fieldnames=assign_fields)
        w.writeheader()
        w.writerows(rows)

    grouped_fields = [
        "opportunity_id",
        "first_candidate_id",
        "first_signal_utc",
        "last_signal_utc",
        "candidate_count",
        "candidate_ids",
        "tracked_zone_lower",
        "tracked_zone_upper",
        "tracked_origin_ids",
    ]
    g_rows = []
    for g in result["grouped_opportunities"]:
        gr = {k: g.get(k) for k in grouped_fields}
        gr["candidate_ids"] = json.dumps(g.get("candidate_ids") or [])
        gr["tracked_origin_ids"] = json.dumps(g.get("tracked_origin_ids") or [])
        g_rows.append(gr)

    with (RESULTS / "grouped_trade_opportunities.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        w = csv.DictWriter(f, fieldnames=grouped_fields)
        w.writeheader()
        w.writerows(g_rows)

    by_key = {candidate_key(r["candidate_id"]): r for r in result["assignments"]}
    ref_opps = {by_key[k]["opportunity_id"] for k in GROUND_TRUTH["group"] if k in by_key}
    ref_ok = (
        len(ref_opps) == 1
        and by_key.get("C010", {}).get("dedup_reason") == "PRIMARY"
        and by_key.get("C011", {}).get("dedup_reason") == "SAME_OPPORTUNITY_DUPLICATE"
        and by_key.get("C013", {}).get("dedup_reason") == "SAME_OPPORTUNITY_DUPLICATE"
    )
    forensics = write_forensics(result)

    raw_n = len(result["assignments"])
    opp_n = len(result["grouped_opportunities"])
    dup_n = sum(
        1 for r in result["assignments"] if r["dedup_reason"] == "SAME_OPPORTUNITY_DUPLICATE"
    )
    nv_n = sum(
        1 for r in result["assignments"] if r["identity_confidence"] == "NOT_VERIFIED"
    )
    final_verdict = verdict(result, ref_ok)

    summary = [
        "# Phase 2G deduplication summary",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        "## Counts",
        f"- Raw scanner candidates: **{raw_n}**",
        f"- Distinct trade opportunities: **{opp_n}**",
        f"- Duplicate raw signals (linked to prior opportunity): **{dup_n}**",
        f"- NOT_VERIFIED identity_confidence rows: **{nv_n}**",
        "",
        "## Reference case C010/C011/C013",
        f"- Auto matches manual ground truth: **{ref_ok}**",
        "",
        "## Architecture (backtester)",
        "- Layer 1: `raw_candidates_with_opportunity_id.csv` — all scanner hits preserved",
        "- Layer 2: `grouped_trade_opportunities.csv` — causal upper-pool groups",
        "- Layer 3: executed trades — future; simulator picks one entry per opportunity",
        "",
        "## Identity basis",
        "- Phase 2C match score (IoU + origin Jaccard) on SSOT landscape bands",
        "- Bar-by-bar band continuity between signals (no time cooldown)",
        "- Origin-lineage fallback when D2 bounds drift but anchor origins persist",
        "- Pool disappearance breaks track → new opportunity",
        "",
        f"Full forensics: `C010_C011_C013_forensics.md`",
    ]
    (RESULTS / "dedup_summary.md").write_text("\n".join(summary), encoding="utf-8")

    report = [
        "# DOGEUSDT Phase 2G — Upper opportunity deduplication V1",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        "## Configuration",
        "- DOGEUSDT · 5m PD1 · min depth 2 · SSOT ACTIVE pools",
        "- Research params: min_pool_age_bars=5, min_pool_gap_pct=0.5 (metadata only)",
        "- Phase 2C identity heuristic extended with inter-signal continuity walk",
        "",
        "## Results",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Raw candidates | {raw_n} |",
        f"| Trade opportunities | {opp_n} |",
        f"| Duplicates | {dup_n} |",
        f"| NOT_VERIFIED | {nv_n} |",
        "",
        "## C010/C011/C013 reference",
        "",
        f"Automatic reconstruction of user grouping: **{'YES' if ref_ok else 'NO / PARTIAL'}**",
        "",
        "Manual ground truth is stored separately in forensics; algorithm is not hardcoded to these IDs.",
        "",
        "## Outputs",
        f"- `{RESULTS.relative_to(ROOT)}/raw_candidates_with_opportunity_id.csv`",
        f"- `{RESULTS.relative_to(ROOT)}/grouped_trade_opportunities.csv`",
        f"- `{RESULTS.relative_to(ROOT)}/opportunity_identity_timeline.json`",
        f"- `{RESULTS.relative_to(ROOT)}/C010_C011_C013_forensics.md`",
        "",
        "## Phase 2C sufficiency",
        "",
        "Phase 2C backward age tracking alone is **not sufficient** for forward deduplication.",
        "2G adds causal inter-signal linking with:",
        "",
        "1. Strict price+origin match (Phase 2C IoU/Jaccard) on `anchorRef` / `linkRef`",
        "2. Bar continuity on matched bands between signals",
        "3. **Origin-lineage fallback** when exact D2 bounds drift but anchor origins persist bar-to-bar",
        "",
        "Reference C010→C013 required mode (3) between C012 and C013 (documented in forensics).",
        "",
    ]
    REPORT.write_text("\n".join(report), encoding="utf-8")

    print(
        json.dumps(
            {
                "verdict": final_verdict,
                "raw": raw_n,
                "opportunities": opp_n,
                "ref_case_ok": ref_ok,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

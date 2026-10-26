#!/usr/bin/env python3
"""Phase 2C — pool age enrichment for Phase 2A candidates (read-only)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_1B = ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
RESULTS = ROOT / "results" / "phase2c_5m_pd1_depth2_candidate_pool_age_v1"
ENRICHER_JS = Path(__file__).resolve().parent / "pool_age_enricher.js"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE2C_5M_PD1_DEPTH2_CANDIDATE_POOL_AGE_V1.md"
SCAN_TO = "2026-09-13T02:45:00Z"

sys.path.insert(0, str(TOOLS_1B))
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


def run_enricher(window: dict[str, Any], candidates: list[dict]) -> dict[str, Any]:
    win = window.get("window") or {}
    payload = {
        "pools": window.get("pools") or [],
        "window": win,
        "context_from_utc": win.get("context_from_utc"),
        "candidates": candidates,
        "config": {"age_gate_bars": 10, "slot_minutes": 5},
    }
    proc = subprocess.run(
        ["node", str(ENRICHER_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(ENRICHER_JS.parent),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "enricher failed")
    return json.loads(proc.stdout)


def row_from_candidate(c: dict[str, Any]) -> dict[str, Any]:
    age = c.get("pool_age") or {}
    return {
        "candidate_id": c["candidate_id"],
        "signal_close_utc": c["signal_close_utc"],
        "entry_open_utc": c["entry_open_utc"],
        "upper_pool_lower": c["upper_pool_lower"],
        "upper_pool_upper": c["upper_pool_upper"],
        "upper_pool_depth": c["upper_pool_depth"],
        "touch_pool_first_seen_utc": age.get("touch_pool_first_seen_utc"),
        "touch_pool_age_bars": age.get("touch_pool_age_bars"),
        "touch_pool_age_minutes": age.get("touch_pool_age_minutes"),
        "eligible_by_age_10bars": age.get("eligible_by_age_10bars"),
        "eligible_since_utc": age.get("eligible_since_utc"),
        "bars_between_eligibility_and_signal": age.get(
            "bars_between_eligibility_and_signal"
        ),
        "signal_before_age_gate": age.get("signal_before_age_gate"),
        "signal_after_age_gate": age.get("signal_after_age_gate"),
        "identity_uncertain": age.get("identity_uncertain"),
        "manual_decision": c.get("manual_decision", "UNREVIEWED"),
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    prof_dir = RESULTS / "candidate_age_profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_phase2a_candidates()
    window = load_window_from_ssot(SCAN_TO)
    enriched = run_enricher(window, candidates)
    out_cands = enriched["candidates"]
    out_cands.sort(key=lambda x: x["signal_close_utc"])

    rows = [row_from_candidate(c) for c in out_cands]
    fieldnames = list(rows[0].keys()) if rows else []

    with (RESULTS / "candidate_pool_age_enriched.csv").open(
        "w", newline="", encoding="utf-8"
    ) as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    (RESULTS / "candidate_pool_age_enriched.json").write_text(
        json.dumps(enriched, indent=2), encoding="utf-8"
    )

    for c in out_cands:
        cid = c["candidate_id"]
        (prof_dir / f"{cid}.json").write_text(json.dumps(c, indent=2), encoding="utf-8")

    eligible = sum(1 for r in rows if r.get("eligible_by_age_10bars"))
    not_eligible = len(rows) - eligible
    c001_c005 = [r for r in rows if r["candidate_id"].startswith(tuple(f"C00{i}_" for i in range(1, 6)))]

    md = [
        "# Phase 2C pool age enrichment",
        "",
        f"- Candidates: {len(rows)} (Phase 2A export)",
        f"- Age gate: **10** completed 5m bars visible as Depth-2 upper zone",
        f"- Eligible at signal: **{eligible}**",
        f"- Before gate: **{not_eligible}**",
        "",
        "## C001–C005",
        "",
        "| id | age_bars | eligible | first_seen | bars after eligible |",
        "|---|---:|---|---|---:|",
    ]
    for r in c001_c005:
        md.append(
            f"| {r['candidate_id']} | {r['touch_pool_age_bars']} | {r['eligible_by_age_10bars']} | "
            f"{r['touch_pool_first_seen_utc']} | {r['bars_between_eligibility_and_signal']} |"
        )
    md.append("")
    md.append("Identity rules: see `candidate_pool_age_enriched.json` → `identity_rules`.")
    (RESULTS / "scan_summary.md").write_text("\n".join(md), encoding="utf-8")

    verdict = (
        "CANDIDATE_POOL_AGE_READY"
        if rows and all(r["touch_pool_age_bars"] is not None for r in rows)
        else "CANDIDATE_POOL_AGE_BLOCKED"
    )
    write_report(verdict, enriched, rows, eligible, not_eligible)
    print(json.dumps({"verdict": verdict, "count": len(rows), "eligible": eligible}))
    return 0 if verdict == "CANDIDATE_POOL_AGE_READY" else 2


def write_report(
    verdict: str,
    enriched: dict,
    rows: list[dict],
    eligible: int,
    not_eligible: int,
) -> None:
    lines = [
        "# DOGEUSDT – PHASE 2C",
        "# 5M PD1 DEPTH2 SHORT CANDIDATE AGE ENRICHMENT V1",
        "",
        f"**VERDICT:** `{verdict}`",
        "",
        "## Purpose",
        "",
        "Analysis-only enrichment of Phase 2A candidates with causal upper-pool age.",
        "User rule under test: Depth-2 upper zone visible ≥ **10** completed 5m bars before signal is age-eligible.",
        "**Not** applied to scanner; no outcome tuning.",
        "",
        "## Pool identity (operational)",
        "",
        "```json",
        json.dumps(enriched.get("identity_rules"), indent=2),
        "```",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Candidates enriched | {len(rows)} |",
        f"| `eligible_by_age_10bars` true | {eligible} |",
        f"| `signal_before_age_gate` | {not_eligible} |",
        "",
        "## C001–C005",
        "",
    ]
    for r in rows:
        if not r["candidate_id"].startswith(("C001_", "C002_", "C003_", "C004_", "C005_")):
            continue
        lines.append(
            f"- **{r['candidate_id']}** age={r['touch_pool_age_bars']} bars, "
            f"first_seen=`{r['touch_pool_first_seen_utc']}`, "
            f"eligible={r['eligible_by_age_10bars']}, "
            f"after_gate_bars={r['bars_between_eligibility_and_signal']}"
        )
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            "`results/phase2c_5m_pd1_depth2_candidate_pool_age_v1/`",
            "",
            "## Manual review note",
            "",
            "Phase 2B cases remain UNREVIEWED; age gate is exploratory only.",
            "",
        ]
    )
    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

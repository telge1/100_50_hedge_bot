#!/usr/bin/env python3
"""Phase 2A — read-only short candidate timestamp scanner pilot."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_1B = ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"
TOOLS_1C = ROOT / "tools" / "phase1c_depth2_manual_case_parity_v1"
RESULTS = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
SCANNER_JS = Path(__file__).resolve().parent / "timeline_scanner.js"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE2A_5M_PD1_DEPTH2_SHORT_CANDIDATE_SCANNER_V1.md"

# September 2026 pilot slice (single SSOT window, not full month).
SCAN_FROM_UTC = "2026-09-11T12:00:00Z"
SCAN_TO_UTC = "2026-09-13T02:45:00Z"

sys.path.insert(0, str(TOOLS_1B))
sys.path.insert(0, str(TOOLS_1C))

from profile_lib import ema_context, parse_utc  # noqa: E402
from run_pilot import load_window_from_ssot  # noqa: E402


def run_timeline_scan(window: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "pools": window.get("pools") or [],
        "prices": window.get("prices") or [],
        "window": window.get("window"),
        "provenance": window.get("provenance"),
        "scan_from_utc": SCAN_FROM_UTC,
        "scan_to_utc": SCAN_TO_UTC,
        "config": {
            "proximity_pct": 0.05,
            "large_gap_pct": 0.5,
            "rejection_mode": "rejection_v1",
            "cooldown_candles": 6,
            "max_output": 20,
        },
    }
    proc = subprocess.run(
        ["node", str(SCANNER_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(SCANNER_JS.parent),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "timeline scanner failed")
    return json.loads(proc.stdout)


def ema_at_signal(prices: list[dict], signal_close_utc: str) -> str:
    ctx = ema_context(prices, signal_close_utc)
    if ctx.get("status") != "VERIFIED":
        return json.dumps({"status": ctx.get("status")}, separators=(",", ":"))
    return json.dumps(
        {
            "status": "VERIFIED",
            "emas": ctx.get("emas"),
            "slopes_pct": ctx.get("slopes_pct"),
            "reference_close": ctx.get("reference_close"),
        },
        separators=(",", ":"),
    )


def lower_targets(entry: float, intervals: list[dict]) -> list[dict]:
    out = []
    for iv in intervals:
        if iv.get("side") != "lower":
            continue
        top = float(iv["top_price"])
        if top >= entry:
            continue
        dist_pct = (entry - top) / entry * 100.0
        out.append(
            {
                "bottom": float(iv["bottom_price"]),
                "top": top,
                "depth": iv.get("depth"),
                "distance_entry_to_cluster_top_pct": dist_pct,
                "meets_min_0p5_entry_to_tp_top": dist_pct >= 0.5,
            }
        )
    out.sort(key=lambda x: -x["top"])
    return out


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    prof_dir = RESULTS / "candidate_landscape_profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)

    window = load_window_from_ssot(SCAN_TO_UTC)
    scan = run_timeline_scan(window)
    prices = window.get("prices") or []

    rows = []
    for i, cand in enumerate(scan["candidates"]):
        cid = f"C{i+1:03d}_{cand['signal_close_utc'].replace(':', '').replace('-', '')}"
        ema_s = ema_at_signal(prices, cand["signal_close_utc"])
        intervals = cand.get("landscape_intervals") or []
        lowers = lower_targets(float(cand["entry_price"]), intervals)

        row = {
            "candidate_id": cid,
            "signal_close_utc": cand["signal_close_utc"],
            "entry_open_utc": cand["entry_open_utc"],
            "entry_price": cand["entry_price"],
            "upper_pool_lower": cand["upper_pool_lower"],
            "upper_pool_upper": cand["upper_pool_upper"],
            "upper_pool_depth": cand["upper_pool_depth"],
            "distance_to_pool_pct": cand["distance_to_pool_lower_pct"],
            "nearest_upper_large_gap_pct": cand.get("nearest_upper_large_gap_pct"),
            "upper_large_gap_count": cand.get("upper_large_gap_count"),
            "lower_large_gap_count": cand.get("lower_large_gap_count"),
            "ema_context": ema_s,
            "trigger_reason": cand["trigger_reason"],
            "manual_decision": "UNREVIEWED",
            "sl_reference_upper_top_plus_0p2pct": cand.get("sl_reference_price"),
            "lower_target_candidates": lowers[:10],
        }
        rows.append(row)

        prof_path = prof_dir / f"{cid}.json"
        export = {
            "candidate_id": cid,
            "scan_slice": {"from": SCAN_FROM_UTC, "to": SCAN_TO_UTC},
            "candidate": cand,
            "row_summary": row,
            "lower_targets_full": lowers,
        }
        export["candidate"].pop("full_analysis", None)
        prof_path.write_text(json.dumps(export, indent=2), encoding="utf-8")

    (RESULTS / "candidates.json").write_text(
        json.dumps(
            {
                "scan": {
                    "from_utc": SCAN_FROM_UTC,
                    "to_utc": SCAN_TO_UTC,
                    "total_found": scan["total_candidates_found"],
                    "output_count": len(rows),
                    "not_full_september": True,
                    "pipeline_line_id": (window.get("provenance") or {}).get(
                        "pipeline_line_id"
                    ),
                },
                "config": scan["config"],
                "duplicate_rule": scan["duplicate_rule"],
                "rejection_variants": scan["rejection_variants_documented"],
                "candidates": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    fieldnames = [
        "candidate_id",
        "signal_close_utc",
        "entry_open_utc",
        "entry_price",
        "upper_pool_lower",
        "upper_pool_upper",
        "upper_pool_depth",
        "distance_to_pool_pct",
        "nearest_upper_large_gap_pct",
        "upper_large_gap_count",
        "lower_large_gap_count",
        "ema_context",
        "trigger_reason",
        "manual_decision",
    ]
    with (RESULTS / "candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    md = [
        "# Phase 2A scan summary",
        "",
        f"- **Slice:** `{SCAN_FROM_UTC}` → `{SCAN_TO_UTC}` (NOT full September)",
        f"- **Total found:** {scan['total_candidates_found']}",
        f"- **Exported:** {len(rows)} (max 20 chronological)",
        f"- **Proximity:** {scan['config']['proximity_pct']}%",
        f"- **Rejection:** {scan['config']['rejection_mode']}",
        f"- **Cooldown:** {scan['duplicate_rule']}",
        "",
        "## Candidates (UTC)",
        "",
    ]
    for r in rows:
        md.append(
            f"- `{r['candidate_id']}` signal `{r['signal_close_utc']}` → entry open `{r['entry_open_utc']}` @ {r['entry_price']}"
        )
    (RESULTS / "scan_summary.md").write_text("\n".join(md), encoding="utf-8")

    parity_ok = all(
        c.get("parity_ok") for c in scan["candidates"]
    ) if scan["candidates"] else True
    verdict = (
        "SHORT_CANDIDATE_PILOT_READY"
        if parity_ok and len(rows) >= 0
        else "SHORT_CANDIDATE_PILOT_BLOCKED"
    )
    summary = {"verdict": verdict, "candidates": len(rows), "total_found": scan["total_candidates_found"]}
    (RESULTS / "pilot_summary.json").write_text(json.dumps(summary, indent=2))

    write_report(verdict, scan, rows, window)
    print(json.dumps(summary, indent=2))
    return 0 if verdict == "SHORT_CANDIDATE_PILOT_READY" else 2


def write_report(
    verdict: str,
    scan: dict,
    rows: list[dict],
    window: dict,
) -> None:
    lines = [
        "# DOGEUSDT – PHASE 2A",
        "# 5M PD1 DEPTH2 SHORT CANDIDATE TIMESTAMP SCANNER V1",
        "",
        f"**VERDICT:** `{verdict}`",
        "",
        "## Scan scope",
        "",
        f"| Item | Value |",
        f"|------|-------|",
        f"| From UTC | `{SCAN_FROM_UTC}` |",
        f"| To UTC | `{SCAN_TO_UTC}` |",
        f"| Full September 2026 | **No** — single contiguous SSOT window pilot |",
        f"| Total candidates (raw) | {scan['total_candidates_found']} |",
        f"| Exported (chronological cap) | {len(rows)} |",
        f"| Pipeline | `{(window.get('provenance') or {}).get('pipeline_line_id')}` |",
        "",
        "## Trigger definition",
        "",
        "- Depth-2 upper pool only (production JS adapter).",
        "- Proximity 0.05% to pool lower edge (research parameter).",
        "- `rejection_v1`: bearish candle `close < open` after proximity.",
        "- Entry earliest at **next 5m open**.",
        f"- Duplicate control: {scan['duplicate_rule']}",
        "",
        "## Candidates (max 20)",
        "",
    ]
    for r in rows:
        lines.append(
            f"- **{r['candidate_id']}** — signal `{r['signal_close_utc']}`, "
            f"entry open `{r['entry_open_utc']}`, price `{r['entry_price']}`, "
            f"pool `[{r['upper_pool_lower']}, {r['upper_pool_upper']}]` depth {r['upper_pool_depth']}"
        )
    lines.extend(
        [
            "",
            "## Outputs",
            "",
            "`results/phase2a_5m_pd1_depth2_short_candidates_v1/`",
            "",
            "## Limits",
            "",
            "No auto-trading; no quality filters applied; manual_decision=UNREVIEWED.",
            "",
        ]
    )
    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

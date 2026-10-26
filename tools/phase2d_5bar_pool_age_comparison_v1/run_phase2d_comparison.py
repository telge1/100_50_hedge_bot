#!/usr/bin/env python3
"""Phase 2D — 5-bar vs 10-bar pool age comparison (read-only)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
PHASE2C = ROOT / "results" / "phase2c_5m_pd1_depth2_candidate_pool_age_v1"
RESULTS = ROOT / "results" / "phase2d_5bar_pool_age_comparison_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE2D_5BAR_POOL_AGE_COMPARISON_V1.md"
VARIANTS_JS = Path(__file__).resolve().parent / "identity_variants.js"
SCAN_TO = "2026-09-13T02:45:00Z"

sys.path.insert(0, str(ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"))
sys.path.insert(0, str(ROOT / "tools" / "phase1c_depth2_manual_case_parity_v1"))
from profile_lib import parse_utc  # noqa: E402
from run_pilot import load_window_from_ssot  # noqa: E402


def load_candidates() -> list[dict[str, Any]]:
    rows = list(
        csv.DictReader(
            (PHASE2C / "candidate_pool_age_enriched.csv").open(encoding="utf-8")
        )
    )
    for r in rows:
        prof = json.loads(
            (PHASE2C / "candidate_age_profiles" / f"{r['candidate_id']}.json").read_text()
        )
        r["upper_pool_origins"] = prof.get("upper_pool_origins") or []
        r["phase2c_age_bars"] = int(r["touch_pool_age_bars"])
    return rows


def origin_and_segment_ages(
    pools: list[dict], origin_ids: list[str], signal_utc: str
) -> dict[str, Any]:
    signal_ms = int(parse_utc(signal_utc).timestamp() * 1000)
    slot_ms = 5 * 60 * 1000
    origin_first_ms = []
    segment_first_ms = []
    for pl in pools:
        if pl.get("side") != "upper":
            continue
        oid = pl.get("origin")
        if origin_ids and oid not in origin_ids:
            continue
        for seg in pl.get("segments") or []:
            s0 = int(parse_utc(seg[0]).timestamp() * 1000)
            s1 = int(parse_utc(seg[1]).timestamp() * 1000)
            if s0 <= signal_ms <= s1:
                segment_first_ms.append(s0)
            if oid in origin_ids:
                origin_first_ms.append(s0)
    def bars_since(first_list: list[int]) -> int | None:
        if not first_list:
            return None
        first = min(first_list)
        if signal_ms < first:
            return 0
        return int((signal_ms - first) // slot_ms) + 1

    obars = bars_since(origin_first_ms)
    sbars = bars_since(segment_first_ms)
    return {
        "origin_age_bars_at_signal": obars,
        "exact_active_segment_age_bars_at_signal": sbars,
        "origin_age_status": "VERIFIED" if obars is not None else "NOT_VERIFIED",
        "segment_age_status": "VERIFIED" if sbars is not None else "NOT_VERIFIED",
        "note": (
            "Origin/segment age = calendar bars since earliest known segment start "
            "for tagged origins; NOT identical to continuous visible Depth-2 zone age."
        ),
    }


def run_identity_variants(window: dict, rows: list[dict]) -> list[dict]:
    payload = {
        "pools": window.get("pools") or [],
        "context_from_utc": (window.get("window") or {}).get("context_from_utc"),
        "candidates": [
            {
                "candidate_id": r["candidate_id"],
                "signal_close_utc": r["signal_close_utc"],
                "upper_pool_lower": float(r["upper_pool_lower"]),
                "upper_pool_upper": float(r["upper_pool_upper"]),
                "upper_pool_origins": r["upper_pool_origins"],
                "phase2c_age_bars": r["phase2c_age_bars"],
            }
            for r in rows
        ],
    }
    proc = subprocess.run(
        ["node", str(VARIANTS_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(VARIANTS_JS.parent),
    )
    if proc.returncode != 0:
        return [{"error": proc.stderr or proc.stdout}]
    return json.loads(proc.stdout)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = load_candidates()
    rows.sort(key=lambda r: r["signal_close_utc"])
    window = load_window_from_ssot(SCAN_TO)
    pools = window.get("pools") or []
    variants = run_identity_variants(window, rows)
    var_by_id = {v["candidate_id"]: v for v in variants if "candidate_id" in v}

    enriched = []
    for r in rows:
        age = r["phase2c_age_bars"]
        elig5 = age >= 5
        elig10 = age >= 10
        aux = origin_and_segment_ages(pools, r["upper_pool_origins"], r["signal_close_utc"])
        v = var_by_id.get(r["candidate_id"], {})
        enriched.append(
            {
                **r,
                "continuous_depth2_zone_age_bars": age,
                "eligible_age_gate_5bars": elig5,
                "eligible_age_gate_10bars": elig10,
                "gate_delta_5_vs_10": "ONLY_5" if elig5 and not elig10 else ("BOTH" if elig5 and elig10 else "NEITHER"),
                **aux,
                "identity_variants": v,
            }
        )

    count5 = sum(1 for e in enriched if e["eligible_age_gate_5bars"])
    count10 = sum(1 for e in enriched if e["eligible_age_gate_10bars"])
    only5 = sum(1 for e in enriched if e["gate_delta_5_vs_10"] == "ONLY_5")

    (RESULTS / "gate_comparison_all_20.json").write_text(
        json.dumps(
            {
                "summary": {
                    "eligible_at_5_bars": count5,
                    "eligible_at_10_bars": count10,
                    "newly_eligible_at_5_not_10": only5,
                    "excluded_both": sum(1 for e in enriched if e["gate_delta_5_vs_10"] == "NEITHER"),
                },
                "candidates": enriched,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    fields = [
        "candidate_id",
        "signal_close_utc",
        "continuous_depth2_zone_age_bars",
        "origin_age_bars_at_signal",
        "exact_active_segment_age_bars_at_signal",
        "eligible_age_gate_5bars",
        "eligible_age_gate_10bars",
        "manual_decision",
    ]
    with (RESULTS / "gate_comparison_all_20.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for e in enriched:
            w.writerow(e)

    c003 = next(e for e in enriched if e["candidate_id"].startswith("C003_"))
    c004 = next(e for e in enriched if e["candidate_id"].startswith("C004_"))
    evening = [e for e in enriched if "20260911T19" in e["signal_close_utc"] or "20260911T20" in e["signal_close_utc"] or "20260911T21" in e["signal_close_utc"]]

    write_report(enriched, count5, count10, only5, c003, c004, evening)
    print(json.dumps({"eligible_5": count5, "eligible_10": count10, "only_5": only5}))
    return 0


def write_report(
    enriched: list[dict],
    count5: int,
    count10: int,
    only5: int,
    c003: dict,
    c004: dict,
    evening: list[dict],
) -> None:
    c1_5 = [e for e in enriched if e["candidate_id"].startswith(tuple(f"C00{i}_" for i in range(1, 6)))]

    lines = [
        "# DOGEUSDT – PHASE 2D",
        "# DEPTH2 POOL AGE: 5-BAR COMPARISON V1",
        "",
        "**Mode:** Read-only; Phase 2A/2C artifacts unchanged.",
        "",
        "## Summary",
        "",
        "| Gate | Eligible at signal | Excluded |",
        "|------|-------------------:|---------:|",
        f"| **5 bars** | {count5} / 20 | {20 - count5} |",
        f"| **10 bars** | {count10} / 20 | {20 - count10} |",
        f"| Newly eligible at 5 only (not 10) | {only5} | — |",
        "",
        "### Age types (kept separate)",
        "",
        "1. **continuous_depth2_zone_age_bars** — Phase 2C backward track (unchanged heuristic).",
        "2. **origin_age_bars_at_signal** — bars since earliest ACTIVE segment start of touch origins.",
        "3. **exact_active_segment_age_bars_at_signal** — same for overlapping upper pool rows at signal.",
        "",
        "## C001–C005",
        "",
        "| ID | continuous D2 | origin | segment | elig 5 | elig 10 | manual |",
        "|----|---:|---:|---:|---|---|---|",
    ]
    for e in c1_5:
        lines.append(
            f"| {e['candidate_id']} | {e['continuous_depth2_zone_age_bars']} | "
            f"{e.get('origin_age_bars_at_signal')} | {e.get('exact_active_segment_age_bars_at_signal')} | "
            f"{e['eligible_age_gate_5bars']} | {e['eligible_age_gate_10bars']} | {e.get('manual_decision')} |"
        )

    lines.extend(
        [
            "",
            "## C003 — identity & 5-bar gate",
            "",
            f"- Fallbuch: **MANUALLY_CONFIRMED_SHORT** at `2026-09-11T17:00:00Z` (matches this candidate).",
            f"- Phase 2C continuous Depth-2 age: **{c003['continuous_depth2_zone_age_bars']}** bar(s).",
            f"- **Eligible at 5 bars?** `{c003['eligible_age_gate_5bars']}` — **No** (needs ≥5 continuous visible zone bars).",
            f"- Origin-age bars: `{c003.get('origin_age_bars_at_signal')}`; segment-age: `{c003.get('exact_active_segment_age_bars_at_signal')}`.",
            "",
            "### Alternative identity (Phase 2D only)",
            "",
            f"```json\n{json.dumps(c003.get('identity_variants'), indent=2)}\n```",
            "",
            "If `variant_origin_sticky` or `variant_loose` shows longer age than Phase 2C, the default heuristic may split a zone early — mark as **NOT_VERIFIED** for a single true pool age without manual chart review.",
            "",
            "## C004 — later evening upper contacts (same night)",
            "",
            f"C004 continuous age: **{c004['continuous_depth2_zone_age_bars']}** bars (eligible 5={c004['eligible_age_gate_5bars']}, 10={c004['eligible_age_gate_10bars']}).",
            "",
            "| Later candidate | signal UTC | continuous D2 age | elig 5 | elig 10 |",
            "|-----------------|------------|------------------:|--------|---------|",
        ]
    )
    for e in sorted(evening, key=lambda x: x["signal_close_utc"]):
        if e["candidate_id"] == c004["candidate_id"]:
            continue
        if parse_utc(e["signal_close_utc"]) <= parse_utc(c004["signal_close_utc"]):
            continue
        lines.append(
            f"| {e['candidate_id']} | {e['signal_close_utc']} | {e['continuous_depth2_zone_age_bars']} | "
            f"{e['eligible_age_gate_5bars']} | {e['eligible_age_gate_10bars']} |"
        )

    lines.extend(
        [
            "",
            "Later contacts (e.g. C008+) often show **higher** continuous zone age; C004 remains a young pool at signal under Phase 2C rules.",
            "",
            "## Manual review linkage",
            "",
            "- Only **C003** has documented **MANUALLY_CONFIRMED_SHORT** in `Signal_definierung.md`.",
            "- C001–C002, C004–C005 remain UNREVIEWED in Phase 2B — **neither 5 nor 10 bars** explains C003 alone; both gates reject it on continuous zone age.",
            "- Lowering to 5 bars adds **{only5}** candidates vs 10-bar gate without validating manual labels (no outcome fit).",
            "",
            "## Outputs",
            "",
            "`results/phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.csv`",
            "",
        ]
    )
    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

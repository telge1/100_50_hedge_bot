#!/usr/bin/env python3
"""Phase 2F — mature D2 candidate manual review pack (read-only)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
PHASE2D = ROOT / "results" / "phase2d_5bar_pool_age_comparison_v1"
PHASE2E = ROOT / "results" / "phase2e_entry_landscape_contrast_v1"
RESULTS = ROOT / "results" / "phase2f_mature_depth2_manual_review_v1"

MATURE_KEYS = ["C008", "C010", "C011", "C013", "C015", "C016"]
CONTROL_KEY = "C003"

ID_MAP = {
    "C003": "C003_20260911T170000Z",
    "C008": "C008_20260911T211000Z",
    "C010": "C010_20260911T234000Z",
    "C011": "C011_20260911T235500Z",
    "C013": "C013_20260912T003500Z",
    "C015": "C015_20260912T012000Z",
    "C016": "C016_20260912T012500Z",
}

CONFIRMED = {
    "C003": "MANUALLY_CONFIRMED_SHORT (Signal_definierung.md)",
}

sys.path.insert(0, str(ROOT / "tools" / "phase2e_entry_landscape_contrast_v1"))
from build_contrast import (  # noqa: E402
    load_phase2a_profile,
    load_phase2d,
    lower_landscape,
    rejection_features,
)

sys.path.insert(0, str(ROOT / "tools" / "phase1c_depth2_manual_case_parity_v1"))
from profile_lib import ema_context, parse_utc  # noqa: E402
sys.path.insert(0, str(ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"))
from run_pilot import load_window_from_ssot, pool_persistence_rows  # noqa: E402


def top_gaps(gaps_upper: list[dict], pool_upper: float, n: int = 5) -> list[dict]:
    above = sorted(
        [g for g in gaps_upper if float(g["gap_bottom_price"]) >= pool_upper - 1e-12],
        key=lambda g: float(g["gap_bottom_price"]),
    )
    large = [g for g in above if g.get("large_gap")]
    small = [g for g in above if not g.get("large_gap")]
    out = []
    for g in large[:n]:
        out.append(
            {
                "type": "LARGE_GAP",
                "gap_pct": g["gap_pct"],
                "bottom": g["gap_bottom_price"],
                "top": g["gap_top_price"],
            }
        )
    for g in small[: max(0, n - len(out))]:
        out.append(
            {
                "type": "small",
                "gap_pct": g["gap_pct"],
                "bottom": g["gap_bottom_price"],
                "top": g["gap_top_price"],
            }
        )
    return out


def lower_clusters_within_2pct(intervals: list[dict], entry: float, limit: int = 6) -> list[dict]:
    lowers = lower_landscape(intervals, entry)
    below = [
        iv
        for iv in intervals
        if iv.get("side") == "lower" and float(iv["top_price"]) < entry and float(iv["top_price"]) >= entry * 0.98
    ]
    below.sort(key=lambda x: -float(x["top_price"]))
    rows = []
    for iv in below[:limit]:
        rows.append(
            {
                "bottom": float(iv["bottom_price"]),
                "top": float(iv["top_price"]),
                "depth": iv.get("depth"),
                "origins": iv.get("origins") or [],
            }
        )
    return rows


def origin_active_summary(pools: list[dict], origin_ids: list[str], as_of: str) -> list[dict]:
    persist = pool_persistence_rows(pools, as_of)
    by_o = {r["origin"]: r for r in persist if r.get("origin")}
    out = []
    for oid in origin_ids:
        row = by_o.get(oid)
        out.append(
            {
                "origin_id": oid,
                "active_at_as_of": row.get("active_at_as_of") if row else None,
                "first_segment_start_utc": row.get("first_segment_start_utc") if row else None,
                "age_hours_at_as_of": row.get("age_hours_at_as_of") if row else None,
                "status": "VERIFIED" if row else "NOT_VERIFIED",
            }
        )
    return out


def build_row(key: str, pools: list[dict], prices: list[dict]) -> dict[str, Any]:
    cid = ID_MAP[key]
    p2d = load_phase2d(cid)
    prof = load_phase2a_profile(cid)
    cand = prof["candidate"]
    entry = float(cand["entry_price"])
    pool_lo = float(cand["upper_pool_lower"])
    pool_hi = float(cand["upper_pool_upper"])
    intervals = cand.get("landscape_intervals") or []
    ema = ema_context(prices, cand["signal_close_utc"])
    lowers = lower_landscape(intervals, entry)
    gaps = top_gaps(cand.get("gaps_upper") or [], pool_hi)
    clusters = lower_clusters_within_2pct(intervals, entry)
    origins = cand.get("upper_pool_origins") or []
    active = origin_active_summary(pools, origins, cand["entry_open_utc"])

    return {
        "candidate_key": key,
        "candidate_id": cid,
        "review_group": "CONTROL_POSITIVE" if key == CONTROL_KEY else "MATURE_D2_AGE_GE_5",
        "signal_close_utc": cand["signal_close_utc"],
        "entry_open_utc": cand["entry_open_utc"],
        "entry_price": entry,
        "goto_chart_utc": cand["signal_close_utc"],
        "upper_pool_lower": pool_lo,
        "upper_pool_upper": pool_hi,
        "upper_pool_width_pct": (pool_hi - pool_lo) / pool_lo * 100,
        "upper_pool_depth": cand["upper_pool_depth"],
        "continuous_depth2_zone_age_bars": p2d["continuous_depth2_zone_age_bars"],
        "origin_age_bars_at_signal": p2d["origin_age_bars_at_signal"],
        "segment_age_bars_at_signal": p2d["exact_active_segment_age_bars_at_signal"],
        "age_eligible_5bars": p2d["eligible_age_gate_5bars"],
        "top_upper_gaps_above_pool": gaps,
        "nearest_large_upper_gap_pct": cand.get("nearest_upper_large_gap_pct"),
        "lower_zones_within_2pct_count": lowers["lower_bands_within_2pct_below_entry"],
        "max_lower_depth_within_2pct": lowers["max_depth_within_2pct"],
        "lower_clusters_within_2pct": clusters,
        "ema_at_signal": ema,
        "upper_pool_origins_active": active,
        "trigger_reason": cand.get("trigger_reason"),
        "prior_manual_decision": CONFIRMED.get(key, p2d.get("manual_decision", "UNREVIEWED")),
        "rejection_signal_candle": rejection_features(cand["candle"]),
        "manual_review": "",
        "manual_reason": "",
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    window = load_window_from_ssot("2026-09-13T02:45:00Z")
    pools = window.get("pools") or []
    prices = window.get("prices") or []

    keys = [CONTROL_KEY] + MATURE_KEYS
    rows = [build_row(k, pools, prices) for k in keys]
    rows.sort(key=lambda r: r["signal_close_utc"])

    csv_fields = [
        "candidate_key",
        "candidate_id",
        "review_group",
        "signal_close_utc",
        "entry_open_utc",
        "goto_chart_utc",
        "entry_price",
        "upper_pool_lower",
        "upper_pool_upper",
        "upper_pool_width_pct",
        "upper_pool_depth",
        "continuous_depth2_zone_age_bars",
        "origin_age_bars_at_signal",
        "segment_age_bars_at_signal",
        "age_eligible_5bars",
        "nearest_large_upper_gap_pct",
        "lower_zones_within_2pct_count",
        "max_lower_depth_within_2pct",
        "trigger_reason",
        "prior_manual_decision",
        "manual_review",
        "manual_reason",
    ]
    flat_rows = []
    for r in rows:
        fr = {k: r.get(k) for k in csv_fields}
        fr["ema_summary"] = json.dumps(r.get("ema_at_signal"), separators=(",", ":"))
        fr["top_gaps_json"] = json.dumps(r.get("top_upper_gaps_above_pool"))
        fr["lower_clusters_json"] = json.dumps(r.get("lower_clusters_within_2pct"))
        fr["origins_active_json"] = json.dumps(r.get("upper_pool_origins_active"))
        flat_rows.append(fr)

    with (RESULTS / "review_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=list(flat_rows[0].keys()),
            extrasaction="ignore",
        )
        w.writeheader()
        w.writerows(flat_rows)

    md = [
        "# Mature Depth-2 short candidates — manual review",
        "",
        "DOGEUSDT · 5m · PD1 · Min-Depth 2 · Large gap ≥0,5 % · Age research param 5 bars.",
        "",
        "**No auto labels.** Fill `manual_review`: `SHORT` | `NO_SHORT` | `UNCERTAIN` and `manual_reason`.",
        "",
        "## Chronological go-to checklist",
        "",
        "| # | Go-To UTC | ID | Group | D2 age | manual_review | manual_reason |",
        "|--:|-----------|-----|-------|-------:|---------------|---------------|",
    ]
    for i, r in enumerate(rows, 1):
        grp = "control" if r["review_group"] == "CONTROL_POSITIVE" else "mature≥5"
        md.append(
            f"| {i} | `{r['goto_chart_utc']}` | {r['candidate_key']} | {grp} | "
            f"{r['continuous_depth2_zone_age_bars']} | | |"
        )
    md.extend(
        [
            "",
            "## Positive control (included regardless of 5-bar age)",
            "",
        ]
    )
    ctrl = next(r for r in rows if r["candidate_key"] == CONTROL_KEY)
    md.append(f"- **{ctrl['candidate_id']}** — signal `{ctrl['signal_close_utc']}` — {ctrl['prior_manual_decision']}")
    md.append(f"  - Continuous D2 age: **{ctrl['continuous_depth2_zone_age_bars']}** bars (`age_eligible_5bars={ctrl['age_eligible_5bars']}`)")
    md.append("  - Reference only — do not exclude for age; prior label is confirmed SHORT, not a new auto-score.")
    md.append("")

    md.append("## Mature cohort (continuous D2 age ≥ 5 at signal)")
    md.append("")
    md.append("| Go-To (signal) | ID | D2 age | Origin age | Large gap % | Lower≤2% / max depth | Prior manual |")
    md.append("|----------------|-----|-------:|-----------:|------------:|----------------------|--------------|")
    for r in rows:
        if r["review_group"] != "MATURE_D2_AGE_GE_5":
            continue
        md.append(
            f"| `{r['signal_close_utc']}` | {r['candidate_key']} | {r['continuous_depth2_zone_age_bars']} | "
            f"{r['origin_age_bars_at_signal']} | {r['nearest_large_upper_gap_pct']:.4f} | "
            f"{r['lower_zones_within_2pct_count']} / {r['max_lower_depth_within_2pct']} | {r['prior_manual_decision']} |"
        )

    md.append("")
    md.append("## Per-candidate detail")
    md.append("")
    for r in rows:
        md.append(f"### {r['candidate_key']} — `{r['candidate_id']}`")
        md.append("")
        md.append(f"- **Go-To:** `{r['goto_chart_utc']}` (5m PD1, Depth 2)")
        md.append(f"- **Entry open:** `{r['entry_open_utc']}` @ {r['entry_price']}")
        md.append(
            f"- **Upper pool:** [{r['upper_pool_lower']}, {r['upper_pool_upper']}] "
            f"depth {r['upper_pool_depth']} width {r['upper_pool_width_pct']:.5f}%"
        )
        md.append(
            f"- **Age:** continuous D2 {r['continuous_depth2_zone_age_bars']} | "
            f"origin {r['origin_age_bars_at_signal']} | segment {r['segment_age_bars_at_signal']} | "
            f"`age_eligible_5bars`={r['age_eligible_5bars']}"
        )
        md.append(f"- **Top gaps above pool:** {r['top_upper_gaps_above_pool']}")
        md.append(f"- **Lower within 2%:** count {r['lower_zones_within_2pct_count']}, max depth {r['max_lower_depth_within_2pct']}")
        md.append(f"- **Lower clusters:** {r['lower_clusters_within_2pct']}")
        if r["ema_at_signal"].get("status") == "VERIFIED":
            e = r["ema_at_signal"]["emas"]
            s = r["ema_at_signal"]["slopes_pct"]
            md.append(
                f"- **EMA @ signal:** 9={e['ema9']:.8f} 20={e['ema20']:.8f} "
                f"59={e['ema59']:.8f} 200={e['ema200']:.8f}; slopes% {s}"
            )
        md.append(f"- **Origins ACTIVE:** {r['upper_pool_origins_active']}")
        md.append(f"- **Trigger:** {r['trigger_reason']}")
        md.append(f"- **Prior manual:** {r['prior_manual_decision']}")
        md.append(f"- **manual_review:** _{r['manual_review'] or 'pending'}_")
        md.append(f"- **manual_reason:** _{r['manual_reason'] or 'pending'}_")
        md.append("")

    (RESULTS / "REVIEW_MATURE_CANDIDATES.md").write_text("\n".join(md), encoding="utf-8")

    summary = [
        "# Phase 2F summary",
        "",
        f"- Candidates prepared: **{len(rows)}** (6 mature + C003 control)",
        f"- Mature with `age_eligible_5bars`: {sum(1 for r in rows if r['age_eligible_5bars'] and r['review_group']=='MATURE_D2_AGE_GE_5')}",
        "- C003 control: continuous D2 age 1, explicitly retained",
        "- Sources: Phase 2A profiles, Phase 2D ages, SSOT persistence at entry open",
        "",
        "Files: `review_candidates.csv`, `REVIEW_MATURE_CANDIDATES.md`",
    ]
    (RESULTS / "review_summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(json.dumps({"written": str(RESULTS), "count": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

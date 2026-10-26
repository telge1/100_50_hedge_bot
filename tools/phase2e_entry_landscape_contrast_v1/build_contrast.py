#!/usr/bin/env python3
"""Phase 2E — C003/C004/C008 landscape contrast (read-only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"
PHASE2D = ROOT / "results" / "phase2d_5bar_pool_age_comparison_v1"
RESULTS = ROOT / "results" / "phase2e_entry_landscape_contrast_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE2E_ENTRY_LANDSCAPE_CONTRAST_V1.md"

CASES = {
    "C003": {
        "candidate_id": "C003_20260911T170000Z",
        "signal_close_utc": "2026-09-11T17:00:00Z",
        "user_label": "SHORT (MANUALLY_CONFIRMED_SHORT per Signal_definierung.md)",
    },
    "C004": {
        "candidate_id": "C004_20260911T194500Z",
        "signal_close_utc": "2026-09-11T19:45:00Z",
        "user_label": "Too early / not preferred at original time (user narrative; not auto-scored)",
    },
    "C008": {
        "candidate_id": "C008_20260911T211000Z",
        "signal_close_utc": "2026-09-11T21:10:00Z",
        "user_label": "UNREVIEWED — no manual confirmation for this timestamp",
    },
}

sys.path.insert(0, str(ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"))
sys.path.insert(0, str(ROOT / "tools" / "phase1c_depth2_manual_case_parity_v1"))
from profile_lib import ema_context, parse_utc  # noqa: E402
from run_pilot import load_window_from_ssot  # noqa: E402


def load_phase2d(case_id: str) -> dict[str, Any]:
    data = json.loads((PHASE2D / "gate_comparison_all_20.json").read_text())
    for c in data["candidates"]:
        if c["candidate_id"] == case_id:
            return c
    raise KeyError(case_id)


def load_phase2a_profile(case_id: str) -> dict[str, Any]:
    return json.loads(
        (PHASE2A / "candidate_landscape_profiles" / f"{case_id}.json").read_text()
    )


def recent_candles(prices: list[dict], signal_utc: str, n: int = 5) -> list[dict]:
    sig_ms = int(parse_utc(signal_utc).timestamp() * 1000)
    rows = []
    for p in prices:
        t_ms = int(parse_utc(p["t"]).timestamp() * 1000)
        if t_ms <= sig_ms:
            rows.append(p)
    rows.sort(key=lambda x: x["t"])
    return rows[-n:]


def rejection_features(candle: dict) -> dict[str, Any]:
    o, h, l, c = float(candle["o"]), float(candle["h"]), float(candle["l"]), float(candle["c"])
    body = c - o
    range_ = h - l if h > l else 1e-12
    return {
        "bearish_close_lt_open": c < o,
        "body_pct_of_range": abs(body) / range_ * 100,
        "upper_wick_pct": (h - max(o, c)) / range_ * 100,
        "lower_wick_pct": (min(o, c) - l) / range_ * 100,
        "close": c,
        "high": h,
        "low": l,
        "open": o,
    }


def gap_stats(gaps_upper: list[dict], pool_upper: float) -> dict[str, Any]:
    above = [g for g in gaps_upper if float(g["gap_bottom_price"]) >= pool_upper - 1e-12]
    large = [g for g in above if g.get("large_gap")]
    small = [g for g in above if not g.get("large_gap")]
    first = above[0] if above else None
    return {
        "upper_gaps_above_entry_pool": len(above),
        "upper_large_gaps_above": len(large),
        "upper_small_gaps_above": len(small),
        "first_gap_above_pool_pct": first["gap_pct"] if first else None,
        "nearest_large_gap_pct": next((g["gap_pct"] for g in above if g.get("large_gap")), None),
    }


def lower_landscape(intervals: list[dict], entry: float) -> dict[str, Any]:
    lowers = [iv for iv in intervals if iv.get("side") == "lower"]
    below = [iv for iv in lowers if float(iv["top_price"]) < entry]
    within_2pct = [
        iv
        for iv in below
        if float(iv["top_price"]) >= entry * 0.98
    ]
    depths = [int(iv.get("depth") or 0) for iv in within_2pct]
    top_band = max(below, key=lambda x: float(x["top_price"]), default=None)
    return {
        "lower_depth2_band_count_total": len(lowers),
        "lower_bands_below_entry": len(below),
        "lower_bands_within_2pct_below_entry": len(within_2pct),
        "max_depth_within_2pct": max(depths) if depths else None,
        "nearest_lower_top_to_entry": float(top_band["top_price"]) if top_band else None,
        "nearest_lower_depth": top_band.get("depth") if top_band else None,
        "distance_entry_to_nearest_lower_top_pct": (
            (entry - float(top_band["top_price"])) / entry * 100 if top_band else None
        ),
    }


def build_case_bundle(key: str, meta: dict, prices: list[dict]) -> dict[str, Any]:
    cid = meta["candidate_id"]
    p2d = load_phase2d(cid)
    prof = load_phase2a_profile(cid)
    cand = prof["candidate"]
    intervals = cand.get("landscape_intervals") or []
    entry = float(cand["entry_price"])
    pool_lo = float(cand["upper_pool_lower"])
    pool_hi = float(cand["upper_pool_upper"])
    ema = ema_context(prices, meta["signal_close_utc"])
    candles = recent_candles(prices, meta["signal_close_utc"], 5)

    age = {
        "continuous_depth2_zone_age_bars": p2d["continuous_depth2_zone_age_bars"],
        "origin_age_bars_at_signal": p2d["origin_age_bars_at_signal"],
        "exact_active_segment_age_bars_at_signal": p2d[
            "exact_active_segment_age_bars_at_signal"
        ],
        "age_eligible_5bars": p2d["eligible_age_gate_5bars"],
        "age_eligible_10bars": p2d["eligible_age_gate_10bars"],
        "canonical_source": "results/phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.json",
    }

    return {
        "case_key": key,
        "candidate_id": cid,
        "signal_close_utc": meta["signal_close_utc"],
        "entry_open_utc": cand["entry_open_utc"],
        "landscape_as_of_utc": cand["entry_open_utc"],
        "landscape_as_of_note": "Phase 2A full landscape computed at entry open (hypothetical entry).",
        "user_label": meta["user_label"],
        "upper_entry_pool": {
            "lower": pool_lo,
            "upper": pool_hi,
            "depth": cand["upper_pool_depth"],
            "width_pct": (pool_hi - pool_lo) / pool_lo * 100,
            "origins": cand.get("upper_pool_origins") or [],
        },
        "pool_age": age,
        "gap_landscape": gap_stats(cand.get("gaps_upper") or [], pool_hi),
        "gap_landscape_counts_from_phase2a": {
            "upper_large_gap_count": cand.get("upper_large_gap_count"),
            "lower_large_gap_count": cand.get("lower_large_gap_count"),
            "nearest_upper_large_gap_pct": cand.get("nearest_upper_large_gap_pct"),
        },
        "lower_landscape": lower_landscape(intervals, entry),
        "upper_lower_counts": {
            "upper_interval_count": cand.get("upper_interval_count"),
            "lower_interval_count": cand.get("lower_interval_count"),
        },
        "ema_at_signal_close": ema,
        "rejection_signal_candle": rejection_features(cand["candle"]),
        "recent_closed_5m_candles": candles,
        "trigger_reason": cand.get("trigger_reason"),
        "manual_decision_csv": p2d.get("manual_decision"),
    }


def write_report(bundles: list[dict], consistency_note: str) -> None:
    lines = [
        "# DOGEUSDT – PHASE 2E",
        "# SHORT ENTRY AGE + LANDSCAPE CONTRAST V1",
        "",
        "Read-only contrast of **C003** (confirmed SHORT), **C004** (too early), **C008** (scanner only, unreviewed).",
        "",
        "## Data consistency (Phase 2D)",
        "",
        consistency_note,
        "",
        "**Canonical age columns:** `results/phase2d_5bar_pool_age_comparison_v1/gate_comparison_all_20.csv`",
        "",
        "## Comparison table",
        "",
        "| | C003 17:00 | C004 19:45 | C008 21:10 |",
        "|---|---|---|---|",
    ]

    def row(metric: str, keys: list[str]):
        vals = []
        for b in bundles:
            d = b
            for k in keys:
                d = d.get(k) if isinstance(d, dict) else None
            vals.append(str(d))
        lines.append(f"| {metric} | " + " | ".join(vals) + " |")

    row("User label", ["user_label"])
    row("Continuous D2 age (bars)", ["pool_age", "continuous_depth2_zone_age_bars"])
    row("Origin age (bars)", ["pool_age", "origin_age_bars_at_signal"])
    row("Segment age (bars)", ["pool_age", "exact_active_segment_age_bars_at_signal"])
    row("age_eligible_5bars", ["pool_age", "age_eligible_5bars"])
    row("Upper pool width %", ["upper_entry_pool", "width_pct"])
    row("Upper depth", ["upper_entry_pool", "depth"])
    row("Nearest large upper gap %", ["gap_landscape_counts_from_phase2a", "nearest_upper_large_gap_pct"])
    row("Upper small gaps above pool", ["gap_landscape", "upper_small_gaps_above"])
    row("Upper large gaps above pool", ["gap_landscape", "upper_large_gaps_above"])
    row("Lower bands within 2% below", ["lower_landscape", "lower_bands_within_2pct_below_entry"])
    row("Max lower depth within 2%", ["lower_landscape", "max_depth_within_2pct"])
    row("Dist entry→nearest lower top %", ["lower_landscape", "distance_entry_to_nearest_lower_top_pct"])
    row("Bearish signal candle", ["rejection_signal_candle", "bearish_close_lt_open"])

    lines.extend(
        [
            "",
            "## C003 vs C004 (both continuous D2 age = 1)",
            "",
            "Shared: young **continuous** Depth-2 zone at signal; **neither** `age_eligible_5bars`.",
            "Divergence to investigate in JSON profiles: upper gap structure, lower cluster density/depth,",
            "EMA stack, signal candle geometry, and **origin/segment age** (C004 much older origins than C003).",
            "",
            "## C008 (no manual verdict)",
            "",
            "Continuous D2 age **14** → `age_eligible_5bars` and `age_eligible_10bars` true.",
            "Use only for landscape evolution vs C003/C004 — **not** as confirmed user SHORT.",
            "",
            "## Research questions (no rules implemented)",
            "",
            "1. Does user ‘perfect’ SHORT (C003) correlate with lower-target depth despite young D2 zone?",
            "2. Is ‘too early’ (C004) linked to richer lower landscape / different upper gap stacking vs C003?",
            "3. Does C008’s aged upper zone change rejection quality or gap isolation vs C004?",
            "4. Should age gate use continuous D2, origin persistence, or hybrid? (C003 fails all gates.)",
            "",
            "## Outputs",
            "",
            "`results/phase2e_entry_landscape_contrast_v1/`",
            "",
        ]
    )
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    window = load_window_from_ssot("2026-09-13T02:45:00Z")
    prices = window.get("prices") or []

    bundles = [build_case_bundle(k, m, prices) for k, m in CASES.items()]
    (RESULTS / "contrast_C003_C004_C008.json").write_text(
        json.dumps({"cases": bundles}, indent=2), encoding="utf-8"
    )
    for b in bundles:
        (RESULTS / f"{b['case_key']}_profile.json").write_text(
            json.dumps(b, indent=2), encoding="utf-8"
        )

    consistency = (
        "Phase 2D markdown table for C001–C005 used abbreviated numbers in prose; "
        "**authoritative** origin/segment ages are in `gate_comparison_all_20.csv` "
        "(e.g. C001 origin=3, C004 origin=14, C003 origin=3). No silent recalculation in 2E."
    )
    write_report(bundles, consistency)

    # Enriched diff narrative from computed bundles
    c3, c4, c8 = bundles
    diff = {
        "c003_vs_c004_same_continuous_age": {
            "continuous_d2": c3["pool_age"]["continuous_depth2_zone_age_bars"]
            == c4["pool_age"]["continuous_depth2_zone_age_bars"],
            "origin_age_delta": c4["pool_age"]["origin_age_bars_at_signal"]
            - c3["pool_age"]["origin_age_bars_at_signal"],
            "upper_width_pct": {
                "c003": c3["upper_entry_pool"]["width_pct"],
                "c004": c4["upper_entry_pool"]["width_pct"],
            },
            "nearest_large_gap_pct": {
                "c003": c3["gap_landscape_counts_from_phase2a"]["nearest_upper_large_gap_pct"],
                "c004": c4["gap_landscape_counts_from_phase2a"]["nearest_upper_large_gap_pct"],
            },
            "lower_within_2pct_count": {
                "c003": c3["lower_landscape"]["lower_bands_within_2pct_below_entry"],
                "c004": c4["lower_landscape"]["lower_bands_within_2pct_below_entry"],
            },
            "max_lower_depth_2pct": {
                "c003": c3["lower_landscape"]["max_depth_within_2pct"],
                "c004": c4["lower_landscape"]["max_depth_within_2pct"],
            },
        },
        "c008_vs_c004": {
            "continuous_d2_age": c8["pool_age"]["continuous_depth2_zone_age_bars"],
            "age_eligible_5": c8["pool_age"]["age_eligible_5bars"],
            "upper_small_gaps_above": c8["gap_landscape"]["upper_small_gaps_above"],
            "c004_upper_small_gaps": c4["gap_landscape"]["upper_small_gaps_above"],
        },
    }
    (RESULTS / "measured_differences.json").write_text(
        json.dumps(diff, indent=2), encoding="utf-8"
    )
    print(json.dumps({"cases": list(CASES.keys()), "written": str(RESULTS)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

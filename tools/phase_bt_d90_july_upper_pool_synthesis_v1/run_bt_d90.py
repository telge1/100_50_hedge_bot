#!/usr/bin/env python3
"""BT-D90: Synthesize BT-D87/D88/D89 per-trade artifacts (no new data collection)."""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D87 = ROOT / "results/phase_bt_d87_weak_upper_pool_analysis_v1"
D88 = ROOT / "results/phase_bt_d88_upper_pool_persistence_audit_v1"
D89 = ROOT / "results/phase_bt_d89_active_origin_overlap_audit_v1"
OUT = ROOT / "results/phase_bt_d90_july_upper_pool_synthesis_v1"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_d87_all() -> dict[str, dict[str, str]]:
    rows = _read_csv(D87 / "selected_13_pool_properties.csv") + _read_csv(
        D87 / "comparison_12_pool_properties.csv"
    )
    by_fill: dict[str, dict[str, str]] = {}
    for r in rows:
        fill = r["fill_time_utc"]
        if fill in by_fill:
            raise SystemExit(f"D87 duplicate fill: {fill}")
        by_fill[fill] = r
    return by_fill


def _index_by_fill(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for r in _read_csv(path):
        fill = r["fill_time_utc"]
        if fill in out:
            raise SystemExit(f"duplicate fill in {path.name}: {fill}")
        out[fill] = r
    return out


def _summ(vals: list[float | None]) -> dict[str, Any]:
    xs = [float(v) for v in vals if v is not None]
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs),
        "max": max(xs),
        "median": statistics.median(xs),
        "mean": round(statistics.mean(xs), 6),
    }


def _evidence_quality(d87: dict, d88: dict, d89: dict) -> str:
    parts = ["d87_d8_exact_at_signal"]
    if int(_f(d89.get("window_bars_pre_july")) or 0) > 0:
        parts.append("d89_pre_july_ch_region_proxy")
    else:
        parts.append("d89_july_d8_exact_window")
    if d88.get("window_limit_note") == "bundle_start_july_1":
        parts.append("d88_left_censored_july1")
    return "+".join(parts)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    d87 = _load_d87_all()
    d88 = _index_by_fill(D88 / "per_trade_persistence.csv")
    d89 = _index_by_fill(D89 / "per_trade_origin_overlap.csv")

    fills = sorted(set(d87) | set(d88) | set(d89))
    if len(fills) != 25:
        raise SystemExit(f"expected 25 fills, got {len(fills)}")
    for fill in fills:
        if fill not in d87 or fill not in d88 or fill not in d89:
            raise SystemExit(f"missing phase row for {fill}")

    unified: list[dict[str, Any]] = []
    for fill in fills:
        a, b, c = d87[fill], d88[fill], d89[fill]
        if a["signal_close_utc"] != b["signal_close_utc"] != c["signal_close_utc"]:
            raise SystemExit(f"signal mismatch {fill}")
        user = a.get("user_selected_weak") == "True"
        row = {
            "fill_time_utc": fill,
            "signal_close_utc": a["signal_close_utc"],
            "user_visually_flagged_weak": user,
            "exit_reason": a["exit_reason"],
            "entry_price": _f(a["entry_price"]),
            "band_width_pct_of_entry_d87": _f(a["band_width_pct_of_entry"]),
            "width_rank_pct_july25_d87": _f(a["width_rank_pct_among_july_trade_bands"]),
            "unique_origin_count_d87_depth2": int(_f(a["unique_origin_count"]) or 0),
            "gap_to_next_upper_abs_d87": _f(a["gap_to_next_upper_lower_abs"]),
            "structure_label_d87": a.get("structure_label_descriptive"),
            "persistence_bars_price_tracked_d88": int(_f(b["persistence_bars_price_tracked"]) or 0),
            "overlap_bars_depth2_ge2_d88_july_window": int(_f(b["qualifying_depth2_bars_in_track"]) or 0),
            "hours_signal_minus_first_tracked_d88": _f(b["hours_signal_minus_first_tracked"]),
            "sudden_appearance_d88": b.get("sudden_appearance_heuristic") == "True",
            "overlap_bars_ge2_d89_168h_mixed": int(_f(c["bars_with_depth2_ge2_in_region"]) or 0),
            "overlap_fraction_d89_168h_mixed": _f(c["fraction_window_bars_depth2_ge2"]),
            "window_bars_pre_july_d89": int(_f(c["window_bars_pre_july"]) or 0),
            "ch_known_at_min_hours_d89": _f(c["ch_known_at_min_hours_before_signal"]),
            "segment_epoch_min_hours_d89": _f(c["segment_epoch_min_hours_before_signal"]),
            "origin_jaccard_signal_vs_window_start_d89": _f(c["origin_jaccard_signal_vs_window_start"]),
            "fresh_unstable_heuristic_d89": c.get("label_fresh_unstable_overlap_heuristic") == "True",
            "adjacent_upper_neighbors_50bp_d89": int(_f(c["adjacent_upper_neighbors_50bp_at_signal"]) or 0),
            "evidence_quality_coverage": _evidence_quality(a, b, c),
            "missing_or_proxy_notes": (
                "overlap_fraction mixes D8 exact (Jul) + CH upper-region proxy (pre-Jul); "
                "segment_epoch is not full pool lifetime; ch_known_at is minute-scale at signal; "
                "d88 window may start at Jul-1 not full 168h."
            ),
        }
        unified.append(row)

    fields = list(unified[0].keys())
    with (OUT / "unified_25_trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(unified)

    selected = [r for r in unified if r["user_visually_flagged_weak"]]
    comparison = [r for r in unified if not r["user_visually_flagged_weak"]]

    numeric_keys = [
        "band_width_pct_of_entry_d87",
        "width_rank_pct_july25_d87",
        "unique_origin_count_d87_depth2",
        "gap_to_next_upper_abs_d87",
        "persistence_bars_price_tracked_d88",
        "overlap_bars_depth2_ge2_d88_july_window",
        "overlap_fraction_d89_168h_mixed",
        "overlap_bars_ge2_d89_168h_mixed",
        "ch_known_at_min_hours_d89",
        "segment_epoch_min_hours_d89",
        "hours_signal_minus_first_tracked_d88",
        "adjacent_upper_neighbors_50bp_d89",
    ]
    summary = {
        "baseline_commits": "BT-D87/B88/B89 through 5c9b59fd",
        "n_selected": len(selected),
        "n_comparison": len(comparison),
        "exit_reason_selected": {
            "SIMULATED_TP": sum(1 for r in selected if r["exit_reason"] == "SIMULATED_TP"),
            "SIMULATED_SL": sum(1 for r in selected if r["exit_reason"] == "SIMULATED_SL"),
        },
        "exit_reason_comparison": {
            "SIMULATED_TP": sum(1 for r in comparison if r["exit_reason"] == "SIMULATED_TP"),
            "SIMULATED_SL": sum(1 for r in comparison if r["exit_reason"] == "SIMULATED_SL"),
        },
        "metrics_selected": {k: _summ([r.get(k) for r in selected]) for k in numeric_keys},
        "metrics_comparison": {k: _summ([r.get(k) for r in comparison]) for k in numeric_keys},
        "fresh_unstable_d89_selected": sum(1 for r in selected if r["fresh_unstable_heuristic_d89"]),
        "fresh_unstable_d89_comparison": sum(1 for r in comparison if r["fresh_unstable_heuristic_d89"]),
        "sudden_appearance_d88_selected": sum(1 for r in selected if r["sudden_appearance_d88"]),
        "sudden_appearance_d88_comparison": sum(1 for r in comparison if r["sudden_appearance_d88"]),
        "data_quality": {
            "d88_vs_d89_overlap": (
                "D88 tracks price-region overlap within window often capped at Jul-1; "
                "D89 uses 168h from May with D8 exact Jul bars + CH proxy pre-Jul. "
                "Prefer D89 fraction for cross-trade overlap density; D88 bars for July-centric persistence."
            ),
            "origin_age": "Use ch_known_at_min_hours_d89 (CH SSOT), not segment_epoch alone.",
        },
    }
    (OUT / "grouped_feature_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Counterexamples: TP with weak-feature profile, SL with strong profile, selected wide band TP, etc.
    counter: list[dict[str, Any]] = []

    def add_case(case_id: str, reason: str, row: dict[str, Any]) -> None:
        counter.append(
            {
                "case_id": case_id,
                "reason": reason,
                "fill_time_utc": row["fill_time_utc"],
                "user_visually_flagged_weak": row["user_visually_flagged_weak"],
                "exit_reason": row["exit_reason"],
                "highlights": {
                    "width_pct": row["band_width_pct_of_entry_d87"],
                    "width_rank": row["width_rank_pct_july25_d87"],
                    "overlap_bars_d89": row["overlap_bars_ge2_d89_168h_mixed"],
                    "overlap_frac_d89": row["overlap_fraction_d89_168h_mixed"],
                    "persistence_d88": row["persistence_bars_price_tracked_d88"],
                },
            }
        )

    med_sel_overlap = summary["metrics_selected"]["overlap_bars_ge2_d89_168h_mixed"].get("median", 0)
    med_cmp_overlap = summary["metrics_comparison"]["overlap_bars_ge2_d89_168h_mixed"].get("median", 0)

    for r in unified:
        if r["exit_reason"] == "SIMULATED_TP" and r["user_visually_flagged_weak"]:
            add_case("user_tp", "User-flagged weak visual but TP outcome", r)
        if r["exit_reason"] == "SIMULATED_TP" and (r["overlap_bars_ge2_d89_168h_mixed"] or 0) <= 3:
            add_case("tp_short_overlap", "TP with very short D89 overlap track", r)
        if r["exit_reason"] == "SIMULATED_SL" and (r["overlap_bars_ge2_d89_168h_mixed"] or 0) >= med_cmp_overlap:
            add_case("sl_long_overlap", "SL with long overlap (comparison-like)", r)
        if r["user_visually_flagged_weak"] and (r["width_rank_pct_july25_d87"] or 0) >= 80:
            add_case("user_wide_band", "User-flagged but wide band rank (>=80pct)", r)
        if (not r["user_visually_flagged_weak"]) and (r["width_rank_pct_july25_d87"] or 100) <= 12:
            add_case("not_user_thin_band", "Not user-flagged but thin band rank (<=12pct)", r)

    (OUT / "counterexample_cases.json").write_text(json.dumps(counter, indent=2), encoding="utf-8")
    print(f"Wrote {OUT} unified={len(unified)} counterexamples={len(counter)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

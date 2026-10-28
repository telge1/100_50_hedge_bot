#!/usr/bin/env python3
"""BT-D87: Read-only forensic pool properties for user-selected weak upper entries."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
sys.path.insert(0, str(D8))

from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402

BT85 = ROOT / "results/phase_bt_d85_july_2026_no_pause_research_v1"
LEDGER_PATH = BT85 / "trade_ledger_no_pause.json"
BUNDLE = BT85 / "cache_bundle"
OUT = ROOT / "results/phase_bt_d87_weak_upper_pool_analysis_v1"

USER_FILL_UTC = [
    "2026-07-08T07:20:00Z",
    "2026-07-09T03:05:00Z",
    "2026-07-12T01:15:00Z",
    "2026-07-12T07:00:00Z",
    "2026-07-13T04:25:00Z",
    "2026-07-17T09:20:00Z",
    "2026-07-24T00:55:00Z",
    "2026-07-24T12:05:00Z",
    "2026-07-24T17:15:00Z",
    "2026-07-27T15:10:00Z",
    "2026-07-28T05:00:00Z",
    "2026-07-31T03:50:00Z",
    "2026-07-31T11:00:00Z",
]
USER_FILL_SET = set(USER_FILL_UTC)

UNRESOLVED_FIELDS = [
    "pool_age_bars",
    "pool_persistence_utc",
    "origin_first_seen_utc",
    "lifetime_notional",
    "depth2_overlap_score_ssot",
    "chart_pixel_width",
]


def _f(x: str | float) -> float:
    return float(x)


def _pool_key(lower: str, upper: str) -> str:
    return f"{lower}|{upper}"


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _active_uppers(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for b in snapshot.get("upper_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        lo = str(b["lower_price"])
        hi = str(b["upper_price"])
        rows.append(
            {
                **b,
                "lower_f": _f(lo),
                "upper_f": _f(hi),
                "width_f": _f(hi) - _f(lo),
                "center_f": (_f(lo) + _f(hi)) / 2.0,
                "pool_key": _pool_key(lo, hi),
            }
        )
    rows.sort(key=lambda r: (r["lower_f"], r["upper_f"]))
    return rows


def _origin_set(origin_ids: str) -> set[str]:
    if not origin_ids:
        return set()
    return {p.strip() for p in str(origin_ids).split("|") if p.strip()}


def _ranges_overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    return a_lo <= b_hi and b_lo <= a_hi


def _analyze_trade(
    trade: dict[str, Any],
    *,
    user_selected: bool,
    session: ValidatedDensityCacheSessionV1,
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    signal = str(trade["signal_close_utc"])
    fill = str(trade["fill_time_utc"])
    fill_dt = _parse_utc(fill)
    signal_dt = _parse_utc(signal)
    fill_minus_signal_min = (fill_dt - signal_dt).total_seconds() / 60.0
    if abs(fill_minus_signal_min - 5.0) > 0.01:
        issues.append(f"signal_fill_delta_min:{fill_minus_signal_min}")

    snapshot = session.collect_at_close(signal)
    uppers = _active_uppers(snapshot)
    pool_key = str(trade.get("pool_key") or "")
    matched = next((u for u in uppers if u["pool_key"] == pool_key), None)
    if matched is None:
        issues.append("pool_key_not_in_active_uppers_at_signal")
        # fallback: match by numeric tolerance
        parts = pool_key.split("|")
        if len(parts) == 2:
            lo_t, hi_t = _f(parts[0]), _f(parts[1])
            for u in uppers:
                if abs(u["lower_f"] - lo_t) < 1e-12 and abs(u["upper_f"] - hi_t) < 1e-12:
                    matched = u
                    issues.append("pool_key_matched_by_float_tolerance")
                    break

    entry = float(trade["entry_price"])
    row: dict[str, Any] = {
        "user_selected_weak": user_selected,
        "position_id": trade.get("position_id"),
        "fill_time_utc": fill,
        "signal_close_utc": signal,
        "signal_fill_delta_min": round(fill_minus_signal_min, 2),
        "entry_price": entry,
        "sl_price": trade.get("sl_price"),
        "tp_price": trade.get("tp_price"),
        "exit_reason": trade.get("exit_reason"),
        "pool_key_ledger": pool_key,
        "decision_close_utc": signal,
        "n_active_upper_bands_at_signal": len(uppers),
    }

    if matched is None:
        row.update(
            {
                "band_lower": None,
                "band_upper": None,
                "band_width_abs": None,
                "band_width_pct_of_entry": None,
                "unique_origin_count": None,
                "origin_ids": None,
            }
        )
        return row, issues

    width = matched["width_f"]
    width_pct = (width / entry) * 100.0 if entry else None
    idx = uppers.index(matched)
    prev_u = uppers[idx - 1] if idx > 0 else None
    next_u = uppers[idx + 1] if idx + 1 < len(uppers) else None

    gap_above = None
    if next_u is not None:
        gap_above = next_u["lower_f"] - matched["upper_f"]
    gap_below = None
    if prev_u is not None:
        gap_below = matched["lower_f"] - prev_u["upper_f"]

    stacked_neighbors = 0
    for other in uppers:
        if other is matched:
            continue
        if _ranges_overlap(matched["lower_f"], matched["upper_f"], other["lower_f"], other["upper_f"]):
            stacked_neighbors += 1
        elif abs(other["center_f"] - matched["center_f"]) <= max(width, other["width_f"]) * 2.0:
            stacked_neighbors += 1

    local_50bp = sum(
        1
        for u in uppers
        if abs(u["center_f"] - entry) / entry <= 0.0005
    )
    local_20bp = sum(
        1
        for u in uppers
        if abs(u["center_f"] - entry) / entry <= 0.0002
    )

    shared_next = None
    if next_u is not None:
        shared_next = len(_origin_set(matched["origin_ids"]) & _origin_set(next_u["origin_ids"]))

    structure = "isolated"
    if stacked_neighbors >= 2:
        structure = "stacked_cluster"
    elif stacked_neighbors == 1:
        structure = "paired_or_touching"
    elif gap_above is not None and gap_below is not None:
        if gap_above > width * 3 and gap_below > width * 3:
            structure = "isolated"
        else:
            structure = "spaced_single"

    row.update(
        {
            "band_lower": matched["lower_f"],
            "band_upper": matched["upper_f"],
            "band_width_abs": width,
            "band_width_pct_of_entry": round(width_pct, 6) if width_pct is not None else None,
            "unique_origin_count": int(matched["unique_origin_count"]),
            "origin_ids": str(matched["origin_ids"]),
            "pool_status": str(matched.get("pool_status")),
            "density_method": str(matched.get("density_method")),
            "gap_to_next_upper_lower_abs": gap_above,
            "gap_to_prev_upper_upper_abs": gap_below,
            "next_upper_band_lower": next_u["lower_f"] if next_u else None,
            "prev_upper_band_upper": prev_u["upper_f"] if prev_u else None,
            "stacked_or_near_neighbor_count": stacked_neighbors,
            "local_active_upper_count_within_50bp_of_entry": local_50bp,
            "local_active_upper_count_within_20bp_of_entry": local_20bp,
            "shared_origin_count_with_next_upper": shared_next,
            "structure_label_descriptive": structure,
            "width_rank_pct_among_july_trade_bands": None,  # filled later
        }
    )
    return row, issues


def _summarize_numeric(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    vals = [r[key] for r in rows if r.get(key) is not None and isinstance(r[key], (int, float))]
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": round(statistics.mean(vals), 8),
        "median": round(statistics.median(vals), 8),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    if len(ledger) != 25:
        raise SystemExit(f"expected 25 trades, got {len(ledger)}")

    by_fill = {t["fill_time_utc"]: t for t in ledger}
    missing = [f for f in USER_FILL_UTC if f not in by_fill]
    if missing:
        raise SystemExit(f"user fills not in ledger: {missing}")

    session = ValidatedDensityCacheSessionV1(BUNDLE)
    all_rows: list[dict[str, Any]] = []
    all_issues: dict[str, list[str]] = {}

    for trade in ledger:
        fill = trade["fill_time_utc"]
        user_sel = fill in USER_FILL_SET
        row, issues = _analyze_trade(trade, user_selected=user_sel, session=session)
        all_rows.append(row)
        if issues:
            all_issues[fill] = issues

    widths = sorted(r["band_width_pct_of_entry"] for r in all_rows if r.get("band_width_pct_of_entry") is not None)
    for r in all_rows:
        w = r.get("band_width_pct_of_entry")
        if w is None or not widths:
            continue
        rank = sum(1 for x in widths if x <= w) / len(widths) * 100.0
        r["width_rank_pct_among_july_trade_bands"] = round(rank, 2)

    selected = [r for r in all_rows if r["user_selected_weak"]]
    comparison = [r for r in all_rows if not r["user_selected_weak"]]

    numeric_keys = [
        "band_width_abs",
        "band_width_pct_of_entry",
        "unique_origin_count",
        "n_active_upper_bands_at_signal",
        "gap_to_next_upper_lower_abs",
        "stacked_or_near_neighbor_count",
        "local_active_upper_count_within_50bp_of_entry",
        "local_active_upper_count_within_20bp_of_entry",
        "shared_origin_count_with_next_upper",
    ]

    comparison_json: dict[str, Any] = {
        "baseline": "BT-D85 JULY_MONTH_RESEARCH_PASS 434fbbd8",
        "bundle": str(BUNDLE.relative_to(ROOT)),
        "density": "DOGEUSDT 5m PD1 depth2 exact_unique_origin",
        "user_selected_n": len(selected),
        "comparison_n": len(comparison),
        "user_selected_exit_reasons": {
            "SIMULATED_TP": sum(1 for r in selected if r["exit_reason"] == "SIMULATED_TP"),
            "SIMULATED_SL": sum(1 for r in selected if r["exit_reason"] == "SIMULATED_SL"),
        },
        "comparison_exit_reasons": {
            "SIMULATED_TP": sum(1 for r in comparison if r["exit_reason"] == "SIMULATED_TP"),
            "SIMULATED_SL": sum(1 for r in comparison if r["exit_reason"] == "SIMULATED_SL"),
        },
        "metrics_selected": {k: _summarize_numeric(selected, k) for k in numeric_keys},
        "metrics_comparison": {k: _summarize_numeric(comparison, k) for k in numeric_keys},
        "signal_resolution_issues": all_issues,
        "unavailable_in_cache_snapshot": UNRESOLVED_FIELDS,
    }

    fieldnames = list(all_rows[0].keys())
    for path, rows in (
        (OUT / "selected_13_pool_properties.csv", selected),
        (OUT / "comparison_12_pool_properties.csv", comparison),
    ):
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

    (OUT / "pool_property_comparison.json").write_text(
        json.dumps(comparison_json, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (OUT / "unresolved_source_fields.json").write_text(
        json.dumps(
            {
                "fields_not_in_bt_d8_snapshot": UNRESOLVED_FIELDS,
                "note": (
                    "BT-D8 cache upper_bands carry lower/upper price, unique_origin_count, "
                    "origin_ids, pool_status, density_method only. No age/persistence/lifetime "
                    "fields at decision close."
                ),
                "per_trade_issues": all_issues,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {OUT}")
    print(f"selected={len(selected)} comparison={len(comparison)} issues={len(all_issues)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

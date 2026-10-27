"""Diverse chart-review timestamps (not consecutive duplicates)."""

from __future__ import annotations

from typing import Any


def build_chart_shortlist(rows: list[dict[str, Any]], min_count: int = 12) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "short_ok": [],
        "short_no_r6": [],
        "short_no_other": [],
        "uncertain": [],
        "transition": [],
    }
    for r in rows:
        if r.get("evidence_class", "").startswith("development"):
            continue
        p2 = r.get("P2")
        if p2 == "SHORT_OK":
            buckets["short_ok"].append(r)
        elif p2 == "SHORT_NO" and r.get("R6_sideways"):
            buckets["short_no_r6"].append(r)
        elif p2 == "SHORT_NO":
            buckets["short_no_other"].append(r)
        elif p2 == "UNCERTAIN":
            buckets["uncertain"].append(r)
        if "transition" in str(r.get("market_hypothesis", "")):
            buckets["transition"].append(r)

    picks: list[dict[str, Any]] = []
    plan = [
        ("short_ok", 4),
        ("short_no_r6", 4),
        ("transition", 3),
        ("uncertain", 3),
    ]
    used_ts: set[str] = set()
    for key, n in plan:
        for r in sorted(buckets[key], key=lambda x: x["decision_close_utc"]):
            ts = r["decision_close_utc"]
            if ts in used_ts:
                continue
            if len(picks) >= min_count and key != "uncertain":
                break
            used_ts.add(ts)
            picks.append(_chart_row(r))
            if sum(1 for p in picks if p["bucket"] == key) >= n:
                break
    # Dev anchors for user cross-check (not counted toward generalization quota)
    for ts in ("2026-09-09T16:30:00Z", "2026-09-09T20:55:00Z", "2026-09-11T12:05:00Z"):
        r = next((x for x in rows if x["decision_close_utc"] == ts), None)
        if r and ts not in used_ts:
            picks.append(_chart_row(r, extra_note="development_anchor"))
            used_ts.add(ts)
    return picks[: max(min_count, len(picks))]


def _chart_row(r: dict[str, Any], extra_note: str = "") -> dict[str, Any]:
    bucket = "short_ok" if r.get("P2") == "SHORT_OK" else "other"
    if r.get("P2") == "SHORT_NO" and r.get("R6_sideways"):
        bucket = "short_no_r6"
    if r.get("P2") == "UNCERTAIN":
        bucket = "uncertain"
    return {
        "chart_goto_utc": r["decision_close_utc"],
        "window_id": r.get("window_id"),
        "P2": r.get("P2"),
        "P2_reason": r.get("P2_reason"),
        "R5": r.get("R5"),
        "R6_sideways": r.get("R6_sideways"),
        "ema_stack_bearish": r.get("ema9_lt_20_lt_59_lt_200"),
        "ema59_slope_12b_pct": r.get("ema59_slope_12b_pct"),
        "ema200_slope_12b_pct": r.get("ema200_slope_12b_pct"),
        "chop_ratio_24b": r.get("chop_ratio_24b"),
        "gap_to_next_upper_pct": r.get("gap_to_next_upper_pct"),
        "local_stack_density": r.get("local_stack_density_0p35pct"),
        "pool_validity": r.get("pool_validity"),
        "density_mode": r.get("density_mode"),
        "visual_check": (
            "Confirm EMA stack vs chop; nearest upper isolation and gap; "
            "whether price action matches P2 label (no future bars)."
        ),
        "bucket": bucket,
        "note": extra_note or r.get("market_hypothesis"),
    }

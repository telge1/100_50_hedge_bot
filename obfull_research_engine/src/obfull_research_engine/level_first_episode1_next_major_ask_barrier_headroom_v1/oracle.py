"""Independent oracle — no productive barrier/headroom helpers."""

from __future__ import annotations

from typing import Any

from . import (
    ENTRY_FEE_PCT,
    EXIT_FEE_PCT,
    REQUIRED_GROSS_HEADROOM_PCT,
    REQUIRED_NET_PROFIT_PCT,
    ROUNDTRIP_FEE_PCT,
    TICK_SIZE,
)


def _oracle_gross(target: float, entry: float) -> float:
    return (float(target) / float(entry) - 1.0) * 100.0


def oracle_audit(analysis: dict[str, Any]) -> dict[str, Any]:
    fp = fn = mismatch = look_ahead = 0
    details: list[dict[str, Any]] = []

    # Fee contract
    if abs((ENTRY_FEE_PCT + EXIT_FEE_PCT) - ROUNDTRIP_FEE_PCT) > 1e-12:
        mismatch += 1
        details.append({"type": "fee_sum"})
    if abs((REQUIRED_NET_PROFIT_PCT + ROUNDTRIP_FEE_PCT) - REQUIRED_GROSS_HEADROOM_PCT) > 1e-12:
        mismatch += 1
        details.append({"type": "gross_requirement"})

    for d in analysis.get("decisions") or []:
        if d.get("OUTCOME_ONLY") and d["OUTCOME_ONLY"].get("influences_barrier_selection"):
            fp += 1
            details.append({"type": "outcome_leak", "key": d.get("decision_key")})

        entry = d.get("entry") or {}
        if d.get("coverage_ok") and entry.get("ok"):
            if abs(float(entry["theoretical_long_entry"]) - float(entry["best_ask_at_decision"])) > 1e-9:
                mismatch += 1
                details.append({"type": "entry_not_ask", "key": d.get("decision_key")})
            mid = entry.get("midprice_at_decision")
            if mid is not None and abs(float(entry["theoretical_long_entry"]) - float(mid)) < 1e-12:
                # ask equals mid only if spread 0 — flag if spread > 0
                if float(entry.get("spread") or 0) > 1e-12:
                    fp += 1
                    details.append({"type": "entry_equals_mid_with_spread"})

            # walls below entry must not be long targets
            for w in d.get("wall_candidates_top") or []:
                if w and float(w["price"]) <= float(entry["theoretical_long_entry"]) + 1e-12:
                    fp += 1
                    details.append({"type": "wall_below_entry", "price": w["price"]})

            hb = d.get("headroom_1_tick_before_wall")
            ha = d.get("headroom_at_wall")
            if hb and ha:
                if hb["gross_headroom_pct"] > ha["gross_headroom_pct"] + 1e-12:
                    mismatch += 1
                    details.append({"type": "before_wall_gt_at_wall"})
                # recompute gross
                tr = d.get("table_row") or {}
                if tr.get("nearest_major_wall_price") is not None:
                    og = _oracle_gross(float(ha["conservative_target_price"]), float(entry["theoretical_long_entry"]))
                    if abs(og - float(ha["gross_headroom_pct"])) > 1e-6:
                        mismatch += 1
                        details.append({"type": "gross_math", "oracle": og, "got": ha["gross_headroom_pct"]})
                # slippage separate
                if abs(
                    float(hb["net_headroom_after_fees_and_slippage_pct"])
                    - (
                        float(hb["gross_headroom_pct"])
                        - ROUNDTRIP_FEE_PCT
                        - float(hb["estimated_entry_slippage_pct"])
                        - float(hb["estimated_exit_slippage_pct"])
                    )
                ) > 1e-6:
                    mismatch += 1
                    details.append({"type": "slippage_math"})

            # nearest must be <= strongest price if both walls exist
            sel = d.get("selected") or {}
            near_p = (sel.get("NEAREST_MAJOR_BARRIER") or {}).get("price")
            strong_w = ((sel.get("STRONGEST_VISIBLE_BARRIER") or {}).get("wall") or {})
            strong_p = strong_w.get("price")
            if near_p is not None and strong_p is not None:
                # nearest barrier price should be <= strongest only if strongest is also "major";
                # invariant: first blocking / nearest must not skip a nearer Q95 wall
                q95 = ((d.get("major_views") or {}).get("by_percentile") or {}).get("Q95")
                if q95 and near_p is not None and float(near_p) > float(q95["price"]) + 1e-9:
                    fn += 1
                    details.append({"type": "skipped_nearer_q95", "near": near_p, "q95": q95["price"]})

            # look-ahead: max_input <= decision
            cov = d.get("ob_full_coverage") or {}
            if cov.get("event_available_at_le_decision") is False:
                look_ahead += 1
                details.append({"type": "look_ahead", "key": d.get("decision_key")})

            # mirror: ask distance ticks positive above entry
            ticks = TICK_SIZE
            if near_p is not None:
                dist = (float(near_p) - float(entry["theoretical_long_entry"])) / ticks
                if dist < -1e-9:
                    mismatch += 1
                    details.append({"type": "negative_ask_distance"})

        if d.get("decision_key") == "DETECTION" and d.get("coverage_status") != "HEADROOM_CENSORED_AT_DETECTION":
            fn += 1
            details.append({"type": "detection_not_censored"})

    ok = fp == 0 and fn == 0 and mismatch == 0 and look_ahead == 0
    return {
        "ok": ok,
        "fp": fp,
        "fn": fn,
        "mismatch": mismatch,
        "look_ahead": look_ahead,
        "details": details[:50],
    }

"""Independent oracle for outcome contract (no productive helpers)."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import HORIZON_MS, LABEL_CENSORED, MECHANICAL_LABELS


def _oracle_side(price: float | None, wall: float, side: str) -> str | None:
    if price is None:
        return None
    if side == "ask":
        return "ATTACK" if price >= wall else "DEFENDER"
    return "ATTACK" if price <= wall else "DEFENDER"


def oracle_audit(
    *,
    records: list[dict[str, Any]],
    wall_side: str,
    wall_price: float,
    contract_hash: str,
    expected_hash: str,
) -> dict[str, Any]:
    fp = fn = mismatch = look_ahead = 0
    details: list[dict[str, Any]] = []

    if contract_hash != expected_hash:
        mismatch += 1
        details.append({"type": "contract_hash_mismatch"})

    horizons = set()
    for rec in records:
        facts = rec["facts"]
        label = rec["label"]
        horizons.add(int(facts["horizon_ms"]))

        if facts.get("look_ahead"):
            look_ahead += 1
            details.append({"type": "look_ahead", "anchor": facts.get("anchor_type"), "h": facts.get("horizon_ms")})

        # Label vocabulary
        if label.get("label") not in MECHANICAL_LABELS:
            fp += 1
            details.append({"type": "unknown_label", "label": label.get("label")})

        # Censor consistency
        if not facts.get("coverage_ok"):
            if label.get("label") != LABEL_CENSORED:
                fn += 1
                details.append({"type": "censor_label_fn"})
        else:
            if label.get("label") == LABEL_CENSORED:
                fp += 1
                details.append({"type": "censor_label_fp"})

        # Side consistency at end when coverage ok
        if facts.get("coverage_ok") and facts.get("end_midprice") is not None:
            oside = _oracle_side(float(facts["end_midprice"]), wall_price, wall_side)
            if oside != facts.get("end_price_side"):
                mismatch += 1
                details.append({"type": "end_price_side", "oracle": oside, "got": facts.get("end_price_side")})

        # Anchor/horizon not mixed: horizon_end = anchor + horizon
        if facts.get("coverage_ok") or facts.get("censor_reason"):
            try:
                a = _as_dt(facts["anchor_time"])
                he = _as_dt(facts["horizon_end"])
                expect_ms = int(round((he - a).total_seconds() * 1000))
                if abs(expect_ms - int(facts["horizon_ms"])) > 1:
                    mismatch += 1
                    details.append({"type": "horizon_math"})
            except Exception as exc:  # noqa: BLE001
                mismatch += 1
                details.append({"type": "time_parse", "err": str(exc)})

        # Censored must not be counted as reclaim/breakout success
        if label.get("label") == LABEL_CENSORED:
            if facts.get("joint_reclaim_observed") and facts.get("coverage_ok"):
                fp += 1

    # All contract horizons present for at least one anchor
    missing_h = [h for h in HORIZON_MS if h not in horizons]
    if missing_h:
        fn += 1
        details.append({"type": "missing_horizons", "missing": missing_h})

    ok = fp == 0 and fn == 0 and mismatch == 0 and look_ahead == 0
    return {
        "ok": ok,
        "fp": fp,
        "fn": fn,
        "mismatch": mismatch,
        "look_ahead": look_ahead,
        "n_details": len(details),
        "details_head": details[:30],
    }

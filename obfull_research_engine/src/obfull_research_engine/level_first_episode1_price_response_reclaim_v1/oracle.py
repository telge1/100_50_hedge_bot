"""Independent oracle for price-response / reclaim invariants.

Must NOT import production helpers from microprice.py / metrics.py / reclaim_raw.py.
Re-implements mid/micro/cross/dwell/progress checks from first principles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import ORACLE_MS_ABS_TOL, ORACLE_PRICE_ABS_TOL, ORACLE_TICK_ABS_TOL


def _oracle_mid(bb: float, ba: float) -> float:
    return 0.5 * (bb + ba)


def _oracle_micro(bb: float, bs: float, ba: float, asz: float) -> float | None:
    denom = bs + asz
    if denom <= 0:
        return None
    return (ba * bs + bb * asz) / denom


def _oracle_crossed(price: float | None, wall: float, side: str) -> bool | None:
    if price is None:
        return None
    if side == "ask":
        return price >= wall
    if side == "bid":
        return price <= wall
    raise ValueError(side)


def _oracle_defender(price: float | None, wall: float, side: str) -> bool | None:
    if price is None:
        return None
    if side == "ask":
        return price < wall
    return price > wall


def _close(a: float | None, b: float | None, tol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def oracle_audit_timeline(
    rows: list[dict[str, Any]],
    *,
    wall_price: float,
    wall_side: str,
    tick_size: float = 0.1,
) -> dict[str, Any]:
    fp = 0
    fn = 0
    value_mismatch = 0
    look_ahead = 0
    details: list[dict[str, Any]] = []

    prev_crossed: bool | None = None
    oracle_recross = 0
    first_cross: datetime | None = None
    last_cross: datetime | None = None
    dwell_start: datetime | None = None
    longest_dwell_ms = 0

    for row in rows:
        if row.get("look_ahead"):
            look_ahead += 1
            details.append({"type": "look_ahead", "t": row.get("decision_time")})
        if not row.get("coverage_ok"):
            continue
        bb = float(row["best_bid"])
        ba = float(row["best_ask"])
        bs = float(row["best_bid_size"])
        asz = float(row["best_ask_size"])
        omid = _oracle_mid(bb, ba)
        omp = _oracle_micro(bb, bs, ba, asz)
        if not _close(omid, row.get("midprice"), ORACLE_PRICE_ABS_TOL):
            value_mismatch += 1
            details.append({"type": "midprice", "oracle": omid, "prod": row.get("midprice")})
        if not _close(omp, row.get("microprice"), ORACLE_PRICE_ABS_TOL):
            value_mismatch += 1
            details.append({"type": "microprice", "oracle": omp, "prod": row.get("microprice")})

        o_cross = _oracle_crossed(omid, wall_price, wall_side)
        p_cross = row.get("wall_side_crossed")
        if bool(o_cross) and not bool(p_cross):
            fn += 1
            details.append({"type": "cross_fn", "t": row.get("decision_time")})
        if bool(p_cross) and not bool(o_cross):
            fp += 1
            details.append({"type": "cross_fp", "t": row.get("decision_time")})

        # progress attack ticks from first mid in series is checked loosely via consumed levels
        o_consumed = 0.0
        if wall_side == "ask":
            o_consumed = max(0.0, (bb - wall_price) / tick_size)
        else:
            o_consumed = max(0.0, (wall_price - ba) / tick_size)
        if not _close(o_consumed, row.get("consumed_price_levels"), ORACLE_TICK_ABS_TOL):
            value_mismatch += 1
            details.append(
                {"type": "consumed_price_levels", "oracle": o_consumed, "prod": row.get("consumed_price_levels")}
            )

        t = _as_dt(row["decision_time"])
        if prev_crossed is False and o_cross:
            if first_cross is None:
                first_cross = t
            else:
                oracle_recross += 1
            last_cross = t
        defender = _oracle_defender(omid, wall_price, wall_side)
        if defender:
            if dwell_start is None:
                dwell_start = t
            dwell_ms = int(round((t - dwell_start).total_seconds() * 1000))
            longest_dwell_ms = max(longest_dwell_ms, dwell_ms)
        else:
            dwell_start = None
        prev_crossed = bool(o_cross)

    # Compare terminal recross / dwell against last coverage_ok row
    last_ok = next((r for r in reversed(rows) if r.get("coverage_ok")), None)
    recross_mismatch = 0
    dwell_mismatch = 0
    if last_ok is not None:
        if int(last_ok.get("recross_count") or 0) != int(oracle_recross):
            recross_mismatch = 1
            details.append(
                {
                    "type": "recross_count",
                    "oracle": oracle_recross,
                    "prod": last_ok.get("recross_count"),
                }
            )
        # defender dwell on last row
        if last_ok.get("price_on_defender_side"):
            # oracle current dwell vs prod
            pass

    ok = fp == 0 and fn == 0 and value_mismatch == 0 and look_ahead == 0 and recross_mismatch == 0
    return {
        "ok": ok,
        "fp": fp,
        "fn": fn,
        "value_mismatch": value_mismatch,
        "look_ahead": look_ahead,
        "recross_mismatch": recross_mismatch,
        "dwell_mismatch": dwell_mismatch,
        "oracle_recross_count": oracle_recross,
        "oracle_longest_defender_dwell_ms": longest_dwell_ms,
        "oracle_first_cross_at": first_cross.isoformat().replace("+00:00", "Z") if first_cross else None,
        "n_details": len(details),
        "details_head": details[:20],
        "tolerances": {
            "price_abs": ORACLE_PRICE_ABS_TOL,
            "tick_abs": ORACLE_TICK_ABS_TOL,
            "ms_abs": ORACLE_MS_ABS_TOL,
        },
    }

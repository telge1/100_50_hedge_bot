"""Three-way research regime labels (not production classifier)."""

from __future__ import annotations

from typing import Any

CHOP_SIDEWAYS = 6.0
NET_FLAT = 0.35


def ema59_flattening(row: dict[str, Any]) -> bool:
    s6 = row.get("ema59_slope_6b_pct")
    s12 = row.get("ema59_slope_12b_pct")
    if s6 is None or s12 is None:
        return False
    return s6 > s12


def research_regime(row: dict[str, Any]) -> str:
    if row.get("ema_validity") != "VALIDATED_TRP_D60":
        return "UNCERTAIN"
    if row.get("pool_validity") in ("MISSING", "UNCERTAIN_NO_DENSITY_AT_CLOSE"):
        # EMA/chop labels still allowed; pool-dependent variants marked separately.
        pool_ok = False
    else:
        pool_ok = True

    chop = float(row.get("chop_ratio_24b") or 0)
    net = float(row.get("net_move_24b_pct") or 0)
    stack = int(row.get("local_stack_density_0p35pct") or 0)
    ema200_12 = row.get("ema200_slope_12b_pct")
    flat59 = ema59_flattening(row)

    sideways_score = 0
    if chop >= CHOP_SIDEWAYS:
        sideways_score += 1
    if abs(net) < NET_FLAT:
        sideways_score += 1
    if flat59 and ema200_12 is not None and ema200_12 < 0:
        sideways_score += 1
    if pool_ok and stack >= 3:
        sideways_score += 1
    if pool_ok and row.get("origin_ids_added"):
        sideways_score += 1

    if sideways_score >= 2:
        return "SIDEWAYS_CANDIDATE"

    bear_stack = bool(row.get("ema9_lt_20_lt_59_lt_200"))
    below59 = bool(row.get("close_below_ema59"))
    if bear_stack and below59 and net < -0.15 and chop < 8.0:
        if (not pool_ok or stack <= 2) and not flat59:
            return "DOWNTREND_CANDIDATE"
    return "UNCERTAIN"

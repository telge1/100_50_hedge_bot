"""Research ablation variants A–E (past-only, UNVALIDATED thresholds)."""

from __future__ import annotations

from typing import Any

# Transparent research thresholds — NOT production gates.
CHOP_MAX_D = 8.0
NET_MOVE_MAX_D = 0.0
MIN_GAP_PCT_D = 0.5
MAX_STACK_D = 2
STACK_GROWTH_WARN = 3


def ablation_flags(row: dict[str, Any]) -> dict[str, Any]:
    a = bool(row.get("ema9_lt_20_lt_59_lt_200"))
    b = a
    if b:
        s59 = row.get("ema59_slope_12b_pct")
        s200 = row.get("ema200_slope_12b_pct")
        b = s59 is not None and s200 is not None and s59 < 0 and s200 < 0
    c = b
    if c:
        net = row.get("net_move_24b_pct")
        chop = row.get("chop_ratio_24b")
        c = net is not None and chop is not None and net < NET_MOVE_MAX_D and chop < CHOP_MAX_D
    d = c
    if d:
        gap = row.get("gap_to_next_upper_pct")
        between = row.get("upper_bands_between_entry_and_next")
        stack = row.get("local_stack_density_0p35pct")
        d = (
            gap is not None
            and gap >= MIN_GAP_PCT_D
            and between == 0
            and stack is not None
            and stack <= MAX_STACK_D
        )
    e = d
    if e:
        growth = (row.get("local_stack_density_0p35pct") or 0) >= STACK_GROWTH_WARN or bool(
            row.get("origin_ids_added")
        )
        e = not growth
    return {
        "variant_A_ema_order": a,
        "variant_B_plus_slopes_12b": b,
        "variant_C_plus_direction_chop": c,
        "variant_D_plus_upper_isolation": d,
        "variant_E_plus_no_stack_growth": e,
        "research_allow_downtrend_short": e,
        "research_block_sideways_stack": bool(
            (row.get("local_stack_density_0p35pct") or 0) >= STACK_GROWTH_WARN
            or bool(row.get("origin_ids_added"))
            or ((row.get("chop_ratio_24b") or 0) >= CHOP_MAX_D)
        ),
    }


def first_variant_drop(flags: dict[str, Any]) -> str:
    order = [
        ("A", "variant_A_ema_order"),
        ("B", "variant_B_plus_slopes_12b"),
        ("C", "variant_C_plus_direction_chop"),
        ("D", "variant_D_plus_upper_isolation"),
        ("E", "variant_E_plus_no_stack_growth"),
    ]
    for label, key in order:
        if not flags.get(key):
            return f"FAIL_{label}"
    return "PASS_E"

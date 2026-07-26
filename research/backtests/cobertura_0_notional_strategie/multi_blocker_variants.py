"""Research-only Cobertura multi-blocker variant definitions."""

from __future__ import annotations

from typing import Any

VARIANT_BASELINE = "baseline"
VARIANT_NEXT_BAR_EXIT = "next_bar_exit"
VARIANT_GAP_OPEN = "gap_open"
VARIANT_NEXT_BAR_EXIT_GAP_OPEN = "next_bar_exit_gap_open"

ALL_VARIANTS = (
    VARIANT_BASELINE,
    VARIANT_NEXT_BAR_EXIT,
    VARIANT_GAP_OPEN,
    VARIANT_NEXT_BAR_EXIT_GAP_OPEN,
)

DEFAULT_HORIZONS_DAYS = (30, 60, 90, 120)

# APT forensic fingerprint (V0 must reproduce; not used as seed).
APT_TRADE_ID = "APTUSDT|two_early_medium|continuous|0006"
APT_REGRESSION = {
    "fill_timestamp_prefix": "2026-01-19T00:05:00",
    "fill_price": 1.6447,
    "neutralization_qty": 98.768,
    "core_qty": 296.365,
    "core_short_avg": 1.791289264225859,
    "overlay_add_fills": 16,
    "overlay_be_closes": 7,
    "exit_timestamp_prefix": "2026-02-06T00:15:00",
    "realized_overlay_pnl": 46.149957799999854,
    "final_total_exit_economics": 21.858019294808667,
    "combined_before_unresolved_fees": 9.957886192741164,
}


def parse_variants(raw: str | None) -> list[str]:
    if not raw:
        return list(ALL_VARIANTS)
    out: list[str] = []
    for part in str(raw).split(","):
        name = part.strip()
        if not name:
            continue
        if name not in ALL_VARIANTS:
            raise ValueError(f"unknown variant: {name}")
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("no variants selected")
    return out


def variant_engine_flags(variant: str) -> dict[str, Any]:
    if variant == VARIANT_BASELINE:
        return {
            "defer_full_exit_after_same_bar_adds": False,
            "gap_through_trigger_fills": False,
        }
    if variant == VARIANT_NEXT_BAR_EXIT:
        return {
            "defer_full_exit_after_same_bar_adds": True,
            "gap_through_trigger_fills": False,
        }
    if variant == VARIANT_GAP_OPEN:
        return {
            "defer_full_exit_after_same_bar_adds": False,
            "gap_through_trigger_fills": True,
        }
    if variant == VARIANT_NEXT_BAR_EXIT_GAP_OPEN:
        return {
            "defer_full_exit_after_same_bar_adds": True,
            "gap_through_trigger_fills": True,
        }
    raise ValueError(f"unknown variant: {variant}")

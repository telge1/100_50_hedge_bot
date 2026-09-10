"""Fixed fee / headroom arithmetic — slippage separate from fees."""

from __future__ import annotations

from typing import Any

from . import (
    ENTRY_FEE_PCT,
    ESTIMATED_ENTRY_SLIPPAGE_PCT,
    ESTIMATED_EXIT_SLIPPAGE_PCT,
    EXIT_FEE_PCT,
    REQUIRED_GROSS_HEADROOM_PCT,
    REQUIRED_NET_PROFIT_PCT,
    ROUNDTRIP_FEE_PCT,
    TICK_SIZE,
)


def assert_fee_contract() -> None:
    if abs((ENTRY_FEE_PCT + EXIT_FEE_PCT) - ROUNDTRIP_FEE_PCT) > 1e-12:
        raise AssertionError("entry+exit fee must equal roundtrip fee")
    if abs((REQUIRED_NET_PROFIT_PCT + ROUNDTRIP_FEE_PCT) - REQUIRED_GROSS_HEADROOM_PCT) > 1e-12:
        raise AssertionError("0.30 net + 0.11 fees must equal 0.410 gross")


def gross_headroom_pct(*, target: float, entry: float) -> float:
    return (float(target) / float(entry) - 1.0) * 100.0


def required_target_price(*, entry: float, required_gross_pct: float = REQUIRED_GROSS_HEADROOM_PCT) -> float:
    return float(entry) * (1.0 + float(required_gross_pct) / 100.0)


def headroom_bundle(
    *,
    executable_entry: float,
    conservative_target: float,
    entry_slippage_pct: float = ESTIMATED_ENTRY_SLIPPAGE_PCT,
    exit_slippage_pct: float = ESTIMATED_EXIT_SLIPPAGE_PCT,
) -> dict[str, Any]:
    """Compute headroom metrics. Slippage never folded into fee fields."""
    assert_fee_contract()
    entry = float(executable_entry)
    target = float(conservative_target)
    gross = gross_headroom_pct(target=target, entry=entry)
    net_fees = gross - ROUNDTRIP_FEE_PCT
    net_all = gross - ROUNDTRIP_FEE_PCT - float(entry_slippage_pct) - float(exit_slippage_pct)
    req = required_target_price(entry=entry)
    shortfall_pct = max(0.0, REQUIRED_GROSS_HEADROOM_PCT - gross)
    shortfall_ticks = max(0.0, (req - target) / TICK_SIZE) if target < req else 0.0
    return {
        "executable_entry_price": entry,
        "conservative_target_price": target,
        "gross_headroom_pct": gross,
        "entry_fee_pct": ENTRY_FEE_PCT,
        "exit_fee_pct": EXIT_FEE_PCT,
        "roundtrip_fee_pct": ROUNDTRIP_FEE_PCT,
        "estimated_entry_slippage_pct": float(entry_slippage_pct),
        "estimated_exit_slippage_pct": float(exit_slippage_pct),
        "total_cost_pct": ROUNDTRIP_FEE_PCT + float(entry_slippage_pct) + float(exit_slippage_pct),
        "net_headroom_after_fees_pct": net_fees,
        "net_headroom_after_fees_and_slippage_pct": net_all,
        "required_net_profit_pct": REQUIRED_NET_PROFIT_PCT,
        "required_gross_headroom_pct": REQUIRED_GROSS_HEADROOM_PCT,
        "required_target_price_for_0_30_net": req,
        "shortfall_to_required_target_ticks": shortfall_ticks,
        "shortfall_to_required_target_pct": shortfall_pct,
        "gross_requirement_met": gross + 1e-15 >= REQUIRED_GROSS_HEADROOM_PCT,
        "net_requirement_met_before_slippage": net_fees + 1e-15 >= REQUIRED_NET_PROFIT_PCT,
        "net_requirement_met_after_slippage": net_all + 1e-15 >= REQUIRED_NET_PROFIT_PCT,
    }


def conservative_targets(*, wall_price: float, spread: float | None, tick: float = TICK_SIZE) -> dict[str, float]:
    """Descriptive exit targets before / at wall — no Ep1-optimal pick."""
    w = float(wall_price)
    sp = float(spread) if spread is not None and spread > 0 else tick
    return {
        "target_at_wall": w,
        "target_1_tick_before_wall": w - tick,
        "target_2_ticks_before_wall": w - 2 * tick,
        "target_5_ticks_before_wall": w - 5 * tick,
        "target_1_spread_before_wall": w - sp,
    }

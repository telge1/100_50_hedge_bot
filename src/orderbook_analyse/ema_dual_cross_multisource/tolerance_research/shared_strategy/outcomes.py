"""Canonical TP/SL outcome simulation wrappers."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..tpsl_pnl_engine import apply_costs, simulate_tpsl_trade
from .semantics import (
    INCOMPLETE_OUTCOME_REASON,
    REF_COST_PCT,
    REF_NOTIONAL,
    REQUIRE_FULL_HORIZON,
    SAME_BAR_RULE,
)


def simulate_canonical_trade(
    candles_1m: pd.DataFrame,
    *,
    direction: str,
    entry_at,
    entry_price: float,
    tp_pct: float,
    sl_pct: float,
    horizon_min: int,
) -> dict[str, Any]:
    """Shared outcome path used by XRP matrix replay and multicoin.

    ``REQUIRE_FULL_HORIZON`` is frozen False. Premature 1m path end is classified
    as INCOMPLETE (not TIME) via engine policy below.
    """
    sim = simulate_tpsl_trade(
        candles_1m,
        direction=direction,
        entry_at=entry_at,
        entry_price=entry_price,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        horizon_min=horizon_min,
        require_full_horizon=REQUIRE_FULL_HORIZON,
        incomplete_if_truncated_path=True,
    )
    paid = apply_costs(sim, REF_COST_PCT, funding_pnl_usdt=0.0)
    paid["same_bar_rule"] = SAME_BAR_RULE
    paid["notional_usdt"] = REF_NOTIONAL
    paid["roundtrip_cost_pct"] = REF_COST_PCT
    if paid.get("exit_reason") == INCOMPLETE_OUTCOME_REASON:
        paid["include_in_primary_pnl"] = False
    return paid

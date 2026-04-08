from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FixedCycleConfig:
    symbol: str = "BTCUSDT"
    category: str = "linear"

    # Entry
    base_notional_usdt: float = 100.0  # quote amount for sizing the initial long leg
    hedge_ratio_short: float = 0.5  # short leg size as a fraction of the long base
    initial_entry_order_type: str = "Market"
    long_exit_reduce_only: bool = True  # whether the long exit is reduce-only

    # Cycle sizing
    reduction_pct_per_fill: float = 0.25  # percent of remaining position reduced per cycle
    long_cycle_qty_pct_of_initial: float = 0.25  # chunk size of each pre-placed long cycle
    short_cycle_qty_pct_of_initial: float = 0.25  # chunk size of each pre-placed short cycle

    # Cycle placement
    long_fill_distance_pct: float = 0.15  # trigger distance below entry for long cycle orders
    short_fill_distance_pct: float = 0.45  # trigger distance below entry for short cycle orders
    max_cycles: int = 10  # maximum number of pre-placed cycle orders per side

    # TP / Break-even
    tp_buffer_pct: float = 0.04  # extra TP buffer added to the break-even-derived price
    fee_safety_buffer_pct: float = 0.11  # percent buffer reserved for fees inside break-even
    net_realized_pnl_target: float = 0.0  # base target for the calculated break-even

    # Hard stop
    hard_stop_cycle: int = 8  # switch into hard-stop mode when this cycle index is reached
    hard_stop_pct: float = 1.0  # extra short stop buffer applied in hard-stop mode

    # Runtime / exchange constraints
    rest_poll_after_fill_ms: int = 250  # pause (ms) before refreshing REST state after a fill
    order_refresh_cooldown_ms: int = 750  # minimum ms between rebuild attempts


StrategyConfig = FixedCycleConfig

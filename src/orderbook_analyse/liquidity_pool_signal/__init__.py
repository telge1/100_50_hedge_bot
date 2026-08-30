"""Liquidity Pool Signal — Chart Liquidity Location detection foundation.

Pool detection only. Not a trading signal or strategy.
"""

from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    chart_lookback_start,
    chart_pool_engine,
    classify_market_pool_location,
    export_snapshot,
    get_engine_function,
    nearest_front,
    parity_pair,
    run_chart_backend_lld,
)
from orderbook_analyse.liquidity_pool_signal.contracts import (
    MarketPoolLocation,
    PoolSide,
    PoolSnapshot,
)

# Foundation invariant: same object as Research Charts LLD engine.
engine_function = get_engine_function

__all__ = [
    "MarketPoolLocation",
    "PoolSide",
    "PoolSnapshot",
    "chart_lookback_start",
    "chart_pool_engine",
    "classify_market_pool_location",
    "engine_function",
    "export_snapshot",
    "get_engine_function",
    "nearest_front",
    "parity_pair",
    "run_chart_backend_lld",
]

"""Liquidity Pool Signal — Chart Liquidity Location detection foundation.

Pool detection only. Not a trading signal or strategy.
"""

from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    chart_lookback_start,
    chart_pool_engine,
    classify_market_pool_location,
    export_snapshot,
    fingerprint,
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
from orderbook_analyse.liquidity_pool_signal.canonical import (
    CANONICAL_PROVIDER_VERSION,
    CANONICAL_RESTORE_ANCHOR_SHA,
    POOL_ARRIVALS_CSV_NOT_POOL_SOURCE_OF_TRUTH,
    CanonicalPoolParityError,
    assert_canonical_pool_parity,
    causal_pane_lld_bundle,
    clip_overlays_to_as_of,
    liquidity_settings_dict,
    parse_as_of_iso,
)

# Foundation invariant: same object as Research Charts LLD engine.
engine_function = get_engine_function

__all__ = [
    "CANONICAL_PROVIDER_VERSION",
    "CANONICAL_RESTORE_ANCHOR_SHA",
    "POOL_ARRIVALS_CSV_NOT_POOL_SOURCE_OF_TRUTH",
    "CanonicalPoolParityError",
    "MarketPoolLocation",
    "PoolSide",
    "PoolSnapshot",
    "assert_canonical_pool_parity",
    "causal_pane_lld_bundle",
    "chart_lookback_start",
    "chart_pool_engine",
    "classify_market_pool_location",
    "clip_overlays_to_as_of",
    "engine_function",
    "export_snapshot",
    "fingerprint",
    "get_engine_function",
    "liquidity_settings_dict",
    "nearest_front",
    "parse_as_of_iso",
    "parity_pair",
    "run_chart_backend_lld",
]

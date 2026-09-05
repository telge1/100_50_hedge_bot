"""F3 aggregate wall proxy discovery."""

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.discovery import (
    AggregateProxyRunResult,
    run_aggregate_proxy_discovery,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import AggregateProxyError

__all__ = [
    "AggregateProxyError",
    "AggregateProxyRunResult",
    "run_aggregate_proxy_discovery",
]

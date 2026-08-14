"""Dashboard Research Charts adapter.

Python engines live in trading_research_platform. Named research_charts to
avoid colliding with the repo-root ``research/`` package.
"""

from .boundary import (
    DESKTOP_ONLY,
    REUSE_DIRECTLY,
    ADAPT_FOR_WEB,
    TRP_ROOT,
    PHASE_1_FEED_READY,
)
from .clickhouse_source import ClickHouseResearchCandleSource
from .data_source import MySQLResearchCandleSource

__all__ = [
    "DESKTOP_ONLY",
    "REUSE_DIRECTLY",
    "ADAPT_FOR_WEB",
    "TRP_ROOT",
    "PHASE_1_FEED_READY",
    "MySQLResearchCandleSource",
    "ClickHouseResearchCandleSource",
]

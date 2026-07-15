"""Isolated MySQL candle storage layer for the regime scanner research data path.

Operational candles:
- 5m from canonical Freqtrade feather (``freqtrade_direct``)
- 15m / 30m only via ``timeframes.aggregate_candles`` (``aggregated_from_5m``)

Direct Freqtrade 15m/30m staging files are validation references only and must
not be imported into ``market_candles``.
"""

from __future__ import annotations

from research.regime_scanner.mysql_candle_store.config import (
    RegimeDbConfig,
    RegimeDbConfigError,
    load_regime_db_config,
)
from research.regime_scanner.mysql_candle_store.repository import load_candles
from research.regime_scanner.mysql_candle_store.schema import SCHEMA_SQL, SCHEMA_VERSION

__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "RegimeDbConfig",
    "RegimeDbConfigError",
    "load_candles",
    "load_regime_db_config",
]

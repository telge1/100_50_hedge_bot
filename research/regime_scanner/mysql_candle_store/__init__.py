"""Isolated MySQL candle storage layer for the regime scanner research data path.

Scanner operational path (unchanged):
- 5m / aggregated 15m / 30m via ``timeframes.aggregate_candles``

Research HTF path (additive):
- Direct Freqtrade feathers for ``4h`` / ``1d`` / ``1w`` / ``1M`` (and optionally
  ``1h``) may be imported as ``freqtrade_direct`` with ``source_timeframe`` equal
  to the candle timeframe. Existing rows are never deleted; upserts follow
  ``source_policy``.
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

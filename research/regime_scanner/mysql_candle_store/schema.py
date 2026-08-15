"""SQL schema for regime scanner candle storage."""

from __future__ import annotations

SCHEMA_VERSION = "1"

# DATETIME(6) stores UTC wall-clock without session TZ conversion (unlike TIMESTAMP).
# DOUBLE stores IEEE float64 values matching Feather float64 OHLCV.

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_candles (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  exchange VARCHAR(32) NOT NULL,
  symbol VARCHAR(32) NOT NULL,
  -- utf8mb4_bin: required so Freqtrade labels ``1m`` (minute) and ``1M`` (month)
  -- remain distinct under UNIQUE (exchange, symbol, timeframe, open_time).
  timeframe VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  open_time DATETIME(6) NOT NULL COMMENT 'UTC candle open',
  close_time DATETIME(6) NOT NULL COMMENT 'UTC candle close = open + timeframe',
  open DOUBLE NOT NULL,
  high DOUBLE NOT NULL,
  low DOUBLE NOT NULL,
  close DOUBLE NOT NULL,
  volume DOUBLE NOT NULL,
  is_closed TINYINT(1) NOT NULL,
  source VARCHAR(32) NOT NULL COMMENT 'freqtrade_direct | aggregated_from_5m',
  source_timeframe VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL COMMENT 'for direct: same as timeframe, for aggregated HTF: 5m',
  source_hash CHAR(64) NULL COMMENT 'SHA256 of input feather or aggregation batch',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_market_candles_identity (exchange, symbol, timeframe, open_time),
  KEY idx_market_candles_lookup (exchange, symbol, timeframe, open_time),
  KEY idx_market_candles_close (exchange, symbol, timeframe, close_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS data_validation_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  validation_type VARCHAR(64) NOT NULL,
  exchange VARCHAR(32) NOT NULL,
  symbol VARCHAR(32) NOT NULL,
  timeframe VARCHAR(8) NULL,
  canonical_source VARCHAR(128) NULL,
  comparison_source VARCHAR(128) NULL,
  input_path TEXT NULL,
  input_sha256 CHAR(64) NULL,
  common_start DATETIME(6) NULL,
  common_end DATETIME(6) NULL,
  row_count BIGINT NULL,
  shared_buckets BIGINT NULL,
  ohlc_mismatches BIGINT NULL,
  volume_mismatches BIGINT NULL,
  volume_within_tolerance BIGINT NULL,
  max_open_diff DOUBLE NULL,
  max_high_diff DOUBLE NULL,
  max_low_diff DOUBLE NULL,
  max_close_diff DOUBLE NULL,
  max_volume_diff DOUBLE NULL,
  deterministic_output_hash CHAR(64) NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY idx_data_validation_runs_lookup (exchange, symbol, validation_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

SOURCE_FREQTRADE_DIRECT = "freqtrade_direct"
SOURCE_AGGREGATED_FROM_5M = "aggregated_from_5m"

ALLOWED_SOURCES = frozenset({SOURCE_FREQTRADE_DIRECT, SOURCE_AGGREGATED_FROM_5M})
# Scanner operational TFs (unchanged).
OPERATIONAL_TIMEFRAMES = ("5m", "15m", "30m")
# Direct import allowlist for full MTF candle database research.
DIRECT_IMPORT_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")

# Existing DBs created with table default unicode_ci cannot store both ``1m`` and ``1M``.
# Idempotent ALTER: column types unchanged; only collation becomes case-sensitive.
ENSURE_TIMEFRAME_BIN_COLLATION_SQL = """
ALTER TABLE market_candles
  MODIFY COLUMN timeframe VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  MODIFY COLUMN source_timeframe VARCHAR(8) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL
  COMMENT 'for direct: same as timeframe, for aggregated HTF: 5m'
"""

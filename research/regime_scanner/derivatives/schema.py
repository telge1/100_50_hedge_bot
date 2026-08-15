"""Additive schema for curated derivatives 5m research cache."""

from __future__ import annotations

SCHEMA_VERSION = "derivatives_5m_v1"

# Split as separate statements (do not join with ';'-split on comments).
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS research_derivative_import_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  import_label VARCHAR(128) NOT NULL,
  import_version VARCHAR(64) NOT NULL,
  source_database VARCHAR(64) NOT NULL,
  source_table VARCHAR(64) NOT NULL,
  source_min_timestamp DATETIME(6) NULL,
  source_max_timestamp DATETIME(6) NULL,
  target_timeframe VARCHAR(8) NOT NULL DEFAULT '5m',
  symbols_requested JSON NOT NULL,
  symbols_completed JSON NULL,
  status VARCHAR(32) NOT NULL,
  dry_run TINYINT(1) NOT NULL DEFAULT 0,
  source_query_hash CHAR(64) NULL,
  config_hash CHAR(64) NULL,
  rows_read BIGINT NOT NULL DEFAULT 0,
  buckets_generated BIGINT NOT NULL DEFAULT 0,
  rows_inserted BIGINT NOT NULL DEFAULT 0,
  rows_updated BIGINT NOT NULL DEFAULT 0,
  rows_unchanged BIGINT NOT NULL DEFAULT 0,
  rows_rejected BIGINT NOT NULL DEFAULT 0,
  started_at DATETIME(6) NULL,
  finished_at DATETIME(6) NULL,
  error_message TEXT NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_deriv_import_label (import_label),
  KEY idx_deriv_import_version (import_version, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""",
    """
CREATE TABLE IF NOT EXISTS research_open_interest_5m (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  symbol VARCHAR(32) NOT NULL,
  bucket_start DATETIME(6) NOT NULL,
  bucket_end DATETIME(6) NOT NULL,
  open_interest DOUBLE NULL,
  open_interest_usd DOUBLE NULL,
  source_first_timestamp DATETIME(6) NULL,
  source_last_timestamp DATETIME(6) NULL,
  source_row_count INT NOT NULL,
  expected_source_rows INT NOT NULL,
  coverage_ratio DOUBLE NOT NULL,
  data_available TINYINT(1) NOT NULL,
  gap_before_seconds INT NULL,
  sequence_id INT NOT NULL,
  source_database VARCHAR(64) NOT NULL,
  source_table VARCHAR(64) NOT NULL,
  import_version VARCHAR(64) NOT NULL,
  source_hash CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_oi_5m (symbol, bucket_start, import_version),
  KEY idx_research_oi_5m_lookup (symbol, bucket_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""",
    """
CREATE TABLE IF NOT EXISTS research_liquidations_5m (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  symbol VARCHAR(32) NOT NULL,
  bucket_start DATETIME(6) NOT NULL,
  bucket_end DATETIME(6) NOT NULL,
  long_liquidation_usd DOUBLE NULL,
  short_liquidation_usd DOUBLE NULL,
  total_liquidation_usd DOUBLE NULL,
  liquidation_event_count INT NULL,
  source_first_timestamp DATETIME(6) NULL,
  source_last_timestamp DATETIME(6) NULL,
  source_row_count INT NOT NULL,
  expected_source_rows INT NOT NULL,
  coverage_ratio DOUBLE NOT NULL,
  data_available TINYINT(1) NOT NULL,
  sequence_id INT NOT NULL,
  source_database VARCHAR(64) NOT NULL,
  source_table VARCHAR(64) NOT NULL,
  import_version VARCHAR(64) NOT NULL,
  source_hash CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_liq_5m (symbol, bucket_start, import_version),
  KEY idx_research_liq_5m_lookup (symbol, bucket_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""",
    """
CREATE TABLE IF NOT EXISTS research_orderflow_5m (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  symbol VARCHAR(32) NOT NULL,
  bucket_start DATETIME(6) NOT NULL,
  bucket_end DATETIME(6) NOT NULL,
  buy_volume DOUBLE NULL,
  sell_volume DOUBLE NULL,
  total_volume DOUBLE NULL,
  delta DOUBLE NULL,
  delta_ratio DOUBLE NULL,
  spread_mean DOUBLE NULL,
  spread_max DOUBLE NULL,
  source_first_timestamp DATETIME(6) NULL,
  source_last_timestamp DATETIME(6) NULL,
  source_row_count INT NOT NULL,
  expected_source_rows INT NOT NULL,
  coverage_ratio DOUBLE NOT NULL,
  data_available TINYINT(1) NOT NULL,
  sequence_id INT NOT NULL,
  source_database VARCHAR(64) NOT NULL,
  source_table VARCHAR(64) NOT NULL,
  import_version VARCHAR(64) NOT NULL,
  source_hash CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_of_5m (symbol, bucket_start, import_version),
  KEY idx_research_of_5m_lookup (symbol, bucket_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""",
)

IMPORT_STATUS_VALUES = (
    "planned",
    "running",
    "dry_run_ok",
    "persisted",
    "verified",
    "failed",
)

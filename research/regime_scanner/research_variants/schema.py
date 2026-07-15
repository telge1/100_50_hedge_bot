"""SQL schema for variant comparison tables (separate from market_candles)."""

from __future__ import annotations

VARIANT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS research_variant_sets (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  variant_set_hash CHAR(64) NOT NULL,
  name VARCHAR(128) NOT NULL,
  description TEXT NULL,
  variants_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_variant_sets_hash (variant_set_hash),
  UNIQUE KEY uq_research_variant_sets_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_variant_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  variant_set_id BIGINT UNSIGNED NOT NULL,
  variant_name VARCHAR(128) NOT NULL,
  variant_hash CHAR(64) NOT NULL,
  run_id CHAR(36) NOT NULL,
  parameter_hash CHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  rank_position INT NULL,
  score DOUBLE NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_variant_runs_set_variant (variant_set_id, variant_name),
  KEY idx_research_variant_runs_run (run_id),
  KEY idx_research_variant_runs_hash (variant_hash),
  KEY idx_research_variant_runs_score (variant_set_id, score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_window_sets (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  window_set_hash CHAR(64) NOT NULL,
  name VARCHAR(128) NOT NULL,
  description TEXT NULL,
  windows_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_window_sets_hash (window_set_hash),
  UNIQUE KEY uq_research_window_sets_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_variant_window_runs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  variant_set_id BIGINT UNSIGNED NOT NULL,
  window_set_id BIGINT UNSIGNED NOT NULL,
  variant_name VARCHAR(128) NOT NULL,
  window_name VARCHAR(128) NOT NULL,
  variant_hash CHAR(64) NOT NULL,
  window_hash CHAR(64) NOT NULL,
  run_id CHAR(36) NOT NULL,
  parameter_hash CHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  score DOUBLE NULL,
  degenerate TINYINT(1) NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_variant_window_runs_combo (
    variant_set_id, window_set_id, variant_name, window_name
  ),
  KEY idx_research_variant_window_runs_run (run_id),
  KEY idx_research_variant_window_runs_score (variant_set_id, window_set_id, score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_prepared_contexts (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  prepared_context_hash CHAR(64) NOT NULL,
  exchange VARCHAR(32) NOT NULL,
  symbol VARCHAR(32) NOT NULL,
  data_source VARCHAR(16) NOT NULL,
  warmup_start DATETIME(6) NOT NULL,
  timeline_end DATETIME(6) NOT NULL,
  candle_hashes_json JSON NOT NULL,
  feature_config_hash CHAR(64) NOT NULL,
  scanner_code_version VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at DATETIME(6) NULL,
  metadata_json JSON NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_prepared_contexts_hash (prepared_context_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_window_evaluations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  timeline_id CHAR(36) NOT NULL,
  window_hash CHAR(64) NOT NULL,
  window_name VARCHAR(128) NULL,
  metric_version INT NOT NULL,
  score_version INT NOT NULL,
  metrics_json JSON NOT NULL,
  score DOUBLE NULL,
  degenerate TINYINT(1) NULL,
  degenerate_reason VARCHAR(64) NULL,
  rankable TINYINT(1) NULL,
  character_fit DOUBLE NULL,
  evaluation_hash CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_window_evaluations_key (
    timeline_id, window_hash, metric_version, score_version
  ),
  KEY idx_research_window_evaluations_timeline (timeline_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

VARIANT_STATUS_COMPLETED = "completed"
VARIANT_STATUS_FAILED = "failed"
VARIANT_STATUS_RUNNING = "running"

"""SQL schema for research-run result tables (separate from market_candles)."""

from __future__ import annotations

RESEARCH_SCHEMA_VERSION = "1"

# Use explicit statement list — never split on ";" (comments may contain semicolons).
RESEARCH_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS research_parameter_sets (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  parameter_hash CHAR(64) NOT NULL,
  scanner_name VARCHAR(64) NOT NULL,
  parameters_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_parameter_sets_hash (parameter_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_runs (
  run_id CHAR(36) NOT NULL,
  run_fingerprint CHAR(64) NOT NULL,
  parameter_set_id BIGINT UNSIGNED NOT NULL,
  exchange VARCHAR(32) NOT NULL,
  symbol VARCHAR(32) NOT NULL,
  data_source VARCHAR(16) NOT NULL,
  start_time DATETIME(6) NOT NULL,
  end_time DATETIME(6) NOT NULL,
  warmup_start DATETIME(6) NOT NULL,
  decision_time DATETIME(6) NULL,
  status VARCHAR(16) NOT NULL,
  started_at DATETIME(6) NOT NULL,
  finished_at DATETIME(6) NULL,
  duration_seconds DOUBLE NULL,
  git_commit CHAR(40) NULL,
  git_branch VARCHAR(255) NULL,
  working_tree_dirty TINYINT(1) NOT NULL DEFAULT 0,
  candle_hash_5m CHAR(64) NULL,
  candle_hash_15m CHAR(64) NULL,
  candle_hash_30m CHAR(64) NULL,
  trend_state_hash CHAR(64) NULL,
  structure_event_hash CHAR(64) NULL,
  price_action_hash CHAR(64) NULL,
  momentum_hash CHAR(64) NULL,
  signal_hash CHAR(64) NULL,
  combined_output_hash CHAR(64) NULL,
  error_type VARCHAR(128) NULL,
  error_message TEXT NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (run_id),
  KEY idx_research_runs_fingerprint (run_fingerprint),
  KEY idx_research_runs_symbol_window (symbol, start_time, end_time),
  KEY idx_research_runs_status (status),
  KEY idx_research_runs_parameter_set (parameter_set_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_trend_states (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  event_key VARCHAR(255) NOT NULL,
  timestamp DATETIME(6) NOT NULL,
  state VARCHAR(32) NOT NULL,
  previous_state VARCHAR(32) NULL,
  direction VARCHAR(16) NULL,
  strength DOUBLE NULL,
  transition_reason TEXT NULL,
  confirmation_count INT NULL,
  protective_high DOUBLE NULL,
  protective_low DOUBLE NULL,
  metadata_json JSON NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_trend_states_run_event (run_id, event_key),
  KEY idx_research_trend_states_run_ts (run_id, timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_structure_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  event_key VARCHAR(512) NOT NULL,
  timestamp DATETIME(6) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  direction VARCHAR(16) NULL,
  price DOUBLE NULL,
  swing_type VARCHAR(32) NULL,
  protective_level DOUBLE NULL,
  structure_state VARCHAR(32) NULL,
  metadata_json JSON NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_structure_events_run_event (run_id, event_key),
  KEY idx_research_structure_events_run_ts (run_id, timestamp, event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_signals (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  signal_key VARCHAR(512) NOT NULL,
  timestamp DATETIME(6) NOT NULL,
  direction VARCHAR(16) NULL,
  signal_type VARCHAR(64) NOT NULL,
  setup_id VARCHAR(64) NULL,
  status VARCHAR(32) NULL,
  entry_time DATETIME(6) NULL,
  entry_price DOUBLE NULL,
  invalidation_time DATETIME(6) NULL,
  invalidation_price DOUBLE NULL,
  reason TEXT NULL,
  metadata_json JSON NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_signals_run_signal (run_id, signal_key),
  KEY idx_research_signals_run_ts (run_id, timestamp, direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_run_metrics (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id CHAR(36) NOT NULL,
  metric_name VARCHAR(64) NOT NULL,
  metric_value DOUBLE NULL,
  metric_text VARCHAR(255) NULL,
  metadata_json JSON NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_run_metrics_run_name (run_id, metric_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_INTERRUPTED = "interrupted"

HASH_NOT_AVAILABLE = "not_available"
HASH_NOT_EXPORTED = "not_exported"

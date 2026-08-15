"""Additive schema for C3.5c post-entry path checkpoints / labels.

Does not ALTER existing research_signals / features / outcomes.
"""

from __future__ import annotations

C35C_PATH_SCHEMA_VERSION = "c35c_post_entry_path_v1"
DEFAULT_PATH_VERSION = "c35c_post_entry_path_v1"
DEFAULT_CHECKPOINT_BARS = (1, 2, 3, 4)

# Checkpoint N = after close of the N-th 15m candle since fill (inclusive).
# bars_since_fill = N - 1; CP1 = fill candle close (bar_0).
CHECKPOINT_SEMANTICS = {
    "fill": "open of fill candle (bar_0)",
    "checkpoint_bar": "1..4 after close of bars_since_fill 0..3",
    "decision": "only fully closed candles up to checkpoint",
    "early_exit": "next 15m open after checkpoint close (no same-candle backdate)",
    "prior_exit": "no artificial checkpoint if bars_held < checkpoint_bar - 1",
}

C35C_PATH_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS research_signal_path_checkpoints (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id BIGINT UNSIGNED NOT NULL,
  run_id CHAR(36) NOT NULL,
  path_version VARCHAR(64) NOT NULL,
  checkpoint_bar INT NOT NULL,
  checkpoint_timestamp DATETIME(6) NULL,
  checkpoint_close DOUBLE NULL,
  bars_since_fill INT NULL,
  close_return_pct DOUBLE NULL,
  directional_close_return_pct DOUBLE NULL,
  mfe_so_far_pct DOUBLE NULL,
  mae_so_far_pct DOUBLE NULL,
  mfe_so_far_atr DOUBLE NULL,
  mae_so_far_atr DOUBLE NULL,
  max_high_so_far DOUBLE NULL,
  min_low_so_far DOUBLE NULL,
  entry_reclaimed TINYINT(1) NULL,
  entry_lost TINYINT(1) NULL,
  breakout_level_lost TINYINT(1) NULL,
  breakout_level_reclaimed TINYINT(1) NULL,
  protected_level_broken TINYINT(1) NULL,
  ema9 DOUBLE NULL,
  ema20 DOUBLE NULL,
  ema9_20_aligned TINYINT(1) NULL,
  ema9_20_lost TINYINT(1) NULL,
  adx DOUBLE NULL,
  di_plus DOUBLE NULL,
  di_minus DOUBLE NULL,
  directional_di_spread DOUBLE NULL,
  micro_counter_bos TINYINT(1) NULL,
  micro_counter_choch TINYINT(1) NULL,
  major_structure_opposed TINYINT(1) NULL,
  checkpoint_candle_direction VARCHAR(16) NULL,
  checkpoint_body_atr DOUBLE NULL,
  checkpoint_range_atr DOUBLE NULL,
  close_location_in_range DOUBLE NULL,
  adverse_candle_count INT NULL,
  favorable_candle_count INT NULL,
  direction_change_count INT NULL,
  no_positive_mfe TINYINT(1) NULL,
  small_mfe TINYINT(1) NULL,
  deep_mae TINYINT(1) NULL,
  availability VARCHAR(64) NOT NULL DEFAULT 'ok',
  feature_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_signal_path_cp (signal_id, path_version, checkpoint_bar),
  KEY idx_research_signal_path_cp_run (run_id),
  KEY idx_research_signal_path_cp_avail (availability)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_signal_path_labels (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id BIGINT UNSIGNED NOT NULL,
  run_id CHAR(36) NOT NULL,
  path_version VARCHAR(64) NOT NULL,
  path_type VARCHAR(64) NOT NULL,
  path_thresholds_json JSON NULL,
  label_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_signal_path_labels (signal_id, path_version),
  KEY idx_research_signal_path_labels_run (run_id),
  KEY idx_research_signal_path_labels_type (path_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

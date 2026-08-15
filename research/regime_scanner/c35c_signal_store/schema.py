"""Additive schema for C3.5c A6 signal feature/outcome store.

Does not ALTER existing research_runs / research_signals tables.
Uses CREATE TABLE IF NOT EXISTS (project migration style; no Alembic).
"""

from __future__ import annotations

C35C_SIGNAL_SCHEMA_VERSION = "c35c_signal_store_v1"

# Additive only — never DROP / ALTER existing research_* tables here.
C35C_SIGNAL_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS research_signal_features (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id BIGINT UNSIGNED NOT NULL,
  run_id CHAR(36) NOT NULL,
  feature_version VARCHAR(64) NOT NULL,
  feature_stage VARCHAR(16) NOT NULL,
  feature_timestamp DATETIME(6) NOT NULL,
  ema9 DOUBLE NULL,
  ema20 DOUBLE NULL,
  ema50 DOUBLE NULL,
  ema59 DOUBLE NULL,
  ema200 DOUBLE NULL,
  ema9_slope_3 DOUBLE NULL,
  ema20_slope_3 DOUBLE NULL,
  ema59_slope_3 DOUBLE NULL,
  ema200_slope_3 DOUBLE NULL,
  ema9_20_distance_pct DOUBLE NULL,
  ema20_59_distance_pct DOUBLE NULL,
  ema59_200_distance_pct DOUBLE NULL,
  adx DOUBLE NULL,
  di_plus DOUBLE NULL,
  di_minus DOUBLE NULL,
  di_spread_signed DOUBLE NULL,
  di_spread_abs DOUBLE NULL,
  di_spread_dir_norm DOUBLE NULL,
  atr DOUBLE NULL,
  atr_pct DOUBLE NULL,
  dist_ema_atr DOUBLE NULL,
  move_since_arm_atr DOUBLE NULL,
  breakout_candle_atr DOUBLE NULL,
  pullback_depth_atr DOUBLE NULL,
  dist_breakout_atr DOUBLE NULL,
  dist_protected_atr DOUBLE NULL,
  major_direction INT NULL,
  structure_state VARCHAR(64) NULL,
  protected_high DOUBLE NULL,
  protected_low DOUBLE NULL,
  breakout_level DOUBLE NULL,
  pullback_high DOUBLE NULL,
  pullback_low DOUBLE NULL,
  lh_confirmed TINYINT(1) NULL,
  hl_confirmed TINYINT(1) NULL,
  entry_candle_return_pct DOUBLE NULL,
  entry_candle_body_pct DOUBLE NULL,
  entry_candle_range_pct DOUBLE NULL,
  entry_upper_wick_ratio DOUBLE NULL,
  entry_lower_wick_ratio DOUBLE NULL,
  entry_close_position DOUBLE NULL,
  entry_bullish TINYINT(1) NULL,
  volume DOUBLE NULL,
  volume_ratio DOUBLE NULL,
  hour_utc INT NULL,
  day_of_week INT NULL,
  month INT NULL,
  split VARCHAR(16) NULL,
  feature_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_signal_features_sig_ver_stage (signal_id, feature_version, feature_stage),
  KEY idx_research_signal_features_run (run_id),
  KEY idx_research_signal_features_stage (feature_stage),
  KEY idx_research_signal_features_adx (adx)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
CREATE TABLE IF NOT EXISTS research_signal_outcomes (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  signal_id BIGINT UNSIGNED NOT NULL,
  run_id CHAR(36) NOT NULL,
  outcome_version VARCHAR(64) NOT NULL,
  exit_model VARCHAR(64) NOT NULL,
  tp_pct DOUBLE NOT NULL,
  sl_pct DOUBLE NOT NULL,
  horizon_bars INT NOT NULL,
  cost_pct DOUBLE NOT NULL,
  same_bar_policy VARCHAR(32) NOT NULL,
  exit_timestamp DATETIME(6) NULL,
  exit_price DOUBLE NULL,
  exit_reason VARCHAR(64) NULL,
  gross_pnl_pct DOUBLE NULL,
  net_pnl_pct DOUBLE NULL,
  is_winner TINYINT(1) NULL,
  tp_first TINYINT(1) NULL,
  sl_first TINYINT(1) NULL,
  same_bar_ambiguous TINYINT(1) NULL,
  time_exit TINYINT(1) NULL,
  data_end TINYINT(1) NULL,
  bars_held INT NULL,
  bars_to_tp INT NULL,
  bars_to_sl INT NULL,
  mfe_pct DOUBLE NULL,
  mae_pct DOUBLE NULL,
  mae_before_tp_pct DOUBLE NULL,
  reclaimed_after_adverse TINYINT(1) NULL,
  max_underwater_bars INT NULL,
  outcome_json JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uq_research_signal_outcomes_model (
    signal_id, outcome_version, exit_model, tp_pct, sl_pct, horizon_bars, cost_pct
  ),
  KEY idx_research_signal_outcomes_run (run_id),
  KEY idx_research_signal_outcomes_net (net_pnl_pct),
  KEY idx_research_signal_outcomes_reason (exit_reason)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)

SIGNAL_TYPE_A6_FILL = "c35c_a6_fill"
SCANNER_NAME_A6_STORE = "c35c_pullback_entry_a6_signal_store"
EXIT_MODEL_TP3_SL2 = "tp3_sl2_h192_cost020"
SAME_BAR_POLICY = "conservative_sl"

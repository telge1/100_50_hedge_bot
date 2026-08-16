(base) telgenbuescher@server-telgenbuescher:~/projects/Signal_Generator_Ralf/signal_generator_stoch_waves$ cd /home/telgenbuescher/projects/orderbook_analyse || exit 1

echo "===== CURRENT REPO ====="
git rev-parse --show-toplevel
git status --short
git log -1 --oneline

0e41184 (HEAD -> research/confirmed-orderbook-entries, origin/research/confirmed-orderbook-entries) chore: checkpoint validated stochastic fractal wave fade BE50 baseline



echo
echo "===== SEARCH EXACT COMMIT ====="
git show --stat --oneline --decorate \
  f16ae32da38da86f39e75b09c63c31f62d11996b
===== CURRENT REPO =====
/home/telgenbuescher/projects/orderbook_analyse
 M README.md
 M scripts/run_trend_scanner_multitimeframe.py
 M src/orderbook_analyse/dynamic_wall_detector.py
 M src/orderbook_analyse/trend_scanner_adapter.py
 M src/orderbook_analyse/trend_scanner_multitimeframe.py
 M src/orderbook_analyse/wall_history.py
 M tests/test_dynamic_wall_detector.py
?? Todo.md
?? data/wall_transitions/
?? imports/
?? research/
?? scripts/audit_fixed_sl_tp_025_entry_identity.py
?? scripts/compare_microstructure_signal_variants.py
?? scripts/generate_tradingview_entry_pinescript.py
?? scripts/generate_tradingview_stateful_scanner_5s_pine.py
?? scripts/query_research_microstructure.py
?? scripts/run_anomaly_event_causal_enrichment.py
?? scripts/run_anomaly_event_feature_discrimination_audit.py
?? scripts/run_anomaly_event_frozen_rule_oos_validation.py
?? scripts/run_apt_001_protected_low_break_deep_dive.py
?? scripts/run_apt_05925_bidirectional_breakout_confirmation_audit.py
?? scripts/run_apt_05925_break_ob_trade_reversal_audit.py
?? scripts/run_apt_05925_microstructure_anatomy_audit.py
?? scripts/run_apt_1h_4h_protected_level_event_inventory.py
?? scripts/run_apt_1h_4h_protected_level_microstructure_batch.py
?? scripts/run_apt_doge_ob_levels_window_1500_0200.py
?? scripts/run_bearish_blind_validation.py
?? scripts/run_bearish_orderbook_strategy_research.py
?? scripts/run_c3_frozen_break_warning.py
?? scripts/run_c3_frozen_warning_event_independence.py
?? scripts/run_c3_frozen_warning_five_regime_timeline.py
?? scripts/run_c3_frozen_warning_hedge_overlay.py
?? scripts/run_c3_full_context_break_audit.py
?? scripts/run_c3_join_smoke.py
?? scripts/run_c3_match1h_ob_trades_acceptance_audit.py
?? scripts/run_c3_ob_clickhouse_coverage_audit.py
?? scripts/run_c3_oi_price_context_audit.py
?? scripts/run_c3_protected_high_historical_catalog.py
?? scripts/run_c3_protected_low_event_driven_decision.py
?? scripts/run_c3_protected_low_historical_catalog.py
?? scripts/run_c3_protected_structure_combined_catalog.py
?? scripts/run_c3_structure_break_quality_multi_event.py
?? scripts/run_c3_trend_ob_comparison.py
?? scripts/run_continuation_trade_outcome_audit.py
?? scripts/run_doge_mid_history_wall_level_extraction.py
?? scripts/run_doge_recursive_ask_ob_grid.py
?? scripts/run_doge_recursive_ob_grid.py
?? scripts/run_doge_recursive_ob_grid_lookahead_audit.py
?? scripts/run_dynamic_sl_replay.py
?? scripts/run_execution_wall_detector.py
?? scripts/run_execution_wall_edge_analysis.py
?? scripts/run_execution_wall_lifecycle_audit.py
?? scripts/run_execution_wall_memory_audit.py
?? scripts/run_failed_break_signal_audit.py
?? scripts/run_failed_signal_recovery_audit.py
?? scripts/run_fixed_sl_tp_replay.py
?? scripts/run_fractal_wave_fade_collision_selection_audit.py
?? scripts/run_fractal_wave_fade_pre_entry_quality_audit.py
?? scripts/run_full_history_trade_research.py
?? scripts/run_hedgebot_orderbook_start_filter.py
?? scripts/run_historical_clickhouse_anomaly_multiwindow.py
?? scripts/run_hype_pool_orderbook_liquidation_audit.py
?? scripts/run_hype_pre_liquidation_ob_audit.py
?? scripts/run_live_level_watch.py
?? scripts/run_local_trend_pullback_phase_audit.py
?? scripts/run_market_activity_1m_export.py
?? scripts/run_market_data_collector_audit.py
?? scripts/run_market_data_health_audit.py
?? scripts/run_microstructure_full_history_strategy_replay.py
?? scripts/run_microstructure_minute_state_audit.py
?? scripts/run_microstructure_minute_state_deep_audit.py
?? scripts/run_microstructure_minute_state_exit_audit.py
?? scripts/run_microstructure_pressure_audit.py
?? scripts/run_microstructure_signal_variant_audit.py
?? scripts/run_mtf_rsi_stoch_audit.py
?? scripts/run_mtf_stoch_conflict_quality_audit.py
?? scripts/run_multisymbol_recursive_ob_grid_comparison.py
?? scripts/run_ob200_file_replay_smoke.py
?? scripts/run_orderbook_anomaly_event_audit.py
?? scripts/run_orderbook_signal_root_cause_audit.py
?? scripts/run_orderbook_stateful_market_scanner.py
?? scripts/run_pl_15m_failure_1m_realign_apt.py
?? scripts/run_pl_cci_overlay_apt.py
?? scripts/run_protected_level_attack_definition_matrix.py
?? scripts/run_protected_level_attack_trend_context_join_audit.py
?? scripts/run_protected_level_break_quality_audit.py
?? scripts/run_protected_level_fractal_control_apt.py
?? scripts/run_protected_level_historical_download_catalog.py
?? scripts/run_protective_structure_sl_replay.py
?? scripts/run_public_trade_file_smoke.py
?? scripts/run_recursive_ob_grid_memory_safe.py
?? scripts/run_research_feature_store_build.py
?? scripts/run_signal_price_path_weakness_audit.py
?? scripts/run_strong_wall_interaction_audit.py
?? scripts/run_strong_wall_touch_backtest.py
?? scripts/run_trend_channel_replay.py
?? scripts/run_trend_permission_mtf_audit.py
?? scripts/run_trend_permission_sticky_lag_audit.py
?? scripts/run_trend_scanner_multitimeframe_origin_fix.py
?? scripts/run_trend_structure_quality_audit_full.py
?? scripts/run_wall_approach_interaction_audit.py
?? scripts/run_wall_attack_microstructure_audit.py
?? scripts/run_wall_consumption_signal_audit.py
?? scripts/run_wall_decision_consumption_audit.py
?? scripts/run_wall_multi_horizon_consumption_audit.py
?? scripts/run_wall_relocation_signal_audit.py
?? scripts/run_wall_retest_hold_confirmation_audit.py
?? scripts/run_wall_sequence_anomaly_audit.py
?? scripts/run_wall_survival_calibration_audit.py
?? scripts/run_wall_survival_prediction_audit.py
?? scripts/run_wall_toxicity_audit.py
?? scripts/run_wall_toxicity_batch.py
?? scripts/run_wall_transition_collector.py
?? scripts/start_wall_transition_collectors.sh
?? scripts/status_wall_transition_collectors.sh
?? scripts/stop_wall_transition_collectors.sh
?? scripts/watch_market_data_health.sh
?? src/orderbook_analyse/anomaly_event_causal_enrichment/
?? src/orderbook_analyse/anomaly_event_feature_discrimination/
?? src/orderbook_analyse/anomaly_event_oos_validation/
?? src/orderbook_analyse/apt_001_protected_low_break_deep_dive.py
?? src/orderbook_analyse/apt_05925_bidirectional_breakout/
?? src/orderbook_analyse/apt_05925_break_audit/
?? src/orderbook_analyse/apt_05925_microstructure_anatomy/
?? src/orderbook_analyse/apt_1h_4h_protected_level_event_inventory/
?? src/orderbook_analyse/apt_1h_4h_protected_level_microstructure_batch/
?? src/orderbook_analyse/bearish_orderbook_strategy_research/
?? src/orderbook_analyse/c3_frozen_break_warning.py
?? src/orderbook_analyse/c3_frozen_break_warning_state.py
?? src/orderbook_analyse/c3_frozen_warning_event_independence.py
?? src/orderbook_analyse/c3_frozen_warning_five_regime_timeline.py
?? src/orderbook_analyse/c3_frozen_warning_hedge_overlay.py
?? src/orderbook_analyse/c3_full_context_break_audit.py
?? src/orderbook_analyse/c3_full_context_features.py
?? src/orderbook_analyse/c3_join_smoke.py
?? src/orderbook_analyse/c3_liquidation_feature_adapter.py
?? src/orderbook_analyse/c3_match1h_ob_trades_acceptance_audit.py
?? src/orderbook_analyse/c3_oi_price_context_audit.py
?? src/orderbook_analyse/c3_oi_price_features.py
?? src/orderbook_analyse/c3_protected_low_event_driven_decision.py
?? src/orderbook_analyse/c3_protected_low_historical_catalog.py
?? src/orderbook_analyse/c3_structure_break_quality_multi_event.py
?? src/orderbook_analyse/c3_trend_ob_comparison.py
?? src/orderbook_analyse/c3_wall_feature_adapter.py
?? src/orderbook_analyse/continuation_trade_outcome_audit/
?? src/orderbook_analyse/doge_mid_history_wall_levels/
?? src/orderbook_analyse/doge_recursive_ob_grid/
?? src/orderbook_analyse/dynamic_sl_replay/
?? src/orderbook_analyse/execution_wall_detector/
?? src/orderbook_analyse/execution_wall_edge_analysis/
?? src/orderbook_analyse/execution_wall_lifecycle_audit/
?? src/orderbook_analyse/execution_wall_memory_audit/
?? src/orderbook_analyse/failed_break_signal_audit/
?? src/orderbook_analyse/fixed_sl_tp_replay/
?? src/orderbook_analyse/fractal_wave_fade_collision_selection_audit/
?? src/orderbook_analyse/fractal_wave_fade_pre_entry_quality_audit/
?? src/orderbook_analyse/full_history_trade_research/
?? src/orderbook_analyse/hedgebot_orderbook_start_filter/
?? src/orderbook_analyse/historical_anomaly_multiwindow/
?? src/orderbook_analyse/hype_pool_orderbook_liquidation_audit.py
?? src/orderbook_analyse/hype_pre_liquidation_ob_audit.py
?? src/orderbook_analyse/hype_pre_liquidation_ob_core.py
?? src/orderbook_analyse/live_level_watch.py
?? src/orderbook_analyse/live_level_zones.py
?? src/orderbook_analyse/live_terminal_report.py
?? src/orderbook_analyse/live_wall_follow.py
?? src/orderbook_analyse/live_zone_state.py
?? src/orderbook_analyse/local_trend_pullback_phase_audit/
?? src/orderbook_analyse/market_data_coverage/
?? src/orderbook_analyse/microstructure_full_history_strategy_replay/
?? src/orderbook_analyse/microstructure_minute_state_audit/
?? src/orderbook_analyse/microstructure_pressure_audit/
?? src/orderbook_analyse/mtf_rsi_stoch_audit/
?? src/orderbook_analyse/mtf_stoch_conflict_quality_audit/
?? src/orderbook_analyse/ob_data_source/
?? src/orderbook_analyse/ob_grid_snapshot.py
?? src/orderbook_analyse/orderbook_anomaly_event_audit/
?? src/orderbook_analyse/orderbook_signal_root_cause_audit/
?? src/orderbook_analyse/orderbook_stateful_market_scanner/
?? src/orderbook_analyse/pl_15m_failure_1m_realign/
?? src/orderbook_analyse/pl_cci_overlay/
?? src/orderbook_analyse/protected_level_attack_definition/
?? src/orderbook_analyse/protected_level_break_quality/
?? src/orderbook_analyse/protected_level_fractal_control/
?? src/orderbook_analyse/protected_level_historical_download_catalog/
?? src/orderbook_analyse/protected_level_origin_audit.py
?? src/orderbook_analyse/protective_structure_sl_replay/
?? src/orderbook_analyse/public_trade_source/
?? src/orderbook_analyse/research_feature_store/
?? src/orderbook_analyse/signal_price_path_weakness_audit/
?? src/orderbook_analyse/strong_wall_interaction_audit/
?? src/orderbook_analyse/strong_wall_touch_backtest/
?? src/orderbook_analyse/trend_channel_replay.py
?? src/orderbook_analyse/trend_permission_sticky_lag.py
?? src/orderbook_analyse/trend_structure_quality_audit/
?? src/orderbook_analyse/wall_approach_interaction_audit/
?? src/orderbook_analyse/wall_attack_microstructure_audit/
?? src/orderbook_analyse/wall_consumption_signal_audit/
?? src/orderbook_analyse/wall_decision_consumption_audit/
?? src/orderbook_analyse/wall_multi_horizon_consumption_audit/
?? src/orderbook_analyse/wall_relocation_signal_audit/
?? src/orderbook_analyse/wall_retest_hold_confirmation_audit/
?? src/orderbook_analyse/wall_sequence_anomaly_audit/
?? src/orderbook_analyse/wall_survival_prediction_audit/
?? src/orderbook_analyse/wall_toxicity_audit/
?? src/orderbook_analyse/wall_transition_collector/
?? tests/fixtures/
?? tests/test_anomaly_event_causal_enrichment_oos.py
?? tests/test_anomaly_event_feature_discrimination.py
?? tests/test_apt_001_protected_low_break_deep_dive.py
?? tests/test_apt_05925_bidirectional_breakout.py
?? tests/test_apt_05925_break_audit.py
?? tests/test_apt_05925_microstructure_anatomy.py
?? tests/test_apt_1h_4h_protected_level_event_inventory.py
?? tests/test_apt_1h_4h_protected_level_microstructure_batch.py
?? tests/test_bearish_orderbook_strategy_research.py
?? tests/test_c3_frozen_break_warning.py
?? tests/test_c3_frozen_warning_event_independence.py
?? tests/test_c3_frozen_warning_five_regime_timeline.py
?? tests/test_c3_frozen_warning_hedge_overlay.py
?? tests/test_c3_full_context_break_audit.py
?? tests/test_c3_join_smoke.py
?? tests/test_c3_match1h_ob_trades_acceptance_audit.py
?? tests/test_c3_oi_price_context_audit.py
?? tests/test_c3_protected_high_historical_catalog.py
?? tests/test_c3_protected_low_event_driven_decision.py
?? tests/test_c3_protected_low_historical_catalog.py
?? tests/test_c3_structure_break_quality_multi_event.py
?? tests/test_c3_trend_ob_comparison.py
?? tests/test_continuation_trade_outcome_audit.py
?? tests/test_doge_mid_history_wall_levels.py
?? tests/test_doge_recursive_ob_grid.py
?? tests/test_dynamic_sl_replay.py
?? tests/test_execution_wall_detector.py
?? tests/test_execution_wall_edge_analysis.py
?? tests/test_execution_wall_lifecycle_audit.py
?? tests/test_execution_wall_memory_audit.py
?? tests/test_failed_break_signal_audit.py
?? tests/test_failed_signal_recovery_audit.py
?? tests/test_fixed_sl_tp_replay.py
?? tests/test_fractal_wave_fade_collision_selection_audit.py
?? tests/test_fractal_wave_fade_pre_entry_quality_audit.py
?? tests/test_full_history_trade_research.py
?? tests/test_hedgebot_orderbook_start_filter.py
?? tests/test_historical_anomaly_multiwindow.py
?? tests/test_hype_pool_orderbook_liquidation_audit.py
?? tests/test_hype_pre_liquidation_ob_audit.py
?? tests/test_live_level_watch.py
?? tests/test_live_level_zones.py
?? tests/test_live_terminal_report.py
?? tests/test_live_wall_follow.py
?? tests/test_live_zone_state.py
?? tests/test_local_trend_pullback_phase_audit.py
?? tests/test_microstructure_full_history_strategy_replay.py
?? tests/test_microstructure_minute_state_audit.py
?? tests/test_microstructure_minute_state_deep_audit.py
?? tests/test_microstructure_minute_state_exit_audit.py
?? tests/test_microstructure_pressure_audit.py
?? tests/test_microstructure_signal_variants.py
?? tests/test_mtf_rsi_stoch_audit.py
?? tests/test_mtf_stoch_conflict_quality_audit.py
?? tests/test_ob200_file_source.py
?? tests/test_ob_data_source_factory.py
?? tests/test_ob_grid_snapshot.py
?? tests/test_orderbook_anomaly_event_audit.py
?? tests/test_orderbook_signal_root_cause_audit.py
?? tests/test_orderbook_stateful_market_scanner.py
?? tests/test_pl_15m_failure_1m_realign.py
?? tests/test_pl_cci_overlay.py
?? tests/test_protected_level_attack_definition.py
?? tests/test_protected_level_break_quality.py
?? tests/test_protected_level_fractal_control.py
?? tests/test_protected_level_historical_download_catalog.py
?? tests/test_protected_level_origin_audit.py
?? tests/test_protective_structure_sl_replay.py
?? tests/test_public_trade_factory.py
?? tests/test_public_trade_file_source.py
?? tests/test_research_feature_store.py
?? tests/test_signal_price_path_weakness_audit.py
?? tests/test_strong_wall_interaction_audit.py
?? tests/test_strong_wall_touch_backtest.py
?? tests/test_tp025_entry_identity_audit.py
?? tests/test_tradingview_entry_pinescript.py
?? tests/test_trend_channel_replay.py
?? tests/test_trend_permission_sticky_lag.py
?? tests/test_trend_structure_quality_audit_full.py
?? tests/test_wall_approach_interaction_audit.py
?? tests/test_wall_attack_microstructure_audit.py
?? tests/test_wall_consumption_signal_audit.py
?? tests/test_wall_decision_consumption_audit.py
?? tests/test_wall_multi_horizon_consumption_audit.py
?? tests/test_wall_relocation_signal_audit.py
?? tests/test_wall_retest_hold_confirmation_audit.py
?? tests/test_wall_sequence_anomaly_audit.py
?? tests/test_wall_survival_calibration_audit.py
?? tests/test_wall_survival_external_test.py
?? tests/test_wall_survival_prediction_audit.py
?? tests/test_wall_toxicity_audit.py
?? tests/test_wall_toxicity_batch.py
?? tests/test_wall_transition_collector_and_coverage.py
?? tradingview/
0e41184 (HEAD -> research/confirmed-orderbook-entries, origin/research/confirmed-orderbook-entries) chore: checkpoint validated stochastic fractal wave fade BE50 baseline

===== SEARCH EXACT COMMIT =====
f16ae32 chore: checkpoint validated fractal wave fade BE50 baseline
 scripts/run_fractal_15m_failure_confirmation_entry_apt.py                            |  41 ++
 scripts/run_fractal_15m_failure_early_detection_apt.py                               |  41 ++
 scripts/run_fractal_15m_failure_tpsl_apt.py                                          |  38 ++
 scripts/run_fractal_all_wave_fade_apt.py                                             |  38 ++
 scripts/run_fractal_all_wave_fade_generalization.py                                  |  41 ++
 scripts/run_fractal_cycle_phase_failure_apt.py                                       |  37 ++
 scripts/run_fractal_cycle_wave_analysis_apt.py                                       |  49 +++
 scripts/run_fractal_direction_and_entry_apt.py                                       |  37 ++
 scripts/run_fractal_directional_control_apt.py                                       |  41 ++
 scripts/run_fractal_dynamic_cluster_upgrade_db.py                                    |  39 ++
 scripts/run_fractal_failure_multitimeframe_apt.py                                    |  37 ++
 scripts/run_fractal_parent_lower_tf_quality_db.py                                    |  38 ++
 scripts/run_fractal_parent_signal_lower_tf_context.py                                |  42 ++
 scripts/run_fractal_signal_confluence_db.py                                          |  39 ++
 scripts/run_fractal_wave_fade_1h4h_exit_path.py                                      |  38 ++
 scripts/run_fractal_wave_fade_be50_anti_repeat_full_backtest.py                      |  44 +++
 scripts/run_fractal_wave_fade_be50_drawdown_audit.py                                 |  37 ++
 scripts/run_fractal_wave_fade_be50_full_backtest.py                                  |  39 ++
 scripts/run_fractal_wave_fade_be50_july_2026.py                                      |  35 ++
 scripts/run_fractal_wave_fade_cashout_reimbursement_analysis.py                      |  54 +++
 scripts/run_fractal_wave_fade_cashout_reserve_analysis.py                            |  52 +++
 scripts/run_fractal_wave_fade_equity_acceleration_analysis.py                        |  83 ++++
 scripts/run_fractal_wave_fade_equity_curve_analysis.py                               |  52 +++
:
f16ae32 chore: checkpoint validated fractal wave fade BE50 baseline
 scripts/run_fractal_15m_failure_confirmation_entry_apt.py                            |  41 ++
 scripts/run_fractal_15m_failure_early_detection_apt.py                               |  41 ++
 scripts/run_fractal_15m_failure_tpsl_apt.py                                          |  38 ++
 scripts/run_fractal_all_wave_fade_apt.py                                             |  38 ++
 scripts/run_fractal_all_wave_fade_generalization.py                                  |  41 ++
 scripts/run_fractal_cycle_phase_failure_apt.py                                       |  37 ++
 scripts/run_fractal_cycle_wave_analysis_apt.py                                       |  49 +++
 scripts/run_fractal_direction_and_entry_apt.py                                       |  37 ++
 scripts/run_fractal_directional_control_apt.py                                       |  41 ++
 scripts/run_fractal_dynamic_cluster_upgrade_db.py                                    |  39 ++
 scripts/run_fractal_failure_multitimeframe_apt.py                                    |  37 ++
 scripts/run_fractal_parent_lower_tf_quality_db.py                                    |  38 ++
 scripts/run_fractal_parent_signal_lower_tf_context.py                                |  42 ++
 scripts/run_fractal_signal_confluence_db.py                                          |  39 ++
 scripts/run_fractal_wave_fade_1h4h_exit_path.py                                      |  38 ++
 scripts/run_fractal_wave_fade_be50_anti_repeat_full_backtest.py                      |  44 +++
 scripts/run_fractal_wave_fade_be50_drawdown_audit.py                                 |  37 ++
 scripts/run_fractal_wave_fade_be50_full_backtest.py                                  |  39 ++
 scripts/run_fractal_wave_fade_be50_july_2026.py                                      |  35 ++
 scripts/run_fractal_wave_fade_cashout_reimbursement_analysis.py                      |  54 +++
 scripts/run_fractal_wave_fade_cashout_reserve_analysis.py                            |  52 +++
 scripts/run_fractal_wave_fade_equity_acceleration_analysis.py                        |  83 ++++
 scripts/run_fractal_wave_fade_equity_curve_analysis.py                               |  52 +++
:
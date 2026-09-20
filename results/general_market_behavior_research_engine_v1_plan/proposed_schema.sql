-- DESIGN ONLY — DO NOT EXECUTE
-- Proposed derived tables for later implementation (btc_doge_research or dedicated DB)

-- market_behavior_state_1s_v1
-- ORDER BY (symbol, bucket_time); PARTITION BY toYYYYMM(bucket_time)
-- Columns (sketch): symbol, bucket_time, book_track Enum('FULL_OB','OB200','OB1000'),
-- mid, spread_bps, imbalance_bands Map/Array, flow_* , taker_* , oi_* , liq_* ,
-- regime_* , coverage_status, replay_status, book_epoch_id, source_ages Map,
-- feature_valid UInt8, build_id, computed_at

-- market_behavior_book_flow_1s_v1
-- Compact flow detail without level-per-row explosion; band aggregates + attribution counts

-- market_behavior_episodes_v1
-- episode_id, symbol, pattern_id, start_time, end_time, trigger_features JSON,
-- coverage_status, build_id — NO outcome columns

-- market_behavior_outcomes_v1
-- episode_id OR (symbol, decision_time), horizon_s, mid_return_bps, mfe, mae, direction, path_class, ...

-- market_behavior_predictions_v1
-- later: model_id, decision_time, probs, calibrated_flag — empty in early phases

-- TTL: research retention policy TBD (suggest 180d derived; RAW FS retained separately)
-- Idempotency: ReplacingMergeTree(build_id) or deterministic delete-by-(symbol,day,build)

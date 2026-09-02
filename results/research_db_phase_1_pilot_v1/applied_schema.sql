CREATE DATABASE IF NOT EXISTS btc_doge_research;

CREATE TABLE IF NOT EXISTS btc_doge_research.research_ingestion_batches
(
    batch_id String,
    input_fingerprint FixedString(64),
    contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    symbol LowCardinality(String),
    window_start DateTime64(3, 'UTC'),
    window_end DateTime64(3, 'UTC'),
    status LowCardinality(String),
    rows_written UInt64,
    manifest_fingerprint FixedString(64),
    started_at DateTime64(6, 'UTC'),
    completed_at DateTime64(6, 'UTC'),
    error String DEFAULT ''
)
ENGINE = MergeTree
ORDER BY batch_id;

CREATE TABLE IF NOT EXISTS btc_doge_research.research_source_files
(
    source_file_id FixedString(64),
    source_relative_path String,
    file_name String,
    symbol LowCardinality(String),
    segment_start DateTime64(3, 'UTC'),
    segment_end DateTime64(3, 'UTC'),
    file_size UInt64,
    compression LowCardinality(String),
    source_fingerprint FixedString(64),
    parser_version LowCardinality(String),
    source_contract_version LowCardinality(String),
    event_count UInt64,
    import_status LowCardinality(String),
    import_batch_id String,
    first_event_time Nullable(DateTime64(3, 'UTC')),
    last_event_time Nullable(DateTime64(3, 'UTC')),
    duplicate_status LowCardinality(String),
    overlap_status LowCardinality(String),
    error_status String,
    registered_at DateTime64(6, 'UTC')
)
ENGINE = MergeTree
ORDER BY source_file_id;

CREATE TABLE IF NOT EXISTS btc_doge_research.research_public_trades
(
    symbol LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    receive_time DateTime64(6, 'UTC'),
    trade_id String,
    price Decimal(18, 8),
    base_size Decimal(24, 12),
    quote_notional Decimal(24, 8),
    taker_side Enum8('Buy' = 1, 'Sell' = 2),
    source_id LowCardinality(String),
    source_contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    ingested_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String),
    event_key String
)
ENGINE = MergeTree
PARTITION BY (symbol, toYYYYMM(event_time))
ORDER BY (symbol, event_time, trade_id);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_liquidation_events
(
    symbol LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    receive_time Nullable(DateTime64(6, 'UTC')),
    position_side_raw Enum8('Buy' = 1, 'Sell' = 2),
    liquidated_position_side Enum8('LIQUIDATED_LONG' = 1, 'LIQUIDATED_SHORT' = 2),
    forced_flow Enum8('FORCED_BUY' = 1, 'FORCED_SELL' = 2),
    executed_base_size Decimal(24, 12),
    bankruptcy_price Decimal(18, 8),
    bankruptcy_reference_quote Decimal(24, 8),
    execution_price Nullable(Decimal(18, 8)),
    execution_notional Nullable(Decimal(24, 8)),
    event_key String,
    event_key_version LowCardinality(String),
    source_id LowCardinality(String),
    source_contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    ingested_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY (symbol, toYYYYMM(event_time))
ORDER BY (symbol, event_time, event_key);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_orderbook_ob200_snapshots
(
    symbol LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    receive_time Nullable(DateTime64(6, 'UTC')),
    exchange_sequence UInt64,
    update_id UInt64,
    raw_event_type LowCardinality(String),
    bid_prices Array(Decimal(18, 8)) CODEC(ZSTD(3)),
    bid_sizes Array(Decimal(24, 12)) CODEC(ZSTD(3)),
    ask_prices Array(Decimal(18, 8)) CODEC(ZSTD(3)),
    ask_sizes Array(Decimal(24, 12)) CODEC(ZSTD(3)),
    bid_level_count UInt16,
    ask_level_count UInt16,
    is_genuine UInt8,
    is_carried_forward UInt8,
    source_file_id FixedString(64),
    source_segment String,
    source_record UInt64,
    source_fingerprint FixedString(64),
    source_id LowCardinality(String),
    source_contract_version LowCardinality(String),
    parser_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    ingested_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String),
    event_key FixedString(64),
    key_version LowCardinality(String),
    content_fingerprint FixedString(64)
)
ENGINE = MergeTree
PARTITION BY (symbol, toYYYYMMDD(event_time))
ORDER BY (symbol, event_time, update_id, source_file_id, source_record);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_orderbook_levels_pilot
(
    symbol LowCardinality(String),
    event_key FixedString(64),
    event_time DateTime64(3, 'UTC'),
    side Enum8('bid' = 1, 'ask' = 2),
    level_rank UInt16,
    price Decimal(18, 8),
    size Decimal(24, 12),
    pilot_status LowCardinality(String) DEFAULT 'PILOT_ONLY',
    ingestion_batch_id String
)
ENGINE = MergeTree
ORDER BY (symbol, event_time, event_key, side, level_rank);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_orderbook_1s
(
    symbol LowCardinality(String),
    bucket_time DateTime64(0, 'UTC'),
    mid Decimal(18, 8),
    best_bid Decimal(18, 8),
    best_ask Decimal(18, 8),
    spread_abs Decimal(18, 8),
    spread_bps Float64,
    bid_qty_l50 Decimal(24, 12),
    ask_qty_l50 Decimal(24, 12),
    imbalance_l50 Float64,
    bid_notional_l50 Decimal(24, 8),
    ask_notional_l50 Decimal(24, 8),
    bid_level_count UInt16,
    ask_level_count UInt16,
    is_genuine UInt8,
    is_carried_forward UInt8,
    source_snapshot_time DateTime64(3, 'UTC'),
    last_update_id UInt64,
    sequence_status LowCardinality(String),
    source_id LowCardinality(String),
    source_contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    ingested_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String),
    bucket_key String
)
ENGINE = MergeTree
PARTITION BY (symbol, toYYYYMMDD(bucket_time))
ORDER BY (symbol, bucket_time);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_market_1s
(
    symbol LowCardinality(String),
    bucket_time DateTime64(0, 'UTC'),
    last_trade_price Nullable(Decimal(18, 8)),
    mid Nullable(Decimal(18, 8)),
    taker_buy_base Decimal(24, 12),
    taker_sell_base Decimal(24, 12),
    taker_buy_quote Decimal(24, 8),
    taker_sell_quote Decimal(24, 8),
    trade_count UInt32,
    taker_delta_base Decimal(24, 12),
    open_interest Nullable(Decimal(24, 12)),
    oi_delta Nullable(Decimal(24, 12)),
    oi_freshness_ms Nullable(UInt64),
    oi_status LowCardinality(String),
    long_liquidation_base Decimal(24, 12),
    short_liquidation_base Decimal(24, 12),
    forced_buy_base Decimal(24, 12),
    forced_sell_base Decimal(24, 12),
    spread_bps Nullable(Float64),
    imbalance_l50 Nullable(Float64),
    ob_is_genuine Nullable(UInt8),
    ob_is_carried_forward Nullable(UInt8),
    funding_status LowCardinality(String),
    source_coverage_mask UInt16,
    source_id LowCardinality(String),
    source_contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    ingested_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String),
    bucket_key String
)
ENGINE = MergeTree
PARTITION BY (symbol, toYYYYMMDD(bucket_time))
ORDER BY (symbol, bucket_time);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_market_1m
(
    symbol LowCardinality(String),
    bucket_time DateTime64(0, 'UTC'),
    open Nullable(Decimal(18, 8)),
    high Nullable(Decimal(18, 8)),
    low Nullable(Decimal(18, 8)),
    close Nullable(Decimal(18, 8)),
    volume_base Decimal(24, 12),
    volume_quote Decimal(24, 8),
    taker_buy_base Decimal(24, 12),
    taker_sell_base Decimal(24, 12),
    taker_delta_base Decimal(24, 12),
    trade_count UInt32,
    oi_open Nullable(Decimal(24, 12)),
    oi_close Nullable(Decimal(24, 12)),
    oi_delta Nullable(Decimal(24, 12)),
    long_liquidation_base Decimal(24, 12),
    short_liquidation_base Decimal(24, 12),
    forced_buy_base Decimal(24, 12),
    forced_sell_base Decimal(24, 12),
    mid_open Nullable(Decimal(18, 8)),
    mid_close Nullable(Decimal(18, 8)),
    spread_bps_mean Nullable(Float64),
    imbalance_l50_mean Nullable(Float64),
    genuine_seconds UInt32,
    carried_forward_seconds UInt32,
    funding_status LowCardinality(String),
    source_id LowCardinality(String),
    source_contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    ingested_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String),
    bucket_key String
)
ENGINE = MergeTree
PARTITION BY (symbol, toYYYYMM(bucket_time))
ORDER BY (symbol, bucket_time);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_coverage
(
    coverage_key FixedString(64),
    source_id LowCardinality(String),
    symbol LowCardinality(String),
    data_type LowCardinality(String),
    period_start DateTime64(0, 'UTC'),
    period_end DateTime64(0, 'UTC'),
    expected_buckets UInt64,
    present_buckets UInt64,
    genuine_buckets UInt64,
    carried_forward_buckets UInt64,
    gap_count UInt32,
    duplicate_count UInt32,
    quality_status LowCardinality(String),
    contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    checked_at DateTime64(6, 'UTC')
)
ENGINE = MergeTree
ORDER BY (symbol, data_type, period_start, coverage_key);

CREATE TABLE IF NOT EXISTS btc_doge_research.research_pipeline_state
(
    state_key String,
    processor LowCardinality(String),
    source_id LowCardinality(String),
    symbol LowCardinality(String),
    last_read_ts DateTime64(3, 'UTC'),
    last_finalized_ts DateTime64(3, 'UTC'),
    watermark_ts DateTime64(3, 'UTC'),
    overlap_seconds UInt32,
    last_successful_run DateTime64(6, 'UTC'),
    rows_read UInt64,
    rows_written UInt64,
    processor_version LowCardinality(String),
    contract_version LowCardinality(String),
    status LowCardinality(String),
    error String DEFAULT ''
)
ENGINE = MergeTree
ORDER BY state_key;

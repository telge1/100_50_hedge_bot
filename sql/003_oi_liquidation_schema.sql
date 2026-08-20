-- OI + allLiquidation collector tables.
-- Does not ALTER orderbook_deltas / public_trades / ticker_samples / liquidations.
-- Queries must logically dedupe (argMax by received_at / inserted_at); no ReplacingMergeTree.

CREATE TABLE IF NOT EXISTS orderbook_analysis.all_liquidations
(
    `exchange` LowCardinality(String),
    `category` LowCardinality(String),
    `symbol` LowCardinality(String),
    `event_time` DateTime64(3, 'UTC'),
    `system_generated_at` DateTime64(3, 'UTC'),
    `received_at` DateTime64(3, 'UTC'),
    `position_side_raw` LowCardinality(String),
    `liquidated_position_side` LowCardinality(String),
    `size` Decimal(38, 8),
    `bankruptcy_price` Decimal(38, 8),
    `notional_estimate` Decimal(38, 8),
    `source_topic` String,
    `event_key` String,
    `raw_payload_hash` FixedString(64),
    `collector_instance_id` String,
    `inserted_at` DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, event_time, position_side_raw, event_key)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.open_interest_events
(
    `exchange` LowCardinality(String),
    `category` LowCardinality(String),
    `symbol` LowCardinality(String),
    `event_time` DateTime64(3, 'UTC'),
    `received_at` DateTime64(3, 'UTC'),
    `cross_sequence` Nullable(UInt64),
    `open_interest` Decimal(38, 8),
    `open_interest_value` Decimal(38, 8),
    `single_open_interest` Nullable(Decimal(38, 8)),
    `single_open_interest_value` Nullable(Decimal(38, 8)),
    `last_price` Nullable(Decimal(38, 8)),
    `mark_price` Nullable(Decimal(38, 8)),
    `index_price` Nullable(Decimal(38, 8)),
    `funding_rate` Nullable(Decimal(38, 10)),
    `message_type` LowCardinality(String),
    `source_topic` String,
    `state_valid` UInt8,
    `event_key` String,
    `collector_instance_id` String,
    `inserted_at` DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (symbol, event_time, event_key)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.open_interest_5s
(
    `exchange` LowCardinality(String),
    `category` LowCardinality(String),
    `symbol` LowCardinality(String),
    `bucket_time` DateTime64(3, 'UTC'),
    `source_event_time` DateTime64(3, 'UTC'),
    `received_at` DateTime64(3, 'UTC'),
    `open_interest` Decimal(38, 8),
    `open_interest_value` Decimal(38, 8),
    `single_open_interest` Nullable(Decimal(38, 8)),
    `single_open_interest_value` Nullable(Decimal(38, 8)),
    `last_price` Nullable(Decimal(38, 8)),
    `mark_price` Nullable(Decimal(38, 8)),
    `index_price` Nullable(Decimal(38, 8)),
    `state_age_ms` Int64,
    `state_valid` UInt8,
    `source` LowCardinality(String),
    `collector_instance_id` String,
    `inserted_at` DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(bucket_time)
ORDER BY (symbol, bucket_time, received_at, collector_instance_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.open_interest_5m_history
(
    `exchange` LowCardinality(String),
    `category` LowCardinality(String),
    `symbol` LowCardinality(String),
    `bucket_time` DateTime64(3, 'UTC'),
    `open_interest` Decimal(38, 8),
    `open_interest_value` Nullable(Decimal(38, 8)),
    `source` LowCardinality(String),
    `collector_instance_id` String,
    `inserted_at` DateTime64(3, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(bucket_time)
ORDER BY (symbol, bucket_time, source, inserted_at)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.oi_liquidation_health
(
    `event_ts` DateTime64(3, 'UTC'),
    `collector_instance_id` String,
    `symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `event_type` LowCardinality(String),
    `ws_connected` UInt8,
    `ping_ok` UInt8,
    `subscription_confirmed` UInt8,
    `oi_state_valid` UInt8,
    `oi_state_age_ms` Nullable(Int64),
    `last_event_time` Nullable(DateTime64(3, 'UTC')),
    `last_received_at` Nullable(DateTime64(3, 'UTC')),
    `last_liquidation_time` Nullable(DateTime64(3, 'UTC')),
    `lag_ms` Nullable(Int64),
    `messages_received` UInt64,
    `rows_inserted` UInt64,
    `duplicates_suppressed` UInt64,
    `parse_errors` UInt64,
    `insert_errors` UInt64,
    `reconnect_count` UInt32,
    `subscription_count` UInt32,
    `queue_size` UInt32,
    `queue_drops` UInt64,
    `clock_offset_ms` Nullable(Int64),
    `message` String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_ts)
ORDER BY (collector_instance_id, event_ts, symbol, event_type)
SETTINGS index_granularity = 8192;

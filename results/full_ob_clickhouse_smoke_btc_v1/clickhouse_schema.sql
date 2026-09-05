CREATE DATABASE IF NOT EXISTS research_full_ob_smoke;

CREATE TABLE IF NOT EXISTS research_full_ob_smoke.full_ob_packets_smoke_v1
(
    packet_sha256 FixedString(64),
    fight_event_id String,
    segment_index UInt32,
    source_file String,
    source_line_number UInt64,
    symbol LowCardinality(String),
    topic String,
    message_type LowCardinality(String),
    marker_type Nullable(String),
    exchange_ts_ms Nullable(Int64),
    cts_ms Nullable(Int64),
    receive_time_ns Nullable(Int64),
    update_id Nullable(Int64),
    seq Nullable(Int64),
    bids Array(Tuple(String, String)),
    asks Array(Tuple(String, String)),
    raw_payload String,
    ingestion_ts DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingestion_ts)
ORDER BY (packet_sha256)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS research_full_ob_smoke.full_ob_level_changes_smoke_v1
(
    packet_sha256 FixedString(64),
    symbol LowCardinality(String),
    side Enum8('bid' = 1, 'ask' = 2),
    price String,
    quantity String,
    action Enum8('UPSERT' = 1, 'DELETE' = 2),
    update_id Int64,
    seq Int64,
    exchange_ts_ms Int64,
    ingestion_ts DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingestion_ts)
ORDER BY (packet_sha256, side, price)
SETTINGS index_granularity = 8192;

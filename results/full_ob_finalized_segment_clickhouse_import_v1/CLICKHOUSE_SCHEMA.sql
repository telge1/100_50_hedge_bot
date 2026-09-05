-- Isolated pilot schema for full_ob_finalized_segment_clickhouse_import_v1
-- Do NOT apply to production / protected research DBs.


CREATE DATABASE IF NOT EXISTS research_full_ob_import_pilot_v1;

CREATE TABLE IF NOT EXISTS research_full_ob_import_pilot_v1.full_ob_import_state
(
    segment_id FixedString(64),
    source_path String,
    source_sha256 FixedString(64),
    file_size UInt64,
    symbol LowCardinality(String),
    topic String,
    fight_event_id String,
    segment_index UInt32,
    continuation_index UInt32,
    contract_version LowCardinality(String),
    status LowCardinality(String),
    first_ts Nullable(String),
    last_ts Nullable(String),
    first_u Nullable(Int64),
    last_u Nullable(Int64),
    first_seq Nullable(Int64),
    last_seq Nullable(Int64),
    record_count UInt64,
    checkpoint_count UInt64,
    continuity_epochs UInt32,
    import_attempts UInt32,
    last_error String,
    import_time Nullable(DateTime64(3, 'UTC')),
    verify_time Nullable(DateTime64(3, 'UTC')),
    db_rows_physical UInt64,
    db_rows_logical UInt64,
    replay_status LowCardinality(String),
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (segment_id);

CREATE TABLE IF NOT EXISTS research_full_ob_import_pilot_v1.full_ob_events
(
    fight_event_id String,
    symbol LowCardinality(String),
    trigger_type LowCardinality(String),
    start_ts Nullable(String),
    end_ts Nullable(String),
    status LowCardinality(String),
    contract_versions Array(String),
    parent_signal_ids Array(String),
    nested_signal_ids Array(String),
    segment_count UInt32,
    continuous_capture UInt8,
    replayable_by_epochs UInt8,
    research_eligible UInt8,
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (fight_event_id);

CREATE TABLE IF NOT EXISTS research_full_ob_import_pilot_v1.full_ob_segments
(
    segment_id FixedString(64),
    fight_event_id String,
    symbol LowCardinality(String),
    continuation_index UInt32,
    source_path String,
    source_sha256 FixedString(64),
    previous_segment_sha256 Nullable(String),
    file_size UInt64,
    status LowCardinality(String),
    record_count UInt64,
    checkpoint_count UInt64,
    first_ts Nullable(String),
    last_ts Nullable(String),
    first_u Nullable(Int64),
    last_u Nullable(Int64),
    last_error String,
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (segment_id);

CREATE TABLE IF NOT EXISTS research_full_ob_import_pilot_v1.full_ob_records
(
    record_id FixedString(64),
    record_kind LowCardinality(String),
    fight_event_id String,
    segment_id FixedString(64),
    segment_index UInt32,
    continuation_index UInt32,
    record_ordinal UInt64,
    symbol LowCardinality(String),
    topic String,
    continuity_epoch_id Nullable(Int64),
    u Nullable(Int64),
    seq Nullable(Int64),
    exchange_ts_ms Nullable(Int64),
    cts_ms Nullable(Int64),
    receive_time_ns Nullable(Int64),
    bids Array(Tuple(String, String)),
    asks Array(Tuple(String, String)),
    marker_type Nullable(String),
    book_hash Nullable(String),
    source_path String,
    source_sha256 FixedString(64),
    raw_payload_hash FixedString(64),
    canonical_payload_hash FixedString(64),
    raw_payload String,
    ingestion_ts DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingestion_ts)
ORDER BY (record_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS research_full_ob_import_pilot_v1.full_ob_signals
(
    signal_id String,
    parent_event_id String,
    symbol LowCardinality(String),
    profile_contract String,
    signal_role LowCardinality(String),
    edge LowCardinality(String),
    trigger_type LowCardinality(String),
    arm_cycle Nullable(UInt32),
    continuity_epoch_id Nullable(Int64),
    overlap_cluster_id Nullable(String),
    vah Nullable(String),
    val Nullable(String),
    poc Nullable(String),
    coverage LowCardinality(String),
    research_eligible UInt8,
    payload_json String,
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (signal_id);

CREATE TABLE IF NOT EXISTS research_full_ob_import_pilot_v1.signal_analysis_contracts
(
    contract_id String,
    signal_id String,
    parent_event_id String,
    profile_contract String,
    pre_window_ms Int64,
    post_window_ms Int64,
    continuity_epoch_id Nullable(Int64),
    gap_coverage LowCardinality(String),
    eligibility LowCardinality(String),
    overlap_cluster_id Nullable(String),
    payload_json String,
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (contract_id);

-- Canonical dedup views (logical = one row per record_id)
CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_records_canonical AS
SELECT *
FROM research_full_ob_import_pilot_v1.full_ob_records
WHERE (record_id, ingestion_ts) IN (
    SELECT record_id, max(ingestion_ts)
    FROM research_full_ob_import_pilot_v1.full_ob_records
    GROUP BY record_id
);

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_checkpoints AS
SELECT *
FROM research_full_ob_import_pilot_v1.v_full_ob_records_canonical
WHERE record_kind IN ('INITIAL_CHECKPOINT', 'RESYNC_CHECKPOINT');

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_book_deltas AS
SELECT *
FROM research_full_ob_import_pilot_v1.v_full_ob_records_canonical
WHERE record_kind = 'BOOK_DELTA';

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_markers AS
SELECT *
FROM research_full_ob_import_pilot_v1.v_full_ob_records_canonical
WHERE record_kind IN ('EVENT_MARKER', 'EVENT_END', 'RESYNC_BOUNDARY', 'NESTED_PROFILE_EDGE_SIGNAL');

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_level_changes AS
SELECT
    record_id,
    fight_event_id,
    symbol,
    continuity_epoch_id,
    u,
    seq,
    exchange_ts_ms,
    'bid' AS side,
    tupleElement(lv, 1) AS price,
    tupleElement(lv, 2) AS quantity
FROM research_full_ob_import_pilot_v1.v_full_ob_book_deltas
ARRAY JOIN bids AS lv
UNION ALL
SELECT
    record_id,
    fight_event_id,
    symbol,
    continuity_epoch_id,
    u,
    seq,
    exchange_ts_ms,
    'ask' AS side,
    tupleElement(lv, 1) AS price,
    tupleElement(lv, 2) AS quantity
FROM research_full_ob_import_pilot_v1.v_full_ob_book_deltas
ARRAY JOIN asks AS lv;

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_signals_canonical AS
SELECT *
FROM research_full_ob_import_pilot_v1.full_ob_signals
WHERE (signal_id, updated_at) IN (
    SELECT signal_id, max(updated_at)
    FROM research_full_ob_import_pilot_v1.full_ob_signals
    GROUP BY signal_id
);

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_signal_analysis_contracts_canonical AS
SELECT *
FROM research_full_ob_import_pilot_v1.signal_analysis_contracts
WHERE (contract_id, updated_at) IN (
    SELECT contract_id, max(updated_at)
    FROM research_full_ob_import_pilot_v1.signal_analysis_contracts
    GROUP BY contract_id
);

CREATE VIEW IF NOT EXISTS research_full_ob_import_pilot_v1.v_full_ob_import_state_canonical AS
SELECT *
FROM research_full_ob_import_pilot_v1.full_ob_import_state
WHERE (segment_id, updated_at) IN (
    SELECT segment_id, max(updated_at)
    FROM research_full_ob_import_pilot_v1.full_ob_import_state
    GROUP BY segment_id
);

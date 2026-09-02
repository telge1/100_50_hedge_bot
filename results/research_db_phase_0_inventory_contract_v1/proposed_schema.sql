-- DESIGN ONLY
-- DO NOT EXECUTE DURING PHASE 0
--
-- Target database: btc_doge_research
-- Symbols: BTCUSDT, DOGEUSDT only (enforced at processor; optional CHECK via LowCardinality)
-- All timestamps UTC DateTime64 unless noted.

CREATE DATABASE IF NOT EXISTS btc_doge_research;

-- ---------------------------------------------------------------------------
-- 8.1 research_public_trades
-- Event-level canonical trades; dedup by trade_id
-- Engine: ReplacingMergeTree(ingested_at) — merges duplicates on read without FINAL in hot path
-- Expected: BTC ~2-4M rows/day; DOGE ~200-400K rows/day
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_public_trades
(
    symbol              LowCardinality(String),
    event_time          DateTime64(3, 'UTC'),
    trade_id            String,
    price               Decimal(18, 8),
    base_size           Decimal(24, 12),
    quote_notional      Decimal(24, 8),
    taker_side          Enum8('Buy' = 1, 'Sell' = 2),
    source              LowCardinality(String),
    source_contract_version LowCardinality(String),
    ingested_at         DateTime64(6, 'UTC'),
    quality_flags       LowCardinality(String) DEFAULT '',
    processor_version   LowCardinality(String),
    event_key           String MATERIALIZED concat(symbol, '|', trade_id)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (symbol, toYYYYMM(event_time))
ORDER BY (symbol, event_time, trade_id)
SETTINGS index_granularity = 8192;

-- Insert strategy: batch insert; idempotent via trade_id; re-insert with newer ingested_at replaces
-- Late arrival: overlap window re-reads source FINAL; upsert via ReplacingMergeTree
-- Risk: ReplacingMergeTree not immediately unique — use argMax view or periodic OPTIMIZE in maintenance window only

-- ---------------------------------------------------------------------------
-- 8.2 research_liquidation_events
-- Frozen liquidation_flow_facts_v1
-- Expected: sparse; BTC ~100-500 events/day; DOGE lower
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_liquidation_events
(
    symbol                      LowCardinality(String),
    event_time                  DateTime64(3, 'UTC'),
    position_side_raw           Enum8('Buy' = 1, 'Sell' = 2),
    liquidated_position_side    Enum8('LIQUIDATED_LONG' = 1, 'LIQUIDATED_SHORT' = 2),
    forced_flow                 Enum8('FORCED_BUY' = 1, 'FORCED_SELL' = 2),
    executed_base_size          Decimal(24, 12),
    bankruptcy_price            Decimal(18, 8),
    bankruptcy_reference_quote  Decimal(24, 8),
    execution_price             Nullable(Decimal(18, 8)),  -- always NULL per contract
    execution_notional          Nullable(Decimal(24, 8)),   -- always NULL per contract
    event_key                   String,
    event_key_version           LowCardinality(String) DEFAULT 'event_key_v1',
    source                      LowCardinality(String),
    source_contract_version     LowCardinality(String) DEFAULT 'liquidation_flow_facts_v1',
    quality_flags               LowCardinality(String) DEFAULT '',
    processor_version           LowCardinality(String),
    ingested_at                 DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (symbol, toYYYYMM(event_time))
ORDER BY (symbol, event_time, event_key)
SETTINGS index_granularity = 8192;

-- Mapping frozen: raw Buy -> LIQUIDATED_LONG -> FORCED_SELL; raw Sell -> LIQUIDATED_SHORT -> FORCED_BUY

-- ---------------------------------------------------------------------------
-- 8.3 research_orderbook_1s
-- Neutral second buckets from raw replay or historical aggregate import
-- Expected: 86400 rows/day/symbol
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_orderbook_1s
(
    symbol                  LowCardinality(String),
    bucket_time             DateTime64(0, 'UTC'),
    mid                     Decimal(18, 8),
    best_bid                Decimal(18, 8),
    best_ask                Decimal(18, 8),
    spread_abs              Decimal(18, 8),
    spread_bps              Float64,
    bid_qty_l50             Decimal(24, 12),
    ask_qty_l50             Decimal(24, 12),
    imbalance_l50           Float64,
    bid_notional_l50        Decimal(24, 8),
    ask_notional_l50        Decimal(24, 8),
    impact_buy_bps_10       Nullable(Float64),
    impact_sell_bps_10      Nullable(Float64),
    is_genuine              UInt8,
    is_carried_forward      UInt8,
    source_snapshot_time    Nullable(DateTime64(3, 'UTC')),
    last_update_seq         Nullable(Int64),
    sequence_status         LowCardinality(String),
    source                  LowCardinality(String),
    source_contract_version LowCardinality(String),
    quality_flags           LowCardinality(String) DEFAULT '',
    processor_version       LowCardinality(String),
    ingested_at             DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (symbol, toYYYYMMDD(bucket_time))
ORDER BY (symbol, bucket_time)
SETTINGS index_granularity = 8192;

-- BTC tick 0.1; DOGE tick 0.00001 — Decimal(18,8) sufficient for both
-- genuine/carried_forward: derived from replay clock or quality_flags='carried_forward' on import

-- ---------------------------------------------------------------------------
-- 8.4 research_market_1s
-- Wide neutral merge — NO strategy thresholds, NO hindsight phases
-- Populated from event tables + orderbook_1s via processor (not denormalized duplicates of raw events)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_market_1s
(
    symbol                      LowCardinality(String),
    bucket_time                 DateTime64(0, 'UTC'),
    last_trade_price            Nullable(Decimal(18, 8)),
    mid                         Nullable(Decimal(18, 8)),
    taker_buy_base              Decimal(24, 12) DEFAULT 0,
    taker_sell_base             Decimal(24, 12) DEFAULT 0,
    taker_buy_quote             Decimal(24, 8) DEFAULT 0,
    taker_sell_quote            Decimal(24, 8) DEFAULT 0,
    trade_count                 UInt32 DEFAULT 0,
    taker_delta_base            Decimal(24, 12) DEFAULT 0,
    open_interest               Nullable(Decimal(24, 12)),
    oi_delta                    Nullable(Decimal(24, 12)),
    long_liquidation_base       Decimal(24, 12) DEFAULT 0,
    short_liquidation_base      Decimal(24, 12) DEFAULT 0,
    forced_buy_base             Decimal(24, 12) DEFAULT 0,
    forced_sell_base            Decimal(24, 12) DEFAULT 0,
    spread_bps                  Nullable(Float64),
    imbalance_l50               Nullable(Float64),
    ob_is_genuine               Nullable(UInt8),
    ob_is_carried_forward       Nullable(UInt8),
    source_coverage_mask        UInt16,
    late_arrival_status         LowCardinality(String) DEFAULT 'provisional',
    finalization_status         LowCardinality(String) DEFAULT 'open',
    source_contract_version     LowCardinality(String),
    processor_version           LowCardinality(String),
    ingested_at                 DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (symbol, toYYYYMMDD(bucket_time))
ORDER BY (symbol, bucket_time)
SETTINGS index_granularity = 8192;

-- Fields intentionally NOT stored here: individual trade rows, liq event keys, full OB levels
-- finalization_status: open -> finalized after watermark passes

-- ---------------------------------------------------------------------------
-- 8.5 research_market_1m
-- Causal aggregation from research_market_1s + OI open/close
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_market_1m
(
    symbol                      LowCardinality(String),
    bucket_time                 DateTime64(0, 'UTC'),
    open                        Decimal(18, 8),
    high                        Decimal(18, 8),
    low                         Decimal(18, 8),
    close                       Decimal(18, 8),
    volume_base                 Decimal(24, 12),
    volume_quote                Decimal(24, 8),
    taker_buy_base              Decimal(24, 12),
    taker_sell_base             Decimal(24, 12),
    taker_delta_base            Decimal(24, 12),
    trade_count                 UInt32,
    oi_open                     Nullable(Decimal(24, 12)),
    oi_close                    Nullable(Decimal(24, 12)),
    oi_delta                    Nullable(Decimal(24, 12)),
    long_liquidation_base       Decimal(24, 12),
    short_liquidation_base      Decimal(24, 12),
    forced_buy_base             Decimal(24, 12),
    forced_sell_base            Decimal(24, 12),
    mid_open                    Nullable(Decimal(18, 8)),
    mid_close                   Nullable(Decimal(18, 8)),
    spread_bps_mean             Nullable(Float64),
    imbalance_l50_mean          Nullable(Float64),
    genuine_seconds             UInt32,
    carried_forward_seconds     UInt32,
    coverage_quality_status     LowCardinality(String),
    finalization_status         LowCardinality(String),
    source_contract_version     LowCardinality(String),
    processor_version           LowCardinality(String),
    ingested_at                 DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (symbol, toYYYYMM(bucket_time))
ORDER BY (symbol, bucket_time)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- 8.6 research_orderbook_levels (OPTIONAL — Phase 2 if pool/wall research requires)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_orderbook_levels
(
    symbol                  LowCardinality(String),
    bucket_time             DateTime64(0, 'UTC'),
    side                    Enum8('bid' = 1, 'ask' = 2),
    level_rank              UInt16,
    price                   Decimal(18, 8),
    size                    Decimal(24, 12),
    tick_distance           Int32,
    mid_distance_bps        Float64,
    is_genuine              UInt8,
    is_carried_forward      UInt8,
    depth                   UInt16 DEFAULT 200,
    source                  LowCardinality(String),
    source_contract_version LowCardinality(String),
    processor_version       LowCardinality(String),
    ingested_at             DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY (symbol, toYYYYMMDD(bucket_time))
ORDER BY (symbol, bucket_time, side, level_rank)
SETTINGS index_granularity = 8192;

-- Storage estimate: 200 levels * 2 sides * 86400 sec ~ 34M rows/day/symbol — defer until proven needed
-- Alternative: Parquet cold storage + ClickHouse summary only

-- ---------------------------------------------------------------------------
-- 8.7 research_coverage
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_coverage
(
    source                  LowCardinality(String),
    symbol                  LowCardinality(String),
    data_type               LowCardinality(String),
    period_start            DateTime64(0, 'UTC'),
    period_end              DateTime64(0, 'UTC'),
    expected_buckets        UInt64,
    present_buckets         UInt64,
    genuine_buckets         UInt64,
    carried_forward_buckets UInt64,
    gap_count               UInt32,
    duplicate_count         UInt32,
    quality_status          LowCardinality(String),
    contract_version        LowCardinality(String),
    checked_at              DateTime64(6, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(period_start)
ORDER BY (source, symbol, data_type, period_start)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- 8.8 research_pipeline_state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_pipeline_state
(
    processor               LowCardinality(String),
    source                  LowCardinality(String),
    symbol                  LowCardinality(String),
    last_read_ts            DateTime64(3, 'UTC'),
    last_finalized_ts       DateTime64(3, 'UTC'),
    watermark_ts            DateTime64(3, 'UTC'),
    overlap_seconds         UInt32,
    last_successful_run     DateTime64(6, 'UTC'),
    rows_read               UInt64,
    rows_written            UInt64,
    late_rows_corrected     UInt64,
    processor_version       LowCardinality(String),
    contract_version        LowCardinality(String),
    status                  LowCardinality(String),
    error                   String DEFAULT ''
)
ENGINE = ReplacingMergeTree(last_successful_run)
ORDER BY (processor, source, symbol)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- 8.9 research_features (derived layer — separate from neutral facts)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS btc_doge_research.research_features
(
    symbol                      LowCardinality(String),
    bucket_time                 DateTime64(0, 'UTC'),
    feature_name                LowCardinality(String),
    feature_contract_version    LowCardinality(String),
    causal_or_hindsight         Enum8('CAUSAL' = 1, 'HINDSIGHT' = 2),
    usable_for_live_signal      UInt8,
    input_watermark             DateTime64(3, 'UTC'),
    value_json                  String,
    computed_at                 DateTime64(6, 'UTC'),
    processor_version           LowCardinality(String)
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY (symbol, feature_name, toYYYYMM(bucket_time))
ORDER BY (symbol, feature_name, bucket_time, feature_contract_version)
SETTINGS index_granularity = 8192;

-- EMA, ATR, Stochastic, TPO, Market Profile, Pools, Walls, Fight-Facts, Absorption, Breakout/Reclaim
-- NEVER mix HINDSIGHT features into research_market_1s/1m

-- ---------------------------------------------------------------------------
-- Recommended view: deduplicated trades without requiring FINAL on every query
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS btc_doge_research.research_public_trades_v AS
SELECT
    symbol,
    event_time,
    trade_id,
    argMax(price, ingested_at) AS price,
    argMax(base_size, ingested_at) AS base_size,
    argMax(quote_notional, ingested_at) AS quote_notional,
    argMax(taker_side, ingested_at) AS taker_side,
    argMax(source, ingested_at) AS source,
    argMax(source_contract_version, ingested_at) AS source_contract_version,
    max(ingested_at) AS ingested_at,
    argMax(quality_flags, ingested_at) AS quality_flags
FROM btc_doge_research.research_public_trades
GROUP BY symbol, event_time, trade_id;

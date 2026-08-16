-- Signal Generator canonical ClickHouse schema.
-- Idempotent: CREATE DATABASE / CREATE TABLE IF NOT EXISTS only.
-- No DROP / TRUNCATE.

CREATE DATABASE IF NOT EXISTS signal_generator;

-- ---------------------------------------------------------------------------
-- candles_1m
-- Canonical closed 1-minute OHLCV candles (history + live, identical schema).
-- Logical uniqueness: (exchange, symbol, interval, open_time)
-- Dedup: ReplacingMergeTree(ingested_at) — merges are asynchronous.
-- Reads MUST use FINAL or an explicit latest-row dedup, never raw SELECT.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_generator.candles_1m
(
    `exchange` LowCardinality(String),
    `symbol` LowCardinality(String),
    `interval` LowCardinality(String) DEFAULT '1m',
    `open_time` DateTime64(3, 'UTC'),
    `close_time` DateTime64(3, 'UTC'),
    `open` Decimal(18, 8),
    `high` Decimal(18, 8),
    `low` Decimal(18, 8),
    `close` Decimal(18, 8),
    `volume` Decimal(28, 8),
    `turnover` Decimal(28, 8),
    `is_closed` UInt8 DEFAULT 1,
    `source` LowCardinality(String),
    `source_event_time` Nullable(DateTime64(3, 'UTC')),
    `ingested_at` DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(open_time)
ORDER BY (exchange, symbol, interval, open_time)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- signals
-- Every generated signal (selected or not, traded or not).
-- Chart query path: symbol + candle_open_time / generated_at range.
-- Version columns keep historical logic reproducible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_generator.signals
(
    `signal_id` UUID,
    `generated_at` DateTime64(3, 'UTC'),
    `candle_open_time` DateTime64(3, 'UTC'),
    `candle_close_time` DateTime64(3, 'UTC'),
    `symbol` LowCardinality(String),
    `timeframe` LowCardinality(String),

    `direction` Enum8('LONG' = 1, 'SHORT' = 2),
    `signal_type` LowCardinality(String),
    `signal_price` Decimal(18, 8),

    `stoch_k` Nullable(Float64),
    `stoch_d` Nullable(Float64),
    `wave_state` LowCardinality(Nullable(String)),

    `tier_a` UInt8 DEFAULT 0,
    `tier_a_context` String DEFAULT '',

    `rank_score` Nullable(Float64),
    `selected` UInt8 DEFAULT 0,
    `selection_reason` String DEFAULT '',

    `trend_15m` LowCardinality(Nullable(String)),
    `trend_30m` LowCardinality(Nullable(String)),
    `trend_1h` LowCardinality(Nullable(String)),
    `trend_4h` LowCardinality(Nullable(String)),
    `signal_bias` LowCardinality(Nullable(String)),

    `traded` UInt8 DEFAULT 0,
    `trade_id` Nullable(String),

    `generator_version` LowCardinality(String),
    `strategy_version` LowCardinality(String),

    `metadata` String DEFAULT '{}',
    `ingested_at` DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(generated_at)
ORDER BY (symbol, timeframe, candle_open_time, signal_id)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- signal_outcomes
-- Objective post-hoc evaluation. One signal may have many horizons.
-- Logical uniqueness: (signal_id, horizon)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_generator.signal_outcomes
(
    `signal_id` UUID,
    `evaluated_at` DateTime64(3, 'UTC'),
    `horizon` LowCardinality(String),

    `mfe_pct` Nullable(Float64),
    `mae_pct` Nullable(Float64),
    `tp_hit` UInt8 DEFAULT 0,
    `sl_hit` UInt8 DEFAULT 0,
    `time_to_tp_seconds` Nullable(Int64),
    `time_to_sl_seconds` Nullable(Int64),
    `price_after_horizon` Nullable(Decimal(18, 8)),

    `metadata` String DEFAULT '{}',
    `ingested_at` DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(evaluated_at)
ORDER BY (signal_id, horizon)
SETTINGS index_granularity = 8192;

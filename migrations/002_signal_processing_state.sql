-- Persistent watermark for shadow/live signal processing.
-- Tracks which closed HTF candles were already evaluated (signal or none).
-- Logical uniqueness: (symbol, timeframe, strategy_version)
-- Dedup: ReplacingMergeTree(updated_at)

CREATE TABLE IF NOT EXISTS signal_generator.signal_processing_state
(
    `exchange` LowCardinality(String) DEFAULT 'bybit',
    `symbol` LowCardinality(String),
    `timeframe` LowCardinality(String),
    `strategy_version` LowCardinality(String),
    `last_processed_candle_open_time` DateTime64(3, 'UTC'),
    `last_processed_available_at` DateTime64(3, 'UTC'),
    `updated_at` DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC'),
    `metadata` String DEFAULT '{}'
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (exchange, symbol, timeframe, strategy_version)
SETTINGS index_granularity = 8192;

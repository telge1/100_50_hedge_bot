-- Historical public-trade archive. Isolated from the live 30-day recorder table.
-- Do NOT ALTER public_trades / orderbook_deltas / ticker_samples.
-- Do NOT write to signal_generator.* (live candle collector / scanner).

CREATE TABLE IF NOT EXISTS orderbook_analysis.public_trades_archive
(
    `trade_ts` DateTime64(3, 'UTC'),
    `received_ts` DateTime64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `trade_id` String,
    `side` Enum8('Buy' = 1, 'Sell' = 2),
    `price` Decimal(18, 8),
    `quantity` Decimal(18, 8),
    `notional` Decimal(18, 8),
    `tick_direction` LowCardinality(String),
    `is_block_trade` UInt8,
    `is_rpi_trade` UInt8,
    `ingest_source` LowCardinality(String),
    `source_file` String
)
ENGINE = ReplacingMergeTree(received_ts)
PARTITION BY toYYYYMMDD(trade_ts)
ORDER BY (symbol, trade_ts, trade_id)
SETTINGS index_granularity = 8192;

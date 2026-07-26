-- Reproducible schema matching the currently deployed ClickHouse tables.
-- Database: orderbook_analysis
-- Do not DROP or ALTER existing tables unless a proven schema defect exists.

CREATE DATABASE IF NOT EXISTS orderbook_analysis;

CREATE TABLE IF NOT EXISTS orderbook_analysis.orderbook_deltas
(
    `exchange_ts` DateTime64(3, 'UTC'),
    `received_ts` DateTime64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `side` Enum8('bid' = 1, 'ask' = 2),
    `price` Decimal(18, 8),
    `quantity` Decimal(18, 8),
    `message_type` Enum8('snapshot' = 1, 'delta' = 2),
    `update_id` UInt64,
    `cross_sequence` UInt64,
    `level_index` UInt16
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(exchange_ts)
ORDER BY (symbol, exchange_ts, cross_sequence, update_id, side, price)
TTL exchange_ts + toIntervalDay(30)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.public_trades
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
    `is_rpi_trade` UInt8
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(trade_ts)
ORDER BY (symbol, trade_ts, trade_id)
TTL trade_ts + toIntervalDay(30)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.ticker_samples
(
    `exchange_ts` DateTime64(3, 'UTC'),
    `received_ts` DateTime64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `last_price` Nullable(Decimal(18, 8)),
    `mark_price` Nullable(Decimal(18, 8)),
    `index_price` Nullable(Decimal(18, 8)),
    `best_bid_price` Nullable(Decimal(18, 8)),
    `best_ask_price` Nullable(Decimal(18, 8)),
    `open_interest` Nullable(Decimal(18, 8)),
    `open_interest_value` Nullable(Decimal(18, 8)),
    `funding_rate` Nullable(Decimal(18, 10)),
    `volume_24h` Nullable(Decimal(18, 8)),
    `turnover_24h` Nullable(Decimal(18, 8))
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(exchange_ts)
ORDER BY (symbol, exchange_ts)
TTL exchange_ts + toIntervalDay(90)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.liquidations
(
    `liquidation_ts` DateTime64(3, 'UTC'),
    `received_ts` DateTime64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `side` Enum8('Buy' = 1, 'Sell' = 2),
    `price` Decimal(18, 8),
    `quantity` Decimal(18, 8),
    `notional` Decimal(18, 8)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(liquidation_ts)
ORDER BY (symbol, liquidation_ts, side, price)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS orderbook_analysis.recorder_health
(
    `event_ts` DateTime64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `event_type` LowCardinality(String),
    `stream` LowCardinality(String),
    `message` String,
    `websocket_reconnects` UInt32,
    `messages_received` UInt64,
    `rows_inserted` UInt64,
    `queue_size` UInt32
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_ts)
ORDER BY (symbol, event_ts, event_type)
SETTINGS index_granularity = 8192;

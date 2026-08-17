-- Canonical public trades (archive backfill + future live ingest).
-- Idempotent: CREATE DATABASE / CREATE TABLE IF NOT EXISTS only.
-- No DROP / TRUNCATE / ALTER of existing tables.
-- Do not write to signal_generator.candles_1m.
-- Do not mutate orderbook_analysis.public_trades or public_trades_archive.

CREATE DATABASE IF NOT EXISTS orderbook_analysis;

-- ---------------------------------------------------------------------------
-- public_trades_canonical
-- Logical uniqueness: (symbol, trade_id)
-- Dedup: ReplacingMergeTree(ingest_timestamp) — merges are asynchronous.
-- Reads MUST use FINAL or uniqExact/GROUP BY trade_id, never raw SELECT for counts.
-- side: Bybit taker side ('Buy' = aggressive buy, 'Sell' = aggressive sell).
-- size: base quantity (CSV column size).
-- notional: quote USDT (CSV foreignNotional, else price * size).
-- source: 'archive' | 'live' | 'gap_fill'
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orderbook_analysis.public_trades_canonical
(
    `trade_ts` DateTime64(3, 'UTC'),
    `ingest_timestamp` DateTime64(6, 'UTC') DEFAULT now64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `trade_id` String,
    `side` Enum8('Buy' = 1, 'Sell' = 2),
    `price` Decimal(18, 8),
    `size` Decimal(18, 8),
    `notional` Decimal(18, 8),
    `tick_direction` LowCardinality(String),
    `is_rpi_trade` UInt8 DEFAULT 0,
    `source` LowCardinality(String),
    `source_file` String DEFAULT '',
    `exchange` LowCardinality(String) DEFAULT 'bybit'
)
ENGINE = ReplacingMergeTree(ingest_timestamp)
PARTITION BY toYYYYMM(trade_ts)
ORDER BY (symbol, trade_id)
SETTINGS index_granularity = 8192;

-- Later, lossless copy of the existing 4,251,001 archive rows (DO NOT RUN in the
-- DOGE/LIT 2-day pilot):
--
-- INSERT INTO orderbook_analysis.public_trades_canonical
--     (trade_ts, ingest_timestamp, symbol, trade_id, side, price, size, notional,
--      tick_direction, is_rpi_trade, source, source_file, exchange)
-- SELECT
--     trade_ts,
--     received_ts,
--     symbol,
--     trade_id,
--     side,
--     price,
--     quantity,
--     notional,
--     tick_direction,
--     is_rpi_trade,
--     'archive',
--     source_file,
--     'bybit'
-- FROM orderbook_analysis.public_trades_archive
-- SETTINGS max_insert_threads = 1, priority = 16;

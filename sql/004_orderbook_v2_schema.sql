-- ORDERBOOK_V2_ADAUSDT_7D_PILOT schema
-- Parser version: ob200_v2
-- No writes to orderbook_deltas (broken). All new tables have _v2 suffix.
-- ClickHouse 26.7+

CREATE TABLE IF NOT EXISTS orderbook_analysis.orderbook_import_manifest_v2
(
    exchange          LowCardinality(String)    NOT NULL,
    market            LowCardinality(String)    NOT NULL,
    symbol            LowCardinality(String)    NOT NULL,
    depth             UInt16                    NOT NULL,
    source_date       Date                      NOT NULL,
    source_url        String                    NOT NULL,
    local_path        String                    NOT NULL,
    sha256            FixedString(64)           NOT NULL,
    compressed_bytes  UInt64                    DEFAULT 0,
    raw_record_count  UInt64                    DEFAULT 0,
    source_min_ts     DateTime64(3, 'UTC')      DEFAULT '1970-01-01 00:00:00',
    source_max_ts     DateTime64(3, 'UTC')      DEFAULT '1970-01-01 00:00:00',
    downloaded_at     DateTime64(3, 'UTC')      DEFAULT '1970-01-01 00:00:00',
    import_started_at DateTime64(3, 'UTC')      DEFAULT '1970-01-01 00:00:00',
    import_completed_at DateTime64(3, 'UTC')    DEFAULT '1970-01-01 00:00:00',
    parser_version    LowCardinality(String)    NOT NULL,
    status            LowCardinality(String)    NOT NULL,
    error_message     String                    DEFAULT '',
    quality_flags     String                    DEFAULT '',
    inserted_feature_rows UInt64               DEFAULT 0,
    updated_at        DateTime64(3, 'UTC')      DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(source_date)
ORDER BY (exchange, market, symbol, depth, source_date)
SETTINGS index_granularity = 8192;


CREATE TABLE IF NOT EXISTS orderbook_analysis.orderbook_features_1s_v2
(
    -- Identity & time
    exchange          LowCardinality(String)    NOT NULL,
    market            LowCardinality(String)    NOT NULL,
    symbol            LowCardinality(String)    NOT NULL,
    depth             UInt16                    NOT NULL,
    bucket_start      DateTime64(3, 'UTC')      NOT NULL,
    first_source_ts   DateTime64(3, 'UTC')      NOT NULL,
    last_source_ts    DateTime64(3, 'UTC')      NOT NULL,
    last_update_seq   UInt64                    DEFAULT 0,
    processed_updates UInt32                    DEFAULT 0,
    parser_version    LowCardinality(String)    NOT NULL,
    created_at        DateTime64(3, 'UTC')      DEFAULT now64(3),
    quality_flags     String                    DEFAULT '',
    is_valid          UInt8                     DEFAULT 1,

    -- Best Bid/Ask (prices as Decimal, qty as Decimal)
    best_bid_price    Decimal(18, 8)            NOT NULL,
    best_bid_qty      Decimal(18, 8)            NOT NULL,
    best_ask_price    Decimal(18, 8)            NOT NULL,
    best_ask_qty      Decimal(18, 8)            NOT NULL,
    mid_price         Decimal(18, 8)            NOT NULL,
    -- microprice = (best_ask_price*best_bid_qty + best_bid_price*best_ask_qty) / (best_bid_qty + best_ask_qty)
    microprice        Decimal(18, 8)            NOT NULL,
    spread_abs        Decimal(18, 8)            NOT NULL,
    spread_bps        Decimal(18, 4)            NOT NULL,

    -- Depth by level count (top N levels)
    bid_qty_l5        Decimal(18, 8)            NOT NULL,
    ask_qty_l5        Decimal(18, 8)            NOT NULL,
    bid_notional_l5   Decimal(18, 8)            NOT NULL,
    ask_notional_l5   Decimal(18, 8)            NOT NULL,
    imbalance_l5      Decimal(12, 8)            NOT NULL,

    bid_qty_l10       Decimal(18, 8)            NOT NULL,
    ask_qty_l10       Decimal(18, 8)            NOT NULL,
    bid_notional_l10  Decimal(18, 8)            NOT NULL,
    ask_notional_l10  Decimal(18, 8)            NOT NULL,
    imbalance_l10     Decimal(12, 8)            NOT NULL,

    bid_qty_l25       Decimal(18, 8)            NOT NULL,
    ask_qty_l25       Decimal(18, 8)            NOT NULL,
    bid_notional_l25  Decimal(18, 8)            NOT NULL,
    ask_notional_l25  Decimal(18, 8)            NOT NULL,
    imbalance_l25     Decimal(12, 8)            NOT NULL,

    bid_qty_l50       Decimal(18, 8)            NOT NULL,
    ask_qty_l50       Decimal(18, 8)            NOT NULL,
    bid_notional_l50  Decimal(18, 8)            NOT NULL,
    ask_notional_l50  Decimal(18, 8)            NOT NULL,
    imbalance_l50     Decimal(12, 8)            NOT NULL,

    -- Depth by bps distance from mid_price
    bid_qty_bps5      Decimal(18, 8)            NOT NULL,
    ask_qty_bps5      Decimal(18, 8)            NOT NULL,
    bid_notional_bps5 Decimal(18, 8)            NOT NULL,
    ask_notional_bps5 Decimal(18, 8)            NOT NULL,
    imbalance_bps5    Decimal(12, 8)            NOT NULL,

    bid_qty_bps10     Decimal(18, 8)            NOT NULL,
    ask_qty_bps10     Decimal(18, 8)            NOT NULL,
    bid_notional_bps10 Decimal(18, 8)           NOT NULL,
    ask_notional_bps10 Decimal(18, 8)           NOT NULL,
    imbalance_bps10   Decimal(12, 8)            NOT NULL,

    bid_qty_bps25     Decimal(18, 8)            NOT NULL,
    ask_qty_bps25     Decimal(18, 8)            NOT NULL,
    bid_notional_bps25 Decimal(18, 8)           NOT NULL,
    ask_notional_bps25 Decimal(18, 8)           NOT NULL,
    imbalance_bps25   Decimal(12, 8)            NOT NULL,

    bid_qty_bps50     Decimal(18, 8)            NOT NULL,
    ask_qty_bps50     Decimal(18, 8)            NOT NULL,
    bid_notional_bps50 Decimal(18, 8)           NOT NULL,
    ask_notional_bps50 Decimal(18, 8)           NOT NULL,
    imbalance_bps50   Decimal(12, 8)            NOT NULL,

    -- Largest visible level (wall candidate) within 200 bps of mid
    -- (no arbitrary classification; raw measurement only)
    bid_wall_price    Decimal(18, 8)            DEFAULT 0,
    bid_wall_qty      Decimal(18, 8)            DEFAULT 0,
    bid_wall_notional Decimal(18, 8)            DEFAULT 0,
    bid_wall_bps_dist Decimal(12, 4)            DEFAULT 0,
    bid_wall_ratio    Decimal(12, 8)            DEFAULT 0,

    ask_wall_price    Decimal(18, 8)            DEFAULT 0,
    ask_wall_qty      Decimal(18, 8)            DEFAULT 0,
    ask_wall_notional Decimal(18, 8)            DEFAULT 0,
    ask_wall_bps_dist Decimal(12, 4)            DEFAULT 0,
    ask_wall_ratio    Decimal(12, 8)            DEFAULT 0,

    -- Dynamics within the second (delta-derived; nullable when not computable)
    -- Available because data has snapshots + deltas with qty=0 removals.
    bid_qty_added     Nullable(Decimal(18, 8))  DEFAULT NULL,
    bid_qty_removed   Nullable(Decimal(18, 8))  DEFAULT NULL,
    ask_qty_added     Nullable(Decimal(18, 8))  DEFAULT NULL,
    ask_qty_removed   Nullable(Decimal(18, 8))  DEFAULT NULL,
    bid_add_count     Nullable(UInt32)          DEFAULT NULL,
    bid_remove_count  Nullable(UInt32)          DEFAULT NULL,
    ask_add_count     Nullable(UInt32)          DEFAULT NULL,
    ask_remove_count  Nullable(UInt32)          DEFAULT NULL,
    -- OFI: sum of signed delta quantities at best bid/ask
    ofi               Nullable(Decimal(18, 8))  DEFAULT NULL,
    mid_price_change  Nullable(Decimal(18, 8))  DEFAULT NULL,
    imbalance_l10_change Nullable(Decimal(12, 8)) DEFAULT NULL,
    imbalance_l50_change Nullable(Decimal(12, 8)) DEFAULT NULL
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY toYYYYMMDD(bucket_start)
ORDER BY (exchange, market, symbol, depth, bucket_start)
SETTINGS index_granularity = 8192;

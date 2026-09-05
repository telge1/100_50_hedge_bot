# clickhouse_schema_audit.md

Read-only. No DDL/DML. `system.tables` scans of `orderbook_analysis` can fail due to broken `orderbook_deltas` attach; `SHOW CREATE` and FQN SELECTs on trade tables still work.

## Tables

### `orderbook_analysis.public_trades_canonical` (analysis SoT)

- **Engine:** `ReplacingMergeTree(ingest_timestamp)`
- **PARTITION BY:** `toYYYYMM(trade_ts)`
- **ORDER BY / sort key:** `(symbol, trade_id)`
- **Primary key:** same as ORDER BY (CH default)
- **Columns:**
  - `trade_ts` DateTime64(3, 'UTC') — exchange event time
  - `ingest_timestamp` DateTime64(6, 'UTC') — insert/version for replacing
  - `symbol` LowCardinality(String)
  - `trade_id` String — Bybit `trdMatchID` (stable)
  - `side` Enum8('Buy','Sell') — **taker/aggressor** side
  - `price` Decimal(18,8)
  - `size` Decimal(18,8) — base qty
  - `notional` Decimal(18,8) — quote
  - `tick_direction` LowCardinality(String)
  - `is_rpi_trade` UInt8
  - `source` LowCardinality(String) — `archive` | `live` | `gap_fill`
  - `source_file` String
  - `exchange` LowCardinality(String) default `bybit`
- **Dedup:** logical uniqueness `(symbol, trade_id)`; async merges
- **Materialized views:** none observed for this table
- **Canonical views:** none separate; table name *is* the canonical store
- **Query safety:** migration states counts must use `FINAL` or `uniqExact`/`GROUP BY trade_id`. Plain `count()` can over-read physical duplicates before merge. Windowed analysis by `trade_ts` is common without FINAL; with low duplicate rate this is often OK, but not formally exact.
- **Re-import:** may increase physical rows; logical IDs unchanged after merge/FINAL

### `orderbook_analysis.public_trades` (legacy TTL)

- **Engine:** MergeTree
- **PARTITION BY:** `toYYYYMMDD(trade_ts)`
- **ORDER BY:** `(symbol, trade_ts, trade_id)`
- **TTL:** `trade_ts + 30 days`
- **Not** the Full-OB analysis target
- Observed span sample: ~2026-08-05 .. 2026-08-11 (TTL shrinking)

### `orderbook_analysis.public_trades_archive` (OA historical sink)

- **Engine:** `ReplacingMergeTree(received_ts)`
- **PARTITION BY:** `toYYYYMMDD(trade_ts)`
- **ORDER BY:** `(symbol, trade_ts, trade_id)`
- **Extra:** `ingest_source`, `source_file`, `quantity` (vs `size` in canonical)
- Observed: 4,251,001 rows, 2 symbols, 2025-12-29 .. 2026-07-30
- OA ingest CLI writes **only** here, never into canonical

### Stale mirror `btc_doge_research.research_public_trades`

- Timestamp column: **`event_time`** (not `trade_ts`)
- BTCUSDT ends **2026-08-31**; not suitable for Sep-4 analysis
- Do not use for Full-OB joins

## Idempotency summary

| Action | Physical | Logical |
|--------|----------|---------|
| Repeat archive backfill (SG) | may +rows | stable via trade_id + ReplacingMergeTree |
| Live + archive same trade_id | may +rows | one logical after merge/FINAL |
| OA archive re-ingest | may +rows in archive | archive ReplacingMergeTree |

**OPTIMIZE FINAL not required** for eventual correctness, but exact count queries should use FINAL/uniqExact.

## Broken sibling note

`orderbook_analysis.orderbook_deltas` fails async attach (`TOO_MANY_UNEXPECTED_DATA_PARTS`). Does **not** block FQN reads of `public_trades_canonical`, but breaks some `system.tables` / `system.parts` queries scoped to the whole database.

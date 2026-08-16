# ClickHouse database design

## Decision: dedicated database

**Choice: A — own database `signal_generator`**

Same ClickHouse instance as `orderbook_analysis` (Docker `orderbook-clickhouse` on `127.0.0.1:8123`), but a separate logical database.

Reasons:

- No shared table-name collisions with orderbook tables
- Independent retention / migration lifecycle
- Same host/user/credentials convention — no extra infrastructure
- Clear ownership for the signal-generator service

## Volume estimate

| Metric | Value |
| ------ | ----- |
| Symbols | ~100 |
| Candles / day | 100 × 1440 = 144 000 |
| Candles / year | ~52.6 M |
| Rows / 5 years | ~263 M |

`ReplacingMergeTree` + monthly partitions + `ORDER BY (exchange, symbol, interval, open_time)` is appropriate. Symbol+range queries hit a tight primary-key prefix. 50–300 M rows is routine for ClickHouse.

## Candle uniqueness & async dedup

Logical key: `(exchange, symbol, interval, open_time)`.

Engine: `ReplacingMergeTree(ingested_at)`.

ClickHouse merges are **asynchronous**. Duplicate logical candles may coexist until a merge. Application reads therefore:

1. Prefer `SELECT ... FROM candles_1m FINAL WHERE ...`, or
2. Deduplicate explicitly with `argMax(..., ingested_at)` / `LIMIT 1 BY ...`

Tests and analysis must never assume immediate physical uniqueness after insert.

Only **closed** candles are stored (`is_closed = 1`). Partial live candles are not written to `candles_1m`.

## Higher timeframe aggregation (from 1m only)

Physical storage is **only** `candles_1m`. Higher TFs are derived deterministically in UTC:

| TF | Bucket start | Closed when |
| -- | ------------ | ----------- |
| 5m | `floor(minute / 5) * 5` | open_time + 5m |
| 15m | `floor(minute / 15) * 15` | open_time + 15m |
| 30m | `floor(minute / 30) * 30` | open_time + 30m |
| 1h | start of hour | open_time + 1h |
| 4h | `floor(hour / 4) * 4` UTC | open_time + 4h |

Aggregation rules (no lookahead):

- `open` = first 1m open in bucket (by open_time ASC)
- `high` = max(high)
- `low` = min(low)
- `close` = last 1m close in bucket
- `volume` / `turnover` = sum
- Bucket is **complete** only when all expected 1m bars exist **and** wall-clock / stream time ≥ bucket close_time

Example: 15m candle `10:00–10:15 UTC` is finished only at `10:15 UTC` (after the `10:14` 1m bar closes).

History backfill and live derivation must use the same functions (`signal_generator.timeframes`).

## Signals chart query

Primary key prefix `(symbol, timeframe, candle_open_time, signal_id)` supports:

```sql
SELECT *
FROM signal_generator.signals FINAL
WHERE symbol = 'DOGEUSDT'
  AND candle_open_time >= {A}
  AND candle_open_time < {B}
ORDER BY candle_open_time, signal_id
```

All signals are stored (`selected`, `traded` flags), including rejected ones.

## Multi-horizon outcomes

Unique key: `(signal_id, horizon)` — one signal may have rows for `15m`, `30m`, `1h`, `4h`, etc.

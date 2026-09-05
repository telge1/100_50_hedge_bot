# Query log — aggressor efficiency data audit v1

All queries were **SELECT/DESCRIBE/SHOW CREATE** only. No DDL/DML/mutations.
Database `orderbook_analysis.orderbook_deltas` was **not queried** (unattachable broken parts).
`SHOW TABLES FROM orderbook_analysis` / broad `system.tables` scans were avoided because they wait on the broken table load.

## 1. candles_desc (0.9 ms, rows=15)
```sql
DESCRIBE TABLE signal_generator.candles_1m
```

## 2. candles_window (3.3 ms, rows=1)
```sql
SELECT count(), min(open_time), max(open_time)
FROM signal_generator.candles_1m
WHERE symbol={sym:String} AND exchange='bybit'
  AND open_time >= toDateTime64({t0:String},3,'UTC')
  AND open_time < toDateTime64({t1:String},3,'UTC')
```

## 3. ticker_all_doge (21.2 ms, rows=1)
```sql
SELECT min(exchange_ts), max(exchange_ts), count()
FROM orderbook_analysis.ticker_samples WHERE symbol={sym:String}
```

## 4. public_trades_window (2.4 ms, rows=1)
```sql
SELECT min(trade_ts), max(trade_ts), count(), uniqExact(trade_id)
FROM orderbook_analysis.public_trades
WHERE symbol={sym:String}
  AND trade_ts >= toDateTime64({t0:String},3,'UTC')
  AND trade_ts < toDateTime64({t1:String},3,'UTC')
```

## 5. public_trades_archive_window (2.3 ms, rows=1)
```sql
SELECT min(trade_ts), max(trade_ts), count(), uniqExact(trade_id)
FROM orderbook_analysis.public_trades_archive
WHERE symbol={sym:String}
  AND trade_ts >= toDateTime64({t0:String},3,'UTC')
  AND trade_ts < toDateTime64({t1:String},3,'UTC')
```

## 6. oi5s (5.1 ms, rows=1)
```sql
SELECT count(), uniqExact(bucket_time), min(bucket_time), max(bucket_time),
 countIf(state_valid=0), quantileExact(0.5)(state_age_ms), quantileExact(0.99)(state_age_ms)
 FROM orderbook_analysis.open_interest_5s
 WHERE symbol={sym:String}
  AND bucket_time >= toDateTime64({t0:String},3,'UTC')
  AND bucket_time < toDateTime64({t1:String},3,'UTC')
```

## 7. oi5s_all (5.3 ms, rows=1)
```sql
SELECT min(bucket_time), max(bucket_time), count() FROM orderbook_analysis.open_interest_5s WHERE symbol={sym:String}
```

## 8. oi_events (3.8 ms, rows=1)
```sql
SELECT count(), min(event_time), max(event_time) FROM orderbook_analysis.open_interest_events
 WHERE symbol={sym:String}
  AND event_time >= toDateTime64({t0:String},3,'UTC')
  AND event_time < toDateTime64({t1:String},3,'UTC')
```

## 9. oi5m (2.8 ms, rows=1)
```sql
SELECT count(), min(bucket_time), max(bucket_time) FROM orderbook_analysis.open_interest_5m_history
 WHERE symbol={sym:String}
  AND bucket_time >= toDateTime64({t0:String},3,'UTC')
  AND bucket_time < toDateTime64({t1:String},3,'UTC')
```

## 10. liq (3.2 ms, rows=1)
```sql
SELECT count(), min(liquidation_ts), max(liquidation_ts) FROM orderbook_analysis.liquidations
 WHERE symbol={sym:String}
  AND liquidation_ts >= toDateTime64({t0:String},3,'UTC')
  AND liquidation_ts < toDateTime64({t1:String},3,'UTC')
```

## 11. all_liq (2.4 ms, rows=1)
```sql
SELECT count(), min(event_time), max(event_time) FROM orderbook_analysis.all_liquidations
 WHERE symbol={sym:String}
  AND event_time >= toDateTime64({t0:String},3,'UTC')
  AND event_time < toDateTime64({t1:String},3,'UTC')
```

## 12. ptc_day_counts (374.7 ms, rows=29)
```sql
SELECT toDate(trade_ts) AS d, count() AS n, uniqExact(trade_id) AS u
FROM orderbook_analysis.public_trades_canonical
WHERE symbol={sym:String}
  AND trade_ts >= toDateTime64('2026-08-01 00:00:00',3,'UTC')
  AND trade_ts <  toDateTime64('2026-08-30 00:00:00',3,'UTC')
GROUP BY d ORDER BY d
```

## 13. ptc_symbols_one_day (115.7 ms, rows=1)
```sql
SELECT count() AS n, uniqExact(symbol) AS nsym
FROM orderbook_analysis.public_trades_canonical
WHERE trade_ts >= toDateTime64('2026-08-28 00:00:00',3,'UTC')
  AND trade_ts <  toDateTime64('2026-08-29 00:00:00',3,'UTC')
```

## 14. ptc_top_symbols_28 (32.0 ms, rows=15)
```sql
SELECT symbol, count() AS n
FROM orderbook_analysis.public_trades_canonical
WHERE trade_ts >= toDateTime64('2026-08-28 00:00:00',3,'UTC')
  AND trade_ts <  toDateTime64('2026-08-29 00:00:00',3,'UTC')
GROUP BY symbol ORDER BY n DESC LIMIT 15
```

## 15. side_vs_tick (8.1 ms, rows=8)
```sql
SELECT side, tick_direction, count() AS c
FROM orderbook_analysis.public_trades_canonical
WHERE symbol={sym:String}
  AND trade_ts >= toDateTime64({t0:String},3,'UTC')
  AND trade_ts < toDateTime64({t1:String},3,'UTC')
GROUP BY side, tick_direction
ORDER BY c DESC
```

## 16. same_ms_multi_price (5.6 ms, rows=1)
```sql
SELECT count() AS groups, max(n_prices), max(n_trades)
FROM (
  SELECT trade_ts, uniqExact(price) AS n_prices, count() AS n_trades, uniqExact(trade_id) AS n_ids
  FROM orderbook_analysis.public_trades_canonical
  WHERE symbol={sym:String}
    AND trade_ts >= toDateTime64({t0:String},3,'UTC')
    AND trade_ts < toDateTime64({t1:String},3,'UTC')
  GROUP BY trade_ts
  HAVING n_trades > 1
)
```

## 17. out_of_order_approx (9.3 ms, rows=1)
```sql
SELECT countIf(trade_ts < prev_ts) AS ooo, count() AS n
FROM (
  SELECT trade_ts,
         lagInFrame(trade_ts) OVER (ORDER BY ingest_timestamp, trade_id) AS prev_ts
  FROM orderbook_analysis.public_trades_canonical
  WHERE symbol={sym:String}
    AND trade_ts >= toDateTime64({t0:String},3,'UTC')
    AND trade_ts < toDateTime64({t1:String},3,'UTC')
)
```

## 18. smoke_1s_buckets (31.7 ms, rows=5010)
```sql
SELECT
  toStartOfSecond(trade_ts) AS sec,
  countIf(side='Buy') AS buy_count,
  countIf(side='Sell') AS sell_count,
  sumIf(size, side='Buy') AS buy_qty,
  sumIf(size, side='Sell') AS sell_qty,
  sumIf(notional, side='Buy') AS buy_notional,
  sumIf(notional, side='Sell') AS sell_notional,
  sumIf(notional, side='Buy') - sumIf(notional, side='Sell') AS net_aggressive_notional,
  argMin(price, trade_ts) AS first_trade_price,
  argMax(price, trade_ts) AS last_trade_price,
  max(price) AS high_trade_price,
  min(price) AS low_trade_price
FROM orderbook_analysis.public_trades_canonical
WHERE symbol={sym:String}
  AND trade_ts >= toDateTime64({t0:String},3,'UTC')
  AND trade_ts < toDateTime64({t1:String},3,'UTC')
GROUP BY sec
ORDER BY sec
```

## 19. oi_vs_price_5m_blocks (17.8 ms, rows=1)
```sql
WITH
oi AS (
  SELECT toStartOfFiveMinutes(bucket_time) AS b,
         argMin(open_interest, bucket_time) AS oi0,
         argMax(open_interest, bucket_time) AS oi1
  FROM orderbook_analysis.open_interest_5s
  WHERE symbol={sym:String}
    AND bucket_time >= toDateTime64({t0:String},3,'UTC')
    AND bucket_time < toDateTime64({t1:String},3,'UTC')
  GROUP BY b
),
px AS (
  SELECT toStartOfFiveMinutes(trade_ts) AS b,
         argMin(price, trade_ts) AS p0,
         argMax(price, trade_ts) AS p1
  FROM orderbook_analysis.public_trades_canonical
  WHERE symbol={sym:String}
    AND trade_ts >= toDateTime64({t0:String},3,'UTC')
    AND trade_ts < toDateTime64({t1:String},3,'UTC')
  GROUP BY b
)
SELECT
  count() AS n,
  countIf(p1>p0 AND oi1<oi0) AS up_oi_down,
  countIf(p1>p0 AND oi1>oi0) AS up_oi_up,
  countIf(p1<p0 AND oi1<oi0) AS down_oi_down,
  countIf(p1<p0 AND oi1>oi0) AS down_oi_up
FROM oi INNER JOIN px USING (b)
```

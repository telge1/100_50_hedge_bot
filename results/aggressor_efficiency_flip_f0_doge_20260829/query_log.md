# Query log — AEF F0

## preflight (8.4 ms, rows=1)
```sql
SELECT
      count() AS rows,
      uniqExact(trade_id) AS uniq_tid,
      countIf(side NOT IN ('Buy','Sell')) AS bad_side,
      min(trade_ts), max(trade_ts)
    FROM orderbook_analysis.public_trades_canonical
    WHERE symbol = {sym:String}
      AND trade_ts >= toDateTime64({t0:String}, 3, 'UTC')
      AND trade_ts <  toDateTime64({t1:String}, 3, 'UTC')
```

## load_trades (30.6 ms, rows=41523)
```sql
SELECT trade_ts, trade_id, side, price, size, notional
    FROM orderbook_analysis.public_trades_canonical
    WHERE symbol = {sym:String}
      AND trade_ts >= toDateTime64({t0:String}, 3, 'UTC')
      AND trade_ts <  toDateTime64({t1:String}, 3, 'UTC')
    ORDER BY trade_ts, trade_id
```

## load_oi (4.4 ms, rows=5400)
```sql
SELECT bucket_time, open_interest
    FROM orderbook_analysis.open_interest_5s
    WHERE symbol = {sym:String}
      AND bucket_time >= toDateTime64({t0:String}, 3, 'UTC')
      AND bucket_time <  toDateTime64({t1:String}, 3, 'UTC')
    ORDER BY bucket_time
```

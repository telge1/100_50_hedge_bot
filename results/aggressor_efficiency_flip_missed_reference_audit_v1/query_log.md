# Query log — missed reference audit (READ-ONLY)

## load_trades (10.1 ms, rows=6272)
```sql
SELECT trade_ts, trade_id, side, price, size, notional
    FROM orderbook_analysis.public_trades_canonical
    WHERE symbol = {sym:String}
      AND trade_ts >= toDateTime64({t0:String}, 3, 'UTC')
      AND trade_ts <  toDateTime64({t1:String}, 3, 'UTC')
    ORDER BY trade_ts, trade_id
```

## load_trades (31.7 ms, rows=41523)
```sql
SELECT trade_ts, trade_id, side, price, size, notional
    FROM orderbook_analysis.public_trades_canonical
    WHERE symbol = {sym:String}
      AND trade_ts >= toDateTime64({t0:String}, 3, 'UTC')
      AND trade_ts <  toDateTime64({t1:String}, 3, 'UTC')
    ORDER BY trade_ts, trade_id
```

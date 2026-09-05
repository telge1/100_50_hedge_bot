# Analysis Query Diff

## Prior path (`results/btc_full_ob_signal_to_crash_20260904_v1/_run_analysis.py`)

1. Tried `btc_doge_research.research_public_trades` / `research_market_1s` / `research_open_interest_observations` / `research_liquidation_events`.
2. Observed coverage ending **2026-08-31**.
3. Assumed `orderbook_analysis` entirely unloadable because `orderbook_deltas` is broken.
4. Wrote `NOT_AVAILABLE` for trades/OI/liq without querying:
   - `orderbook_analysis.public_trades_canonical` (`trade_ts`)
   - `orderbook_analysis.open_interest_events` / `open_interest_5s`
   - `orderbook_analysis.all_liquidations`

## Correct reproduction

```sql
-- WORKS: 389723 rows in analysis window
SELECT count() FROM orderbook_analysis.public_trades_canonical
WHERE symbol='BTCUSDT'
  AND trade_ts >= toDateTime64('2026-09-04 11:17:00',3,'UTC')
  AND trade_ts <  toDateTime64('2026-09-04 12:57:00',3,'UTC');

-- EMPTY (what prior analysis effectively used)
SELECT count() FROM btc_doge_research.research_public_trades
WHERE symbol='BTCUSDT'
  AND event_time >= '2026-09-04 11:17:00'
  AND event_time <  '2026-09-04 12:57:00';
```

## OI / Liquidations

Correct tables are queryable, but **contain no Sep-4 rows** (last write ~2026-09-01T16:46Z). Process 147111 still running; writer historically failed with `SESSION_IS_LOCKED`; health heartbeats stopped 2026-09-01.

# Performance Benchmark Plan (Phase 0 — SQL Entwürfe only)

**Status:** NOT EXECUTED in Phase 0
**Goal:** Replace ~20 min raw pipelines with second-scale repeated research queries

---

## 1. Targets (Phase 1 acceptance — NOT_PROVEN until measured)

| Scenario | Target | Priority |
|----------|--------|----------|
| Point timestamp ±30 min | < 5s | P0 |
| 1 hour window | < 2s | P0 |
| 1 day 1s series | < 5s | P0 |
| 7 days 1m backtest | < 10s | P1 |
| 30 days 1m backtest | < 30s | P1 |
| Full available range 1m | < 120s | P2 |
| Event-level trade lookup | < 1s | P0 |
| Pool/wall level query (if materialized) | < 10s/hour | P2 |
| Liq↔trade heuristic join (feature layer) | < 30s/hour | P2 |

---

## 2. Benchmark SQL Entwürfe

### B1 — Point scene ±30 min (BTC run_018 window)

```sql
-- DESIGN ONLY — run in Phase 1 after tables populated
SELECT
    bucket_time,
    mid, spread_bps, imbalance_l50,
    taker_buy_base, taker_sell_base, trade_count,
    open_interest, long_liquidation_base, short_liquidation_base,
    ob_is_genuine, finalization_status
FROM btc_doge_research.research_market_1s
WHERE symbol = 'BTCUSDT'
  AND bucket_time >= toDateTime64('2026-08-31 18:30:00', 0, 'UTC')
  AND bucket_time <  toDateTime64('2026-08-31 19:30:00', 0, 'UTC')
ORDER BY bucket_time
SETTINGS max_execution_time = 30;
```

### B2 — Single timestamp OB + trades

```sql
SELECT * FROM btc_doge_research.research_orderbook_1s
WHERE symbol = 'BTCUSDT'
  AND bucket_time = toDateTime64('2026-08-31 19:00:00', 0, 'UTC');

SELECT count(), sum(base_size), sum(quote_notional)
FROM btc_doge_research.research_public_trades_v
WHERE symbol = 'BTCUSDT'
  AND event_time >= toDateTime64('2026-08-31 19:00:00.000', 3, 'UTC')
  AND event_time <  toDateTime64('2026-08-31 19:00:01.000', 3, 'UTC');
```

### B3 — 1 hour 1s density

```sql
SELECT count(), sum(trade_count), sum(taker_delta_base)
FROM btc_doge_research.research_market_1s
WHERE symbol = 'DOGEUSDT'
  AND bucket_time >= toDateTime64('2026-08-29 11:00:00', 0, 'UTC')
  AND bucket_time <  toDateTime64('2026-08-29 12:00:00', 0, 'UTC');
```

### B4 — 1 day 1m backtest strip

```sql
SELECT
    bucket_time, open, high, low, close,
    volume_base, taker_delta_base, trade_count,
    oi_close, long_liquidation_base + short_liquidation_base AS liq_base,
    genuine_seconds, carried_forward_seconds
FROM btc_doge_research.research_market_1m
WHERE symbol = 'DOGEUSDT'
  AND bucket_time >= toDateTime64('2026-08-29 00:00:00', 0, 'UTC')
  AND bucket_time <  toDateTime64('2026-08-30 00:00:00', 0, 'UTC')
ORDER BY bucket_time
SETTINGS max_execution_time = 60;
```

### B5 — 7 day 1m aggregation

```sql
SELECT count(), min(bucket_time), max(bucket_time)
FROM btc_doge_research.research_market_1m
WHERE symbol = 'BTCUSDT'
  AND bucket_time >= toDateTime64('2026-08-26 00:00:00', 0, 'UTC')
  AND bucket_time <  toDateTime64('2026-09-02 00:00:00', 0, 'UTC');
```

### B6 — 30 day 1m

```sql
SELECT bucket_time, close, taker_delta_base, oi_delta
FROM btc_doge_research.research_market_1m
WHERE symbol = 'BTCUSDT'
  AND bucket_time >= toDateTime64('2026-08-01 00:00:00', 0, 'UTC')
  AND bucket_time <  toDateTime64('2026-09-01 00:00:00', 0, 'UTC')
ORDER BY bucket_time
SETTINGS max_execution_time = 120;
```

### B7 — Full range metadata (not full scan)

```sql
SELECT min(bucket_time), max(bucket_time), count()
FROM btc_doge_research.research_market_1m
WHERE symbol = 'BTCUSDT';
```

### B8 — Liquidation event drill-down

```sql
SELECT event_time, liquidated_position_side, forced_flow,
       executed_base_size, bankruptcy_price, bankruptcy_reference_quote
FROM btc_doge_research.research_liquidation_events
WHERE symbol = 'BTCUSDT'
  AND event_time >= toDateTime64('2026-08-31 18:30:00', 3, 'UTC')
  AND event_time <  toDateTime64('2026-08-31 19:30:00', 3, 'UTC')
ORDER BY event_time;
```

### B9 — Coverage audit query

```sql
SELECT data_type, period_start, expected_buckets, present_buckets,
       gap_count, quality_status
FROM btc_doge_research.research_coverage
WHERE symbol = 'BTCUSDT'
ORDER BY period_start DESC
LIMIT 30;
```

### B10 — Baseline comparison (current pipeline — Phase 1 only)

Measure wall time of:

```bash
python -m research.btc_ob_fight.cli --anchor 2026-08-31T19:00:00Z ...
```

vs B1 query on populated research DB. Target: **>10× speedup** for repeated queries (ESTIMATED hypothesis).

---

## 3. Measurement Protocol (Phase 1)

1. Warm query (discard first run)
2. Median of 5 runs
3. Record CH `system.query_log` bytes read
4. Compare against acceptance gates `POINT_QUERY_TARGET_MET`, `WINDOW_QUERY_TARGET_MET`
5. Document hardware: server-telgenbuescher baseline

---

## 4. Risks

| Risk | Mitigation |
|------|------------|
| ReplacingMergeTree without view | Use `research_*_v` views in benchmarks |
| Wide table bloat | Benchmark JOIN alternative if 1s wide too slow |
| Cold cache | Note first-run vs warm-run separately |

---

## 5. Explicit Non-Goals Phase 0

- No heavy benchmark execution
- No performance claims marked PROVEN

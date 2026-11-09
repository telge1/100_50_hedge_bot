# Research Charts History — Phase 2

Decision: **RESEARCH_CHARTS_HISTORY_PHASE_2_READY**

Realtime is **Phase 3**. No Bybit WS, no SSE, no ClickHouse candle copy.

```
MySQL market_candles (1m, DATETIME UTC wall-clock)
        ↓
MySQLResearchCandleSource   dashboard/research_charts/data_source.py
        ↓
TRP Candle                  data.models.Candle
        ↓
TRP aggregate() / EMA / Stochastic / Liquidity Location
        ↓
Research API                /api/research/symbols|candles|indicators
        ↓
Dashboard Web Charts        /live-charts/research
```

## market_candles

- DATABASE: `regime_scanner_research`
- TABLE: `market_candles`
- SYMBOL: `symbol` VARCHAR(32)
- TIMESTAMP: `open_time` DATETIME(6) **UTC wall-clock** (not TIMESTAMP; do not apply session CEST)
- OHLC+V: `open/high/low/close/volume` DOUBLE
- UNIQUE: `(exchange, symbol, timeframe, open_time)`
- INDEX for range: `idx_market_candles_lookup (exchange, symbol, timeframe, open_time)`
- Do **not** wrap `timeframe` in SQL `BINARY()` — it disables the index (3s full scan vs ~2ms).

1m symbols (closed): APTUSDT, BTCUSDT, DOGEUSDT. Exchange: bybit.

| symbol | first UTC | last UTC | 1m rows |
|---|---|---|---|
| APTUSDT | 2022-10-19 02:48 | 2026-08-08 10:44 | 2_000_637 |
| BTCUSDT | 2020-03-25 10:36 | 2021-12-09 01:15 | 898_000 |
| DOGEUSDT | 2021-06-02 10:44 | 2026-08-08 10:52 | 2_725_929 |

Quality: 0 duplicates, 0 OHLC violations, 0 negative volume, 1m coverage 100% dense. Zero-volume bars exist (ok). History ends 2026-08-08 (not live).

## Defaults

Layout 4 panes: **1m / 5m / 15m / 1h**. HTF via TRP `strict_complete_buckets=True`. Default windows ~600–1500 visible bars.

## Performance (DOGEUSDT, this host)

| op | ms | n |
|---|---|---|
| symbol list GROUP BY | ~5500 first, then 60s cache | 3 |
| 1m default | 30 | 1500 |
| 5m | 51 | 1500 |
| 15m | 112 | 1200 |
| 30m | 182 | 1000 |
| 1h | 284 | 800 |
| 4h | 861 | 600 |
| 4-pane APT after warmup | 463 | |
| indicators 15m ema+stoch+lld | 15 (candles cached) | |

Index recommendation (not applied): symbol-summary table or periodic cache for DISTINCT/GROUP BY. Range queries are already indexed.

## Phase 3

SSE/WS live candles, optional ClickHouse 1m SoT replacing MySQLResearchCandleSource only.

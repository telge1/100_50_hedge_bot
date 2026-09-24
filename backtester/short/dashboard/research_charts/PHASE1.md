# Research Charts Web Integration — Phase 1

Decision: **RESEARCH_CHARTS_WEB_PHASE_1_READY**

## Engine boundary

```
ClickHouse / Realtime Data     (Phase 2 — not started)
        ↓
Research Python Engine         trading_research_platform: data/, indicators/, overlays/
        ↓
Research API                   dashboard/research_charts/
        ↓
Dashboard Research Charts      /live-charts/research
```

JS does not compute Stochastic, EMA, Liquidity, or TF aggregation.

Recommended Phase-2 integration: **dashboard adapter imports TRP packages**
(`sys.path` insert of TRP root). Import only `data`, `indicators`, `overlays`.
Never import `app.*` or `PySide6` (name collision with `dashboard/app.py`).
Do not copy-paste engines.

## ClickHouse Research Data Plan (not implemented)

Existing CH (`orderbook_analyse/sql/001_initial_schema.sql`, db `orderbook_analysis`):

| Table | Grain | Symbol | Time | Not candles |
|---|---|---|---|---|
| `orderbook_deltas` | book updates | `symbol` | `exchange_ts` UTC | microstructure |
| `public_trades` | prints | `symbol` | `trade_ts` UTC | can *build* 1m OHLC later |
| `ticker_samples` | ticker | `symbol` | `exchange_ts` UTC | last/mark only |
| `liquidations` | liq events | `symbol` | `liquidation_ts` | not OHLC |

**There is no candle/kline table in ClickHouse today.**

Backtest candle SoT in this org is MySQL `market_candles` (1m), not CH.

Phase 2 options (pick one, then TRP `aggregate` for HTF):

1. **Preferred for research charts:** new CH `market_candles_1m` (or similar) as 1m SoT,
   `ORDER BY (symbol, open_time)`, populate later — **do not create in Phase 1**.
2. Adapter over MySQL `market_candles` implementing TRP `DataSource`.
3. Derive 1m OHLC from `public_trades` (lossy vs exchange klines; TTL 30d).

Symbol list later: `SELECT DISTINCT symbol FROM <candle_table>`.
Timeframe column: none if 1m SoT + `data.timeframes.aggregate`.

## Realtime (not started)

Reuse SSE pattern from `/api/live-orderbook/stream`.
Contract stub: `GET /api/research/stream` (no loop, no Bybit WS).

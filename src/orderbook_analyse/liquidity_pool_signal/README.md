# Liquidity Pool Signal — Detection Foundation

Research package for **direct reuse** of the Research Chart
Liquidity Location engine.

## Purpose

Export the same ASK/BID liquidity pools the chart draws when
**Liquidity Location** is enabled — nothing more.

## Engine dependency

```text
orderbook_analyse.liquidity_pool_signal.chart_pool_adapter.get_engine_function()
  is
research_charts.trp_import.load_trp()["run_liquidity_location"]
  is
indicators.liquidity_location.engine.run_liquidity_location
```

The engine is **not** copied or reimplemented here.

## Chart semantics (verified)

- ASK pools → pink (`#ec4079`, engine side `upper`)
- BID pools → turquoise (`#228bab`, engine side `lower`)
- Only the **selected chart timeframe** is computed (e.g. 5m)
- As-of class: **CAUSAL_WITH_CONFIRMATION_DELAY**
- `available_at` (confirmation bar close) is the earliest valid as-of
- `origin_ts` / `source_timestamp` is the swing bar open (not available_at)

## What this is / is not

| Is | Is not |
|----|--------|
| Pool detection foundation | Trading signal |
| Chart-engine adapter | Nested ask strategy |
| ASK/BID geometry + as-of | OB200 walls, absorption, entries |

## Visually verified examples (2026-08-26, BTCUSDT 5m)

- Nearest ASK / BID above/below market
- `MARKET_INSIDE_ASK_POOL` / market inside ask zone
- `MARKET_INSIDE_BID_POOL` / market inside bid zone
- Overlapping / between-pools classification via `MarketPoolLocation`

Audit artifacts (not required in git):  
`results/liquidity_location_chart_engine_direct_reuse_v1/`

## CLI

```bash
PYTHONPATH=src python scripts/run_liquidity_location_chart_pool_export.py
```

## Next planned step (not implemented)

Liquidity-Pool-Edge ↔ Raw-OB200-Wall-Overlap.

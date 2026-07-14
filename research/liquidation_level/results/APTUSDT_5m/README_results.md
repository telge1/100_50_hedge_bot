# Liquidation Levels Audit Results

## What this indicator computes

This audit replicates the LuxAlgo **Liquidation Levels** Pine logic in Python.
On each candle it estimates price levels where leveraged positions *might* get
liquidated, based on:

- a reference price (default: open)
- volume spikes vs a 13-period volume SMA
- a volatility / wick condition
- fixed leverage distances (default 25x / 50x / 100x)

Levels stay active until a later candle's range **strictly crosses** through them
(`high > level` and `low < level`), or until the active-level cap removes the oldest ones.

## Important: these are estimates, not real exchange liquidations

The levels are **heuristic / estimated**. They are **not** taken from Bybit (or any
other exchange) liquidation feeds. Do not treat them as ground-truth liquidations.

## Run summary

- Feather: `/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather`
- Symbol: `APTUSDT`
- Timeframe: `5m`
- Start: `2025-12-27 00:00:00+00:00`
- End: `2026-06-27 12:40:00+00:00`
- Candles: `52569`
- Created levels: `29203`
  - Upper: `14653`
  - Lower: `14550`
- Swept levels: `25370`
  - Upper: `11913`
  - Lower: `13457`
- Active at end: `449`
- Removed by max-active limit: `3384`
- Sweep rate: `86.87463616751704`
- Mean age at sweep: 405.61 candles
- Median age at sweep: 115.50 candles

## Config used

```json
{
  "reference_price": "open",
  "volume_threshold": 1.7,
  "volatility_threshold": 10.0,
  "leverages": [
    25,
    50,
    100
  ],
  "volume_sma_period": 13,
  "max_active_levels": 500
}
```

## What this audit does **not** prove

This first step only validates the causal level lifecycle and exports research
artifacts. It does **not** prove a profitable trading strategy. Entry / TP / SL
optimization and strategy backtests are intentionally deferred to a later task.

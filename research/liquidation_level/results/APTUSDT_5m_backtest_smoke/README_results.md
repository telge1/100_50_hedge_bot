# Liquidation Level Event Backtest Results

## What was tested

Causal event backtest on LuxAlgo-style **estimated** liquidation levels
(not real exchange liquidation feeds).

Pipeline:

1. Replay levels on APTUSDT 5m candles
2. Build sweep events (per level, per candle-side, per cluster)
3. Generate variants L1–L7, S1–S7, F_LONG, F_SHORT
4. Enter at the **open of the next candle** after the sweep candle closes
5. Evaluate fixed horizons and optional TP/SL grids
6. Compare against deterministic random-entry controls (seeded)

## Entry timing

A sweep is only known after the sweep candle **closes**.
Therefore entries are never on the sweep candle itself; the earliest entry is
the **next candle open**.

## Single level vs cluster

- **Candle sweep event**: all upper (or lower) levels swept on the same candle,
  aggregated once so many levels do not spawn duplicate identical trades.
- **Cluster**: research-only grouping of nearby active levels (default gap
  0.10%) before the sweep candle. A cluster sweep needs ≥2 swept members or
  swept strength ≥3.

## Data

- Feather: `/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather`
- Candles: `3000`
- Level sweep events: `1115`
- Candle sweep events: `275`
- Cluster sweeps: `221`
- Signals total: `1064`
- Signals by variant: `{
  "L1": 122,
  "L2": 71,
  "L3": 60,
  "L4": 84,
  "L5": 21,
  "L6": 30,
  "L7": 17,
  "S1": 153,
  "S2": 104,
  "S3": 106,
  "S4": 137,
  "S5": 21,
  "S6": 46,
  "S7": 15,
  "F_SHORT": 31,
  "F_LONG": 46
}`
- In-sample cut index: `2100`
- Round-trip cost: `0.12` %

## Highest signal quality (horizon 12, mean net after costs)

- Best long (full): L7 | n=17 | mean_gross=0.32337182969341044 | mean_net=0.20337182969341044
- Best short (full): S7 | n=15 | mean_gross=0.5001291228480601 | mean_net=0.38012912284806005
- Best long (out-of-sample): L3 | n=18 | mean_gross=0.4238794157969578 | mean_net=0.30387941579695776
- Best short (out-of-sample): S1 | n=48 | mean_gross=0.19450772557049767 | mean_net=0.07450772557049765

## Costs (0.12%)

- Best long mean net (full): `0.20337182969341044`
- Best short mean net (full): `0.38012912284806005`

Positive **gross** with negative **net** means the edge does not clear costs.

## Control comparison (horizon 12, full)

- Long best variant control: event-control=0.2774841083906492 | frac_controls_better=0.05 (empirical share only; not a formal significance claim)
- Short best variant control: event-control=0.5388566969079019 | frac_controls_better=0.0 (empirical share only; not a formal significance claim)

The control share is an empirical bootstrap-style fraction only.
This audit does **not** claim classical statistical significance.

## Honest reading

- Interesting ≠ tradeable.
- No overlap filtering was applied (each event tested independently).
- No parameter selection was done on out-of-sample data in this runner.
- No scanner/live integration recommended from this run alone: treat results as research diagnostics until a variant is robust out-of-sample and clearly above its random-entry control.

## Not claimed

This does **not** prove a profitable live strategy and does **not** authorize
integration into the regime scanner, trend state machine, or bots.
